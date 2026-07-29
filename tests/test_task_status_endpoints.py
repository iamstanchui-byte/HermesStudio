"""Tests for the Task Progress Monitor endpoints (T2, 2026-07-29).

Endpoints under test:
  GET /api/projects/{project_id}/tasks/{task_id}/status
  GET /api/projects/{project_id}/tasks/running

These tests run against the **live** orchestrator (port 8765).
The server's supervisor queries running tasks with stale
liveness (>180s) and marks them failed, so we keep our
"stuck" cases at 150s (still past the 120s STUCK threshold,
but inside the supervisor's 180s grace window).

Seed data is inserted via direct sync sqlite3 connections so
we don't fight the live server's event loop on the async
Database wrapper.

Usage:
    python -m pytest tests/test_task_status_endpoints.py -v
        # (assumes server running on 8765)
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
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


# ===== Direct-SQL seed helpers (sync sqlite3) =====


def _iso(ts: float) -> str:
    """ISO-8601 with UTC offset (matches hermes_orch.utils.now_iso)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _create_project(name: str | None = None) -> str:
    """Insert a project row directly via sync sqlite3. Returns the
    project id. Sync so we don't fight the live server's event
    loop on the async Database wrapper."""
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO projects (id, name, goal, state, coordinator_role, "
            "accept_criteria, deliverable_path, max_iterations, "
            "current_iteration, last_iteration_summary) "
            "VALUES (?, ?, ?, 'planned', '', '', '', 0, 0, '')",
            (pid, name or f"loop-test-{pid[-8:]}", "loop status test"),
        )
        conn.commit()
    finally:
        conn.close()
    return pid


