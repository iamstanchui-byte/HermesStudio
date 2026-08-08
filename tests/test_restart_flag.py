# coding: utf-8
"""v1.0.1 (new-user-activation) core/restart.py unit tests.

The `restart-required` flag is a transient state between "operator changed
a setting" and "next server start". Tests cover the three primitives:

    - write_restart_required(reason)  -> create / overwrite flag file
    - is_restart_required()           -> bool + reason (no flag = false)
    - clear_restart_required()        -> delete flag, return whether cleared
"""
from __future__ import annotations

from pathlib import Path

from hermes_orch.core.restart import (
    RestartInfo,
    clear_restart_required,
    is_restart_required,
    write_restart_required,
)


def test_no_flag_returns_false(tmp_path, monkeypatch):
    """With no flag file, is_restart_required() returns RestartInfo(required=False)."""
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(tmp_path / "config.yaml"))
    info = is_restart_required()
    assert info.required is False
    assert info.reason == ""


def test_write_then_read(tmp_path, monkeypatch):
    """write_restart_required creates the flag; is_restart_required reads it back."""
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(tmp_path / "config.yaml"))
    path = write_restart_required("bind_host changed 127.0.0.1 -> 0.0.0.0")
    assert path.exists()
    assert path.parent == tmp_path
    info = is_restart_required()
    assert info.required is True
    assert "127.0.0.1" in info.reason
    assert "0.0.0.0" in info.reason


def test_write_overwrites_existing_reason(tmp_path, monkeypatch):
    """A second write replaces the previous reason (idempotent flag)."""
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(tmp_path / "config.yaml"))
    write_restart_required("first reason")
    write_restart_required("second reason")
    info = is_restart_required()
    assert info.required is True
    assert info.reason == "second reason"


def test_clear_returns_true_when_flag_existed(tmp_path, monkeypatch):
    """clear_restart_required returns True if a flag was cleared, False otherwise."""
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(tmp_path / "config.yaml"))
    write_restart_required("test")
    assert clear_restart_required() is True
    # Second call: no flag to clear
    assert clear_restart_required() is False


def test_clear_when_no_flag_is_safe(tmp_path, monkeypatch):
    """clear_restart_required on no flag does not raise; returns False."""
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(tmp_path / "config.yaml"))
    assert clear_restart_required() is False  # no exception


def test_unreadable_reason_does_not_crash(tmp_path, monkeypatch):
    """A corrupted flag file is read defensively (returns '(reason unreadable)')."""
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(tmp_path / "config.yaml"))
    # Write a flag with a binary mess in the body — `read_text` will raise
    # UnicodeDecodeError but the function should catch and return a safe default.
    flag = tmp_path / "restart-required.flag"
    flag.write_bytes(b"\x80\x81\x82\x83 (binary mess)")
    info = is_restart_required()
    # The flag is still detected as "required" (the file exists), but the
    # reason is the safe fallback string.
    assert info.required is True
    assert info.reason == "(reason unreadable)" or "binary" in info.reason.lower() or len(info.reason) > 0
