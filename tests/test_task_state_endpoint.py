"""Tests for the bulk task state endpoint (v1.3 hot-fix, 2026-07-29).

Endpoint under test:
  GET /api/projects/{project_id}/tasks/state
    Returns light shape: [{task_id, status, loop_status, ...}, ...]
    covering ALL visible tasks (not just running), so the
    dashboard can update the status pill in-place when a task
    transitions running → done / failed / cancelled.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest


BASE = "http://127.0.0.1:8765"
DB_PATH = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"


def _http(method: str, path: str) -> tuple[int, dict | list | str | None]:
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
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


def _create_project() -> str:
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO projects (id, name, goal, state, coordinator_role, "
            "accept_criteria, deliverable_path, max_iterations, "
            "current_iteration, last_iteration_summary) "
            "VALUES (?, 'state-test', ?, 'planned', '', '', '', 0, 0, '')",
            (pid, "task state endpoint test"),
        )
        conn.commit()
    finally:
        conn.close()
    return pid


def _insert_task(
    project_id: str,
    *,
    status: str = "running",
    name: str | None = None,
) -> str:
    tid = f"t-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, status, "
            "depends_on, on_parent_failure, priority) "
            "VALUES (?, ?, ?, 'super', ?, '[]', 'skip', 'normal')",
            (tid, project_id, name or f"task-{tid[-8:]}", status),
        )
        conn.commit()
    finally:
        conn.close()
    return tid


def _delete_project(project_id: str) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()


# ===== Tests =====


def test_state_404_unknown_project():
    s, _ = _http("GET", "/api/projects/proj-fake/tasks/state")
    assert s == 404


def test_state_returns_empty_for_new_project():
    pid = _create_project()
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/state")
        assert s == 200
        assert body["project_id"] == pid
        assert body["tasks"] == []
        assert body["count"] == 0
    finally:
        _delete_project(pid)


def test_state_includes_non_running_tasks():
    """v1.3 hot-fix core behavior: the endpoint returns DONE / FAILED
    / CANCELLED tasks too, not just running. The old /tasks/running
    would have excluded these, leaving their status pill frozen on
    'running' forever."""
    pid = _create_project()
    tid_run = _insert_task(pid, status="running", name="run-one")
    tid_done = _insert_task(pid, status="done", name="done-one")
    tid_fail = _insert_task(pid, status="failed", name="fail-one")
    tid_canc = _insert_task(pid, status="cancelled", name="canc-one")
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/state")
        assert s == 200
        assert body["count"] == 4
        by_id = {t["task_id"]: t for t in body["tasks"]}
        assert by_id[tid_run]["status"] == "running"
        assert by_id[tid_done]["status"] == "done"
        assert by_id[tid_fail]["status"] == "failed"
        assert by_id[tid_canc]["status"] == "cancelled"
    finally:
        _delete_project(pid)


def test_state_response_shape():
    """The endpoint returns a LIGHT shape (just the fields the UI
    needs to update a row). Critical for not over-fetching on the
    5s polling cycle."""
    pid = _create_project()
    tid = _insert_task(pid, status="running", name="shape-test")
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/state")
        assert s == 200
        t = body["tasks"][0]
        # Required fields
        for f in ("task_id", "project_id", "name", "status",
                  "loop_status", "loop_reason", "started_at",
                  "last_liveness_at"):
            assert f in t, f"missing field: {f}"
        # Fields that should NOT be in the light shape (avoid
        # over-fetching for a 5s polling cycle)
        for f in ("result", "params", "error", "artifacts", "depends_on"):
            assert f not in t, f"unexpected heavy field: {f}"
    finally:
        _delete_project(pid)


def test_state_scoped_to_project():
    """Tasks in project A don't show up in project B's state list."""
    pid_a = _create_project()
    pid_b = _create_project()
    tid_a = _insert_task(pid_a, status="running")
    tid_b = _insert_task(pid_b, status="running")
    try:
        s, body_a = _http("GET", f"/api/projects/{pid_a}/tasks/state")
        assert s == 200
        s, body_b = _http("GET", f"/api/projects/{pid_b}/tasks/state")
        assert s == 200
        ids_a = {t["task_id"] for t in body_a["tasks"]}
        ids_b = {t["task_id"] for t in body_b["tasks"]}
        assert tid_a in ids_a
        assert tid_a not in ids_b
        assert tid_b in ids_b
        assert tid_b not in ids_a
    finally:
        _delete_project(pid_a)
        _delete_project(pid_b)


def test_state_excludes_archived_tasks():
    """Archived tasks (created by Clone chain) are not in the
    default view — only the active plan is."""
    pid = _create_project()
    tid_active = _insert_task(pid, status="done", name="active")
    tid_archived = _insert_task(pid, status="done", name="archived")
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("UPDATE tasks SET archived = 1 WHERE id = ?", (tid_archived,))
        conn.commit()
    finally:
        conn.close()
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/state")
        assert s == 200
        ids = {t["task_id"] for t in body["tasks"]}
        assert tid_active in ids
        assert tid_archived not in ids
    finally:
        _delete_project(pid)
