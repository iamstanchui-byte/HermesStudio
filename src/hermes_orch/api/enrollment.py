# coding: utf-8
"""Enrollment token endpoints (v1.0.1 new-user-activation §3.3 + v0.7).

Endpoints:
- POST /api/enrollment-tokens              issue a new token (admin-only)
- GET  /api/enrollment-tokens              list outstanding tokens (admin-only)
- DELETE /api/enrollment-tokens/{id}       revoke a token (admin-only)
- POST /api/agents/enroll                  consume a token (no auth, uses
                                            the token as proof of identity)
- POST /api/enrollment/v07                 v0.7 §1.4 HMAC-signed enrollment
                                            (no token; agent proves
                                            identity via hmac_key_id +
                                            hmac_secret)

The consume flow (POST /api/agents/enroll) is the most security-sensitive
part of v1.0.1. It:
  1. Hashes the plaintext token
  2. Looks up by token_hash (404 if missing)
  3. Validates expires_at > now (410 if expired) and used_at IS NULL
     (410 if already used)
  4. Inside ONE transaction:
     a. UPDATE enrollment_tokens SET used_at = now WHERE token_hash = ?
        AND used_at IS NULL AND expires_at > now  (atomic, single row)
     b. INSERT agent row with the agent-declared name
     c. UPDATE enrollment_tokens SET used_by_agent_id = <new id>  (§3.3 step 6)
     d. INSERT per-agent hmac_secret
  5. Returns { agent_id, hmac_secret, name_was_overridden }

The atomicity is crucial: a single UPDATE with the WHERE-clause
guard ensures two concurrent consumes resolve cleanly (SQLite
single-writer serializes this for us; busy_timeout=5000 covers the
read path).
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import string
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from hermes_orch.auth.cookie import ROLE_ADMIN, current_user
from hermes_orch.core import enrollment as enrollment_mod
from hermes_orch.core.enrollment import (
    CONSUME_ALREADY_USED,
    CONSUME_EXPIRED,
    CONSUME_NOT_FOUND,
    CONSUME_OK,
    ConsumeResult,
    IssuedToken,
    hash_token,
    issue_enrollment_token,
)
from hermes_orch.core.onboarding import (
    SIGNAL_AGENT_CONNECTED,
    set_user_signal,
)


def _hash_agent_secret(secret: str) -> str:
    """SHA-256 hex of the agent's HMAC secret. Used to populate
    `agents.secret_hash` (the server stores the plaintext alongside
    for HMAC verify, plus the hash for any future verification that
    needs to avoid plaintext — e.g. cross-check during migration).
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()

router = APIRouter()


# ===== Request/response shapes =====

class IssueTokenIn(BaseModel):
    """Body for POST /api/enrollment-tokens."""
    label: str = Field(default="", max_length=200)
    requested_agent_name: str = Field(default="", max_length=100)


class IssueTokenOut(BaseModel):
    """Response from POST /api/enrollment-tokens.

    `token` is the PLAINTEXT — shown to the operator ONCE in the UI
    and never stored. The DB row stores only `token_hash`.

    `install_command` is the FULL one-liner the user pastes on the
    agent host — it includes the `pip install` step (so a brand-new
    host with no hermes-orchestrator package can run the command
    directly), then the `hermes-orch-agent enroll` invocation. Per
    spec §3.3, the one-liner is the contract: paste, run, done.
    """
    id: str
    token: str
    expires_at: str
    label: str
    requested_agent_name: str
    install_command: str  # the full one-liner (install + enroll)


class TokenListItem(BaseModel):
    """One row in GET /api/enrollment-tokens (no plaintext!)."""
    id: str
    label: str
    requested_agent_name: str
    created_at: str
    expires_at: str
    used_at: str | None = None
    used_by_agent_id: str | None = None
    is_expired: bool
    is_used: bool


class EnrollIn(BaseModel):
    """Body for POST /api/agents/enroll (from the agent host)."""
    token: str = Field(..., min_length=1)
    agent_name: str = Field(..., min_length=1, max_length=100)
    hostname: str = Field(default="", max_length=200)
    os_type: str = Field(default="", max_length=50)


