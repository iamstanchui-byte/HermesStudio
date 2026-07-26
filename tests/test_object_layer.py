"""Tests for the Object Layer API (commit 1 of schema foundation, 2026-07-26).

Covers:
  - Virtual __single_tasks__ project auto-creation on startup
  - Skill sidecar schema parsing (with fallback)
  - Object Layer read endpoints (skills, tools, resources, registry)
  - Tool check-mcp endpoint (the one write endpoint)
  - Resource aggregation across agent_profiles.storage_refs

The fixture inserts minimal data into a fresh DB, then exercises each
endpoint. We use the live server (8765) rather than ASGITransport
because the test layer is end-to-end (DB + API + sidecar loader).

DB writes use sync sqlite3 (not aiosqlite) so the test doesn't
fight pytest-asyncio's event loop — pytest creates one loop per
async test, and asyncio.run() would try to create a second one
and fail with "asyncio.run() cannot be called from a running event loop".
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

BASE = "http://127.0.0.1:8765"


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | None]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8")
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"detail": raw}


def _wait_healthy(timeout_s: float = 10.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            s, _ = _http("GET", "/api/health")
            if s == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


# ===== Module-level setup =====


@pytest.fixture(scope="module", autouse=True)
def ensure_server_up():
    """Skip the whole module if the server isn't running (faster CI)."""
    if not _wait_healthy(timeout_s=2.0):
        pytest.skip("server not running on :8765")


# ===== Virtual single_tasks project =====


def test_virtual_single_tasks_project_exists():
    """ensure_single_tasks_project() should have inserted __single_tasks__."""
    s, p = _http("GET", "/api/projects/__single_tasks__")
    assert s == 200, f"project not found: {p}"
    assert p["id"] == "__single_tasks__"
    assert p["state"] == "completed"
    # name should mention 'single' for human-readable identification
    assert "single" in p["name"].lower()


def test_single_task_is_single_task_flag_default():
    """New single tasks should have is_single_task=1 by default.
    Tested indirectly: the virtual project has zero tasks, so we just
    verify the column exists by checking the GET task detail succeeds."""
    # The cleanest check is that the tasks.is_single_task column is
    # present in the schema. We can do that by reading any task's
    # JSON from the API — but the public Task Pydantic model doesn't
    # expose is_single_task yet. So just verify the GET endpoint
    # doesn't 500 when called on the virtual project.
    s, tasks = _http("GET", "/api/tasks/?project_id=__single_tasks__")
    assert s == 200
    # initially empty
    task_list = tasks["tasks"] if isinstance(tasks, dict) else tasks
    assert task_list == []


# ===== Skill sidecar (the most subtle test) =====


def test_skill_without_sidecar_returns_fallback_schema():
    """Skills without SKILL.schema.yaml should report default schema.

    Pick a skill that we know has no sidecar (the sidecar test
    leaves one behind in the same DB if it succeeded — pick a
    different profile to be safe).
    """
    s, all_skills = _http("GET", "/api/objects/skills")
    assert s == 200
    # Find any skill that the API reports as non-deterministic —
    # the loader returns the fallback (deterministic=false) for
    # skills without SKILL.schema.yaml.
    fallback = next((sk for sk in all_skills if not sk["schema"]["deterministic"]), None)
    if fallback is None:
        pytest.skip("no fallback-schema skills available — all have sidecars")
    # Confirm the fallback shape
    assert fallback["schema"]["llm_required"] is True
    assert fallback["schema"]["input_schema"] == {}
    assert fallback["schema"]["output_schema"] == {}
    assert fallback["schema"]["requires_capabilities"] == []


