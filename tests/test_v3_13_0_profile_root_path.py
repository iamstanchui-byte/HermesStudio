"""v3.13.0 Agent Profile Root Path Resolution — unit + smoke tests.

Covers:
  - API: 5 cases (create with/without/empty root_path; patch set/clear)
  - Wrapper helper `_merge_orch_profiles_into_config`: 4 cases
    (root_path priority, empty fall-back, new entry, respects manual)

The API tests require the server running on http://127.0.0.1:8765
(same pattern as test_profile_label.py). The wrapper helper tests
are pure-function, no server needed.

See docs/v3.13.0-agent-profile-root-path.md (v4) for the full spec.
"""
from __future__ import annotations

import importlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BASE = "http://127.0.0.1:8765"

# Path setup so the helper can be imported directly. The helper lives
# inside `agent_cli.py` (a Click module), so we need to be careful
# not to actually invoke the CLI — just import the function.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


# ---------------------------------------------------------------------------
# Wrapper helper: pure-function tests (no server needed)
# ---------------------------------------------------------------------------


def _import_helper():
    """Import `_merge_orch_profiles_into_config` from agent_cli.

    The module is a Click group, so we just `importlib.import_module`
    and grab the function by name. No CLI invocation."""
    import hermes_orch.agent_cli as _mod  # noqa: F401
    return _mod._merge_orch_profiles_into_config


def test_helper_root_path_with_no_existing_entry():
    """AC-5: New role + root_path set → use root_path as 'root'."""
    helper = _import_helper()
    # Helper expects the whole wrapper-config.json (which has a
    # 'profiles' key). For test purposes we pass a minimal cfg.
    cfg: dict = {"profiles": {}}
    orch = [{"name": "win-agent02", "root_path": r"D:\tools\hermes\profiles\win-agent02"}]
    detected = None
    merged_profiles, added = helper(cfg, orch, detected)
    assert added == ["win-agent02"]
    # Helper returns the profiles subdict (per spec).
    assert merged_profiles["win-agent02"] == {"root": r"D:\tools\hermes\profiles\win-agent02"}


def test_helper_root_path_empty_falls_back_to_auto_derive():
    """AC-2 / AC-9: New role + no root_path → auto-derive if detected dir exists."""
    helper = _import_helper()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        detected = Path(td)
        # Create the role dir
        (detected / "win-agent02").mkdir()
        cfg: dict = {"profiles": {}}
        orch = [{"name": "win-agent02", "root_path": None}]
        merged_profiles, added = helper(cfg, orch, detected)
        assert added == ["win-agent02"]
        # Auto-derive writes the absolute path (not the template)
        # so the wrapper doesn't need HERMES_PROFILES_DIR at runtime
        assert merged_profiles["win-agent02"]["root"] == str(detected / "win-agent02")


def test_helper_respects_manual_cfg_override_when_root_path_empty():
    """AC-6: Existing cfg entry (any source) + orch reports no root_path
    → keep existing entry unchanged. v3.13.0 conservative: ALL existing
    entries treated as manual override."""
    helper = _import_helper()
    cfg: dict = {
        "profiles": {
            "win-agent02": {"root": r"C:\Users\stanley\AppData\Local\hermes\profiles\win-agent02"}
        }
    }
    orch = [{"name": "win-agent02", "root_path": None}]
    merged_profiles, added = helper(cfg, orch, None)
    assert added == []
    # CRITICAL: existing entry preserved, not overwritten
    assert merged_profiles["win-agent02"] == {
        "root": r"C:\Users\stanley\AppData\Local\hermes\profiles\win-agent02"
    }


def test_helper_root_path_does_not_overwrite_existing_entry():
    """AC-6 + v3.13.0 conservative: even if orch reports a root_path, we
    do NOT overwrite the existing entry. This is the key change vs
    v2's naive code which had `merged[role] = {"root": root_path}`
    before the existing-check. Helper is idempotent — re-runs
    produce the same output."""
    helper = _import_helper()
    cfg: dict = {
        "profiles": {
            "win-agent02": {"root": r"C:\custom\path\win-agent02"}
        }
    }
    orch = [{"name": "win-agent02", "root_path": r"D:\tools\hermes\profiles\win-agent02"}]
    merged_profiles, added = helper(cfg, orch, None)
    # Conservative: existing entry is preserved, NOT overwritten
    assert added == []
    assert merged_profiles["win-agent02"] == {"root": r"C:\custom\path\win-agent02"}


def test_helper_idempotent():
    """v3.13.0 idempotency guarantee: re-running on the same input
    produces the same output (no surprise modifications)."""
    helper = _import_helper()
    # Initial cfg (whole wrapper-config shape, with 'profiles' key).
    cfg: dict = {
        "profiles": {
            "win-agent01": {"root": r"C:\Users\stanley\AppData\Local\hermes\profiles\win-agent01"},
            "win-agent02": {"root": r"C:\Users\stanley\AppData\Local\hermes\profiles\win-agent02"},
        }
    }
    orch = [
        {"name": "win-agent01", "root_path": None},
        {"name": "win-agent02", "root_path": r"D:\new\win-agent02"},  # would-be override
    ]
    out1_profiles, _ = helper(cfg, orch, None)
    # After first call, mimic what callers do: write profiles back to cfg.
    cfg["profiles"] = out1_profiles
    # Second call (passes the full cfg again, with the first call's output)
    out2_profiles, _ = helper(cfg, orch, None)
    cfg["profiles"] = out2_profiles
    out3_profiles, _ = helper(cfg, orch, None)
    # All three runs produce the same profiles dict (idempotent)
    assert out1_profiles == out2_profiles == out3_profiles
    # And the would-be override is NOT applied
    assert out1_profiles["win-agent02"]["root"] == r"C:\Users\stanley\AppData\Local\hermes\profiles\win-agent02"