class EnrollOut(BaseModel):
    """Response from POST /api/agents/enroll.

    `hmac_secret` is the agent's long-lived shared secret in v0.6
    base64url form (43 chars, no padding) — shown to the agent host
    ONCE for backward compat with v0.6 wrappers.

    v0.7 §1.4 fields (added 2026-08-17, see commit for fix):
    - `hmac_secret_hex`: the same 32 random bytes encoded as 64-char
      lowercase hex. This is the v0.7 canonical format; new wrappers
      should use this and ignore `hmac_secret`.
    - `hmac_key_id`: the operator-assigned key id stored in
      `agents.hmac_key_id` (a separate column added in the v0.7
      hardening migration). The wrapper uses this as the lookup key
      in its `X-Hermes-Key-Id` header; the server-side v0.7 verifier
      looks up the agent by this value (NOT by `agent_id`).

    All three secret/key values are derived from the same 32 random
    bytes server-side (see `_consume_token_atomic`). Legacy v0.6
    wrappers keep working (they read `hmac_secret` and fall back to
    v0.6 base auth); new v0.7 wrappers prefer the hex + key_id pair.
    """
    agent_id: str
    hmac_secret: str
    hmac_secret_hex: str = ""  # v0.7 §1.4 canonical hex (64 lowercase)
    hmac_key_id: str = ""  # v0.7 §1.4 operator-assigned key id
    requested_name_used: bool = False  # informational: True iff the
                                       # operator's requested_agent_name
                                       # ended up as the agent's name


# ===== Issue endpoints =====

@router.post("/enrollment-tokens", response_model=IssueTokenOut)
async def post_enrollment_token(
    body: IssueTokenIn, request: Request
) -> IssueTokenOut:
    """Admin-only: issue a new enrollment token.

    The plaintext token is returned ONCE in the response. The DB
    stores only the SHA-256 hash. The token expires in 15 minutes
    and is single-use.
    """
    user = await current_user(request)
    if not user or user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "Admin-only endpoint")

    issued = issue_enrollment_token(
        label=body.label,
        requested_agent_name=body.requested_agent_name,
    )
    # Persist the row (hash only). created_by is the current admin.
    now = datetime.now(timezone.utc).isoformat()
    await request.app.state.db.execute(
        "INSERT INTO enrollment_tokens "
        "(id, token_hash, created_by, created_at, expires_at, "
        " requested_agent_name, label) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (issued.id, issued.token_hash, user["id"], now, issued.expires_at,
         issued.requested_agent_name, issued.label),
    )

    # Build the install command the user pastes on the agent host.
    # The server URL is the request's host (so the agent host
    # connects back to wherever the dashboard was loaded from).
    scheme = "https" if request.url.scheme == "https" else "http"
    host = request.url.hostname or "localhost"
    port = request.url.port or (443 if scheme == "https" else 80)
    # Default to bare host:port for non-standard ports; else just host
    server_url = f"{scheme}://{host}:{port}" if port not in (80, 443) else f"{scheme}://{host}"
    # The install_command is the FULL one-liner: install the package
    # (from GitHub — the package exposes both `hermes-orch` server
    # and `hermes-orch-agent` wrapper CLIs via [project.scripts]),
    # then run enroll. Per spec §3.3, this is the contract: paste
    # the one line on a brand-new host, get an enrolled agent.
    #
    # We use `pip install` (system or active-venv, whichever the
    # user has) and `git+https://...` for the install source. For
    # most production setups the user will adapt this to their
    # own pip / venv / pipx — but this is the safest default.
    #
    # On PowerShell the user needs `;` instead of `&&` between
    # commands, but `hermes-orch-agent enroll ...` doesn't depend
    # on the install success in a way the user would want to
    # skip past, so `&&` is fine on bash/zsh and they're expected
    # to translate for PowerShell.
    install_command = (
        f'pip install "hermes-orchestrator @ git+https://github.com/iamstanchui-byte/HermesStudio.git@v0.10.0" && '
        f"hermes-orch-agent enroll "
        f"--server {server_url} "
        f"--token {issued.plaintext} "
        f"--agent-name {issued.requested_agent_name or 'agent-1'}"
    )

    return IssueTokenOut(
        id=issued.id,
        token=issued.plaintext,
        expires_at=issued.expires_at,
        label=issued.label,
        requested_agent_name=issued.requested_agent_name,
        install_command=install_command,
    )


