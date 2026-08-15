# coding: utf-8
"""v0.7 §1.4 bound-metadata HMAC verifier (2026-08-15).

Replaces the v1.6 3-header format with the v0.7 7-header format.
The 7 headers (per the orch client build impl plan §1.4 + spec
at docs/specs/orch-server-hmac-v0.7-alignment.md):

  X-Hermes-Method:      GET, POST, etc. (uppercase)
  X-Hermes-Path:        canonical request path (NO query string;
                        v0.7 §1.4 forbids query strings on signed
                        endpoints)
  X-Hermes-Body-SHA256: hex SHA-256 of the raw request body bytes
                        (lowercase hex, 64 chars; for empty body
                        use the well-known empty-body SHA-256
                        e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855)
  X-Hermes-Key-Id:      operator-assigned key id; the server looks
                        up the agent by hmac_key_id (NOT by id)
                        per the key-id-to-agent authorization rule
  X-Hermes-Timestamp:   unix epoch seconds (decimal string)
  X-Hermes-Nonce:       random per-request nonce (32 hex chars
                        recommended); used for replay protection
                        via the in-process nonce store
  X-Hermes-Signature:   base64(HMAC-SHA256(secret, canonical))

Canonical string-to-sign (5 fields, joined by '\\n'):

  <METHOD>\n<PATH>\n<BODY_SHA256_HEX>\n<TIMESTAMP>\n<NONCE>

Path does NOT include the query string (v0.7 §1.4 forbids). Body
is the hex SHA-256 of the raw body bytes (NOT the body itself).
Timestamp and nonce are taken directly from the headers.

Algorithm:
  canonical = f"{METHOD}\\n{PATH}\\n{BODY_SHA256}\\n{TIMESTAMP}\\n{NONCE}"
  sig = base64(HMAC-SHA256(key=secret, msg=canonical).digest())

Server validation (8 steps per spec §1):
  1. All 7 X-Hermes-* headers present
  2. Timestamp parses as int, |now - ts| <= HMAC_WINDOW_SEC (300s)
  3. URL path has no query string (v0.7 §1.4 forbids)
  4. Body SHA-256 matches X-Hermes-Body-SHA256
  5. Look up agent by hmac_key_id (the key-id-to-agent rule)
  6. Compute signature, constant-time compare with X-Hermes-Signature
  7. Reject if nonce already seen (replay protection)
  8. Return the agent_id

Threat model: same as v1.6 (local network, protects against
wrapper impersonation and replay; NOT against DB compromise).
Plaintext secret in DB; encryption is B11 (security/agent-secret-at-rest
track, separate).

Migration (Option B dual-format, per spec §3): during the
Day 0-30 transition, BOTH v0.6 and v0.7 are accepted on the
2 existing HMAC-protected routes (heartbeat, GET /{id}) via
the dispatcher in require_hmac_auth_dispatch (TBD in step 7).
The new route GET /api/agents/{id}/status (step 6) is v0.7-only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

from fastapi import Header, HTTPException, Request


# Reuse v1.6's window default + env var so operators can tune both
# formats in one place. If you need a separate v0.7 window later,
# add HERMES_HMAC_V07_WINDOW_SEC and read it here.
from hermes_orch.auth.hmac import _read_window_sec


# Required v0.7 headers (7 total, per spec §1)
_REQUIRED_HEADERS = (
    "X-Hermes-Method",
    "X-Hermes-Path",
    "X-Hermes-Body-SHA256",
    "X-Hermes-Key-Id",
    "X-Hermes-Timestamp",
    "X-Hermes-Nonce",
    "X-Hermes-Signature",
)


# === Canonical string-to-sign (5 fields) ===

def canonical_v07(method: str, path: str, body_sha256_hex: str,
                  timestamp: str, nonce: str) -> str:
    """Build the v0.7 §1.4 canonical string-to-sign.

    Components joined by '\\n' (newline). Body is the hex SHA-256
    of the raw body bytes (NOT the body itself). Path does NOT
    include query string. Both client and server compute this
    identically; the cross-language compat test on 2026-08-13
    byte-equal-verified Python + PowerShell implementations.
    """
    method_u = method.upper()
    body_hash = (body_sha256_hex or "").lower()
    return f"{method_u}\n{path}\n{body_hash}\n{timestamp}\n{nonce}"


def compute_signature_v07(secret: bytes, method: str, path: str,
                          body_sha256_hex: str, timestamp: str,
                          nonce: str) -> str:
    """Compute the v0.7 base64 HMAC-SHA256 signature.

    secret is bytes (per spec; v1.6 used a string, but the v0.7
    bootstrapper + Python helper use bytes for cleaner encoding).
    Returns the base64 string (matches X-Hermes-Signature format).
    """
    msg = canonical_v07(method, path, body_sha256_hex, timestamp,
                         nonce).encode("utf-8")
    return base64.b64encode(
        hmac.new(secret, msg, hashlib.sha256).digest()
    ).decode("ascii")


def verify_signature_v07(
    secret: bytes,
    method: str,
    path: str,
    body_sha256_hex: str,
    timestamp: str,
    nonce: str,
    provided: str,
) -> bool:
    """Constant-time signature compare. Returns True iff the
    provided signature matches what we'd compute.
    """
    expected = compute_signature_v07(
        secret, method, path, body_sha256_hex, timestamp, nonce
    )
    return hmac.compare_digest(expected, provided or "")


# === FastAPI dependency ===

async def require_hmac_auth_v07(
    request: Request,
    x_hermes_method: str | None = Header(default=None,
                                         alias="X-Hermes-Method"),
    x_hermes_path: str | None = Header(default=None,
                                       alias="X-Hermes-Path"),
    x_hermes_body_sha256: str | None = Header(
        default=None, alias="X-Hermes-Body-SHA256"),
    x_hermes_key_id: str | None = Header(default=None,
                                         alias="X-Hermes-Key-Id"),
    x_hermes_timestamp: str | None = Header(default=None,
                                           alias="X-Hermes-Timestamp"),
    x_hermes_nonce: str | None = Header(default=None,
                                         alias="X-Hermes-Nonce"),
    x_hermes_signature: str | None = Header(default=None,
                                            alias="X-Hermes-Signature"),
) -> str:
    """FastAPI dependency: verify v0.7 §1.4 HMAC and return
    the agent_id.

    Usage:
        @router.get("/...")
        async def my_endpoint(
            request: Request,
            agent_id: str = Depends(require_hmac_auth_v07),
        ):
            ...

    Behavior (8 steps per spec §1):
      - 401 if any of the 7 X-Hermes-* headers is missing
      - 401 if timestamp is malformed or outside the window
      - 401 if URL path has a query string (v0.7 §1.4 forbids)
      - 401 if actual body SHA-256 != X-Hermes-Body-SHA256
      - 401 if no agent has the given hmac_key_id (UNKNOWN_KEY_ID)
      - 401 if signature doesn't match (INVALID_SIGNATURE)
      - 401 if nonce already seen (NONCE_REPLAY)
      - Returns agent_id on success (caller checks that the URL
        path's {agent_id} matches what the verifier returned)
    """
    # 1. All 7 headers present
    headers = {
        "X-Hermes-Method": x_hermes_method,
        "X-Hermes-Path": x_hermes_path,
        "X-Hermes-Body-SHA256": x_hermes_body_sha256,
        "X-Hermes-Key-Id": x_hermes_key_id,
        "X-Hermes-Timestamp": x_hermes_timestamp,
        "X-Hermes-Nonce": x_hermes_nonce,
        "X-Hermes-Signature": x_hermes_signature,
    }
    for name, val in headers.items():
        if not val:
            raise HTTPException(401, f"Missing header: {name}")

    # 2. Timestamp window
    try:
        ts_int = int(x_hermes_timestamp)
    except (TypeError, ValueError):
        raise HTTPException(
            401, "Invalid X-Hermes-Timestamp (not an integer)"
        )
    now = int(time.time())
    window = _read_window_sec()
    if abs(now - ts_int) > window:
        raise HTTPException(
            401,
            f"X-Hermes-Timestamp out of window "
            f"(|now-ts|={abs(now - ts_int)}s > {window}s)",
        )

    # 3. v0.7 §1.4 forbids query strings on signed endpoints
    if request.url.query:
        raise HTTPException(
            401,
            "Query strings are not allowed on v0.7 signed endpoints",
        )

    # 4. Body hash matches the X-Hermes-Body-SHA256 header
    body_bytes = await request.body()
    actual_body_sha256 = hashlib.sha256(body_bytes or b"").hexdigest()
    if actual_body_sha256 != x_hermes_body_sha256.lower():
        raise HTTPException(
            401,
            f"X-Hermes-Body-SHA256 mismatch: "
            f"actual={actual_body_sha256}, "
            f"provided={x_hermes_body_sha256.lower()}",
        )

    # 5. Look up agent by hmac_key_id (key-id-to-agent rule, §1.4)
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT id, hmac_secret, hmac_key_id FROM agents "
        "WHERE hmac_key_id = ?",
        (x_hermes_key_id,),
    )
    if not row:
        raise HTTPException(
            401, f"Unknown hmac_key_id: {x_hermes_key_id}"
        )
    agent_id = row["id"]
    secret_str = row.get("hmac_secret")
    if not secret_str:
        # v0.7 requires hmac_secret to be populated (the v1.6
        # legacy-mode fallback is NOT used for v0.7).
        raise HTTPException(
            401,
            f"Agent {agent_id} has no hmac_secret; "
            f"v0.7 requires HMAC bootstrap",
        )

    # 6. Verify signature (constant-time compare)
    secret = secret_str.encode("utf-8")
    ok = verify_signature_v07(
        secret=secret,
        method=x_hermes_method,
        path=x_hermes_path,
        body_sha256_hex=x_hermes_body_sha256,
        timestamp=x_hermes_timestamp,
        nonce=x_hermes_nonce,
        provided=x_hermes_signature,
    )
    if not ok:
        raise HTTPException(401, "Invalid X-Hermes-Signature")

    # 7. Nonce replay check (in-process store, attached at lifespan)
    nonce_store = getattr(request.app.state, "v07_nonce_store", None)
    if nonce_store is not None:
        if nonce_store.is_seen(x_hermes_nonce):
            raise HTTPException(
                401,
                f"Nonce replay detected: {x_hermes_nonce[:8]}...",
            )
        nonce_store.add(x_hermes_nonce)

    return agent_id
