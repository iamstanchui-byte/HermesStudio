"""Unit tests for hermes_orch.auth.hmac_v07.sign_v07_request (client-side signer).

These tests verify the CANONICAL Python signer (used by the wrapper)
matches the v0.7 §1.4 spec. The cross-language compat test in
tests/test_hmac_v07_golden.py covers the PowerShell bootstrapper
side; these tests focus on Python edge cases.
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import re
import time

import pytest

from hermes_orch.auth.hmac_v07 import (
    canonical_v07,
    compute_signature_v07,
    sign_v07_request,
)


# === Test fixtures ===

TEST_KEY_ID = "win-local-1-key-1"
TEST_SECRET = b"a]3dF9kL0mN2pQ7rT5wX8yZ1bC4eG6hJ"  # 32 bytes, typical secret


# === Header presence + format ===

EXPECTED_KEYS = {
    "X-Hermes-Method",
    "X-Hermes-Path",
    "X-Hermes-Body-SHA256",
    "X-Hermes-Key-Id",
    "X-Hermes-Timestamp",
    "X-Hermes-Nonce",
    "X-Hermes-Signature",
}


def test_returns_exactly_7_headers():
    """sign_v07_request must return exactly the 7 spec'd headers, no more, no less."""
    h = sign_v07_request("POST", "/api/agents/x/heartbeat", b"{}", TEST_KEY_ID, TEST_SECRET)
    assert set(h.keys()) == EXPECTED_KEYS


def test_method_uppercased():
    """X-Hermes-Method is always uppercase."""
    for m in ("get", "Post", "DELETE", "patch", "PUT"):
        h = sign_v07_request(m, "/x", b"", TEST_KEY_ID, TEST_SECRET)
        assert h["X-Hermes-Method"] == m.upper()


def test_path_preserved_verbatim():
    """X-Hermes-Path is the caller's path as-is (caller is responsible for stripping query)."""
    h = sign_v07_request("GET", "/api/agents/win-local-1", b"", TEST_KEY_ID, TEST_SECRET)
    assert h["X-Hermes-Path"] == "/api/agents/win-local-1"


def test_body_sha256_format():
    """X-Hermes-Body-SHA256 is lowercase hex, 64 chars (per spec)."""
    h = sign_v07_request("POST", "/x", b'{"hello":"world"}', TEST_KEY_ID, TEST_SECRET)
    sha = h["X-Hermes-Body-SHA256"]
    assert len(sha) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", sha), f"not lowercase hex: {sha!r}"


def test_empty_body_sha256_well_known():
    """Empty body uses the well-known empty SHA-256 (per spec).

    The well-known value is the SHA-256 of b'':
    e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
    """
    EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    h = sign_v07_request("GET", "/x", b"", TEST_KEY_ID, TEST_SECRET)
    assert h["X-Hermes-Body-SHA256"] == EMPTY_SHA


def test_key_id_preserved_verbatim():
    """X-Hermes-Key-Id is the caller's key_id, no transformation."""
    h = sign_v07_request("GET", "/x", b"", "my-custom-key-id", TEST_SECRET)
    assert h["X-Hermes-Key-Id"] == "my-custom-key-id"


def test_timestamp_format():
    """X-Hermes-Timestamp is unix epoch seconds as decimal string."""
    h = sign_v07_request("GET", "/x", b"", TEST_KEY_ID, TEST_SECRET, timestamp=1700000000)
    assert h["X-Hermes-Timestamp"] == "1700000000"


def test_nonce_format():
    """X-Hermes-Nonce is the caller's nonce, no transformation (server may check length)."""
    h = sign_v07_request("GET", "/x", b"", TEST_KEY_ID, TEST_SECRET, nonce="abcdef1234567890")
    assert h["X-Hermes-Nonce"] == "abcdef1234567890"


def test_timestamp_uses_current_when_none():
    """Default timestamp is within a few seconds of now."""
    before = int(time.time())
    h = sign_v07_request("GET", "/x", b"", TEST_KEY_ID, TEST_SECRET)
    after = int(time.time())
    ts = int(h["X-Hermes-Timestamp"])
    assert before <= ts <= after + 1  # +1 for floor rounding


def test_nonce_default_is_hex():
    """Default nonce is 32-char hex (uuid4().hex)."""
    h = sign_v07_request("GET", "/x", b"", TEST_KEY_ID, TEST_SECRET)
    assert len(h["X-Hermes-Nonce"]) == 32
    assert re.fullmatch(r"[0-9a-f]{32}", h["X-Hermes-Nonce"])


# === Signature correctness ===