@router.get("/enrollment-tokens", response_model=list[TokenListItem])
async def get_enrollment_tokens(request: Request) -> list[TokenListItem]:
    """Admin-only: list outstanding (and recent) tokens.

    Plaintext is NEVER returned (it's not in the DB). Each item
    shows whether the token is expired / used so the operator can
    decide whether to revoke.
    """
    user = await current_user(request)
    if not user or user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "Admin-only endpoint")

    now = datetime.now(timezone.utc).isoformat()
    async with request.app.state.db.conn.execute(
        "SELECT id, label, requested_agent_name, created_at, expires_at, "
        "used_at, used_by_agent_id "
        "FROM enrollment_tokens ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    items: list[TokenListItem] = []
    for r in rows:
        is_expired = bool(r["expires_at"] and r["expires_at"] < now)
        is_used = bool(r["used_at"])
        items.append(TokenListItem(
            id=r["id"],
            label=r["label"] or "",
            requested_agent_name=r["requested_agent_name"] or "",
            created_at=r["created_at"] or "",
            expires_at=r["expires_at"] or "",
            used_at=r["used_at"],
            used_by_agent_id=r["used_by_agent_id"],
            is_expired=is_expired,
            is_used=is_used,
        ))
    return items


@router.delete("/enrollment-tokens/{token_id}")
async def delete_enrollment_token(token_id: str, request: Request) -> dict[str, Any]:
    """Admin-only: revoke an outstanding token.

    Deletes the row (cascade: nothing depends on the token once
    consumed; before consume, there's just the row itself). After
    this, the plaintext is useless (the agent host can't look up
    the hash because the row is gone).
    """
    user = await current_user(request)
    if not user or user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "Admin-only endpoint")
    await request.app.state.db.execute(
        "DELETE FROM enrollment_tokens WHERE id = ?", (token_id,)
    )
    return {"ok": True, "deleted": token_id}


# ===== Consume endpoint =====

@router.post("/agents/enroll", response_model=EnrollOut)
async def post_agent_enroll(body: EnrollIn, request: Request) -> EnrollOut:
    """Consume an enrollment token + create the agent row.

    This is the endpoint `agent_cli enroll` POSTs to. The plaintext
    token is the credential — no session cookie or HMAC needed. The
    server hashes it, looks up the hash, and runs the atomic consume
    transaction.
    """
    db = request.app.state.db
    # v0.7 enroll IP fix (2026-08-17): the IP we store in `agents.ip` is
    # the actual TCP connection's source IP (request.client.host), NOT
    # the agent-declared hostname. Previously the column-position was
    # wrong and `hostname` was being inserted into the `ip` column, so
    # HermesCtl ended up with `ip='HermesCtl'` instead of `ip='192.168.2.153'`.
    # The hostname string still goes into its own `agents.hostname` column
    # (added in v0.7 schema migration).
    client_ip = (request.client.host if request.client else "") or ""
    result = await _consume_token_atomic(
        db,
        plaintext=body.token,
        agent_name=body.agent_name,
        hostname=body.hostname,
        client_ip=client_ip,
        os_type=body.os_type,
    )
    if result.outcome == CONSUME_NOT_FOUND:
        raise HTTPException(404, "Enrollment token not found")
    if result.outcome == CONSUME_EXPIRED:
        raise HTTPException(410, "Enrollment token has expired (15-minute window)")
    if result.outcome == CONSUME_ALREADY_USED:
        raise HTTPException(410, "Enrollment token has already been used")
    if result.outcome != CONSUME_OK:
        raise HTTPException(500, f"Consume failed: {result.outcome}")

    # Flip the agent_connected onboarding signal for the user who
    # ISSUED the token (per spec T1.7). The created_by user is the
    # one who sees the checklist collapse.
    try:
        token_row = await db.fetchone(
            "SELECT created_by FROM enrollment_tokens WHERE id = "
            "(SELECT id FROM enrollment_tokens WHERE used_by_agent_id = ?)",
            (result.agent_id,),
        )
        # Simpler path: re-read the row we just updated
        token_row2 = await db.fetchone(
            "SELECT created_by FROM enrollment_tokens WHERE used_by_agent_id = ?",
            (result.agent_id,),
        )
        if token_row2 and token_row2.get("created_by"):
            await set_user_signal(
                db, token_row2["created_by"],
                SIGNAL_AGENT_CONNECTED, True,
            )
    except Exception:
        # Never let onboarding bookkeeping fail the enroll.
        pass

    return EnrollOut(
        agent_id=result.agent_id,
        hmac_secret=result.hmac_secret,
        hmac_secret_hex=result.hmac_secret_hex,
        hmac_key_id=result.hmac_key_id,
        requested_name_used=result.requested_name_used,
    )


