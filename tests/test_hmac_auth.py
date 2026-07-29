"""Unit tests for the HMAC auth helpers (v1.6, 2026-07-29).

Tests:
  - string_to_sign canonical form
  - compute_signature is deterministic
  - verify_signature positive / negative cases
  - Constant-time behavior (we just check the helper works;
    timing-attack resistance is a property of hmac.compare_digest)
  - Default timestamp window + env override
  - compute_signature handles empty body correctly
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow tests to import from src
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from hermes_orch.auth.hmac import (
    DEFAULT_HMAC_WINDOW_SEC,
    compute_signature,
    hmac_required,
    string_to_sign,
    verify_signature,
)


SECRET = "test-secret-12345"


# ===== string_to_sign =====


def test_string_to_sign_canonical_form():
    """The canonical string-to-sign uses uppercase method, full path,
    SHA256-hex of body, and the timestamp. Components joined by \\n."""
    s = string_to_sign("POST", "/api/agents/foo/heartbeat", b"{}", "1700000000")
    assert s.startswith("POST\n")
    assert "/api/agents/foo/heartbeat" in s
    # body SHA256 of b"{}" is 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
    assert "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a" in s
    assert s.endswith("\n1700000000")
    # 4 lines: METHOD, PATH, body_hash, timestamp
    assert s.count("\n") == 3


def test_string_to_sign_uppercases_method():
    s1 = string_to_sign("post", "/a", b"", "1")
    s2 = string_to_sign("POST", "/a", b"", "1")
    assert s1 == s2


def test_string_to_sign_empty_body_uses_empty_sha256():
    """The SHA256 of empty bytes is the well-known constant."""
    s = string_to_sign("GET", "/foo", b"", "1")
    # SHA256 of b"" = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
    assert (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" in s
    )


def test_string_to_sign_query_string_in_path():
    s = string_to_sign("GET", "/api/projects/p1/session?role=foo", b"", "1")
    assert "/api/projects/p1/session?role=foo" in s


# ===== compute_signature =====


def test_compute_signature_deterministic():
    s1 = compute_signature(SECRET, "POST", "/a", b"x", "100")
    s2 = compute_signature(SECRET, "POST", "/a", b"x", "100")
    assert s1 == s2


def test_compute_signature_format():
    """SHA256 hex is 64 lowercase hex chars."""
    s = compute_signature(SECRET, "GET", "/", b"", "1")
    assert len(s) == 64
    assert all(c in "0123456789abcdef" for c in s)


def test_compute_signature_different_secret_different_result():
    a = compute_signature(SECRET, "GET", "/a", b"", "1")
    b = compute_signature("other-secret", "GET", "/a", b"", "1")
    assert a != b


# ===== verify_signature =====


def test_verify_signature_positive():
    sig = compute_signature(SECRET, "POST", "/a", b"x", "100")
    assert verify_signature(SECRET, "POST", "/a", b"x", "100", sig) is True


def test_verify_signature_tampered_body_rejected():
    sig = compute_signature(SECRET, "POST", "/a", b"x", "100")
    assert verify_signature(SECRET, "POST", "/a", b"y", "100", sig) is False


def test_verify_signature_tampered_path_rejected():
    sig = compute_signature(SECRET, "POST", "/a", b"x", "100")
    assert verify_signature(SECRET, "POST", "/b", b"x", "100", sig) is False


def test_verify_signature_tampered_method_rejected():
    sig = compute_signature(SECRET, "POST", "/a", b"x", "100")
    assert verify_signature(SECRET, "GET", "/a", b"x", "100", sig) is False


def test_verify_signature_tampered_timestamp_rejected():
    sig = compute_signature(SECRET, "POST", "/a", b"x", "100")
    assert verify_signature(SECRET, "POST", "/a", b"x", "200", sig) is False


def test_verify_signature_wrong_secret_rejected():
    sig = compute_signature(SECRET, "POST", "/a", b"x", "100")
    assert verify_signature("evil", "POST", "/a", b"x", "100", sig) is False


def test_verify_signature_empty_provided_rejected():
    """Empty signature must be rejected, not match against an
    empty signature computed by chance."""
    assert verify_signature(SECRET, "POST", "/a", b"x", "100", "") is False


def test_verify_signature_none_safe():
    """Providing None should not crash; treated as empty."""
    assert verify_signature(SECRET, "POST", "/a", b"x", "100", None) is False


# ===== Timestamp window (env override) =====


def test_default_window_constant():
    assert DEFAULT_HMAC_WINDOW_SEC == 300


def test_read_window_sec_default(monkeypatch):
    monkeypatch.delenv("HERMES_HMAC_WINDOW_SEC", raising=False)
    from hermes_orch.auth.hmac import _read_window_sec
    assert _read_window_sec() == 300


def test_read_window_sec_override(monkeypatch):
    monkeypatch.setenv("HERMES_HMAC_WINDOW_SEC", "120")
    from hermes_orch.auth.hmac import _read_window_sec
    assert _read_window_sec() == 120


def test_read_window_sec_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_HMAC_WINDOW_SEC", "not-a-number")
    from hermes_orch.auth.hmac import _read_window_sec
    assert _read_window_sec() == 300


def test_read_window_sec_zero_falls_back(monkeypatch):
    """Zero or negative values are not allowed (would reject all
    requests). Fall back to the default."""
    monkeypatch.setenv("HERMES_HMAC_WINDOW_SEC", "0")
    from hermes_orch.auth.hmac import _read_window_sec
    assert _read_window_sec() == 300


# ===== hmac_required (env) =====


def test_hmac_required_default_off(monkeypatch):
    monkeypatch.delenv("HERMES_HMAC_REQUIRED", raising=False)
    assert hmac_required() is False


def test_hmac_required_on(monkeypatch):
    for v in ("1", "true", "yes", "on", "TRUE", "On"):
        monkeypatch.setenv("HERMES_HMAC_REQUIRED", v)
        assert hmac_required() is True, f"failed for value {v!r}"


def test_hmac_required_off_other(monkeypatch):
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("HERMES_HMAC_REQUIRED", v)
        assert hmac_required() is False, f"failed for value {v!r}"


# ===== End-to-end (server-side) =====


def test_endpoint_rejects_missing_headers():
    """The /api/agents/{id}/secret endpoint itself doesn't require
    HMAC (it's the bootstrap), but other endpoints do. Just smoke
    test that a wrapper endpoint without any headers 401s.

    Requires a running server on port 8765. Skipped if not available.
    """
    import socket
    import urllib.error
    import urllib.request
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=1):
            pass
    except OSError:
        pytest.skip("server not running on 8765")

    req = urllib.request.Request(
        "http://127.0.0.1:8765/api/agents/none/heartbeat",
        method="POST",
        data=b'{"status":"idle"}',
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 401, f"expected 401, got {status}"
