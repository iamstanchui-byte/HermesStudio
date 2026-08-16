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


# === Client-side signer (NEW: 2026-08-16) ===

def sign_v07_request(
    method: str,
    path: str,
    body: bytes,
    key_id: str,
    secret: bytes,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Sign a v0.7 request and return the 7 X-Hermes-* headers.

    This is the canonical Python signer. The PowerShell bootstrapper's
    `Wait-ForEnrollment` (installer/bootstrapper/install-orch-client.ps1
    line ~285) is the PowerShell counterpart and MUST stay byte-for-byte
    in sync — both produce the same canonical input + signature. The
    cross-language compat test on 2026-08-13 byte-equal-verified these.

    Args:
        method: HTTP method (uppercase, e.g. "GET", "POST").
        path:   Canonical path, no query string. v0.7 §1.4 forbids
                query strings on signed endpoints.
        body:   Raw request body bytes (b"" for GET with no body).
        key_id: The agent's HMAC key id; the server looks up the
                agent by this id (per the key-id-to-agent rule).
        secret: The agent's HMAC secret bytes.
        timestamp: Optional override; default is `int(time.time())`.
        nonce:  Optional override; default is `uuid.uuid4().hex`.

    Returns:
        A dict of 7 headers:
            X-Hermes-Method
            X-Hermes-Path
            X-Hermes-Body-SHA256
            X-Hermes-Key-Id
            X-Hermes-Timestamp
            X-Hermes-Nonce
            X-Hermes-Signature

    Thread-safety: not thread-safe at the timestamp/nonce level. If you
    call this from multiple threads concurrently, pass explicit
    `timestamp` and `nonce` to avoid reuse (server rejects replays).
    """
    import time as _time
    import uuid as _uuid

    if timestamp is None:
        timestamp = int(_time.time())
    if nonce is None:
        nonce = _uuid.uuid4().hex

    body_sha256_hex = hashlib.sha256(body or b"").hexdigest()
    sig = compute_signature_v07(
        secret=secret,
        method=method,
        path=path,
        body_sha256_hex=body_sha256_hex,
        timestamp=str(timestamp),
        nonce=nonce,
    )

    return {
        "X-Hermes-Method": method.upper(),
        "X-Hermes-Path": path,
        "X-Hermes-Body-SHA256": body_sha256_hex,
        "X-Hermes-Key-Id": key_id,
        "X-Hermes-Timestamp": str(timestamp),
        "X-Hermes-Nonce": nonce,
        "X-Hermes-Signature": sig,
    }


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

    Error format: the detail is "ERROR_CODE: human message" so
    the test cases can split on ": " to extract the code. Per
    the v0.7 spec §5, error codes are upper-snake-case.

    Note: I tried returning a JSONResponse with `{"error": "..."}`
    top-level keys (so the response is exactly what the spec
    describes), but FastAPI dependencies that return a
    JSONResponse cause the endpoint to receive the JSONResponse
    object as the dependency's return value, which then fails
    the `if auth_agent_id != agent_id` check. So we use
    HTTPException with a structured detail; a follow-up custom
    exception handler in main.py can convert these to the spec
    format if needed.
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
            raise HTTPException(
                401, f"MISSING_AUTH_HEADERS: Missing header: {name}"
            )

    # 1b. Hardening (2026-08-15, security/v07-hardening Phase 1):
    # X-Hermes-Method MUST equal the actual request method (case-insensitive
    # since HTTP methods are case-insensitive per RFC 7230 §3.1.1, but we
    # uppercase both sides for canonical comparison).
    # X-Hermes-Path MUST equal request.url.path byte-for-byte (case-sensitive,
    # no trailing slash, no query string — all enforced by step 3b below).
    #
    # Without this check, an attacker could sign `POST /a` and send
    # `GET /b` with those headers — the signature would still match
    # (the X-Hermes-* headers are internally consistent) but the
    # request would be bound to a different method + path. This
    # defeats the binding that the canonical input is supposed to
    # provide.
    #
    # Order matters: the X-Hermes-Path sub-checks run in a specific
    # order so the error code matches the spec's intent:
    #   1. Reject `?` in x_hermes_path first (spec §1.1 + §1.7: query
    #      strings forbidden in signed path) — 400 MALFORMED_HEADERS.
    #   2. Then compare x_hermes_path to request.url.path (spec §1.8
    #      binding) — 401 MALFORMED_HEADERS.
    # This way, T11 (query string in x_hermes_path) still returns 400
    # as the spec requires, even with the new binding check in place.
    if "?" in x_hermes_path:
        raise HTTPException(
            400,
            f"MALFORMED_HEADERS: X-Hermes-Path contains '?' "
            f"(query strings forbidden on signed endpoints): "
            f"{x_hermes_path!r}",
        )
    if x_hermes_method.upper() != request.method.upper():
        raise HTTPException(
            401,
            f"MALFORMED_HEADERS: X-Hermes-Method ({x_hermes_method!r}) "
            f"does not match the actual request method "
            f"({request.method!r})",
        )
    if x_hermes_path != request.url.path:
        raise HTTPException(
            401,
            f"MALFORMED_HEADERS: X-Hermes-Path ({x_hermes_path!r}) "
            f"does not match the actual request URL path "
            f"({request.url.path!r})",
        )

    # 2. Timestamp window
    try:
        ts_int = int(x_hermes_timestamp)
    except (TypeError, ValueError):
        raise HTTPException(
            401, "MALFORMED_HEADERS: X-Hermes-Timestamp is not a valid integer"
        )
    now = int(time.time())
    window = _read_window_sec()
    if abs(now - ts_int) > window:
        raise HTTPException(
            401,
            f"TIMESTAMP_OUT_OF_WINDOW: X-Hermes-Timestamp out of window "
            f"(|now-ts|={abs(now - ts_int)}s > {window}s)",
        )

    # 3. v0.7 §1.4 forbids query strings on signed endpoints
    if request.url.query:
        raise HTTPException(
            400, "MALFORMED_HEADERS: Query strings are not allowed on v0.7 signed endpoints"
        )
    # 3b. Canonical path form: case-sensitive, no trailing slash
    # (except for the root "/"). The X-Hermes-Path header is the
    # source of truth for what the client signed; if it ends with
    # a slash (other than root), the canonical form is violated.
    # This guards against clients canonicalizing differently
    # (e.g. adding "/" because their HTTP library appended it) —
    # the signature would still match since both sides use the
    # same X-Hermes-Path, but the result is a non-canonical path
    # that violates the spec.
    if x_hermes_path != "/" and x_hermes_path.endswith("/"):
        raise HTTPException(
            400,
            f"MALFORMED_HEADERS: X-Hermes-Path has trailing slash "
            f"(canonical form forbids it): {x_hermes_path!r}",
        )

    # 4. Body hash matches the X-Hermes-Body-SHA256 header
    body_bytes = await request.body()
    actual_body_sha256 = hashlib.sha256(body_bytes or b"").hexdigest()
    if actual_body_sha256 != x_hermes_body_sha256.lower():
        raise HTTPException(
            401,
            f"BODY_HASH_MISMATCH: X-Hermes-Body-SHA256 mismatch: "
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
            401, f"UNKNOWN_KEY_ID: Unknown hmac_key_id: {x_hermes_key_id}"
        )
    agent_id = row["id"]
    secret_str = row.get("hmac_secret")
    if not secret_str:
        # v0.7 requires hmac_secret to be populated (the v1.6
        # legacy-mode fallback is NOT used for v0.7).
        raise HTTPException(
            401,
            f"MISSING_AUTH_HEADERS: Agent {agent_id} has no hmac_secret; "
            f"v0.7 requires HMAC bootstrap",
        )

    # 6. Verify signature (constant-time compare)
    # v0.7 stores the HMAC secret as a hex string in the DB
    # (per the test fixture convention; same as the v0.6
    # register_test_agent pattern). Decode hex -> raw bytes
    # before computing the HMAC. Using `secret_str.encode("utf-8")`
    # here would produce the ASCII bytes of the hex string (e.g.
    # b'0123abcd...'), NOT the original 32 random bytes that the
    # client signed with — that mismatch caused 401 INVALID
    # SIGNATURE on the T1 happy-path test before this fix
    # (debugged via perplexity-web 2026-08-15). Verified 2026-08-15.
    try:
        secret = bytes.fromhex(secret_str)
    except (TypeError, ValueError):
        raise HTTPException(
            401,
            f"MISSING_AUTH_HEADERS: Stored hmac_secret is not valid hex: agent={agent_id}",
        )
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
        raise HTTPException(401, "INVALID_SIGNATURE: Invalid X-Hermes-Signature")

    # 7. Nonce replay check (in-process store, attached at lifespan)
    # Hardening Phase 2 (2026-08-15): use `add_if_absent` for atomic
    # check+record. The previous `is_seen` + `add` two-call pattern
    # had a race window between the two lock acquisitions; two
    # concurrent requests with the same nonce could both pass the
    # is_seen check and both proceed to verify (accepting two
    # requests with the same nonce). `add_if_absent` holds the
    # lock across the full check+insert so the second caller
    # deterministically returns False (replay detected).
    nonce_store = getattr(request.app.state, "v07_nonce_store", None)
    if nonce_store is not None:
        if not nonce_store.add_if_absent(x_hermes_nonce):
            raise HTTPException(
                401,
                f"NONCE_REPLAY: Nonce replay detected: "
                f"{x_hermes_nonce[:8]}...",
            )

    return agent_id