async def _consume_token_atomic(
    db,
    *,
    plaintext: str,
    agent_name: str,
    hostname: str,
    client_ip: str,
    os_type: str,
) -> ConsumeResult:
    """Atomic 7-step consume per spec §3.3.

    Wrapped in a single transaction so partial failures roll back
    the whole thing. SQLite's single-writer + busy_timeout=5000
    handles the concurrent-consume case (the second consume sees
    the UPDATE from the first one and gets 410 already_used).
    """
    token_hash = hash_token(plaintext)
    now = datetime.now(timezone.utc).isoformat()

    # Step 1: look up the row by hash. If not found → 404.
    row = await db.fetchone(
        "SELECT id, expires_at, used_at, requested_agent_name, created_by "
        "FROM enrollment_tokens WHERE token_hash = ?",
        (token_hash,),
    )
    if not row:
        return ConsumeResult(outcome=CONSUME_NOT_FOUND)
    if row["expires_at"] and row["expires_at"] < now:
        return ConsumeResult(outcome=CONSUME_EXPIRED)
    if row["used_at"]:
        return ConsumeResult(outcome=CONSUME_ALREADY_USED)

    # Steps 2-7: atomic transaction.
    # We do this as a single transaction so a crash mid-consume
    # rolls back the whole thing. SQLite's BEGIN IMMEDIATE acquires
    # the write lock upfront (no two consumes can both pass the
    # "not used" check). The transaction context manager handles
    # COMMIT/ROLLBACK for us; inside the block, db.execute() is
    # buffered (the per-call commit is suppressed via the _in_tx flag).
    try:
        async with db.transaction():
            # Step 2: atomic UPDATE — flips used_at ONLY if the row
            # is still unused + unexpired. The WHERE-clause is the
            # atomicity guard.
            cursor = await db.execute(
                "UPDATE enrollment_tokens SET used_at = ? "
                "WHERE id = ? AND used_at IS NULL AND expires_at > ?",
                (now, row["id"], now),
            )
            if cursor.rowcount == 0:
                # Another consume beat us to it. Roll back (the
                # transaction context will handle this) and report
                # already_used.
                return ConsumeResult(outcome=CONSUME_ALREADY_USED)

            # Step 3: name precedence. Per spec §3.3.2 the agent's
            # self-declared agent_name ALWAYS wins over the operator's
            # requested_agent_name hint. If the agent sent an empty
            # name (frontend bug), fall back to the hint.
            effective_name = (agent_name or "").strip() or (row["requested_agent_name"] or "").strip()
            if not effective_name:
                # Spec doesn't define a fallback. Use a deterministic
                # default so the agent row is always created.
                effective_name = "agent"
            # `requested_name_used` is True iff the operator's hint
            # ended up as the agent's effective name. This is the
            # case when:
            #   - the hint was non-empty, AND
            #   - the effective_name matches the hint (whether the
            #     agent's body also matched, or the agent's body was
            #     empty so we fell back to the hint)
            requested_name_used = bool(
                (row["requested_agent_name"] or "").strip()
                and effective_name == (row["requested_agent_name"] or "").strip()
            )

            # Step 4: create the agent row
            agent_id = _new_agent_id()
            # v0.7 §1.4 (2026-08-17): generate ONE 32-byte secret and
            # derive BOTH forms from it. v0.6 wrappers use the base64url
            # text; v0.7 wrappers use the hex. They MUST be the same
            # bytes (server-side invariant: signing key parity).
            secret_bytes = secrets.token_bytes(32)
            # v0.6 legacy form: base64url without padding (43 chars).
            hmac_secret = base64.urlsafe_b64encode(secret_bytes).rstrip(b"=").decode("ascii")
            # v0.7 §1.4 canonical form: 64 lowercase hex chars.
            hmac_secret_hex = secret_bytes.hex()
            # v0.7 §1.4 operator-assigned key id. Format: 'kw_' prefix
            # + 12 random lowercase alphanumeric chars (matches
            # `agent_id` style: 36^12 = 4.7e18 keyspace, collision-
            # negligible for the operator scale; the UNIQUE partial
            # index on agents.hmac_key_id catches the rare collision).
            kw_alphabet = string.ascii_lowercase + string.digits
            hmac_key_id = "kw_" + "".join(
                secrets.choice(kw_alphabet) for _ in range(12)
            )
            secret_hash = _hash_agent_secret(hmac_secret)
            await db.execute(
                "INSERT INTO agents "
                "(id, secret_hash, ip, os_type, status, created_at, name, "
                " hostname, hmac_secret, hmac_secret_hex, hmac_key_id) "
                "VALUES (?, ?, ?, ?, 'verifying', ?, ?, ?, ?, ?, ?)",
                (agent_id, secret_hash, client_ip or "", os_type or "",
                 now, effective_name, hostname or "",
                 hmac_secret, hmac_secret_hex, hmac_key_id),
            )

            # Step 5 (spec §3.3 step 6): write used_by_agent_id back
            # onto the token row. Same transaction so this is atomic
            # with the agent creation.
            await db.execute(
                "UPDATE enrollment_tokens SET used_by_agent_id = ? "
                "WHERE id = ?",
                (agent_id, row["id"]),
            )

            # (Spec §3.3 step 7: per-agent hmac_secret — already
            # inserted in step 4 as part of the agent row.)

        return ConsumeResult(
            outcome=CONSUME_OK,
            agent_id=agent_id,
            agent_name=effective_name,
            hmac_secret=hmac_secret,
            hmac_secret_hex=hmac_secret_hex,
            hmac_key_id=hmac_key_id,
            requested_name_used=requested_name_used,
        )
    except Exception as e:
        # The transaction context manager handles ROLLBACK on
        # exception. Just return a sentinel — the API layer maps
        # this to a 500 with a generic error.
        import logging
        logging.getLogger("hermes_orch.api.enrollment").error(
            "consume_token_atomic failed: %s", e
        )
        raise


