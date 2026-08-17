# coding: utf-8
"""Tests for `agent_http.reload_verify()` (Fix #4, 2026-08-17).

The wrapper's TLS verify policy is computed once at import time and
cached in the `agent_http._VERIFY` module global. The `reload_verify()`
function re-runs the env-var resolution and updates the cache, so
operators can change `INSECURE_SKIP_TLS_VERIFY` or
`ORCHESTRATOR_CA_BUNDLE` at runtime and `kill -HUP <pid>` (on Unix)
or restart (Windows) to pick up the new value without a wrapper
restart that would lose the in-flight tick state.

These tests verify the re-read behavior with `monkeypatch` (pytest
best practice for env-var mutation; do NOT use `os.environ` directly
in tests because pytest can't clean up cross-test mutations).
"""
from __future__ import annotations

import pytest

import hermes_orch.agent_http as agent_http


def test_reload_verify_updates_cache_when_env_changes(monkeypatch):
    """Setting INSECURE_SKIP_TLS_VERIFY=1 then calling reload_verify()
    must update the cached _VERIFY from True (or False) to False.
    """
    # Start: ensure default (no env var set)
    monkeypatch.delenv("INSECURE_SKIP_TLS_VERIFY", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_CA_BUNDLE", raising=False)
    _ = agent_http.reload_verify()
    before = agent_http.get_verify()
    assert before is True, f"default verify should be True, got {before!r}"

    # Change env: skip TLS verify
    monkeypatch.setenv("INSECURE_SKIP_TLS_VERIFY", "1")
    old, new = agent_http.reload_verify()
    assert old is True
    assert new is False, f"after INSECURE_SKIP_TLS_VERIFY=1, verify should be False, got {new!r}"
    assert agent_http.get_verify() is False


def test_reload_verify_with_truthy_variants(monkeypatch):
    """`INSECURE_SKIP_TLS_VERIFY` accepts 1/true/yes/on (case-insensitive)."""
    for truthy in ("1", "true", "yes", "on", "TRUE", "True"):
        monkeypatch.setenv("INSECURE_SKIP_TLS_VERIFY", truthy)
        _, new = agent_http.reload_verify()
        assert new is False, f"INSECURE_SKIP_TLS_VERIFY={truthy!r} should yield False, got {new!r}"


def test_reload_verify_with_falsy_variants(monkeypatch):
    """`INSECURE_SKIP_TLS_VERIFY` accepts 0/false/no/off/garbage as falsy."""
    for falsy in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("INSECURE_SKIP_TLS_VERIFY", falsy)
        _, new = agent_http.reload_verify()
        # Anything that's not in the truthy set is treated as "verify ON"
        assert new is True, (
            f"INSECURE_SKIP_TLS_VERIFY={falsy!r} should yield True (verify), got {new!r}"
        )


def test_reload_verify_with_ca_bundle(monkeypatch, tmp_path):
    """`ORCHESTRATOR_CA_BUNDLE` pointing to a readable file becomes the verify value."""
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\n(placeholder)\n-----END CERTIFICATE-----\n")

    monkeypatch.delenv("INSECURE_SKIP_TLS_VERIFY", raising=False)
    monkeypatch.setenv("ORCHESTRATOR_CA_BUNDLE", str(ca_file))
    _, new = agent_http.reload_verify()
    assert new == str(ca_file), f"ORCHESTRATOR_CA_BUNDLE should be the file path, got {new!r}"


def test_reload_verify_with_missing_ca_bundle(monkeypatch):
    """`ORCHESTRATOR_CA_BUNDLE` pointing to a NON-readable file falls
    through to default (does NOT crash, prints a warning to stderr).
    """
    monkeypatch.delenv("INSECURE_SKIP_TLS_VERIFY", raising=False)
    # Path that definitely doesn't exist
    monkeypatch.setenv("ORCHESTRATOR_CA_BUNDLE", "/nonexistent/ca-bundle.pem")
    # Should not raise
    _, new = agent_http.reload_verify()
    # Falls through to default (no env vars set => True)
    assert new is True
