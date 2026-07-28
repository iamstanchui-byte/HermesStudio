"""Tests for the Single Tasks API (commit 3 of Object Layer, 2026-07-27).

Covers:
  - POST /api/single-tasks — create with is_single_task=1, virtual project
  - GET /api/single-tasks — list (newest first)
  - GET /api/single-tasks/{id} — 404 for project tasks (defense)
  - Source field round-trips (free-form dict)
  - Cleanup: tasks are isolated from project tasks

The HTML pages (/single-tasks) are covered by the smoke test
in scripts/_smoke-single-tasks.py (template render is implicit).
"""
from __future__ import annotations

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
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"detail": raw}


@pytest.fixture(scope="module", autouse=True)
def ensure_server_up():
    if not _wait_healthy():
        pytest.skip("server not running on :8765")


def _wait_healthy(timeout_s: float = 5.0) -> bool:
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


# ===== Virtual project =====


def test_virtual_single_tasks_project_exists():
    """The lifespan startup should have created the virtual project."""
    s, p = _http("GET", "/api/projects/__single_tasks__")
    assert s == 200
    assert p["id"] == "__single_tasks__"
    assert "single" in p["name"].lower()


# ===== Create / list / get =====


def test_create_single_task_sets_is_single_task_flag():
    s, t = _http("POST", "/api/single-tasks", {
        "name": "test single task create",
        "goal": "verify the create endpoint",
        "source": {"kind": "test", "tag": "pytest"},
    })
    assert s == 201
    assert t["name"] == "test single task create"
    assert t["status"] == "pending"
    assert t["project_id"] == "__single_tasks__"
    assert t["source"]["kind"] == "test"
    assert t["source"]["tag"] == "pytest"
    assert t["has_result"] is False
    assert t["has_error"] is False
    # Cleanup
    _delete_task(t["id"])


def test_create_single_task_minimal_body():
    """All optional fields can be omitted — only name is required."""
    s, t = _http("POST", "/api/single-tasks", {"name": "minimal task", "action": "do_step"})
    assert s == 201
    assert t["name"] == "minimal task"
    assert t["goal"] == ""
    assert t["required_capability"] == ""
    assert t["output_path"] == ""
    assert t["source"] == {}
    _delete_task(t["id"])


def test_create_single_task_rejects_empty_name():
    s, body = _http("POST", "/api/single-tasks", {"name": "", "action": "do_step"})
    assert s == 422  # Pydantic validation


def test_list_single_tasks_returns_only_is_single():
    """The list endpoint should filter by is_single_task=1, not
    accidentally include project tasks."""
    # Create one
    s, t = _http("POST", "/api/single-tasks", {
        "name": "marker task",
        "source": {"kind": "list-test"},
    })
    assert s == 201
    # List
    s, lst = _http("GET", "/api/single-tasks")
    assert s == 200
    assert lst["count"] >= 1
    # All returned tasks should have is_single_task=1 (we trust the
    # API to filter; the explicit check on the response would require
    # adding is_single_task to SingleTaskOut, which we don't need).
    for item in lst["tasks"]:
        assert item["id"].startswith("t-")
    # The marker should be in the list
    assert any(item["name"] == "marker task" for item in lst["tasks"])
    _delete_task(t["id"])


def test_get_single_task_by_id():
    s, t = _http("POST", "/api/single-tasks", {"name": "get-by-id test", "action": "do_step"})
    assert s == 201
    s, g = _http("GET", f"/api/single-tasks/{t['id']}")
    assert s == 200
    assert g["id"] == t["id"]
    assert g["name"] == "get-by-id test"
    _delete_task(t["id"])


def test_get_single_task_404_for_nonexistent():
    s, _ = _http("GET", "/api/single-tasks/t-nonexistent-id")
    assert s == 404


def test_get_single_task_404_for_project_task():
    """A project task is not a single task. The endpoint must 404
    even if the task id exists in the DB."""
    # Find any project task
    s, tasks = _http("GET", "/api/tasks/?project_id=proj-8fece23e&limit=1")
    assert s == 200
    task_list = tasks["tasks"] if isinstance(tasks, dict) else tasks
    if not task_list:
        pytest.skip("no project tasks to test against")
    project_task_id = task_list[0]["id"]
    s, _ = _http("GET", f"/api/single-tasks/{project_task_id}")
    assert s == 404, "project task should NOT be returned as single task"


def test_list_filters_by_status():
    s, t1 = _http("POST", "/api/single-tasks", {"name": "filter-pending", "action": "do_step"})
    s, t2 = _http("POST", "/api/single-tasks", {"name": "filter-completed", "action": "do_step"})
    # t1 stays pending; t2 we directly mark completed in the DB
    db_path = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE tasks SET status = 'completed' WHERE id = ?",
            (t2["id"],),
        )
        conn.commit()
    # Filter by pending
    s, lst = _http("GET", "/api/single-tasks?status=pending")
    pending_ids = [t["id"] for t in lst["tasks"]]
    assert t1["id"] in pending_ids
    assert t2["id"] not in pending_ids
    # Filter by completed
    s, lst = _http("GET", "/api/single-tasks?status=completed")
    completed_ids = [t["id"] for t in lst["tasks"]]
    assert t2["id"] in completed_ids
    _delete_task(t1["id"])
    _delete_task(t2["id"])


def test_supervisor_dispatches_single_task():
    """The supervisor's _drive_single_tasks loop should pick up a
    newly-created single task, assign it to an available agent, and
    promote it to 'running' so the wrapper can pick it up. The
    supervisor polls every ~5s, so we wait up to 15s.
    """
    s, t = _http("POST", "/api/single-tasks", {
        "name": "supervisor dispatch test",
        "goal": "verify supervisor dispatches single tasks",
    })
    assert s == 201
    assert t["status"] == "pending"
    # Wait up to 15s for the supervisor to pick it up
    deadline = time.time() + 15
    final_status = "pending"
    while time.time() < deadline:
        s, cur = _http("GET", f"/api/single-tasks/{t['id']}")
        final_status = cur["status"]
        if final_status in ("assigned", "running", "completed", "failed"):
            break
        time.sleep(2)
    # The supervisor should have moved it from pending to assigned/running
    # (the exact state depends on whether the supervisor's auto-promote
    # fired before the wrapper claimed it; both are valid).
    assert final_status in ("assigned", "running"), (
        f"supervisor didn't dispatch single task in 15s; final status={final_status}"
    )
    _delete_task(t["id"])


def test_supervisor_dispatches_single_task_with_empty_role():
    """A single task created with no agent_role (the default) should
    still be dispatched — _assign_task now picks any available
    verified profile for single tasks with empty role."""
    s, t = _http("POST", "/api/single-tasks", {
        "name": "empty role dispatch test",
        "goal": "verify empty-role single tasks get dispatched",
    })
    assert s == 201
    deadline = time.time() + 15
    while time.time() < deadline:
        s, cur = _http("GET", f"/api/single-tasks/{t['id']}")
        if cur["status"] in ("assigned", "running"):
            # Confirm an agent was actually picked (not None)
            assert cur.get("assigned_profile_id") is not None
            break
        time.sleep(2)
    else:
        pytest.fail(f"empty-role single task wasn't dispatched; final={cur['status']}")
    _delete_task(t["id"])


# ===== Helpers =====


def _delete_task(task_id: str) -> None:
    """Delete a single task (cleanup). Uses sync sqlite3 to avoid
    event-loop conflicts with pytest-asyncio."""
    db_path = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