def _new_agent_id() -> str:
    """Generate an agent_id. Match the format used by register_agent."""
    import string
    alphabet = string.ascii_lowercase + string.digits
    return "agent-" + "".join(secrets.choice(alphabet) for _ in range(12))


# ===== v0.7 §1.4 HMAC-signed enrollment (step 8) =====
#
# Different from POST /api/agents/enroll (token-based, v0.6):
#   - No enrollment token needed
#   - The agent proves identity by signing the request with its
#     hmac_key_id + hmac_secret (per the v0.7 verifier in
#     auth/hmac_v07.py)
#   - The agent row must already exist in the DB with status='verifying'
#     and the matching hmac_key_id; the operator pre-issued these
#     out-of-band (e.g. via the orch admin UI or `hermes-orch-agent
#     pre-provision`)
#
# The endpoint reads the JSON body for informational fields
# (hostname, os_type, agent_name) and updates the agent row:
#   - status -> 'verified'
#   - ip, os_type, name (if not already set)
#   - last_heartbeat_at -> now
# Then returns 200 with `{"status": "verified", "agent_id": ...}`
# which the bootstrapper's Wait-ForEnrollment polls for.
#
# The verifier (require_hmac_auth_v07) has already validated the
# 7 X-Hermes-* headers and looked up the agent by hmac_key_id
# (the key-id-to-agent rule). The auth_agent_id returned is the
# canonical agent id from the DB row.

