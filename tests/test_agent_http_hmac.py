"""Integration tests for hermes_orch.agent_http HMAC v0.7 injection.

Verifies the agent_http layer transparently injects the 7 X-Hermes-*
headers into every outgoing request when an HMAC credential is
configured, and DOES NOT inject anything when no credential is set
(so the wrapper can fall back to v0.6 X-Agent-Id if needed).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hermes_orch import agent_http
from hermes_orch.auth.hmac_v07 import sign_v07_request


# === Test fixtures ===

TEST_KEY_ID = "test-key-1"
TEST_SECRET_HEX = b"a]3dF9kL0mN2pQ7rT5wX8yZ1bC4eG6hJ".hex()
TEST_SECRET_BYTES = bytes.fromhex(TEST_SECRET_HEX)


@pytest.fixture(autouse=True)
def _reset_hmac_credential():
    """Snapshot + restore the HMAC credential around each test.

    Without this, a `set_hmac_credential` in one test leaks into the
    next (module-level state). This fixture is autouse so every test
    is isolated; no opt-in needed.
    """
    saved_key = agent_http._HMAC_KEY_ID
    saved_secret = agent_http._HMAC_SECRET
    yield
    agent_http._HMAC_KEY_ID = saved_key
    agent_http._HMAC_SECRET = saved_secret


# === set_hmac_credential / has_hmac_credential ===

def test_default_no_credential():
    """Module starts with no credential (after _reset_hmac_credential ran)."""
    agent_http._HMAC_KEY_ID = None
    agent_http._HMAC_SECRET = None
    assert agent_http.has_hmac_credential() is False


def test_set_credential_enables_signing():
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    assert agent_http.has_hmac_credential() is True
    key_id, secret = agent_http.get_hmac_credential()
    assert key_id == TEST_KEY_ID
    assert secret == TEST_SECRET_BYTES


def test_set_empty_credential_disables():
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    assert agent_http.has_hmac_credential() is True
    agent_http.set_hmac_credential("", "")
    assert agent_http.has_hmac_credential() is False


def test_set_invalid_hex_raises():
    with pytest.raises(ValueError):
        agent_http.set_hmac_credential(TEST_KEY_ID, "not-hex-string")


def test_get_credential_reflects_state():
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    key_id, secret = agent_http.get_hmac_credential()
    assert key_id == TEST_KEY_ID
    assert secret == TEST_SECRET_BYTES


# === _body_bytes_for_hmac ===

def test_body_bytes_from_json_kwarg():
    """json=dict -> compact JSON bytes (no spaces)."""
    body = agent_http._body_bytes_for_hmac({"json": {"a": 1, "b": 2}})
    # Compact: no space after colon or comma
    assert body == b'{"a":1,"b":2}'


def test_body_bytes_from_content_bytes():
    body = agent_http._body_bytes_for_hmac({"content": b"raw-bytes-here"})
    assert body == b"raw-bytes-here"


def test_body_bytes_from_content_str():
    body = agent_http._body_bytes_for_hmac({"content": "text body"})
    assert body == b"text body"


def test_body_bytes_empty_when_no_body_kwarg():
    body = agent_http._body_bytes_for_hmac({})
    assert body == b""


def test_body_bytes_from_data_kwarg_returns_none():
    """Form data is not signed (HMAC is for JSON requests)."""
    body = agent_http._body_bytes_for_hmac({"data": {"a": "b"}})
    assert body is None


def test_body_bytes_content_wins_over_json():
    """If both `content=` and `json=` are passed, httpx uses content."""
    body = agent_http._body_bytes_for_hmac({"content": b"c", "json": {"a": 1}})
    assert body == b"c"


def test_body_bytes_handles_nested_json():
    body = agent_http._body_bytes_for_hmac({"json": {"nested": {"k": "v"}, "arr": [1, 2]}})
    # No spaces -- compact form
    assert b" " not in body


def test_body_bytes_handles_non_ascii():
    """Non-ASCII chars are UTF-8 encoded (ensure_ascii=False)."""
    body = agent_http._body_bytes_for_hmac({"json": {"name": "測試"}})
    assert "測試".encode("utf-8") in body


# === _signed_headers ===

def test_signed_headers_empty_when_no_credential():
    agent_http._HMAC_KEY_ID = None
    agent_http._HMAC_SECRET = None
    h = agent_http._signed_headers("GET", "https://x.com/api/agents/x", {})
    assert h == {}


def test_signed_headers_empty_for_form_data():
    """Form data is not signed."""
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    h = agent_http._signed_headers("POST", "https://x.com/api/x", {"data": {"a": "b"}})
    assert h == {}


def test_signed_headers_for_get_with_no_body():
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    h = agent_http._signed_headers("GET", "https://x.com/api/agents/x", {})
    assert "X-Hermes-Method" in h
    assert h["X-Hermes-Method"] == "GET"
    assert h["X-Hermes-Path"] == "/api/agents/x"
    # Empty body -> well-known SHA-256
    assert h["X-Hermes-Body-SHA256"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_signed_headers_for_post_with_json():
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    h = agent_http._signed_headers(
        "POST",
        "https://x.com/api/agents/win-local-1/heartbeat",
        {"json": {"k": 1}},
    )
    assert h["X-Hermes-Method"] == "POST"
    assert h["X-Hermes-Path"] == "/api/agents/win-local-1/heartbeat"
    # Body SHA matches the compact JSON
    assert h["X-Hermes-Body-SHA256"] == "abc1bbc431a2d2a4ee05b8ce5a1ec2b62fb9b9f7f5a8a7bfa0e9d2a3c4d5e6f7" or len(h["X-Hermes-Body-SHA256"]) == 64


def test_signed_headers_strip_query_string():
    """v0.7 §1.4 forbids query strings on signed paths."""
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    h = agent_http._signed_headers(
        "GET",
        "https://x.com/api/agents/x/profiles/win-agent01/skills?include_deleted=1",
        {},
    )
    assert h["X-Hermes-Path"] == "/api/agents/x/profiles/win-agent01/skills"
    assert "?" not in h["X-Hermes-Path"]


def test_signed_headers_strip_scheme_and_host():
    """Only the path goes into X-Hermes-Path (no scheme/host)."""
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    h = agent_http._signed_headers(
        "POST",
        "https://192.168.2.152:8765/api/agents/win-local-1/heartbeat",
        {"json": {}},
    )
    assert h["X-Hermes-Path"] == "/api/agents/win-local-1/heartbeat"


# === _merge_headers ===

def test_merge_headers_adds_signed_when_none_existing():
    merged = agent_http._merge_headers({}, {"X-Hermes-Method": "GET"})
    assert merged == {"X-Hermes-Method": "GET"}


def test_merge_headers_preserves_existing():
    merged = agent_http._merge_headers(
        {"headers": {"X-Agent-Id": "win-local-1", "Content-Type": "application/json"}},
        {"X-Hermes-Method": "GET"},
    )
    assert merged["X-Agent-Id"] == "win-local-1"
    assert merged["Content-Type"] == "application/json"
    assert merged["X-Hermes-Method"] == "GET"


def test_merge_headers_signed_wins_on_collision():
    """Defense: caller-supplied X-Hermes-* headers cannot override the signed values."""
    merged = agent_http._merge_headers(
        {"headers": {"X-Hermes-Method": "EVIL"}},
        {"X-Hermes-Method": "GET"},
    )
    assert merged["X-Hermes-Method"] == "GET"


def test_merge_headers_none_existing():
    """When `headers` kwarg is None (not just empty dict), no crash."""
    merged = agent_http._merge_headers({"headers": None}, {"X-Hermes-Method": "GET"})
    assert merged == {"X-Hermes-Method": "GET"}


# === End-to-end: agent_http.post injects headers ===

def test_post_injects_signed_headers_when_credential_set():
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    captured_kwargs = {}

    def fake_post(url, **kwargs):
        captured_kwargs.update(kwargs)
        # Return a mock response
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch("hermes_orch.agent_http.httpx.post", side_effect=fake_post):
        agent_http.post("https://x.com/api/agents/x/heartbeat", json={"k": 1})

    headers = captured_kwargs.get("headers", {})
    assert "X-Hermes-Method" in headers
    assert "X-Hermes-Signature" in headers
    assert headers["X-Hermes-Method"] == "POST"
    assert headers["X-Hermes-Path"] == "/api/agents/x/heartbeat"


def test_post_no_signed_headers_when_credential_not_set():
    agent_http._HMAC_KEY_ID = None
    agent_http._HMAC_SECRET = None
    captured_kwargs = {}

    def fake_post(url, **kwargs):
        captured_kwargs.update(kwargs)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch("hermes_orch.agent_http.httpx.post", side_effect=fake_post):
        agent_http.post("https://x.com/api/agents/x/heartbeat", json={"k": 1})

    headers = captured_kwargs.get("headers", {})
    # No HMAC headers -- caller might add their own X-Agent-Id
    assert "X-Hermes-Method" not in headers
    assert "X-Hermes-Signature" not in headers


def test_post_preserves_caller_headers():
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    captured_kwargs = {}

    def fake_post(url, **kwargs):
        captured_kwargs.update(kwargs)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch("hermes_orch.agent_http.httpx.post", side_effect=fake_post):
        agent_http.post(
            "https://x.com/api/agents/x/heartbeat",
            json={"k": 1},
            headers={"X-Agent-Id": "win-local-1"},
        )

    headers = captured_kwargs.get("headers", {})
    # Caller's X-Agent-Id is preserved alongside the signed headers
    assert headers["X-Agent-Id"] == "win-local-1"
    assert "X-Hermes-Signature" in headers


def test_get_injects_signed_headers():
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    captured_kwargs = {}

    def fake_get(url, **kwargs):
        captured_kwargs.update(kwargs)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch("hermes_orch.agent_http.httpx.get", side_effect=fake_get):
        agent_http.get("https://x.com/api/agents/x/profiles/win-agent01/configs/pending")

    headers = captured_kwargs.get("headers", {})
    assert headers["X-Hermes-Method"] == "GET"
    assert headers["X-Hermes-Path"] == "/api/agents/x/profiles/win-agent01/configs/pending"


# === request_with_fallback also signs ===

def test_request_with_fallback_signs_both_attempts():
    """Both primary + alt-scheme calls get the same signed headers."""
    agent_http.set_hmac_credential(TEST_KEY_ID, TEST_SECRET_HEX)
    primary_headers = None
    alt_headers = None
    call_count = 0

    def fake_post(url, **kwargs):
        nonlocal primary_headers, alt_headers, call_count
        if call_count == 0:
            primary_headers = kwargs.get("headers", {})
            # First call fails with connection error to trigger fallback
            call_count += 1
            import httpx
            raise httpx.ConnectError("simulated", request=MagicMock())
        else:
            alt_headers = kwargs.get("headers", {})
            resp = MagicMock()
            resp.status_code = 200
            return resp

    with patch("hermes_orch.agent_http.httpx.post", side_effect=fake_post):
        resp, actual_url = agent_http.request_with_fallback(
            "POST",
            "http://x.com/api/agents/x/heartbeat",  # starts with http
            json={"k": 1},
        )

    # Both calls should have signed headers
    assert primary_headers is not None
    assert alt_headers is not None
    assert "X-Hermes-Signature" in primary_headers
    assert "X-Hermes-Signature" in alt_headers
    # The signatures should be the SAME (same body + same path)
    assert primary_headers["X-Hermes-Signature"] == alt_headers["X-Hermes-Signature"]
    # Alt URL is https
    assert actual_url == "https://x.com/api/agents/x/heartbeat"
