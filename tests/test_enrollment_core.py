# coding: utf-8
"""Tests for v1.0.1 core/enrollment.py (pure function tests).

Covers token generation, hashing, and the IssuedToken shape. The
DB-bound consume flow (transaction, atomic UPDATE, agent row
creation) is tested in test_enrollment_api.py.
"""
from __future__ import annotations

import hashlib
import string

import pytest

from hermes_orch.core.enrollment import (
    CONSUME_ALREADY_USED,
    CONSUME_EXPIRED,
    CONSUME_NOT_FOUND,
    CONSUME_OK,
    DEFAULT_TTL_MINUTES,
    ConsumeResult,
    IssuedToken,
    hash_token,
    issue_enrollment_token,
)


# ===== hash_token =====

def test_hash_token_returns_sha256_hex():
    """The hash is SHA-256 hex (64 chars, lowercase)."""
    h = hash_token("etok-abc123")
    assert len(h) == 64
    assert all(c in string.hexdigits.lower() for c in h)
    # And it matches what hashlib would produce
    assert h == hashlib.sha256(b"etok-abc123").hexdigest()


def test_hash_token_different_inputs_different_outputs():
    """Even a 1-char difference produces a totally different hash."""
    a = hash_token("etok-aaa")
    b = hash_token("etok-aab")
    assert a != b
    # SHA-256 avalanche: very different prefixes should produce
    # drastically different outputs (sanity check)
    assert a[0] != b[0] or a[-1] != b[-1]


def test_hash_token_same_input_same_output():
    """Deterministic — same plaintext always produces the same hash."""
    assert hash_token("etok-xyz") == hash_token("etok-xyz")


# ===== issue_enrollment_token =====

def test_issue_returns_issued_token():
    """issue returns an IssuedToken with all fields populated."""
    tok = issue_enrollment_token(label="Home laptop", requested_agent_name="win-01")
    assert isinstance(tok, IssuedToken)
    assert tok.id.startswith("etok-")
    assert tok.plaintext.startswith("etok-")
    assert tok.label == "Home laptop"
    assert tok.requested_agent_name == "win-01"
    assert tok.token_hash == hash_token(tok.plaintext)


def test_issue_empty_label_and_name_are_empty_strings():
    """Default label / requested_agent_name are empty strings, not None."""
    tok = issue_enrollment_token()
    assert tok.label == ""
    assert tok.requested_agent_name == ""


def test_issue_expires_at_is_utc_iso_in_future():
    """expires_at is ISO 8601 UTC and 15 minutes (default) in the future."""
    from datetime import datetime, timezone, timedelta
    tok = issue_enrollment_token()
    # Strip the timezone for naive comparison (datetime.fromisoformat
    # handles +00:00 fine on 3.11+)
    exp = datetime.fromisoformat(tok.expires_at)
    now = datetime.now(timezone.utc)
    delta = (exp - now).total_seconds()
    # Should be ~ DEFAULT_TTL_MINUTES * 60 seconds, +/- 5s tolerance
    expected = DEFAULT_TTL_MINUTES * 60
    assert abs(delta - expected) < 5, f"expected ~{expected}s, got {delta}s"


def test_issue_each_token_is_unique():
    """256 bits of entropy means consecutive issues never collide."""
    tokens = {issue_enrollment_token().plaintext for _ in range(100)}
    assert len(tokens) == 100  # all unique


def test_issue_ttl_minutes_honored():
    """Custom TTL is respected (5 minutes, 60 minutes, etc)."""
    from datetime import datetime, timezone
    for ttl in (5, 30, 60, 1):
        tok = issue_enrollment_token(ttl_minutes=ttl)
        exp = datetime.fromisoformat(tok.expires_at)
        now = datetime.now(timezone.utc)
        delta = (exp - now).total_seconds()
        expected = ttl * 60
        assert abs(delta - expected) < 5, f"ttl={ttl}: expected ~{expected}s, got {delta}s"


# ===== token plaintext format =====

def test_token_plaintext_uses_etok_prefix():
    """All enrollment tokens start with `etok-` (recognisable in logs)."""
    for _ in range(10):
        tok = issue_enrollment_token()
        assert tok.plaintext.startswith("etok-")
        assert tok.id.startswith("etok-")


def test_token_plaintext_has_at_least_32_bytes_of_entropy():
    """The base64 part is `secrets.token_urlsafe(32)` = 256 bits."""
    tok = issue_enrollment_token()
    # strip the "etok-" prefix, count the rest
    b64_part = tok.plaintext[5:]
    # 32 bytes -> ~43 chars of base64url
    assert len(b64_part) >= 43, f"only {len(b64_part)} chars, expected >= 43"


# ===== ConsumeResult sentinel values =====

def test_consume_result_outcome_constants():
    """The CONSUME_* constants are stable strings (used in API responses)."""
    assert CONSUME_OK == "ok"
    assert CONSUME_NOT_FOUND == "not_found"
    assert CONSUME_EXPIRED == "expired"
    assert CONSUME_ALREADY_USED == "already_used"


def test_consume_result_defaults():
    """A fresh ConsumeResult has empty agent_id/agent_name/hmac_secret."""
    r = ConsumeResult(outcome=CONSUME_OK)
    assert r.agent_id == ""
    assert r.agent_name == ""
    assert r.hmac_secret == ""
    assert r.requested_name_used is False


def test_consume_result_is_frozen():
    """ConsumeResult is a frozen dataclass (immutable result)."""
    r = ConsumeResult(outcome=CONSUME_OK, agent_id="agent-1")
    with pytest.raises((AttributeError, Exception)):
        r.outcome = "tampered"  # type: ignore[misc]
