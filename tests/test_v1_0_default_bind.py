# coding: utf-8
"""v1.0.1 (new-user-activation) regression guard.

T0.1: Fresh `init` writes `bind_host: "127.0.0.1"` in config (not 0.0.0.0)
T0.2: Regression test: default `init` config rejects LAN bind (bind_host is loopback)

Before v1.0.1, `init` baked `host: "0.0.0.0"` into config.yaml — meaning a
fresh install exposed the dashboard to the entire LAN by default. This
test ensures the default is loopback-only. A LAN-enabled install must
explicitly opt in via /settings#network (which requires a server restart).
"""
from __future__ import annotations

import os
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


def test_default_config_does_not_have_bind_host(monkeypatch):
    """The DEFAULT_CONFIG must NOT declare `bind_host`.

    This is intentional (see comment in config.py). The `bind_host` value
    is set inside `load_config()` instead, which lets the legacy `host:`
    migration block fire when a file has only the legacy key.
    """
    from hermes_orch.config import DEFAULT_CONFIG

    assert "orchestrator" in DEFAULT_CONFIG
    assert "bind_host" not in DEFAULT_CONFIG["orchestrator"], (
        "DEFAULT_CONFIG['orchestrator'] must NOT declare 'bind_host' — it is "
        "set by the migration block in load_config() so legacy `host:` is detected"
    )
    assert "host" not in DEFAULT_CONFIG["orchestrator"], (
        "DEFAULT_CONFIG should NOT have the legacy 'host' key either"
    )


def test_load_config_with_no_file_returns_loopback(monkeypatch, tmp_path):
    """When no config file exists, load_config returns the loopback default."""
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(tmp_path / "nonexistent.yaml"))
    cfg = load_config()
    assert cfg["orchestrator"]["bind_host"] == "127.0.0.1"


def test_load_config_with_explicit_bind_host(monkeypatch, tmp_path):
    """A config that sets bind_host explicitly is read verbatim."""
    cfg_path = _write_config_with_yaml(
        "orchestrator:\n  port: 9999\n  bind_host: 10.0.0.5\n  log_level: DEBUG\n"
    )
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg["orchestrator"]["bind_host"] == "10.0.0.5"
    assert cfg["orchestrator"]["port"] == 9999
    assert cfg["orchestrator"]["log_level"] == "DEBUG"


def test_cli_init_writes_bind_host_in_default_config(monkeypatch, tmp_path):
    """`hermes-orch init` must write `bind_host: 127.0.0.1` in the new config file.

    This guards the regression: v1.0's init baked `host: 0.0.0.0` into the
    file, exposing the dashboard to LAN. v1.0.1 must not.
    """
    from click.testing import CliRunner
    from hermes_orch.cli import init

    runner = CliRunner()
    result = runner.invoke(init, ["--config-dir", str(tmp_path)])
    assert result.exit_code == 0, f"init failed: {result.output}"

    config_file = tmp_path / "config.yaml"
    assert config_file.exists()
    text = config_file.read_text(encoding="utf-8")

    # bind_host: 127.0.0.1 must be present
    assert "bind_host:" in text, (
        f"New config must declare bind_host, got:\n{text}"
    )
    assert "bind_host: \"127.0.0.1\"" in text, (
        f"New config must default to bind_host: 127.0.0.1, got:\n{text}"
    )

    # Legacy host: 0.0.0.0 must NOT be present (it's the regression we're fixing)
    assert "host: \"0.0.0.0\"" not in text, (
        f"New config must not contain legacy 'host: 0.0.0.0', got:\n{text}"
    )

    # And the default port 8765 is still there
    assert "port: 8765" in text
