"""Regression: agent_cli reads wrapper-config.json correctly when PowerShell's
Set-Content wrote it with a UTF-8 BOM.

Before this fix, a config file saved by PowerShell's `Set-Content` (which
writes UTF-8 with BOM by default) would crash the wrapper with:
  json.decoder.JSONDecodeError: Unexpected UTF-8 BOM
  (decode using utf-8-sig): line 1 column 1 (char 0)

The wrapper on windows would then fail to start, agent goes stale.

Fix: use `encoding="utf-8-sig"` in all wrapper-side read_text calls
(handles both BOM and no-BOM transparently). Verified on agent_cli.py.
"""
import json
from pathlib import Path

import pytest


@pytest.fixture
def config_with_bom(tmp_path):
    """Write a config file with UTF-8 BOM (mimics PowerShell Set-Content)."""
    p = tmp_path / "wrapper-config.json"
    text = json.dumps({
        "agent_id": "test-agent",
        "orchestrator_url": "https://hermes-win:8765",
        "secret_file": str(tmp_path / ".secret-test"),
        "profiles": {"test": {"root": "<profiles_dir>/test"}},
    })
    # Write WITH BOM (EF BB BF)
    p.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    return p


def test_json_loads_with_bom(config_with_bom):
    """json.loads itself handles BOM if you read with utf-8-sig."""
    raw = config_with_bom.read_text(encoding="utf-8-sig")
    cfg = json.loads(raw)
    assert cfg["agent_id"] == "test-agent"
    assert cfg["orchestrator_url"] == "https://hermes-win:8765"


def test_json_loads_without_bom(tmp_path):
    """utf-8-sig should also work without BOM (the common case for
    hand-written config files)."""
    p = tmp_path / "wrapper-config.json"
    p.write_text(json.dumps({"agent_id": "test", "orchestrator_url": "https://x"}), encoding="utf-8")
    raw = p.read_text(encoding="utf-8-sig")
    cfg = json.loads(raw)
    assert cfg["agent_id"] == "test"


def test_old_utf8_fails_on_bom(config_with_bom):
    """The OLD behavior (encoding='utf-8') would crash. This test
    documents the regression so we know what the fix was for."""
    with pytest.raises(json.JSONDecodeError) as exc:
        json.loads(config_with_bom.read_text(encoding="utf-8"))
    assert "BOM" in str(exc.value) or "utf-8-sig" in str(exc.value)


def test_agent_cli_uses_utf8_sig_for_config_reads():
    """Grep guard: no `read_text(encoding=\"utf-8\")` should remain in
    agent_cli.py for the config-file read sites (only the lenient
    `errors=\"replace\"` text-content reads are allowed). If you add
    a new config-file read, use `encoding=\"utf-8-sig\"`."""
    p = Path(__file__).resolve().parent.parent / "src" / "hermes_orch" / "agent_cli.py"
    text = p.read_text(encoding="utf-8")
    # Allowed: read_text(encoding="utf-8", errors="replace") for plain text
    # Forbidden: read_text(encoding="utf-8") without errors (JSON / config reads)
    import re
    bad = re.findall(r'read_text\(encoding="utf-8"\)', text)
    assert not bad, (
        f"agent_cli.py has {len(bad)} read_text(encoding='utf-8') without 'errors' — "
        f"PowerShell's Set-Content writes UTF-8 BOM, which crashes the JSON parser. "
        f"Use encoding='utf-8-sig' instead."
    )
