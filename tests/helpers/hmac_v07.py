"""Test helper: sign_v07_request (DRAFT 2026-08-13).

This file is a DRAFT for future Day 5+ implementation. It is NOT
executed today. The draft provides the v0.7 §1.4 bound-metadata
HMAC signing helper that the test client uses to make authenticated
requests.

MUST stay in sync with the bootstrapper's Wait-ForEnrollment
function at installer/bootstrapper/install-orch-client.ps1 (line
~285). Both implementations compute the same canonical input +
HMAC-SHA256 signature; if they diverge, the bootstrapper cannot
complete enrollment against the v0.7-aligned server.

Per v0.7 §1.4 (docs/proposals/orch-client-build-impl-plan-v0.7.md
line 327+), the canonical input format is:
    <METHOD>\\n<PATH>\\n<BODY_SHA256_HEX>\\n<TIMESTAMP>\\n<NONCE>

The signature is base64(HMAC-SHA256(secret, canonical_input_bytes)).

The 7 X-Hermes-* headers are documented in the spec §1.1.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Optional


def sign_v07_request(
    method: str,
    path: str,
    body: bytes,
    key_id: str,
    secret: bytes,
    timestamp: Optional[int] = None,
    nonce: Optional[str] = None,
) -> dict[str, str]:
    """Return the 7 X-Hermes-* headers that would make this request
    pass v0.7 §1.4 verification on the orch server.

    The test client sends these headers along with the request.

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
    """
    if timestamp is None:
        timestamp = int(time.time())
    if nonce is None:
        nonce = uuid.uuid4().hex

    body_sha256_hex = hashlib.sha256(body or b"").hexdigest()
    canonical = (
        f"{method.upper()}\n{path}\n{body_sha256_hex}\n{timestamp}\n{nonce}"
    )
    sig = base64.b64encode(
        hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")

    return {
        "X-Hermes-Method": method.upper(),
        "X-Hermes-Path": path,
        "X-Hermes-Body-SHA256": body_sha256_hex,
        "X-Hermes-Key-Id": key_id,
        "X-Hermes-Timestamp": str(timestamp),
        "X-Hermes-Nonce": nonce,
        "X-Hermes-Signature": sig,
    }
