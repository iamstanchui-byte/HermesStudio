"""Unit tests for hermes_orch.agent_cli._hmac_headers v0.7 transition.

Background (2026-08-16 22:24 hotfix):
  Before this fix, _hmac_headers (and the inner _auth_headers) returned
  `{}` when an HMAC v0.7 credential was configured, assuming that
  `agent_http.get/post` would auto-inject the 7 X-Hermes-* headers.

  That assumption is FALSE for any call site that uses
  `httpx.Client.get/post` directly (the config-poll loop, the skills
  sync, apply_configs, _claim_one, _ack, ...). For those sites, no
  v0.7 headers ever went out, and the server returned 401.

  The fix: _hmac_headers (and _auth_headers) now sign and return the
  7 X-Hermes-* headers themselves when v0.7 is configured. The
  `httpx.Client` call sites then forward the dict as `headers=` to the
  server, which verifies the signature and accepts the request.

  These tests verify:
    1. When v0.7 is configured, returns 7 X-Hermes-* headers (not empty)
    2. The signature is valid (verified by re-signing with the same
       credential and comparing)
    3. Query string in the path is stripped from the signed path
       (v0.7 §1.4 forbids query strings)
    4. When v0.7 is NOT configured, returns the 3 v0.6 headers
       (backwards compat with v0.6-only setups)
"""
from __future__ import annotations

import base64
import hmac as _hmac
import hashlib

import pytest

from hermes_orch import agent_cli
from hermes_orch import agent_http
from hermes_orch.auth.hmac_v07 import (
    canonical_v07,
    compute_signature_v07,
    sign_v07_request,
)


# === Test fixtures ===

TEST_KEY_ID = "win-local-1-key-1"
# 32 bytes of deterministic test secret (NOT the production one)
TEST_SECRET_HEX = "75d94e4b24d1032221eb63bf813dac00e12c761b84b363b0ea626ed6eca10a58"
TEST_SECRET = bytes.fromhex(TEST_SECRET_HEX)
TEST_SECRET_B64 = base64.b64encode(TEST_SECRET).decode("ascii")  # v0.6 text format

# Use a placeholder for the v0.6 path; _hmac_headers takes `secret`
# as a parameter, so we just pass bytes.
TEST_AGENT_ID = "win-local-1"


@pytest.fixture(autouse=True)
def reset_hmac_credential():
    """Snapshot and restore agent_http's HMAC credential around each test.

    Without this, tests would leak credentials into the global
    module state and break unrelated tests.
    """
    saved = (agent_http._HMAC_KEY_ID, agent_http._HMAC_SECRET)
    try:
        yield
    finally:
        agent_http._HMAC_KEY_ID, agent_http._HMAC_SECRET = saved


# === v0.7 path: must return 7 X-Hermes-* headers ===

EXPECTED_V07_KEYS = {
    "X-Hermes-Method",
    "X-Hermes-Path",
    "X-Hermes-Body-SHA256",
    "X-Hermes-Key-Id",
    "X-Hermes-Timestamp",
    "X-Hermes-Nonce",
    "X-Hermes-Signature",
}


def test_v07_returns_7_headers_not_empty():
    """_hmac_headers with v0.7 credential must return 7 headers, not {}.

    Regression test for the 2026-08-16 hotfix: the function used to
    return `{}`, breaking all `httpx.Client`-based call sites.
    """
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    assert agent_http.has_hmac_credential()

    h = agent_cli._hmac_headers(
        TEST_AGENT_ID, TEST_SECRET,
        method="GET",
        path="/api/agents/win-local-1/profiles/win-agent01/configs/pending",
    )
    assert h, "_hmac_headers returned empty dict; v0.7 headers not signed"
    assert set(h.keys()) == EXPECTED_V07_KEYS, (
        f"wrong header set. got: {set(h.keys())}, expected: {EXPECTED_V07_KEYS}"
    )


def test_v07_signed_signature_is_valid():
    """The signature returned by _hmac_headers must verify with the same secret."""
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    path = "/api/agents/win-local-1/profiles/win-agent01/configs/pending"
    h = agent_cli._hmac_headers(
        TEST_AGENT_ID, TEST_SECRET, method="GET", path=path,
    )

    # Rebuild the canonical input the way the server's verifier does
    nonce = h["X-Hermes-Nonce"]
    timestamp = h["X-Hermes-Timestamp"]  # str (Unix epoch)
    expected_sig = compute_signature_v07(
        secret=TEST_SECRET,
        method="GET",
        path=path,
        body_sha256_hex=h["X-Hermes-Body-SHA256"],
        timestamp=timestamp,
        nonce=nonce,
    )
    actual_sig_b64 = h["X-Hermes-Signature"]
    actual_sig = base64.b64decode(actual_sig_b64)
    expected_sig_bytes = base64.b64decode(expected_sig)

    assert _hmac.compare_digest(actual_sig, expected_sig_bytes), (
        "signature mismatch — _hmac_headers is signing the wrong canonical input"
    )