def test_signature_matches_manual_computation():
    """Signature = base64(HMAC-SHA256(secret, canonical)). Verified independently."""
    body = b'{"event":"heartbeat"}'
    h = sign_v07_request("POST", "/api/agents/x/heartbeat", body, TEST_KEY_ID, TEST_SECRET,
                         timestamp=1700000000, nonce="deadbeef" * 4)

    body_sha = hashlib.sha256(body).hexdigest()
    canonical = canonical_v07("POST", "/api/agents/x/heartbeat", body_sha, "1700000000", "deadbeef" * 4)
    expected_sig = base64.b64encode(
        _hmac.new(TEST_SECRET, canonical.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")

    assert h["X-Hermes-Signature"] == expected_sig


def test_signature_changes_with_body():
    """Different body -> different signature (regression test for canonical body_sha binding)."""
    h1 = sign_v07_request("POST", "/x", b"body-1", TEST_KEY_ID, TEST_SECRET, timestamp=1700000000, nonce="n")
    h2 = sign_v07_request("POST", "/x", b"body-2", TEST_KEY_ID, TEST_SECRET, timestamp=1700000000, nonce="n")
    assert h1["X-Hermes-Signature"] != h2["X-Hermes-Signature"]


def test_signature_changes_with_method():
    """Different method -> different signature."""
    h1 = sign_v07_request("GET", "/x", b"", TEST_KEY_ID, TEST_SECRET, timestamp=1700000000, nonce="n")
    h2 = sign_v07_request("POST", "/x", b"", TEST_KEY_ID, TEST_SECRET, timestamp=1700000000, nonce="n")
    assert h1["X-Hermes-Signature"] != h2["X-Hermes-Signature"]


def test_signature_changes_with_path():
    """Different path -> different signature."""
    h1 = sign_v07_request("GET", "/api/agents/a", b"", TEST_KEY_ID, TEST_SECRET, timestamp=1700000000, nonce="n")
    h2 = sign_v07_request("GET", "/api/agents/b", b"", TEST_KEY_ID, TEST_SECRET, timestamp=1700000000, nonce="n")
    assert h1["X-Hermes-Signature"] != h2["X-Hermes-Signature"]


def test_signature_changes_with_secret():
    """Different secret -> different signature (regression: protects against key confusion)."""
    h1 = sign_v07_request("GET", "/x", b"", TEST_KEY_ID, b"secret-1", timestamp=1700000000, nonce="n")
    h2 = sign_v07_request("GET", "/x", b"", TEST_KEY_ID, b"secret-2", timestamp=1700000000, nonce="n")
    assert h1["X-Hermes-Signature"] != h2["X-Hermes-Signature"]


def test_signature_changes_with_timestamp():
    """Different timestamp -> different signature (replay protection)."""
    h1 = sign_v07_request("GET", "/x", b"", TEST_KEY_ID, TEST_SECRET, timestamp=1700000000, nonce="n")
    h2 = sign_v07_request("GET", "/x", b"", TEST_KEY_ID, TEST_SECRET, timestamp=1700000001, nonce="n")
    assert h1["X-Hermes-Signature"] != h2["X-Hermes-Signature"]


def test_signature_changes_with_nonce():
    """Different nonce -> different signature (replay protection)."""
    h1 = sign_v07_request("GET", "/x", b"", TEST_KEY_ID, TEST_SECRET, timestamp=1700000000, nonce="nonce-1")
    h2 = sign_v07_request("GET", "/x", b"", TEST_KEY_ID, TEST_SECRET, timestamp=1700000000, nonce="nonce-2")
    assert h1["X-Hermes-Signature"] != h2["X-Hermes-Signature"]


# === Stability: same input -> same output (golden check) ===

def test_stable_signature_for_same_input():
    """Determinism check: identical input produces byte-identical headers."""
    h1 = sign_v07_request("POST", "/api/agents/win-local-1/heartbeat", b'{"k":1}',
                          TEST_KEY_ID, TEST_SECRET, timestamp=1700000000, nonce="fixed-nonce")
    h2 = sign_v07_request("POST", "/api/agents/win-local-1/heartbeat", b'{"k":1}',
                          TEST_KEY_ID, TEST_SECRET, timestamp=1700000000, nonce="fixed-nonce")
    assert h1 == h2


# === Realistic use case: full POST heartbeat ===

def test_realistic_heartbeat_post():
    """End-to-end shape test: a real POST /heartbeat request signs correctly."""
    body = b'{"ts":1700000000,"status":"ok"}'
    h = sign_v07_request("POST", "/api/agents/win-local-1/heartbeat", body,
                          "win-local-1-key-1", TEST_SECRET, timestamp=1700000000, nonce="aabbccdd" * 4)

    # Sanity: signature matches what we expect
    expected = compute_signature_v07(
        secret=TEST_SECRET,
        method="POST",
        path="/api/agents/win-local-1/heartbeat",
        body_sha256_hex=hashlib.sha256(body).hexdigest(),
        timestamp="1700000000",
        nonce="aabbccdd" * 4,
    )
    assert h["X-Hermes-Signature"] == expected
    # Body SHA matches the actual body
    assert h["X-Hermes-Body-SHA256"] == hashlib.sha256(body).hexdigest()
    # Headers are usable in httpx (all values are strings, no None)
    assert all(isinstance(v, str) and v for v in h.values())
