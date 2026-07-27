"""Tests for POST /api/projects/{id}/plan/run (Phase B, 2026-07-27).

The endpoint materializes a plan into actual tasks. Covers:
  - Happy path: plan with N steps → N pending tasks created,
    state set to 'ready'
  - depends_on resolution (plan-internal name -> task id)
  - depends_on resolution (project-external name -> existing task id)
  - archive_existing=true archives old non-running tasks
  - archive_existing=false keeps old tasks (additive mode)
  - 404 for unknown project
  - 400 for project with no plan
  - 400 for project with empty plan (no steps)
  - 400 for terminal-state project (completed / cancelled / archived / deleted)
  - Audit: project.plan.ran + per-task task.created events
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


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | str | None]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
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


def _create_test_project() -> str:
    name = f"run-plan-test-{uuid.uuid4().hex[:8]}"
    s, body = _http("POST", "/api/projects/", {"name": name})
    return body["id"]


def _delete_project(pid: str) -> None:
    try:
        _http("DELETE", f"/api/projects/{pid}")
    except Exception:
        pass


def _put_plan(pid: str, plan: dict) -> None:
    s, _ = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
    assert s == 200


# ===== Happy path =====


def test_run_plan_materializes_steps_into_tasks():
    pid = _create_test_project()
    try:
        _put_plan(pid, {
            "version": "1.0",
            "name": "happy-test",
            "steps": [
                {"name": "fetch", "agent_role": "super", "action": "fetch_data"},
                {"name": "process", "agent_role": "super", "action": "process"},
                {"name": "summarize", "agent_role": "super", "action": "summarize"},
            ],
        })
        s, body = _http("POST", f"/api/projects/{pid}/plan/run", {})
        assert s == 200
        assert body["tasks_created"] == 3
        assert body["tasks_archived"] == 0
        assert len(body["task_ids"]) == 3
        assert body["state"] == "ready"
        assert body["plan_name"] == "happy-test"
        # Verify the tasks were actually created
        conn = sqlite3.connect(str(DB_PATH))
        try:
            rows = conn.execute(
                "SELECT id, name, status, agent_role, action, project_id, archived "
                "FROM tasks WHERE project_id = ? AND archived = 0 ORDER BY name",
                (pid,),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 3
        names = {r[1] for r in rows}
        assert names == {"fetch", "process", "summarize"}
        # All are pending (the supervisor's next tick will dispatch)
        for r in rows:
            assert r[2] == "pending"
            assert r[3] == "super"
    finally:
        _delete_project(pid)


def test_run_plan_resolves_plan_internal_depends_on():
    """A step's depends_on that references another step in the same
    plan should resolve to the new task IDs (not project-existing IDs)."""
    pid = _create_test_project()
    try:
        _put_plan(pid, {
            "version": "1.0",
            "name": "dep-test",
            "steps": [
                {"name": "fetch", "agent_role": "super"},
                {"name": "process", "agent_role": "super", "depends_on": ["fetch"]},
                {"name": "summarize", "agent_role": "super", "depends_on": ["fetch", "process"]},
            ],
        })
        s, body = _http("POST", f"/api/projects/{pid}/plan/run", {})
        assert s == 200
        new_ids = body["task_ids"]
        # The new_ids are in plan step order: [fetch, process, summarize]
        fetch_tid, process_tid, summarize_tid = new_ids
        # Verify depends_on was resolved to the NEW task IDs
        conn = sqlite3.connect(str(DB_PATH))
        try:
            rows = conn.execute(
                "SELECT name, depends_on FROM tasks WHERE project_id = ? AND archived = 0 "
                "ORDER BY name",
                (pid,),
            ).fetchall()
        finally:
            conn.close()
        by_name = {r[0]: json.loads(r[1] or "[]") for r in rows}
        assert by_name["fetch"] == []
        assert by_name["process"] == [fetch_tid]
        assert set(by_name["summarize"]) == {fetch_tid, process_tid}
    finally:
        _delete_project(pid)


def test_run_plan_resolves_project_external_depends_on():
    """A plan step depending on a name that matches an existing
    non-archived project task should resolve to that existing task."""
    pid = _create_test_project()
    try:
        # Pre-create a project task with name "shared-fetch"
        s, body = _http("POST", "/api/tasks/", {
            "project_id": pid,
            "name": "shared-fetch",
            "agent_role": "super",
            "action": "fetch",
        })
        assert s == 201
        existing_tid = body["id"]
        # Now put a plan with a step that depends on "shared-fetch"
        _put_plan(pid, {
            "version": "1.0",
            "name": "ext-dep-test",
            "steps": [
                {"name": "use-shared", "agent_role": "super", "depends_on": ["shared-fetch"]},
            ],
        })
        # archive_existing=False so the pre-existing task stays
        # live (the dependency resolver only sees non-archived rows,
        # and default-true would archive the existing task before
        # the new one could depend on it).
        s, body = _http("POST", f"/api/projects/{pid}/plan/run",
                          {"archive_existing": False})
        assert s == 200
        # Verify the new task's depends_on points to the EXISTING task
        new_tid = body["task_ids"][0]
        conn = sqlite3.connect(str(DB_PATH))
        try:
            deps_raw = conn.execute(
                "SELECT depends_on FROM tasks WHERE id = ?",
                (new_tid,),
            ).fetchone()
        finally:
            conn.close()
        deps = json.loads(deps_raw[0] or "[]")
        assert deps == [existing_tid]
    finally:
        _delete_project(pid)


def test_run_plan_with_skill_param():
    """Plan steps with `skill` should propagate as _workflow_skill
    in the task params (same convention as apply-workflow)."""
    pid = _create_test_project()
    try:
        _put_plan(pid, {
            "version": "1.0",
            "name": "skill-test",
            "steps": [
                {"name": "weather", "agent_role": "super", "skill": "weather_api"},
            ],
        })
        s, body = _http("POST", f"/api/projects/{pid}/plan/run", {})
        assert s == 200
        new_tid = body["task_ids"][0]
        conn = sqlite3.connect(str(DB_PATH))
        try:
            params_raw = conn.execute(
                "SELECT params FROM tasks WHERE id = ?",
                (new_tid,),
            ).fetchone()
        finally:
            conn.close()
        params = json.loads(params_raw[0] or "{}")
        assert params.get("_workflow_skill") == "weather_api"
    finally:
        _delete_project(pid)


# ===== archive_existing behavior =====


def test_run_plan_archives_existing_tasks_by_default():
    pid = _create_test_project()
    try:
        # Pre-create some non-archived tasks
        for n in ["old-a", "old-b"]:
            s, _ = _http("POST", "/api/tasks/", {
                "project_id": pid, "name": n, "agent_role": "super",
                "action": "noop",
            })
            assert s == 201
        # Set a plan with 1 step
        _put_plan(pid, {
            "version": "1.0", "name": "archive-test",
            "steps": [{"name": "new-step", "agent_role": "super"}],
        })
        s, body = _http("POST", f"/api/projects/{pid}/plan/run", {})
        assert s == 200
        assert body["tasks_archived"] == 2
        assert body["tasks_created"] == 1
        # Verify old tasks are archived, new one is not
        conn = sqlite3.connect(str(DB_PATH))
        try:
            old_rows = conn.execute(
                "SELECT archived FROM tasks WHERE project_id = ? AND name LIKE 'old-%'",
                (pid,),
            ).fetchall()
            new_row = conn.execute(
                "SELECT archived FROM tasks WHERE id = ?",
                (body["task_ids"][0],),
            ).fetchone()
        finally:
            conn.close()
        assert all(r[0] == 1 for r in old_rows)
        assert new_row[0] == 0
    finally:
        _delete_project(pid)


def test_run_plan_keeps_old_tasks_when_archive_existing_false():
    pid = _create_test_project()
    try:
        s, _ = _http("POST", "/api/tasks/", {
            "project_id": pid, "name": "keep-me", "agent_role": "super",
            "action": "noop",
        })
        assert s == 201
        _put_plan(pid, {
            "version": "1.0", "name": "additive-test",
            "steps": [{"name": "new-step", "agent_role": "super"}],
        })
        s, body = _http("POST", f"/api/projects/{pid}/plan/run",
                          {"archive_existing": False})
        assert s == 200
        assert body["tasks_archived"] == 0
        assert body["tasks_created"] == 1
        # Both old and new should be non-archived
        conn = sqlite3.connect(str(DB_PATH))
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND archived = 0",
                (pid,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 2  # old + new
    finally:
        _delete_project(pid)


def test_run_plan_does_not_archive_running_tasks():
    """A running task should NOT be archived by /plan/run — that
    would orphan the agent. The supervisor's _drive_project will
    let it finish naturally."""
    pid = _create_test_project()
    try:
        # Create a task and force it to 'running' (bypass supervisor)
        s, body = _http("POST", "/api/tasks/", {
            "project_id": pid, "name": "running-task", "agent_role": "super",
            "action": "noop",
        })
        assert s == 201
        tid = body["id"]
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
            conn.commit()
        finally:
            conn.close()
        # Put a plan + run
        _put_plan(pid, {
            "version": "1.0", "name": "running-test",
            "steps": [{"name": "x", "agent_role": "super"}],
        })
        s, body = _http("POST", f"/api/projects/{pid}/plan/run", {})
        assert s == 200
        assert body["tasks_archived"] == 0  # running task not archived
        # Verify it's still running, not archived
        conn = sqlite3.connect(str(DB_PATH))
        try:
            r = conn.execute(
                "SELECT status, archived FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        finally:
            conn.close()
        assert r[0] == "running"
        assert r[1] == 0
    finally:
        _delete_project(pid)


# ===== Error cases =====


def test_run_plan_404_for_unknown_project():
    s, body = _http("POST", "/api/projects/does-not-exist/plan/run", {})
    assert s == 404


def test_run_plan_400_when_no_plan():
    pid = _create_test_project()
    try:
        # No PUT /plan call — plan_json is NULL
        s, body = _http("POST", f"/api/projects/{pid}/plan/run", {})
        assert s == 400
        assert "no plan" in str(body).lower()
    finally:
        _delete_project(pid)


def test_run_plan_400_when_plan_has_no_steps():
    pid = _create_test_project()
    try:
        _put_plan(pid, {
            "version": "1.0", "name": "empty", "steps": [],
        })
        s, body = _http("POST", f"/api/projects/{pid}/plan/run", {})
        assert s == 400
        assert "no steps" in str(body).lower()
    finally:
        _delete_project(pid)


def test_run_plan_400_for_terminal_state_project():
    """Terminal-state projects (completed / cancelled) can't have
    plans re-run. User must reset state first."""
    pid = _create_test_project()
    try:
        _put_plan(pid, {
            "version": "1.0", "name": "terminal-test",
            "steps": [{"name": "x", "agent_role": "super"}],
        })
        # Force project to 'completed'
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute("UPDATE projects SET state = 'completed' WHERE id = ?", (pid,))
            conn.commit()
        finally:
            conn.close()
        s, body = _http("POST", f"/api/projects/{pid}/plan/run", {})
        assert s == 400
        assert "completed" in str(body).lower() or "terminal" in str(body).lower()
    finally:
        _delete_project(pid)


# ===== Audit log integration =====


def test_run_plan_writes_audit_log():
    pid = _create_test_project()
    try:
        _put_plan(pid, {
            "version": "1.0", "name": "audit-run-test",
            "steps": [
                {"name": "a", "agent_role": "super"},
                {"name": "b", "agent_role": "super"},
            ],
        })
        s, _ = _http("POST", f"/api/projects/{pid}/plan/run", {})
        assert s == 200
        conn = sqlite3.connect(str(DB_PATH))
        try:
            plan_run = conn.execute(
                "SELECT payload FROM audit_log WHERE project_id = ? "
                "AND event_type = 'project.plan.ran' ORDER BY created_at DESC LIMIT 1",
                (pid,),
            ).fetchone()
            task_created_count = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE project_id = ? "
                "AND event_type = 'task.created' "
                "AND json_extract(payload, '$.source') = 'run_plan'",
                (pid,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert plan_run is not None, "project.plan.ran audit missing"
        payload = json.loads(plan_run[0] or "{}")
        assert payload.get("plan_name") == "audit-run-test"
        assert payload.get("task_count") == 2
        assert task_created_count == 2
    finally:
        _delete_project(pid)


def test_run_plan_sets_project_state_to_ready():
    """After /plan/run, the project state should be 'ready' (so
    the supervisor dispatches the new tasks on next tick)."""
    pid = _create_test_project()
    try:
        _put_plan(pid, {
            "version": "1.0", "name": "state-test",
            "steps": [{"name": "x", "agent_role": "super"}],
        })
        s, _ = _http("POST", f"/api/projects/{pid}/plan/run", {})
        assert s == 200
        conn = sqlite3.connect(str(DB_PATH))
        try:
            state = conn.execute(
                "SELECT state FROM projects WHERE id = ?", (pid,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert state == "ready"
    finally:
        _delete_project(pid)
