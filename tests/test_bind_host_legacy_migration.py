# coding: utf-8
"""v1.0.1 (new-user-activation) backward-compat test.

T0.4: Legacy config with `host:` key (no `bind_host`) is read via fallback,
      logged once as "Migrating legacy config key 'host' -> 'bind_host'",
      and rewritten on next save.

Without this fallback, an operator who had explicitly set `host: 0.0.0.0`
on a v1.0 install would be silently downgraded to loopback-only on
upgrade. v1.0.1 reads the legacy key, copies the value into the new
`bind_host` slot, and emits a one-time migration log to stderr.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

from hermes_orch.config import load_config


def _write_config_with_yaml(yaml_text: str) -> Path:
    """Write yaml_text to a temp file and return the path."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    Path(path).write_text(yaml_text, encoding="utf-8")
    return Path(path)


def test_legacy_host_lan_is_migrated(monkeypatch, capfd):
    """`host: 0.0.0.0` (legacy) -> `bind_host: 0.0.0.0` (new)."""
    cfg_path = _write_config_with_yaml(
        "orchestrator:\n  port: 8765\n  host: 0.0.0.0\n  log_level: INFO\n"
    )
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg["orchestrator"]["bind_host"] == "0.0.0.0", (
        "Legacy host: 0.0.0.0 should be migrated to bind_host: 0.0.0.0"
    )
    # The migration log must be emitted
    captured = capfd.readouterr()
    assert "Migrating legacy config key 'host' -> 'bind_host'" in captured.err, (
        f"Migration log should be emitted to stderr, got err={captured.err!r}"
    )
    assert "0.0.0.0" in captured.err


def test_legacy_host_loopback_is_migrated(monkeypatch):
    """`host: 127.0.0.1` (legacy) -> `bind_host: 127.0.0.1` (new)."""
    cfg_path = _write_config_with_yaml(
        "orchestrator:\n  port: 8765\n  host: 127.0.0.1\n  log_level: INFO\n"
    )
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg["orchestrator"]["bind_host"] == "127.0.0.1"


def test_both_bind_host_and_host_picks_bind_host(monkeypatch):
    """When both keys are present, bind_host wins (operator explicitly saved the new key)."""
    cfg_path = _write_config_with_yaml(
        "orchestrator:\n  port: 8765\n  host: 0.0.0.0\n  bind_host: 127.0.0.1\n  log_level: INFO\n"
    )
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg["orchestrator"]["bind_host"] == "127.0.0.1", (
        "bind_host (new key) must take precedence over host (legacy key)"
    )


def test_no_host_no_bind_host_uses_default(monkeypatch):
    """Neither key present -> falls through to DEFAULT_CONFIG (loopback)."""
    cfg_path = _write_config_with_yaml(
        "orchestrator:\n  port: 8765\n  log_level: INFO\n"
    )
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg["orchestrator"]["bind_host"] == "127.0.0.1"


def test_legacy_migration_does_not_mutate_file(monkeypatch):
    """load_config() must NOT rewrite the file. The legacy key is left in place
    until the next explicit config save."""
    cfg_path = _write_config_with_yaml(
        "orchestrator:\n  port: 8765\n  host: 0.0.0.0\n"
    )
    original_text = cfg_path.read_text(encoding="utf-8")
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))
    load_config()
    # File is unchanged
    assert cfg_path.read_text(encoding="utf-8") == original_text, (
        "load_config() must not auto-rewrite the file; rewrite happens on next save"
    )