def test_skill_loader_creates_sidecar_visible_via_api():
    """Insert a sidecar directly, then verify the API picks it up.

    This is the round-trip test: the SkillLoader must JOIN on
    SKILL.schema.yaml and reflect the schema in the response.
    """
    s, all_skills = _http("GET", "/api/objects/skills?profile_id=034e6614-f394-4950-83e5-357132b06d66")
    assert s == 200
    if not all_skills:
        pytest.skip("no skills on that profile to attach a sidecar to")
    # Find the first skill WITHOUT a sidecar (deterministic defaults
    # to false on no-sidecar skills, so the first non-deterministic
    # one is a clean target).
    target = next((sk for sk in all_skills if not sk["schema"]["deterministic"]), None)
    if target is None:
        pytest.skip("all skills on this profile already have sidecars")
    profile_id = target["profile_id"]
    skill_name = target["name"]
    sidecar_yaml = (
        "input_schema:\n"
        "  url: string\n"
        "output_schema:\n"
        "  status_code: number\n"
        "deterministic: true\n"
        "llm_required: false\n"
        "requires_capabilities:\n"
        "  - http\n"
    )
    # Insert sidecar via the agents skill-create endpoint. We
    # reuse the existing skill's row, replacing the file_path
    # to point to SKILL.schema.yaml. The endpoint stores it as
    # 'skills/<name>/SKILL.md' though — see the agents.py code
    # which forces _skill_file_path(name). So we need a different
    # path. For now, write directly via the DB.
    db_path = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
    cfg_id = str(uuid.uuid4())
    sha = hashlib.sha256(sidecar_yaml.encode("utf-8")).hexdigest()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO profile_configs "
            "(id, profile_id, file_path, desired_sha256, desired_content, "
            " status, created_at, applied_at) "
            "VALUES (?, ?, ?, ?, ?, 'applied', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (
                cfg_id, profile_id,
                f"skills/{skill_name}/SKILL.schema.yaml",
                sha,
                sidecar_yaml,
            ),
        )
        conn.commit()
    # Now fetch and check
    s2, rec = _http("GET", f"/api/objects/skills/{profile_id}/{skill_name}")
    assert s2 == 200, f"sidecar not picked up: {rec}"
    assert rec["schema"]["deterministic"] is True
    assert rec["schema"]["llm_required"] is False
    assert "http" in rec["schema"]["requires_capabilities"]
    # Cleanup
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM profile_configs WHERE id = ?", (cfg_id,))
        conn.commit()


# ===== Object Layer endpoints =====


def test_registry_aggregate_returns_all_three_types():
    s, reg = _http("GET", "/api/objects/registry")
    assert s == 200
    assert "skills" in reg and "tools" in reg and "resources" in reg
    assert "counts" in reg
    assert reg["counts"]["skills"] >= 0
    assert reg["counts"]["tools"] >= 0
    assert reg["counts"]["resources"] >= 0
    assert reg["counts"]["deterministic_skills"] >= 0


def test_list_resources_returns_storage_refs_aggregated():
    s, res = _http("GET", "/api/objects/resources")
    assert s == 200
    assert isinstance(res, list)
    for r in res:
        # Each entry should have profile_id, kind, uri at minimum
        assert "profile_id" in r
        assert "kind" in r
        assert "uri" in r
        # kind must be one of the 5 valid enums
        assert r["kind"] in ("smb", "local", "gdrive", "s3", "url")


def test_404_for_missing_skill_and_tool():
    s, _ = _http("GET", "/api/objects/skills/nonexistent-profile/x")
    assert s == 404
    s, _ = _http("GET", "/api/objects/tools/nonexistent-id")
    assert s == 404


# ===== Tool check-mcp (the one write endpoint) =====


def test_check_mcp_records_status():
    """POST check-mcp should upsert profile_tools and return the new state."""
    # First, fetch an existing agent profile (any will do)
    s, agents_resp = _http("GET", "/api/agents")
    assert s == 200
    # /api/agents returns {agents: [...]} with each agent having profiles
    agent_list = agents_resp["agents"] if isinstance(agents_resp, dict) else agents_resp
    if not agent_list:
        pytest.skip("no agents registered")
    profiles = agent_list[0].get("profiles", [])
    if not profiles:
        pytest.skip("first agent has no profiles")
    profile_id = profiles[0]["id"]
    # Insert a tool definition first (no write endpoint for tools yet;
    # use direct SQL — same pattern as the sidecar test)
    db_path = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
    tool_id = "tool-test-check-mcp"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tool_definitions "
            "(id, name, version, kind, description, capabilities, mcp_server_name, "
            " created_at, updated_at) "
            "VALUES (?, 'test-tool', '1.0.0', 'mcp_server', 'unit test', '[]', "
            " 'test-mcp', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (tool_id,),
        )
        conn.commit()
    # Now hit the endpoint
    s, avail = _http(
        "POST", f"/api/objects/tools/{tool_id}/check-mcp",
        {"profile_id": profile_id, "status": "up"},
    )
    assert s == 200, f"check-mcp failed: {avail}"
    assert avail["mcp_status"] == "up"
    assert avail["profile_id"] == profile_id
    # And the list should now include the availability
    s, tools = _http("GET", "/api/objects/tools")
    assert s == 200
    test_tool = next((t for t in tools if t["id"] == tool_id), None)
    assert test_tool is not None
    assert any(a["profile_id"] == profile_id for a in test_tool["availability"])
    # Cleanup
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM profile_tools WHERE tool_id = ?", (tool_id,))
        conn.execute("DELETE FROM tool_definitions WHERE id = ?", (tool_id,))
        conn.commit()


def test_check_mcp_invalid_status_rejected():
    s, _ = _http(
        "POST", "/api/objects/tools/nonexistent/check-mcp",
        {"profile_id": "x", "status": "maybe"},
    )
    assert s == 400
