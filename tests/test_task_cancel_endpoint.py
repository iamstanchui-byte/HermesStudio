"""Tests for the Task Progress Monitor cancel endpoint (T3, 2026-07-29).

Endpoint under test:
  POST /api/projects/{project_id}/tasks/{task_id}/cancel

This is a project-scoped wrapper around /api/tasks/{id}/cancel
with an IDOR guard (the unscoped endpoint is kept for backwards
compatibility but the UI should use the project-scoped one).

We seed task rows via sync sqlite3 (same approach as the T2
tests) and verify via the live server.
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


# ===== HTTP helpers (live server) =====


def _http(
    method: str, path: str, body: dict | None = None
) -> tuple[int, dict | list | str | None]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
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


# ===== Seed / cleanup =====


def _create_project(name: str | None = None) -> str:
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO projects (id, name, goal, state, coordinator_role, "
            "accept_criteria, deliverable_path, max_iterations, "
            "current_iteration, last_iteration_summary) "
            "VALUES (?, ?, ?, 'planned', '', '', '', 0, 0, '')",
            (pid, name or f"cancel-test-{pid[-8:]}", "cancel test"),
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
        conn.execute("DELETE FROM audit_log WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()


def _task_status(tid: str) -> str | None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _audit_count(tid: str, event_type: str) -> int:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE task_id = ? AND event_type = ?",
            (tid, event_type),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


# ===== Tests =====


def test_cancel_404_unknown_project():
    s, body = _http(
        "POST",
        "/api/projects/proj-fake/tasks/t-fake/cancel",
    )
    assert s == 404
    assert "not found" in str(body).lower()


def test_cancel_404_task_in_other_project_idor():
    """Critical: cancelling a task in project A via project B's path
    MUST fail. The endpoint enforces IDOR safety."""
    pid_a = _create_project("A")
    pid_b = _create_project("B")
    tid = _insert_task(pid_a, status="running")
    try:
        s, body = _http(
            "POST",
            f"/api/projects/{pid_b}/tasks/{tid}/cancel",
        )
        assert s == 404
        # Task should still be running (not cancelled)
        assert _task_status(tid) == "running"
    finally:
        _delete_project(pid_a)
        _delete_project(pid_b)


def test_cancel_running_task_succeeds():
    """The happy path: cancel a running task. Returns 200 with
    was_running=True and the updated task dict."""
    pid = _create_project()
    tid = _insert_task(pid, status="running")
    try:
        s, body = _http(
            "POST", f"/api/projects/{pid}/tasks/{tid}/cancel"
        )
        assert s == 200, f"{s} {body}"
        assert body["was_running"] is True
        assert body["task"]["id"] == tid
        assert body["task"]["status"] == "cancelled"
        assert body["cancelled_at"] is not None
        # DB also reflects the cancel
        assert _task_status(tid) == "cancelled"
        # Audit log entry was written
        assert _audit_count(tid, "task.cancelled") == 1
    finally:
        _delete_project(pid)


def test_cancel_pending_task_succeeds():
    """Pending (never-started) tasks are also cancellable."""
    pid = _create_project()
    tid = _insert_task(pid, status="pending")
    try:
        s, body = _http(
            "POST", f"/api/projects/{pid}/tasks/{tid}/cancel"
        )
        assert s == 200
        assert body["was_running"] is False  # was pending, not running
        assert body["task"]["status"] == "cancelled"
    finally:
        _delete_project(pid)


def test_cancel_assigned_task_succeeds():
    """Assigned (not yet running) tasks are cancellable too."""
    pid = _create_project()
    tid = _insert_task(pid, status="assigned")
    try:
        s, body = _http(
            "POST", f"/api/projects/{pid}/tasks/{tid}/cancel"
        )
        assert s == 200
        assert body["was_running"] is False
        assert body["task"]["status"] == "cancelled"
    finally:
        _delete_project(pid)


def test_cancel_done_task_400():
    """Cannot cancel a task that's already in a terminal state."""
    pid = _create_project()
    tid = _insert_task(pid, status="done")
    try:
        s, body = _http(
            "POST", f"/api/projects/{pid}/tasks/{tid}/cancel"
        )
        assert s == 400
        assert "cancellable" in str(body).lower() or "state" in str(body).lower()
        # Status unchanged
        assert _task_status(tid) == "done"
    finally:
        _delete_project(pid)


def test_cancel_failed_task_400():
    """Cannot cancel a failed task."""
    pid = _create_project()
    tid = _insert_task(pid, status="failed")
    try:
        s, body = _http(
            "POST", f"/api/projects/{pid}/tasks/{tid}/cancel"
        )
        assert s == 400
    finally:
        _delete_project(pid)


def test_cancel_then_status_endpoint_shows_cancelled():
    """After cancellation, GET /tasks/{id}/status should show the
    task as cancelled (loop_status=ok, reason='task is cancelled').
    This verifies the cancel → status pipeline that the UI relies
    on (the badge will update after the next 5s poll)."""
    pid = _create_project()
    tid = _insert_task(pid, status="running")
    try:
        s, _ = _http(
            "POST", f"/api/projects/{pid}/tasks/{tid}/cancel"
        )
        assert s == 200
        s, body = _http("GET", f"/api/projects/{pid}/tasks/{tid}/status")
        assert s == 200
        assert body["status"] == "cancelled"
        assert body["loop_status"] == "ok"
        assert body["loop_reason"] == "task is cancelled"
    finally:
        _delete_project(pid)


def test_cancel_unknown_task_404():
    """Cancelling a non-existent task_id in a valid project → 404."""
    pid = _create_project()
    try:
        s, body = _http(
            "POST", f"/api/projects/{pid}/tasks/t-fake/cancel"
        )
        assert s == 404
    finally:
        _delete_project(pid)