from hermes_orch.auth.hmac_v07 import require_hmac_auth_v07
from pydantic import BaseModel


class EnrollV07In(BaseModel):
    """Body of POST /api/enrollment/v07. All fields informational;
    the auth_agent_id from the v0.7 verifier is the source of truth.
    """
    agent_name: str | None = None
    hostname: str | None = None
    os_type: str | None = None


@router.post("/enrollment/v07")
async def post_enrollment_v07(
    body: EnrollV07In,
    request: Request,
    auth_agent_id: str = Depends(require_hmac_auth_v07),
) -> dict:
    """v0.7 §1.4 HMAC-signed enrollment.

    Per spec §4: the agent presents its hmac_key_id + hmac_secret
    via the v0.7 7-header signature, the verifier looks up the
    agent row by hmac_key_id, and this endpoint updates the row to
    mark it verified (status='verified', last_heartbeat_at=now,
    optional ip/os_type from the request body).

    The body is informational only; the auth_agent_id from the
    verifier is authoritative.

    On success: 200 + {"status": "verified", "agent_id": ...}
    The bootstrapper's Wait-ForEnrollment polls this; on receiving
    status='verified' it considers enrollment complete and stops.
    """
    from datetime import datetime, timezone
    db = request.app.state.db
    now_iso = datetime.now(timezone.utc).isoformat()

    # Hardening Phase 3 (2026-08-15): strict state machine guard.
    # Per spec §1.10, the only allowed start state for enrollment
    # is `verifying`. The atomic UPDATE's WHERE clause includes
    # `status = 'verifying'`; if no row matches, the row was
    # already in a non-verifying state (verified / blocked /
    # suspended / pending / typo'd value) and we return 409
    # `ENROLLMENT_STATE_CONFLICT` rather than silently
    # overwriting the existing status.
    #
    # Without this guard, a malicious or buggy agent could
    # re-enroll an already-verified row, resetting its
    # last_heartbeat_at and changing os_type / hostname / name.
    # The guard makes enrollment single-shot per pre-provisioned
    # row.
    if body.hostname or body.os_type or body.agent_name:
        cursor = await db.execute(
            "UPDATE agents SET status = 'verified', "
            "last_heartbeat_at = ?, "
            "hostname = COALESCE(?, hostname), "
            "os_type = COALESCE(?, os_type), "
            "name = COALESCE(?, name) "
            "WHERE id = ? AND status = 'verifying'",
            (now_iso, body.hostname, body.os_type, body.agent_name,
             auth_agent_id),
        )
    else:
        cursor = await db.execute(
            "UPDATE agents SET status = 'verified', "
            "last_heartbeat_at = ? "
            "WHERE id = ? AND status = 'verifying'",
            (now_iso, auth_agent_id),
        )
    if cursor.rowcount == 0:
        # Row was not in `verifying` state. The verifier already
        # confirmed the row exists (UNKNOWN_KEY_ID 401 otherwise),
        # so this is strictly a state-machine conflict. Read the
        # current status to include it in the error message.
        row = await db.fetchone(
            "SELECT status FROM agents WHERE id = ?", (auth_agent_id,)
        )
        current_status = row.get("status") if row else "missing"
        raise HTTPException(
            409,
            f"ENROLLMENT_STATE_CONFLICT: agent is in "
            f"status={current_status!r}; enrollment requires "
            f"status='verifying' (operator pre-provision state)",
        )
    # NOTE: db.execute() auto-commits when not inside a transaction
    # block (per Database.execute docstring). Don't call db.commit()
    # here — Database has no commit() method.

    return {
        "agent_id": auth_agent_id,
        "status": "verified",
    }