def _insert_task(
    project_id: str,
    *,
    status: str = "running",
    started_at: str | None = None,
    last_liveness_at: str | None = None,
    name: str | None = None,
) -> str:
    """Insert a task row directly. Returns the task id."""
    tid = f"t-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, status, "
            "depends_on, on_parent_failure, priority, started_at, "
            "last_liveness_at) "
            "VALUES (?, ?, ?, 'super', ?, '[]', 'skip', 'normal', ?, ?)",
            (
                tid,
                project_id,
                name or f"task-{tid[-8:]}",
                status,
                started_at,
                last_liveness_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return tid


def _insert_audit(
    *,
    task_id: str,
    event_type: str,
    created_at: str,
) -> None:
    """Insert an audit_log row directly (so we can simulate the
    supervisor's `task.stuck_wrapper` event at a controlled time)."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO audit_log (event_type, task_id, created_at) "
            "VALUES (?, ?, ?)",
            (event_type, task_id, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _delete_task(task_id: str) -> None:
    """Best-effort cleanup so reruns don't pollute the DB."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.execute("DELETE FROM audit_log WHERE task_id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()


def _delete_project(project_id: str) -> None:
    """Best-effort project cleanup."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM audit_log WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()


# ===== GET /api/projects/{id}/tasks/{task_id}/status =====


def test_status_404_unknown_project():
    s, body = _http(
        "GET", "/api/projects/proj-does-not-exist/tasks/t-fake/status"
    )
    assert s == 404
    assert "not found" in str(body).lower()


def test_status_404_unknown_task_in_existing_project():
    pid = _create_project()
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/t-fake/status")
        assert s == 404
        assert "not found" in str(body).lower()
    finally:
        _delete_project(pid)


def test_status_404_task_in_other_project_idor():
    """A task in project A must not be readable via project B's path
    — that's an IDOR (insecure direct object reference) bug."""
    pid_a = _create_project("project-A")
    pid_b = _create_project("project-B")
    tid = _insert_task(pid_a, status="running")
    try:
        s, body = _http("GET", f"/api/projects/{pid_b}/tasks/{tid}/status")
        assert s == 404
    finally:
        _delete_task(tid)
        _delete_project(pid_a)
        _delete_project(pid_b)


def test_status_200_running_with_fresh_liveness():
    pid = _create_project()
    now = datetime.now(timezone.utc).timestamp()
    tid = _insert_task(
        pid,
        status="running",
        started_at=_iso(now - 60),
        last_liveness_at=_iso(now - 5),
    )
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/{tid}/status")
        assert s == 200, f"{s} {body}"
        assert body["task_id"] == tid
        assert body["project_id"] == pid
        assert body["status"] == "running"
        assert body["loop_status"] == "ok"
        assert body["loop_reason"] == "liveness OK"
        assert body["last_event_age_s"] == 5
        assert body["duration_s"] == 60
    finally:
        _delete_task(tid)
        _delete_project(pid)


def test_status_200_running_slow():
    """Liveness 45s ago → between SLOW (30) and STUCK (120) → slow."""
    pid = _create_project()
    now = datetime.now(timezone.utc).timestamp()
    tid = _insert_task(
        pid,
        status="running",
        started_at=_iso(now - 60),
        last_liveness_at=_iso(now - 45),
    )
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/{tid}/status")
        assert s == 200
        assert body["loop_status"] == "slow"
        assert "45s" in body["loop_reason"]
    finally:
        _delete_task(tid)
        _delete_project(pid)


def test_status_200_running_stuck_by_liveness():
    """Liveness 150s ago → past STUCK (120) but within supervisor's
    180s grace window (we don't want supervisor to mark it failed
    during the test)."""
    pid = _create_project()
    now = datetime.now(timezone.utc).timestamp()
    tid = _insert_task(
        pid,
        status="running",
        started_at=_iso(now - 300),
        last_liveness_at=_iso(now - 150),
    )
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/{tid}/status")
        assert s == 200
        assert body["loop_status"] == "stuck"
        assert "150s" in body["loop_reason"]
    finally:
        _delete_task(tid)
        _delete_project(pid)


def test_status_200_running_stuck_by_supervisor_event():
    """A recent task.stuck_wrapper audit event makes the task stuck
    even if the liveness looks fine. The supervisor's signal is
    the strongest indicator that the wrapper died."""
    pid = _create_project()
    now = datetime.now(timezone.utc).timestamp()
    tid = _insert_task(
        pid,
        status="running",
        started_at=_iso(now - 60),
        last_liveness_at=_iso(now - 2),  # would be ok on its own
    )
    _insert_audit(
        task_id=tid,
        event_type="task.stuck_wrapper",
        created_at=_iso(now - 30),
    )
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/{tid}/status")
        assert s == 200
        assert body["loop_status"] == "stuck"
        assert (
            "supervisor" in body["loop_reason"].lower()
            or "wrapper" in body["loop_reason"].lower()
        )
    finally:
        _delete_task(tid)
        _delete_project(pid)


def test_status_200_done_task():
    """Non-running tasks return ok + 'task is X' reason."""
    pid = _create_project()
    now = datetime.now(timezone.utc).timestamp()
    tid = _insert_task(
        pid,
        status="done",
        started_at=_iso(now - 100),
        last_liveness_at=_iso(now - 10),
    )
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/{tid}/status")
        assert s == 200
        assert body["status"] == "done"
        assert body["loop_status"] == "ok"
        assert body["loop_reason"] == "task is done"
        # Non-running → duration_s is 0 (UI uses started_at for display)
        assert body["duration_s"] == 0
    finally:
        _delete_task(tid)
        _delete_project(pid)


def test_status_200_running_no_liveness_within_grace():
    """A just-started running task with no liveness data yet (we
    set last_liveness_at to 5s ago to stay inside the supervisor
    grace window; the test verifies the 'no liveness' branch
    coverage via the dedicated unit test in test_loop_status.py).
    This endpoint-level test verifies the JSON shape."""
    pid = _create_project()
    now = datetime.now(timezone.utc).timestamp()
    tid = _insert_task(
        pid,
        status="running",
        started_at=_iso(now - 5),
        last_liveness_at=_iso(now - 0),  # just polled, would be ok
    )
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/{tid}/status")
        assert s == 200
        # 0s liveness → ok, not unknown
        assert body["loop_status"] == "ok"
        assert body["last_event_age_s"] == 0
    finally:
        _delete_task(tid)
        _delete_project(pid)


def test_status_response_includes_name_and_agent_role():
    """The frontend uses name + agent_role for display. Verify
    they are present in the response."""
    pid = _create_project()
    now = datetime.now(timezone.utc).timestamp()
    tid = _insert_task(
        pid,
        status="running",
        started_at=_iso(now - 10),
        last_liveness_at=_iso(now - 1),
        name="fetch-data",
    )
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/{tid}/status")
        assert s == 200
        assert body["name"] == "fetch-data"
        assert body["agent_role"] == "super"
    finally:
        _delete_task(tid)
        _delete_project(pid)


# ===== GET /api/projects/{id}/tasks/running =====


def test_running_list_404_unknown_project():
    s, _ = _http("GET", "/api/projects/proj-fake/tasks/running")
    assert s == 404


def test_running_list_empty_when_no_running_tasks():
    pid = _create_project()
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/running")
        assert s == 200
        assert body["project_id"] == pid
        # count is the COUNT for THIS project; could be > 0 if other
        # running tasks exist globally — but no other running tasks
        # were inserted for this project.
        tasks_in_project = [
            t for t in body["tasks"] if t["project_id"] == pid
        ]
        assert tasks_in_project == []
    finally:
        _delete_project(pid)


def test_running_list_filters_to_running_only():
    """The list endpoint should return only status='running' tasks,
    not pending/done/failed/cancelled."""
    pid = _create_project()
    now = datetime.now(timezone.utc).timestamp()
    # One running + one done + one pending
    tid_run = _insert_task(
        pid, status="running",
        started_at=_iso(now - 30), last_liveness_at=_iso(now - 5),
    )
    tid_done = _insert_task(
        pid, status="done",
        started_at=_iso(now - 100), last_liveness_at=_iso(now - 50),
    )
    tid_pend = _insert_task(pid, status="pending")
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/running")
        assert s == 200
        # Filter to just our tasks (ignore other global running tasks)
        our = [t for t in body["tasks"] if t["project_id"] == pid]
        our_ids = {t["task_id"] for t in our}
        assert tid_run in our_ids
        assert tid_done not in our_ids
        assert tid_pend not in our_ids
        # The running one we inserted should have loop_status=ok
        running_match = next(t for t in our if t["task_id"] == tid_run)
        assert running_match["status"] == "running"
        assert running_match["loop_status"] == "ok"
    finally:
        for t in (tid_run, tid_done, tid_pend):
            _delete_task(t)
        _delete_project(pid)


def test_running_list_returns_all_running_tasks_with_correct_status():
    """Multiple running tasks get their individual loop_status."""
    pid = _create_project()
    now = datetime.now(timezone.utc).timestamp()
    # Fresh running task
    tid_fresh = _insert_task(
        pid, status="running",
        started_at=_iso(now - 10), last_liveness_at=_iso(now - 2),
        name="fresh",
    )
    # Slow running task
    tid_slow = _insert_task(
        pid, status="running",
        started_at=_iso(now - 60), last_liveness_at=_iso(now - 45),
        name="slow-one",
    )
    # Stuck running task (within supervisor grace)
    tid_stuck = _insert_task(
        pid, status="running",
        started_at=_iso(now - 300), last_liveness_at=_iso(now - 150),
        name="stuck-one",
    )
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/running")
        assert s == 200
        # Filter to our project
        our = {t["name"]: t for t in body["tasks"] if t["project_id"] == pid}
        assert our["fresh"]["loop_status"] == "ok"
        assert our["slow-one"]["loop_status"] == "slow"
        assert our["stuck-one"]["loop_status"] == "stuck"
        assert our["slow-one"]["task_id"] == tid_slow
    finally:
        for t in (tid_fresh, tid_slow, tid_stuck):
            _delete_task(t)
        _delete_project(pid)


def test_running_list_scoped_to_project():
    """Running tasks in project A must not show up under project B."""
    pid_a = _create_project("A")
    pid_b = _create_project("B")
    now = datetime.now(timezone.utc).timestamp()
    tid = _insert_task(
        pid_a, status="running",
        started_at=_iso(now - 10), last_liveness_at=_iso(now - 1),
    )
    try:
        s, body = _http("GET", f"/api/projects/{pid_b}/tasks/running")
        assert s == 200
        # No tasks in B
        b_tasks = [t for t in body["tasks"] if t["project_id"] == pid_b]
        assert b_tasks == []
    finally:
        _delete_task(tid)
        _delete_project(pid_a)
        _delete_project(pid_b)


def test_running_list_ordered_by_started_at():
    """Earlier-started tasks come first (oldest at top)."""
    pid = _create_project()
    now = datetime.now(timezone.utc).timestamp()
    # Insert in non-chronological order
    tid_mid = _insert_task(
        pid, status="running",
        started_at=_iso(now - 30), last_liveness_at=_iso(now - 1),
        name="middle",
    )
    tid_new = _insert_task(
        pid, status="running",
        started_at=_iso(now - 10), last_liveness_at=_iso(now - 1),
        name="newest",
    )
    tid_old = _insert_task(
        pid, status="running",
        started_at=_iso(now - 100), last_liveness_at=_iso(now - 1),
        name="oldest",
    )
    try:
        s, body = _http("GET", f"/api/projects/{pid}/tasks/running")
        assert s == 200
        our = [t for t in body["tasks"] if t["project_id"] == pid]
        names = [t["name"] for t in our]
        assert names == ["oldest", "middle", "newest"]
    finally:
        for t in (tid_mid, tid_new, tid_old):
            _delete_task(t)
        _delete_project(pid)
