"""Regression test for the 2026-07-28 LLM planner context bug.

The /api/projects/{id}/plan/from-llm endpoint was passing MACHINE
NAMES (agent_id) to the planner instead of ROLE NAMES (name), and
wasn't including storage_refs. The LLM ended up picking the first
machine alphabetically ("linux-a-01") as the agent_role for every
step, even when the role with the right storage was on a different
machine. Result: dispatch failed silently (no profile with that
name), and the user saw tasks going to the wrong machine.

Per user feedback 2026-07-28: "之前出 task 的時候, 他會看到
win-agent01 storage_refs 中的 project_temp_folder 會派 task 去
win-agent01, 但現在派了 linux-a-01, 而且不是 agent profile".

Two layers of test:
  1. In-process unit test of the Planner's _format_*_block
     methods, asserting the prompt includes role names + storage
     aliases. This runs in the test process and catches the bug
     deterministically.
  2. A live HTTP integration test that hits the server with a
     real (mock-mode) goal and asserts the returned plan uses a
     role NAME (not a machine id) for agent_role. mock mode is
     deterministic so this is reliable.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid

import pytest

BASE = os.environ.get("HERMES_TEST_BASE", "http://127.0.0.1:8765")


# ===== Unit tests (in-process) =====


def test_format_storage_block_renders_aliases():
    """The Planner._format_storage_block should render the role +
    alias in a way the LLM can read. Regression: 2026-07-28 this
    block was empty because plans.py wasn't passing role_storage."""
    from hermes_orch.core.planner import Planner

    # Three roles: one with a 'project_temp_folder' alias (SMB),
    # one with 'stanley' (gdrive), one with no storage.
    role_storage = {
        "win-agent01": [
            {"name": "project_temp_folder", "kind": "smb",
             "ref": "\\\\HERMES-WIN\\project_temp_folder",
             "description": "Shared reports (SMB)", "action": "do_step"},
            {"name": "stanley", "kind": "gdrive",
             "ref": "https://drive.google.com/drive/folders/ABC",
             "description": "stanley/ folder", "action": "do_step"},
        ],
        "super": [],  # no storage
    }
    roles = ["super", "win-agent01"]
    block = Planner._format_storage_block(roles, role_storage)

    assert "win-agent01" in block, f"block doesn't name the role: {block!r}"
    assert "project_temp_folder" in block, f"block doesn't render the alias: {block!r}"
    # The format should make it easy for the LLM to see WHICH role
    # owns WHICH alias. The most useful framing is: "<role>: <alias>
    # (<kind>: <ref>)". The test is loose — just verify both pieces
    # are present and the role and alias are on the same logical line.
    win_line = [ln for ln in block.split("\n") if "win-agent01" in ln]
    assert win_line, f"no line mentions win-agent01: {block!r}"
    assert "project_temp_folder" in win_line[0], (
        f"win-agent01's line doesn't list the alias: {win_line[0]!r}"
    )


def test_format_storage_block_empty_when_no_storage():
    """If no role has storage_refs, the block should be empty (don't
    pollute the prompt)."""
    from hermes_orch.core.planner import Planner
    block = Planner._format_storage_block(["super", "win"], None)
    assert block == ""
    block2 = Planner._format_storage_block(["super", "win"], {})
    assert block2 == ""


def test_format_role_skills_uses_role_names():
    """Regression 2026-07-28: the role_skills block must list role
    NAMES, not agent_ids. If the LLM sees agent_ids in the role
    list, the validator will reject its output (agent_role not in
    available_roles)."""
    from hermes_orch.core.planner import Planner
    role_skills = {
        "win-agent01": ["mt5", "google_drive"],
        "super": ["general"],
    }
    roles = ["super", "win-agent01"]
    block = Planner._format_role_skills(roles, role_skills)
    assert "win-agent01: mt5, google_drive" in block
    assert "super: general" in block


# ===== Integration test (via HTTP, mock planner) =====


def _http(method, path, body=None):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw or b"null")
            except (json.JSONDecodeError, TypeError):
                return r.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except (json.JSONDecodeError, TypeError):
            return e.code, raw.decode("utf-8", errors="replace")


def _create_test_project(name_suffix=""):
    name = f"planner-ctx-{uuid.uuid4().hex[:8]}"
    body = {"name": name, "action": "do_step"}
    if name_suffix:
        body["goal"] = name_suffix
    s, resp = _http("POST", "/api/projects/", body)
    if isinstance(resp, dict) and "id" in resp:
        return resp["id"]
    pytest.fail(f"create project failed: {s} {resp}")


def _delete_project(pid):
    try:
        _http("DELETE", f"/api/projects/{pid}")
    except Exception:
        pass


# MACHINE_ID_PATTERN: matches "linux-a-01", "win-local-1", "mac-b-12" — the
# shape of agent_id values. We use this to assert that the LLM's chosen
# agent_role is NOT a machine id.
_MACHINE_ID = re.compile(r"^(linux|win|mac)-[a-z]+-\d+$")


def test_from_llm_step_agent_role_is_a_role_name_not_machine_id():
    """Integration regression: when the LLM picks an agent_role for
    a step, it must be a ROLE NAME (e.g. "win-agent01"), not a
    MACHINE ID (e.g. "linux-a-01"). The old bug had the LLM
    pick "linux-a-01" because that was the only thing in
    available_roles.

    The server is in mock mode (no real LLM call) so this is
    deterministic. The mock planner picks the first available
    role; we just assert the first available role is a name.
    """
    pid = _create_test_project(name_suffix="Analyze project_temp_folder")
    try:
        s, body = _http(
            "POST",
            f"/api/projects/{pid}/plan/from-llm",
            {"goal": "Analyze project_temp_folder"},
        )
        assert s == 200, f"from-llm failed: {s} {body}"
        steps = body["plan"]["steps"]
        assert steps, "no steps in plan"
        for step in steps:
            role = step.get("agent_role", "")
            assert role, f"step has empty agent_role: {step!r}"
            assert not _MACHINE_ID.match(role), (
                f"step {step['name']!r} agent_role={role!r} looks like "
                f"a MACHINE id (agent_id), not a role name. The "
                f"planner was called with the wrong column. Roles "
                f"should come from agent_profiles.name."
            )
    finally:
        _delete_project(pid)
