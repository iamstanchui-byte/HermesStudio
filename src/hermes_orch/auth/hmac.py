# coding: utf-8
"""HMAC-SHA256 agent authentication helpers (v1.6, 2026-07-29).

Replaces the placeholder `X-Signature = SHA256(secret)` that v1.0
through v1.5 used. The old scheme just stamped the secret hash; anyone
who intercepted one request could replay it forever. The real HMAC
binds the signature to (method, path, body, timestamp), so a captured
request can't be replayed against a different endpoint, with a
different body, or outside a 5-minute window.

Wire format (per REVIEW.md §6.1):

  Request headers:
    X-Agent-Id:    <agent id, e.g. "win-local-1">
    X-Timestamp:   <unix epoch seconds, decimal>
    X-Signature:   <hex HMAC-SHA256>

  Signature input (string-to-sign):
    <METHOD>\n<PATH>\n<SHA256_HEX(body_bytes)>\n<TIMESTAMP>

  Where:
    METHOD:    uppercase ("GET", "POST", ...)
    PATH:      full request path including query string
               (e.g. "/api/agents/win-local-1/heartbeat?force=1")
    body_bytes: raw request body bytes (empty for GET)
    TIMESTAMP: same value as the X-Timestamp header (string)

  Algorithm:
    sig = HMAC-SHA256(key=secret, msg=string-to-sign).hexdigest()

Server validation:
  1. All 3 headers present
  2. TIMESTAMP parses as int, |now - ts| <= HMAC_WINDOW_SEC (default 300)
  3. Look up agent.hmac_secret; if NULL, fall back to legacy mode
     (just check X-Agent-Id matches task owner) for backward compat
     during the migration window. After migration, set
     HERMES_HMAC_REQUIRED=true to refuse legacy mode.
  4. Recompute sig, compare with hmac.compare_digest (constant-time)

Threat model (v1.6 scope):
  - Local network orchestrator. We're protecting against a wrapper
    impersonating a different wrapper (or replaying an old request),
    NOT against a DB compromise. The secret is stored plaintext in
    the orchestrator's DB; if an attacker has DB read, they can
    impersonate any agent.
  - If you need to defend against DB compromise, store the secret
    encrypted with a server master key (KMS-style) or move to
    asymmetric (Ed25519) signing. Both are out of scope for v1.6.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

from fastapi import Header, HTTPException, Request


# Default timestamp tolerance: 5 minutes. Tunable via env
# HERMES_HMAC_WINDOW_SEC. Generous enough for NTP drift + slow
# networks; tight enough to make replays useless.
DEFAULT_HMAC_WINDOW_SEC = 300


def _read_window_sec() -> int:
    raw = os.environ.get("HERMES_HMAC_WINDOW_SEC", "").strip()
    if not raw:
        return DEFAULT_HMAC_WINDOW_SEC
    try:
        v = int(raw)
        return v if v > 0 else DEFAULT_HMAC_WINDOW_SEC
    except ValueError:
        return DEFAULT_HMAC_WINDOW_SEC


def string_to_sign(method: str, path: str, body: bytes, timestamp: str) -> str:
    """Build the canonical string-to-sign. Stable across wrapper and
    server implementations (tested by test_hmac_auth.py).

    Components joined by '\\n' (newline). Path includes query string.
    Body is the hex SHA256 of the raw bytes (empty body -> SHA256 of
    empty bytes = e3b0c4...855).
    """
    method_u = method.upper()
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return f"{method_u}\n{path}\n{body_hash}\n{timestamp}"


def compute_signature(
    secret: str, method: str, path: str, body: bytes, timestamp: str
) -> str:
    """Compute the hex HMAC-SHA256 signature for a request.

    Wrapper-side helper. The server recomputes the same way and
    compares with hmac.compare_digest.
    """
    msg = string_to_sign(method, path, body, timestamp).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    method: str,
    path: str,
    body: bytes,
    timestamp: str,
    provided: str,
) -> bool:
    """Constant-time comparison. Returns True iff the provided
    signature matches what we'd compute for this request.
    """
    expected = compute_signature(secret, method, path, body, timestamp)
    return hmac.compare_digest(expected, provided or "")


def hmac_required() -> bool:
    """True if HMAC must be present and valid (no legacy fallback).

    Set via HERMES_HMAC_REQUIRED=true|1|yes|on. Default false for
    the migration window; flip to true after all agents are
    bootstrapped.
    """
    return os.environ.get("HERMES_HMAC_REQUIRED", "").lower() in (
        "1", "true", "yes", "on",
    )


# ===== FastAPI dependency =====


async def require_hmac_auth(
    request: Request,
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> str:
    """FastAPI dependency: verify HMAC and return the agent_id.

    Usage:
        @router.post("/...")
        async def my_endpoint(
            request: Request,
            agent_id: str = Depends(require_hmac_auth),
        ):
            ...

    Behavior:
      - 401 if any header is missing
      - 401 if timestamp is malformed or outside the window
      - 401 if agent not found
      - 401 if agent has no hmac_secret (legacy mode) AND
        HERMES_HMAC_REQUIRED=true
      - 401 if signature doesn't match (constant-time compare)
      - Returns x_agent_id on success

    Legacy mode (hmac_secret IS NULL on the agent row): for backward
    compat during the migration window, the dependency returns
    x_agent_id without checking a signature. This is gated by
    HERMES_HMAC_REQUIRED (default false). After all agents have
    hmac_secret populated, set HERMES_HMAC_REQUIRED=true.
    """
    from hermes_orch.core.audit import audit_log  # late import: avoid cycle

    if not x_agent_id or not x_timestamp or not x_signature:
        raise HTTPException(
            401,
            "Missing auth headers (X-Agent-Id, X-Timestamp, X-Signature)",
        )

    # Parse timestamp
    try:
        ts_int = int(x_timestamp)
    except (TypeError, ValueError):
        raise HTTPException(401, "Invalid X-Timestamp (not an integer)")

    now = int(time.time())
    window = _read_window_sec()
    if abs(now - ts_int) > window:
        raise HTTPException(
            401,
            f"Timestamp out of window (|now-ts|={abs(now - ts_int)}s > {window}s)",
        )

    # Look up the agent + its secret
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT id, hmac_secret FROM agents WHERE id = ?", (x_agent_id,)
    )
    if not row:
        raise HTTPException(401, f"Unknown agent: {x_agent_id}")

    secret = row.get("hmac_secret")
    if not secret:
        # Legacy mode: no secret yet. Allow the request IF the env
        # flag says we can. Otherwise fail.
        if hmac_required():
            # Log so operators see the issue
            try:
                await audit_log(
                    db,
                    "agent.hmac_missing",
                    actor="auth",
                    agent_id=x_agent_id,
                    payload={"path": request.url.path, "method": request.method},
                )
            except Exception:
                pass
            raise HTTPException(
                401,
                f"Agent {x_agent_id} has no hmac_secret; "
                "run 'hermes-orch-agent bootstrap' or set HERMES_HMAC_REQUIRED=false",
            )
        # Legacy mode: still audit (insecure auth) for visibility
        try:
            await audit_log(
                db,
                "agent.hmac_legacy_auth",
                actor="auth",
                agent_id=x_agent_id,
                payload={"path": request.url.path, "method": request.method},
            )
        except Exception:
            pass
        return x_agent_id

    # Read the raw body BEFORE Pydantic parses it. Important: must
    # be done in the dependency (before the endpoint's body arg
    # materializes) so the body we sign is exactly the body bytes
    # the wrapper signed.
    body_bytes = await request.body()

    # Verify signature. Path includes query string.
    full_path = request.url.path
    if request.url.query:
        full_path = full_path + "?" + request.url.query

    ok = verify_signature(
        secret=secret,
        method=request.method,
        path=full_path,
        body=body_bytes,
        timestamp=x_timestamp,
        provided=x_signature,
    )
    if not ok:
        # Audit failed attempts (potential impersonation / replay)
        try:
            await audit_log(
                db,
                "agent.hmac_auth_failed",
                actor="auth",
                agent_id=x_agent_id,
                payload={
                    "path": request.url.path,
                    "method": request.method,
                    "ts_drift": abs(now - ts_int),
                },
            )
        except Exception:
            pass
        raise HTTPException(401, "Invalid HMAC signature")

    return x_agent_id
