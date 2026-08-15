# coding: utf-8
"""Dual-format HMAC dispatcher (2026-08-15, hardened 2026-08-15).

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

Hardening 2026-08-15 (Phase 1 of security/v07-hardening):
- Mixed v0.6 + v0.7 header set -> 401 MIXED_HEADERS (strict
  reject; no fallthrough to v0.7 or v0.6 path)
- Partial v0.7 header set (any X-Hermes-* but not all 7) -> 401
  MIXED_HEADERS (strict reject; no fallthrough to v0.6 path)
- Rationale: a request with 4/7 v0.7 headers + 1/3 v0.6 headers
  is ambiguous. The previous dispatcher would route to v0.7 (because
  X-Hermes-Method was present) and let the v0.7 verifier fail with
  MISSING_AUTH_HEADERS on the missing 3 — a fail-open that leaks
  the v0.6 header's value to the v0.7 verifier (which silently
  ignores it) and lets an attacker fingerprint the server by
  including X-Agent-Id without effect. Strict reject eliminates
  this attack surface.

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
from hermes_orch.auth.hmac import _read_accept_v06, require_hmac_auth


# v0.7 §1.4 requires all 7 of these headers. Any subset of <7 with
# at least 1 present triggers MIXED_HEADERS reject. Constants here
# so the dispatcher + tests + spec share one definition.
_V07_REQUIRED_HEADERS = (
    "X-Hermes-Method",
    "X-Hermes-Path",
    "X-Hermes-Body-SHA256",
    "X-Hermes-Key-Id",
    "X-Hermes-Timestamp",
    "X-Hermes-Nonce",
    "X-Hermes-Signature",
)
_V06_REQUIRED_HEADERS = (
    "X-Agent-Id",
    "X-Timestamp",
    "X-Signature",
)


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

    Hardening (2026-08-15): strict reject of:
      - mixed v0.6 + v0.7 header sets (any header from each format)
      - partial v0.7 header sets (any X-Hermes-* but not all 7)

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
    # === Hardening: detect mixed/partial header sets before routing ===
    # Collect the present headers from each format into sorted lists
    # so the error message is deterministic.
    v07_present = sorted([
        name for name, val in (
            ("X-Hermes-Method", x_hermes_method),
            ("X-Hermes-Path", x_hermes_path),
            ("X-Hermes-Body-SHA256", x_hermes_body_sha256),
            ("X-Hermes-Key-Id", x_hermes_key_id),
            ("X-Hermes-Timestamp", x_hermes_timestamp),
            ("X-Hermes-Nonce", x_hermes_nonce),
            ("X-Hermes-Signature", x_hermes_signature),
        ) if val is not None
    ])
    v06_present = sorted([
        name for name, val in (
            ("X-Agent-Id", x_agent_id),
            ("X-Timestamp", x_timestamp),
            ("X-Signature", x_signature),
        ) if val is not None
    ])

    if v07_present and v06_present:
        raise HTTPException(
            401,
            f"MIXED_HEADERS: Mixed v0.6 ({','.join(v06_present)}) + v0.7 "
            f"({','.join(v07_present)}) headers in single request; "
            f"spec forbids combining formats",
        )

    if v07_present and len(v07_present) != len(_V07_REQUIRED_HEADERS):
        # Partial v0.7 set: 1-6 of 7 headers present, no v0.6 headers.
        # Strict reject — no fallthrough to v0.6 path (which would
        # fail with MISSING_AUTH_HEADERS anyway, but leaking the
        # v0.7 header set to the v0.6 verifier is a fail-open attack
        # surface).
        raise HTTPException(
            401,
            f"MIXED_HEADERS: Partial v0.7 header set "
            f"({len(v07_present)}/{len(_V07_REQUIRED_HEADERS)} present: "
            f"{','.join(v07_present)}); all 7 required when any "
            f"X-Hermes-* header is sent",
        )

    if v06_present and len(v06_present) != len(_V06_REQUIRED_HEADERS):
        # Partial v0.6 set: 1-2 of 3 headers present, no v0.7 headers.
        # The v1.6 verifier already returns MISSING_AUTH_HEADERS for
        # this, but hardening demands the dispatcher surfaces the
        # same MIXED_HEADERS error code as the v0.7 partial case
        # for consistency.
        raise HTTPException(
            401,
            f"MIXED_HEADERS: Partial v0.6 header set "
            f"({len(v06_present)}/{len(_V06_REQUIRED_HEADERS)} present: "
            f"{','.join(v06_present)}); all 3 required when any "
            f"X-Agent-Id/X-Timestamp/X-Signature header is sent",
        )

    if v07_present:
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
    if v06_present:
        # Hardening Phase 4 (2026-08-15): check the
        # HERMES_HMAC_ACCEPT_V06 env var. If the operator has
        # flipped the flag to false (post-migration), reject
        # v0.6 requests with 401 V0_6_DEPRECATED so the
        # bootstrapper / client sees a clear signal to
        # upgrade. v0.7 requests (the v07_present branch above)
        # are unaffected.
        #
        # Per spec §1.13: this is the standard 12-factor
        # pattern for a soft cutover. Default behavior (flag
        # unset) is True, preserving the pre-Phase-4 contract.
        if not _read_accept_v06():
            raise HTTPException(
                401,
                "V0_6_DEPRECATED: v0.6 HMAC format is disabled; "
                "use v0.7 (X-Hermes-* headers) — see "
                "docs/specs/orch-server-hmac-v0.7-alignment.md",
            )
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
