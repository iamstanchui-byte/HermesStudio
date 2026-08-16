"""Tests for the wrapper's URL discovery + atomic config rewrite
(2026-08-16 wrapper self-heal).

The wrapper periodically calls `/api/server/info` to learn the
canonical orchestrator URL. If the canonical URL differs from
what's in `wrapper-config.json`, the wrapper atomically rewrites
the JSON file so the NEXT restart uses the new URL. The current
session continues with the working URL (the in-memory variable
is not changed -- the user explicitly wanted a no-mid-flight-switch
design).

Helper under test: `hermes_orch.agent_cli._discover_and_persist_url`
- pure: takes (cfg_path, current_url) -> str | None
- side effect: rewrites cfg_path atomically if URL changed
- returns the canonical URL on success, None on failure
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# Allow tests to import from src
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def _reset_discovery_throttle():
    """Reset the module-level discovery throttle between tests so
    the 60s rate-limit doesn't bleed across cases (the wrapper sets
    it to monotonic time when a check completes; without a reset,
    the second test in the same process would be throttled)."""
    from hermes_orch import agent_cli
    agent_cli._last_discovery_at = 0.0
    yield
    agent_cli._last_discovery_at = 0.0


def _write_cfg(path: Path, orchestrator_url: str) -> None:
    """Write a minimal wrapper-config.json."""
    path.write_text(
        json.dumps({
            "agent_id": "test-agent",
            "orchestrator_url": orchestrator_url,
            "secret_file": str(path.parent / "secret"),
            "profiles": {"p": {"root": "/tmp/p"}},
        }),
        encoding="utf-8",
    )


def _read_cfg(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# 1) no change -> no rewrite
# ----------------------------------------------------------------------

def test_discover_noop_when_url_matches(tmp_path, monkeypatch):
    """If server-info returns the same URL as configured, the
    config file is NOT rewritten."""
    from hermes_orch import agent_cli
    cfg = tmp_path / "wrapper-config.json"
    _write_cfg(cfg, "https://hermes-win:8765")

    info_body = b'{"scheme":"https","public_origin":"https://hermes-win:8765","cert_fingerprint_sha256":""}'
    info_resp = mock.Mock(status_code=200, content=info_body)
    with mock.patch.object(agent_cli, "request_with_fallback", return_value=(info_resp, "https://hermes-win:8765/api/server/info")):
        out = agent_cli._discover_and_persist_url(cfg, "https://hermes-win:8765")

    assert out == "https://hermes-win:8765"
    # Config not rewritten
    reloaded = _read_cfg(cfg)
    assert reloaded["orchestrator_url"] == "https://hermes-win:8765"


# ----------------------------------------------------------------------
# 2) scheme changed -> atomic rewrite
# ----------------------------------------------------------------------

def test_discover_rewrites_when_url_changed(tmp_path, monkeypatch):
    """The actual production scenario: server flipped to HTTPS, the
    wrapper still has the old HTTP URL. The helper must atomically
    rewrite wrapper-config.json with the new URL."""
    from hermes_orch import agent_cli
    cfg = tmp_path / "wrapper-config.json"
    _write_cfg(cfg, "http://hermes-win:8765")

    # server-info says https now
    info_body = b'{"scheme":"https","public_origin":"https://hermes-win:8765","cert_fingerprint_sha256":""}'
    info_resp = mock.Mock(status_code=200, content=info_body)
    with mock.patch.object(agent_cli, "request_with_fallback", return_value=(info_resp, "https://hermes-win:8765/api/server/info")):
        out = agent_cli._discover_and_persist_url(cfg, "http://hermes-win:8765")

    assert out == "https://hermes-win:8765"
    reloaded = _read_cfg(cfg)
    assert reloaded["orchestrator_url"] == "https://hermes-win:8765"
    # other fields preserved
    assert reloaded["agent_id"] == "test-agent"
    assert reloaded["secret_file"] == str(tmp_path / "secret")
    assert reloaded["profiles"] == {"p": {"root": "/tmp/p"}}


def test_discover_atomic_write_uses_temp_file_then_rename(tmp_path, monkeypatch):
    """The rewrite must be atomic: write to .tmp + rename, so a
    crash mid-write doesn't leave the wrapper-config.json half-written."""
    from hermes_orch import agent_cli
    cfg = tmp_path / "wrapper-config.json"
    _write_cfg(cfg, "http://hermes-win:8765")
    info_body = b'{"scheme":"https","public_origin":"https://hermes-win:8765","cert_fingerprint_sha256":""}'
    info_resp = mock.Mock(status_code=200, content=info_body)

    # Spy on Path.replace / os.replace
    with mock.patch.object(agent_cli, "request_with_fallback", return_value=(info_resp, "https://hermes-win:8765/api/server/info")):
        with mock.patch("pathlib.Path.replace") as mreplace:
            out = agent_cli._discover_and_persist_url(cfg, "http://hermes-win:8765")

    # Replace was used (atomic rename) -- not a separate write+delete
    assert mreplace.called
    assert out == "https://hermes-win:8765"