def test_v07_key_id_and_body_sha256_match():
    """X-Hermes-Key-Id and X-Hermes-Body-SHA256 are passed through correctly."""
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    body = b'{"status":"idle"}'
    h = agent_cli._hmac_headers(
        TEST_AGENT_ID, TEST_SECRET, method="POST", path="/api/agents/x/heartbeat",
        body=body,
    )
    assert h["X-Hermes-Key-Id"] == TEST_KEY_ID
    assert h["X-Hermes-Body-SHA256"] == hashlib.sha256(body).hexdigest()
    assert h["X-Hermes-Method"] == "POST"
    # X-Hermes-Path is the path verbatim (caller's responsibility to strip query)
    assert h["X-Hermes-Path"] == "/api/agents/x/heartbeat"


# === v0.7 §1.4: query string must be stripped from the signed path ===

def test_v07_strips_query_string_from_signed_path():
    """If the caller passes a path with a query string, the SIGNED path
    must NOT include the query string (v0.7 §1.4 forbids query strings).

    The X-Hermes-Path header is what the server verifies, so it must
    match the canonical (path-only) form.
    """
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    path_with_query = (
        "/api/agents/win-local-1/profiles/win-agent01/skills?include_deleted=1"
    )
    h = agent_cli._hmac_headers(
        TEST_AGENT_ID, TEST_SECRET, method="GET", path=path_with_query,
    )
    # The signed path (X-Hermes-Path) must be path-only, no query
    assert h["X-Hermes-Path"] == (
        "/api/agents/win-local-1/profiles/win-agent01/skills"
    )
    assert "?" not in h["X-Hermes-Path"], (
        f"query string leaked into signed path: {h['X-Hermes-Path']!r}"
    )


def test_v07_signature_uses_stripped_path():
    """The signature must be computed against the stripped path.

    If we accidentally signed the full path including query, the
    server's verifier (which uses path-only) would reject with
    401 INVALID_SIGNATURE.
    """
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    path_with_query = (
        "/api/agents/win-local-1/profiles/win-agent01/skills?include_deleted=1"
    )
    h = agent_cli._hmac_headers(
        TEST_AGENT_ID, TEST_SECRET, method="GET", path=path_with_query,
    )

    # Re-verify using the path-only (as the server does)
    path_only = "/api/agents/win-local-1/profiles/win-agent01/skills"
    expected_sig = compute_signature_v07(
        secret=TEST_SECRET,
        method="GET",
        path=path_only,
        body_sha256_hex=h["X-Hermes-Body-SHA256"],
        timestamp=h["X-Hermes-Timestamp"],
        nonce=h["X-Hermes-Nonce"],
    )
    actual_sig = base64.b64decode(h["X-Hermes-Signature"])
    expected_sig_bytes = base64.b64decode(expected_sig)
    assert _hmac.compare_digest(actual_sig, expected_sig_bytes), (
        "signature was computed against the wrong (non-stripped) path"
    )


# === v0.6 path: backwards compat ===

def test_no_v07_credential_returns_v06_headers():
    """Without an HMAC v0.7 credential, _hmac_headers returns 3 v0.6 headers."""
    # Make sure no credential is set (fixture restores state, but be explicit)
    agent_http.set_hmac_credential("", "")
    assert not agent_http.has_hmac_credential()

    h = agent_cli._hmac_headers(
        TEST_AGENT_ID, "any-text-secret",
        method="GET", path="/api/agents/x/heartbeat",
    )
    assert set(h.keys()) == {"X-Agent-Id", "X-Timestamp", "X-Signature"}, (
        f"v0.6 path returned wrong header set: {set(h.keys())}"
    )
    assert h["X-Agent-Id"] == TEST_AGENT_ID
    # X-Timestamp is a unix timestamp string
    assert h["X-Timestamp"].isdigit()


def test_v06_path_unchanged_when_v07_disabled():
    """The v0.6 path must work even if v0.7 is not configured, for the
    3 fetch_*_http helpers and any other v0.6-only setups.
    """
    agent_http.set_hmac_credential("", "")

    # Mimic the v0.6 default path: secret is text (base64) and gets
    # encoded as utf-8 bytes inside compute_signature.
    h = agent_cli._hmac_headers(
        TEST_AGENT_ID, TEST_SECRET_B64,
        method="GET", path="/api/projects/x/memory/state",
    )
    # 3 headers, not 7
    assert len(h) == 3
    assert "X-Hermes-Method" not in h
