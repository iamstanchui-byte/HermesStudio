# coding: utf-8
"""Enrollment token endpoints (v1.0.1 new-user-activation §3.3).

Endpoints:
- POST /api/enrollment-tokens              issue a new token (admin-only)
- GET  /api/enrollment-tokens              list outstanding tokens (admin-only)
- DELETE /api/enrollment-tokens/{id}       revoke a token (admin-only)
- POST /api/agents/enroll                  consume a token (no auth, uses
                                            the token as proof of identity)

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

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
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
    """
    id: str
    token: str
    expires_at: str
    label: str
    requested_agent_name: str
    install_command: str  # the one-liner the user pastes on the agent host


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

    `hmac_secret` is the agent's long-lived shared secret — shown to
    the agent host ONCE. The agent must store it in agent.yaml (mode
    0600) and use it for all subsequent HMAC-authenticated requests.
    """
    agent_id: str
    hmac_secret: str
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
    install_command = (
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
    result = await _consume_token_atomic(
        db,
        plaintext=body.token,
        agent_name=body.agent_name,
        hostname=body.hostname,
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
        requested_name_used=result.requested_name_used,
    )


async def _consume_token_atomic(
    db,
    *,
    plaintext: str,
    agent_name: str,
    hostname: str,
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
            # hmac_secret: 32 random bytes (256 bits), base64-encoded
            hmac_secret = secrets.token_urlsafe(32)
            secret_hash = _hash_agent_secret(hmac_secret)
            await db.execute(
                "INSERT INTO agents "
                "(id, secret_hash, ip, os_type, status, created_at, name, hmac_secret) "
                "VALUES (?, ?, ?, ?, 'verifying', ?, ?, ?)",
                (agent_id, secret_hash, hostname or "", os_type or "",
                 now, effective_name, hmac_secret),
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
