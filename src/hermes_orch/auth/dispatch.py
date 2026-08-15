# coding: utf-8
"""Dual-format HMAC dispatcher (2026-08-15).

Routes incoming HMAC-authenticated requests to the right verifier
based on header presence:

  - X-Hermes-Method present  -> v0.7 §1.4 verifier (require_hmac_auth_v07)
  - X-Agent-Id present       -> v1.6 3-header verifier (require_hmac_auth)
  - neither                  -> 401 MISSING_AUTH_HEADERS

Per the v0.7 spec §3 (Option B dual-format with version detection),
this is the migration path for the Day 0-30 transition. Both
formats are accepted on the 2 existing HMAC-protected routes:
  - POST /api/agents/{id}/heartbeat
  - GET  /api/agents/{id}

The dispatcher does NOT include HERMES_HMAC_ACCEPT_V06 env var yet
(spec §3 also mentions it for production). When the operator flips
that env to false at Day 30, the v0.6 path raises 401 explicitly.
For now both paths work unconditionally.

Per the v0.7 spec §1, the 7 X-Hermes-* headers are mutually
exclusive with the 3 v1.6 headers in any single request (a request
signed with v0.7 won't have X-Agent-Id, and vice versa). The
dispatcher enforces this implicitly by inspecting the presence of
X-Hermes-Method (the v0.7 header) vs X-Agent-Id (the v1.6 header).

Why a separate dispatcher instead of inlining the v0.7 verifier
in the route:
  - Keeps the v0.7 verifier as a self-contained dependency
    (also used by the v0.7-only /status endpoint)
  - Centralizes the routing decision in one place
  - Future: HERMES_HMAC_ACCEPT_V06 env var + deprecation logging
    can be added here without touching the route
  - Future: dual-format on GET /{id} (step 7 also covers this)
    just changes the import in that route
"""
from __future__ import annotations

from fastapi import Header, HTTPException, Request

# v0.7 §1.4 verifier (7 X-Hermes-* headers, base64 HMAC, key_id
# lookup, nonce replay protection). See auth/hmac_v07.py.
from hermes_orch.auth.hmac_v07 import require_hmac_auth_v07

# v1.6 verifier (3 headers X-Agent-Id / X-Timestamp / X-Signature,
# hex HMAC, agent id lookup). See auth/hmac.py. The 2 existing
# HMAC-protected routes (heartbeat, GET /{id}) currently use this.
from hermes_orch.auth.hmac import require_hmac_auth


async def dispatch_hmac_auth(
    request: Request,
    # v0.7 §1.4 headers
    x_hermes_method: str | None = Header(
        default=None, alias="X-Hermes-Method"
    ),
    x_hermes_path: str | None = Header(
        default=None, alias="X-Hermes-Path"
    ),
    x_hermes_body_sha256: str | None = Header(
        default=None, alias="X-Hermes-Body-SHA256"
    ),
    x_hermes_key_id: str | None = Header(
        default=None, alias="X-Hermes-Key-Id"
    ),
    x_hermes_timestamp: str | None = Header(
        default=None, alias="X-Hermes-Timestamp"
    ),
    x_hermes_nonce: str | None = Header(
        default=None, alias="X-Hermes-Nonce"
    ),
    x_hermes_signature: str | None = Header(
        default=None, alias="X-Hermes-Signature"
    ),
    # v1.6 headers (only used if no v0.7 headers)
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> str:
    """FastAPI dependency: dispatch HMAC verification by header set.

    Routes to the v0.7 §1.4 verifier if X-Hermes-Method is present
    (the canonical v0.7 header). Otherwise routes to the v1.6
    verifier if X-Agent-Id is present. If neither, returns 401
    MISSING_AUTH_HEADERS.

    Returns the agent_id (str) on success. The route handler
    should compare this to the URL path's {agent_id} to prevent
    a valid signature for agent A from being used to access
    agent B's resources (defense in depth — the verifier already
    enforces this for the v0.7 path via the key-id-to-agent rule,
    but the v0.6 path only does it via the X-Agent-Id header check).

    Usage:
        @router.post("/{agent_id}/heartbeat")
        async def heartbeat(
            agent_id: str,
            request: Request,
            auth_agent_id: str = Depends(dispatch_hmac_auth),
        ):
            if auth_agent_id != agent_id:
                raise HTTPException(401, ...)
            ...
    """
    if x_hermes_method is not None:
        # v0.7 §1.4 path. Pass the v0.7 headers explicitly so
        # the v0.7 verifier doesn't see None (its Header defaults).
        return await require_hmac_auth_v07(
            request=request,
            x_hermes_method=x_hermes_method,
            x_hermes_path=x_hermes_path,
            x_hermes_body_sha256=x_hermes_body_sha256,
            x_hermes_key_id=x_hermes_key_id,
            x_hermes_timestamp=x_hermes_timestamp,
            x_hermes_nonce=x_hermes_nonce,
            x_hermes_signature=x_hermes_signature,
        )
    if x_agent_id is not None:
        # v0.6 / v1.6 path (the 2 existing routes currently use this).
        return await require_hmac_auth(
            request=request,
            x_agent_id=x_agent_id,
            x_timestamp=x_timestamp,
            x_signature=x_signature,
        )
    # Neither format present
    raise HTTPException(
        401, "MISSING_AUTH_HEADERS: No v0.6 (X-Agent-Id) or "
        "v0.7 (X-Hermes-Method) auth headers present"
    )