# ----------------------------------------------------------------------
# 3) network failure -> no rewrite, return None
# ----------------------------------------------------------------------

def test_discover_network_failure_returns_none_and_does_not_rewrite(tmp_path, monkeypatch):
    """If the server-info call fails (network), the helper must NOT
    corrupt the config. Just return None and let the wrapper keep
    using whatever URL it has."""
    from hermes_orch import agent_cli
    cfg = tmp_path / "wrapper-config.json"
    _write_cfg(cfg, "http://hermes-win:8765")

    import httpx
    with mock.patch.object(agent_cli, "request_with_fallback", side_effect=httpx.ConnectError("refused")):
        out = agent_cli._discover_and_persist_url(cfg, "http://hermes-win:8765")

    assert out is None
    reloaded = _read_cfg(cfg)
    # URL unchanged
    assert reloaded["orchestrator_url"] == "http://hermes-win:8765"


def test_discover_non_200_status_returns_none(tmp_path, monkeypatch):
    """server-info returned a non-2xx -- probably the wrong path or
    the server is misconfigured. Don't touch the config."""
    from hermes_orch import agent_cli
    cfg = tmp_path / "wrapper-config.json"
    _write_cfg(cfg, "http://hermes-win:8765")
    bad = mock.Mock(status_code=500, content=b"oops")
    with mock.patch.object(agent_cli, "request_with_fallback", return_value=(bad, "http://hermes-win:8765/api/server/info")):
        out = agent_cli._discover_and_persist_url(cfg, "http://hermes-win:8765")
    assert out is None


def test_discover_malformed_json_returns_none(tmp_path, monkeypatch):
    """server-info returned non-JSON. Defensive: don't crash, don't
    rewrite. Just return None and log."""
    from hermes_orch import agent_cli
    cfg = tmp_path / "wrapper-config.json"
    _write_cfg(cfg, "http://hermes-win:8765")
    bad = mock.Mock(status_code=200, content=b"<html>oops</html>")
    with mock.patch.object(agent_cli, "request_with_fallback", return_value=(bad, "http://hermes-win:8765/api/server/info")):
        out = agent_cli._discover_and_persist_url(cfg, "http://hermes-win:8765")
    assert out is None
    # Config unchanged
    reloaded = _read_cfg(cfg)
    assert reloaded["orchestrator_url"] == "http://hermes-win:8765"


def test_discover_missing_public_origin_key_returns_none(tmp_path, monkeypatch):
    """The response shape is documented; if the server omits
    public_origin, we have nothing to compare against. Return None."""
    from hermes_orch import agent_cli
    cfg = tmp_path / "wrapper-config.json"
    _write_cfg(cfg, "http://hermes-win:8765")
    no_origin = b'{"scheme":"http","cert_fingerprint_sha256":""}'
    resp = mock.Mock(status_code=200, content=no_origin)
    with mock.patch.object(agent_cli, "request_with_fallback", return_value=(resp, "http://hermes-win:8765/api/server/info")):
        out = agent_cli._discover_and_persist_url(cfg, "http://hermes-win:8765")
    assert out is None
    reloaded = _read_cfg(cfg)
    assert reloaded["orchestrator_url"] == "http://hermes-win:8765"