# ---------------------------------------------------------------------------
# API tests: require server on http://127.0.0.1:8765
# ---------------------------------------------------------------------------


def _admin_cookie() -> str:
    """Mint a session cookie for the local admin user.

    Same approach as the manual _dbg_*.py scripts in
    ~/.hermes-orchestrator: read the session_secret on disk, then
    use the project's cookie helper to sign a session for the
    known admin user. Returns the cookie VALUE (the part after
    the `=` sign)."""
    from hermes_orch.cli import _default_db_path
    from hermes_orch.auth.cookie import make_session_cookie_value
    config_dir = _default_db_path().parent
    secret_path = config_dir / "session_secret"
    if not secret_path.exists():
        pytest.skip("session_secret not found; run server at least once")
    # Secret is unused by the signer (it's in the cookie body itself)
    _ = secret_path.read_text(encoding="utf-8").strip()
    return make_session_cookie_value("usr-c25e2ef2")


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Cookie": f"hermes_orch_session={_admin_cookie()}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=data, method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw or b"null")
            except json.JSONDecodeError:
                return r.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body


def _find_test_agent() -> str:
    """Find an existing agent to use for the test. Pick the first one."""
    status, data = _http("GET", "/api/agents")
    assert status == 200, f"GET /api/agents failed: {status} body={data}"
    # /api/agents returns {"agents": [...]}
    if isinstance(data, dict):
        agents = data.get("agents", [])
    else:
        agents = data if isinstance(data, list) else []
    if not agents:
        pytest.skip("No agents registered; cannot run API tests")
    return agents[0]["id"]


def _cleanup_profile(agent_id: str, name: str):
    """Best-effort delete after test (ignore 404)."""
    _http("DELETE", f"/api/agents/{agent_id}/profiles/{name}")


def test_add_profile_with_root_path():
    """AC-1: POST with root_path → DB row has non-NULL root_path."""
    agent_id = _find_test_agent()
    profile_name = "_test_v3130_rootpath_a"
    try:
        status, data = _http("POST", f"/api/agents/{agent_id}/profiles", {
            "name": profile_name,
            "description": "v3.13.0 test: with root_path",
            "root_path": r"D:\tools\hermes\profiles\_test",
        })
        assert status == 201, f"POST failed: {status} body={data}"
        assert isinstance(data, dict)
        assert data.get("root_path") == r"D:\tools\hermes\profiles\_test", (
            f"root_path not returned correctly: {data}"
        )
    finally:
        _cleanup_profile(agent_id, profile_name)


def test_add_profile_without_root_path():
    """AC-2: POST without root_path → DB row has NULL root_path (default)."""
    agent_id = _find_test_agent()
    profile_name = "_test_v3130_rootpath_b"
    try:
        status, data = _http("POST", f"/api/agents/{agent_id}/profiles", {
            "name": profile_name,
            "description": "v3.13.0 test: without root_path",
        })
        assert status == 201, f"POST failed: {status} body={data}"
        assert isinstance(data, dict)
        assert data.get("root_path") is None, (
            f"root_path should be None for no-field case: {data}"
        )
    finally:
        _cleanup_profile(agent_id, profile_name)


def test_add_profile_empty_root_path_treated_as_null():
    """AC-2 (edge case): POST with root_path='' → DB NULL (auto-derive)."""
    agent_id = _find_test_agent()
    profile_name = "_test_v3130_rootpath_c"
    try:
        status, data = _http("POST", f"/api/agents/{agent_id}/profiles", {
            "name": profile_name,
            "description": "v3.13.0 test: empty root_path",
            "root_path": "",
        })
        assert status == 201, f"POST failed: {status} body={data}"
        assert isinstance(data, dict)
        assert data.get("root_path") is None, (
            f"root_path='' should normalize to None: {data}"
        )
    finally:
        _cleanup_profile(agent_id, profile_name)


def test_patch_profile_set_root_path():
    """AC-3: PATCH to set root_path → DB updates."""
    agent_id = _find_test_agent()
    profile_name = "_test_v3130_rootpath_d"
    try:
        # First create
        status, _ = _http("POST", f"/api/agents/{agent_id}/profiles", {
            "name": profile_name,
            "description": "v3.13.0 test: patch set",
        })
        assert status == 201
        # Then PATCH to set root_path
        status, data = _http("PATCH", f"/api/agents/{agent_id}/profiles/{profile_name}", {
            "root_path": r"C:\patched\path\win-agent02"
        })
        assert status == 200, f"PATCH failed: {status} body={data}"
        assert isinstance(data, dict)
        assert data.get("root_path") == r"C:\patched\path\win-agent02"
    finally:
        _cleanup_profile(agent_id, profile_name)


def test_patch_profile_clear_root_path():
    """AC-4: PATCH with root_path=null → DB NULL (back to auto-derive)."""
    agent_id = _find_test_agent()
    profile_name = "_test_v3130_rootpath_e"
    try:
        # First create with root_path
        status, _ = _http("POST", f"/api/agents/{agent_id}/profiles", {
            "name": profile_name,
            "description": "v3.13.0 test: patch clear",
            "root_path": r"D:\will\be\cleared",
        })
        assert status == 201
        # Then PATCH to clear
        status, data = _http("PATCH", f"/api/agents/{agent_id}/profiles/{profile_name}", {
            "root_path": None
        })
        assert status == 200, f"PATCH failed: {status} body={data}"
        assert isinstance(data, dict)
        assert data.get("root_path") is None, (
            f"root_path=null should clear it: {data}"
        )
    finally:
        _cleanup_profile(agent_id, profile_name)
