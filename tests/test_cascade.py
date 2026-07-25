"""Unit tests for the cascading-invalidation + loop-back primitive
introduced in Phase 0 of the visual workflow builder (2026-07-24).

Tests:
  1. _cascade_reset on a 3-step chain (A → B → C): resetting A cascades
     to B and C.
  2. _cascade_reset on a diamond DAG (A → B, A → C, B → D, C → D):
     resetting A cascades to B, C, D, but D is only reset once
     (no double-reset, no infinite loop).
  3. _cascade_reset skips non-terminal tasks (in-flight work stays
     in-flight). B is 'assigned' → not reset.
  4. _maybe_loop_back fires when a failed task is in another task's
     feedback_to: search re-dispatched + cascade resets analyze, audit.
  5. _maybe_loop_back respects max_iterations cap: at cap, no fire;
     project marked failed with human-readable summary.
  6. _maybe_loop_back is a no-op when no task has feedback_to set
     (returns False fast).
  7. _maybe_loop_back handles self-reference silently: step with
     feedback_to containing its own name = no-op.
  8. Integration: workflow package validation accepts feedback_to as a
     valid step field, rejects forward references.
"""
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_orch.db import Database
from hermes_orch.core.notifier import Notifier
from hermes_orch.core.planner import Planner
from hermes_orch.core.supervisor import Supervisor


# ----- helpers -----

async def _new_db() -> Database:
    """Create an in-memory-ish database (temp file for clean teardown)."""
    tmpdir = tempfile.mkdtemp(prefix="cascade_test_")
    db = Database(Path(tmpdir) / "test.db")
    await db.connect()
    return db


def _new_supervisor(db: Database) -> Supervisor:
    """Build a Supervisor with empty cfg + mock notifier/planner.
    We never call .start() — these are direct method tests."""
    cfg = {
        "supervisor": {"poll_interval_seconds": 5},
        "projects": {"storage_root": str(Path(tempfile.gettempdir()))},
        "cleanup": {},
    }
    notifier = Notifier({})  # disabled
    planner = Planner({}, db=db)
    return Supervisor(db, cfg, notifier, planner)


async def _make_project(db: Database, max_iterations: int = 3) -> str:
    """Insert a project row and return its id."""
    pid = "p-test-" + str(hash(db.db_path) & 0xFFFF)
    await db.insert("projects", {
        "id": pid,
        "name": f"test {pid}",
        "goal": "test",
        "state": "running",
        "max_iterations": max_iterations,
        "current_iteration": 0,
    })
    return pid


async def _make_task(
    db: Database,
    project_id: str,
    *,
    name: str,
    agent_role: str = "win-agent01",
    status: str = "completed",
    depends_on: list[str] | None = None,
    feedback_to: list[str] | None = None,
    result: str = "ok",
) -> str:
    tid = f"t-{name}"
    await db.insert("tasks", {
        "id": tid,
        "project_id": project_id,
        "name": name,
        "agent_role": agent_role,
        "action": "do_thing",
        "status": status,
        "depends_on": json.dumps(depends_on or []),
        "feedback_to": json.dumps(feedback_to or []),
        "result": result,
        "started_at": "2026-07-24T00:00:00",
        "ended_at": "2026-07-24T00:01:00",
    })
    return tid


async def _get_task(db: Database, tid: str) -> dict:
    rows = await db.fetchall("SELECT * FROM tasks WHERE id = ?", (tid,))
    return rows[0] if rows else {}


# ----- tests -----

@pytest.mark.asyncio
async def test_cascade_chain():
    """A → B → C. Reset A. All three reset to pending."""
    db = await _new_db()
    try:
        sup = _new_supervisor(db)
        pid = await _make_project(db)
        a = await _make_task(db, pid, name="a", status="completed")
        b = await _make_task(db, pid, name="b", status="completed", depends_on=[a])
        c = await _make_task(db, pid, name="c", status="completed", depends_on=[b])

        reset = await sup._cascade_reset(pid, a)
        assert sorted(reset) == sorted([a, b, c])

        for tid in (a, b, c):
            t = await _get_task(db, tid)
            assert t["status"] == "pending", f"{tid} not reset: {t['status']}"
            assert t["result"] is None
            assert t["error"] is None
            assert t["started_at"] is None
            assert t["ended_at"] is None
            assert t["retry_count"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cascade_diamond():
    """Diamond: A → B, A → C, B → D, C → D. Reset A. B, C, D all
    reset. D is only reset ONCE (no double-reset in BFS)."""
    db = await _new_db()
    try:
        sup = _new_supervisor(db)
        pid = await _make_project(db)
        a = await _make_task(db, pid, name="a", status="completed")
        b = await _make_task(db, pid, name="b", status="completed", depends_on=[a])
        c = await _make_task(db, pid, name="c", status="completed", depends_on=[a])
        d = await _make_task(db, pid, name="d", status="completed", depends_on=[b, c])

        reset = await sup._cascade_reset(pid, a)
        assert sorted(reset) == sorted([a, b, c, d])
        # D must appear exactly once in the reset list (BFS uses seen
        # set, so the second visit via C is a no-op).
        assert reset.count(d) == 1, f"D was reset {reset.count(d)} times: {reset}"

        for tid in (a, b, c, d):
            t = await _get_task(db, tid)
            assert t["status"] == "pending"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cascade_skips_inflight():
    """B is 'assigned' (in-flight). Cascade from A only resets A and C,
    not B (we don't want to kill an in-flight wrapper)."""
    db = await _new_db()
    try:
        sup = _new_supervisor(db)
        pid = await _make_project(db)
        a = await _make_task(db, pid, name="a", status="completed")
        b = await _make_task(db, pid, name="b", status="assigned", depends_on=[a])
        c = await _make_task(db, pid, name="c", status="completed", depends_on=[b])

        reset = await sup._cascade_reset(pid, a)
        # B is non-terminal → not reset (BFS still visits its
        # dependents, so C IS reset, but B itself is left alone).
        assert a in reset
        assert b not in reset
        assert c in reset

        t_b = await _get_task(db, b)
        assert t_b["status"] == "assigned", (
            f"B was reset to {t_b['status']!r}; should be left alone"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cascade_resets_all_terminal_states():
    """A cascade should reset ALL terminal states: completed, failed,
    skipped, cancelled, interrupted. The 'skipped' case is the one that
    bit us: _propagate_failures sets downstream to 'skipped' on
    failure, so cascade must override that."""
    db = await _new_db()
    try:
        sup = _new_supervisor(db)
        pid = await _make_project(db)
        a = await _make_task(db, pid, name="a", status="completed")
        b = await _make_task(db, pid, name="b", status="skipped", depends_on=[a])
        c = await _make_task(db, pid, name="c", status="cancelled", depends_on=[b])
        d = await _make_task(db, pid, name="d", status="interrupted", depends_on=[c])
        e = await _make_task(db, pid, name="e", status="failed", depends_on=[d])

        reset = await sup._cascade_reset(pid, a)
        assert sorted(reset) == sorted([a, b, c, d, e])
        for tid in (a, b, c, d, e):
            t = await _get_task(db, tid)
            assert t["status"] == "pending"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cascade_resets_business_profile():
    """If a profile is busy on a task that gets cascade-reset, the
    profile must be freed (set to idle) so the next tick can reassign."""
    db = await _new_db()
    try:
        sup = _new_supervisor(db)
        pid = await _make_project(db)
        # Set up an agent + profile busy on a task. The agents table
        # is identified by id (no `name` column); the human-readable
        # name lives on agent_profiles.
        await db.insert("agents", {
            "id": "ag-1", "secret_hash": "x",
            "status": "verified",
            "last_heartbeat_at": "2026-07-24T00:00:00",
        })
        await db.insert("agent_profiles", {
            "id": "ap-1", "agent_id": "ag-1", "name": "win-agent01",
            "status": "busy", "current_task_id": "t-a",
        })
        a = await _make_task(db, pid, name="a", status="completed")
        b = await _make_task(db, pid, name="b", status="completed", depends_on=[a])

        await sup._cascade_reset(pid, a)

        # Profile should now be idle + have no current task
        prof = (await db.fetchall(
            "SELECT * FROM agent_profiles WHERE id = ?", ("ap-1",)
        ))[0]
        assert prof["status"] == "idle", (
            f"profile still {prof['status']!r} after cascade"
        )
        assert prof["current_task_id"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_loop_back_fires():
    """audit fails. search.feedback_to = ['audit']. search +
    analyze + audit all reset to pending; iteration counter bumps."""
    db = await _new_db()
    try:
        sup = _new_supervisor(db)
        pid = await _make_project(db, max_iterations=3)
        # search, analyze, audit, deliver all terminal (search completed,
        # analyze completed, audit failed, deliver skipped by propagate)
        search = await _make_task(db, pid, name="search", status="completed",
                                  feedback_to=["audit"])
        analyze = await _make_task(db, pid, name="analyze", status="skipped",
                                   depends_on=[search])
        audit = await _make_task(db, pid, name="audit", status="failed",
                                 depends_on=[analyze])
        deliver = await _make_task(db, pid, name="deliver", status="skipped",
                                   depends_on=[audit])

        fired = await sup._maybe_loop_back(pid)
        assert fired is True

        # search, analyze, audit all reset to pending
        for tid, expected_name in [
            (search, "search"),
            (analyze, "analyze"),
            (audit, "audit"),
        ]:
            t = await _get_task(db, tid)
            assert t["status"] == "pending", (
                f"{expected_name} not reset: {t['status']}"
            )
        # deliver (depends on audit) ALSO reset because of cascade
        t = await _get_task(db, deliver)
        assert t["status"] == "pending", (
            f"deliver not reset (cascade should reach it): {t['status']}"
        )

        # Iteration counter bumped
        proj = (await db.fetchall(
            "SELECT * FROM projects WHERE id = ?", (pid,)
        ))[0]
        assert proj["current_iteration"] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_loop_back_cap():
    """current_iteration == max_iterations. Loop-back must NOT fire.
    Project marked failed with a human-readable summary."""
    db = await _new_db()
    try:
        sup = _new_supervisor(db)
        pid = await _make_project(db, max_iterations=2)
        # Set current_iteration = 2 (already at cap)
        await db.execute(
            "UPDATE projects SET current_iteration = 2 WHERE id = ?", (pid,)
        )
        await _make_task(db, pid, name="search", status="completed",
                         feedback_to=["audit"])
        await _make_task(db, pid, name="audit", status="failed")

        fired = await sup._maybe_loop_back(pid)
        assert fired is False

        # Project state should be 'failed' with a readable summary
        proj = (await db.fetchall(
            "SELECT * FROM projects WHERE id = ?", (pid,)
        ))[0]
        assert proj["state"] == "failed"
        assert "Loop-back cap reached" in proj["last_iteration_summary"]
        assert "audit" in proj["last_iteration_summary"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_loop_back_no_feedback_to():
    """No task has feedback_to set. No-op, returns False, no audit
    events, no DB changes."""
    db = await _new_db()
    try:
        sup = _new_supervisor(db)
        pid = await _make_project(db, max_iterations=3)
        await _make_task(db, pid, name="search", status="completed")
        await _make_task(db, pid, name="analyze", status="failed",
                         depends_on=["t-search"])
        before_iter = (await db.fetchall(
            "SELECT current_iteration FROM projects WHERE id = ?", (pid,)
        ))[0]["current_iteration"]

        fired = await sup._maybe_loop_back(pid)
        assert fired is False

        after_iter = (await db.fetchall(
            "SELECT current_iteration FROM projects WHERE id = ?", (pid,)
        ))[0]["current_iteration"]
        assert after_iter == before_iter
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_loop_back_self_reference_silent_noop():
    """If a task's feedback_to references itself, that's a silent
    no-op (we drop self-refs at task insert time anyway, but the
    supervisor should also tolerate it if a bad row slips through)."""
    db = await _new_db()
    try:
        sup = _new_supervisor(db)
        pid = await _make_project(db, max_iterations=3)
        # audit has feedback_to=['audit'] (self-ref)
        await _make_task(db, pid, name="audit", status="failed",
                         feedback_to=["audit"])

        fired = await sup._maybe_loop_back(pid)
        # No target should match (the only candidate, audit, has a
        # self-ref, which doesn't trigger)
        assert fired is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_loop_back_max_iter_zero_disables_loop():
    """max_iterations = 0 (the default for projects that don't opt in)
    disables loop-back entirely, even if feedback_to is set."""
    db = await _new_db()
    try:
        sup = _new_supervisor(db)
        pid = await _make_project(db, max_iterations=0)
        await _make_task(db, pid, name="search", status="completed",
                         feedback_to=["audit"])
        await _make_task(db, pid, name="audit", status="failed")

        fired = await sup._maybe_loop_back(pid)
        # 0 means disabled, so we don't fire. But we also don't
        # mark the project failed (the user opted out of looping).
        assert fired is False

        proj = (await db.fetchall(
            "SELECT * FROM projects WHERE id = ?", (pid,)
        ))[0]
        # Project state untouched (still 'running')
        assert proj["state"] == "running"
    finally:
        await db.close()


# ----- validator tests (workflows.py) -----

def test_validator_accepts_feedback_to():
    """feedback_to as a list of valid step names is accepted.

    Phase 2 (2026-07-25): feedback_to can reference ANY step in the
    workflow, not just earlier ones. The earlier restriction was wrong —
    the canonical search→analyze→audit loop-back pattern needs
    search.feedback_to=['audit'] (a LATER step) to mean "re-run me
    if audit fails". This is the trigger semantic."""
    from hermes_orch.api.workflows import _validate_workflow_package
    pkg = {
        "description": "test",
        "step_template": [
            {"name": "search", "agent_role": "r1", "action": "a",
             "depends_on": [], "params_template": {}},
            {"name": "analyze", "agent_role": "r1", "action": "a",
             "depends_on": ["search"], "params_template": {}},
            {"name": "audit", "agent_role": "r1", "action": "a",
             "depends_on": ["analyze"], "params_template": {}},
            {"name": "deliver", "agent_role": "r1", "action": "a",
             "depends_on": ["audit"], "params_template": {},
             "feedback_to": ["audit"]},  # re-dispatch deliver if audit fails
        ],
        "variables": [],
    }
    ok, err = _validate_workflow_package(pkg)
    assert ok, f"valid package rejected: {err}"


def test_validator_accepts_forward_feedback_to():
    """Phase 2 (2026-07-25): feedback_to with a FORWARD ref is now
    accepted. The earlier 'must be earlier step' rule was wrong
    (see _validate_workflow_package docstring). The canonical pattern
    is search.feedback_to=['audit'] where 'audit' is a LATER step —
    that's the search→analyze→audit loop-back semantic."""
    from hermes_orch.api.workflows import _validate_workflow_package
    pkg = {
        "description": "test",
        "step_template": [
            {"name": "search", "agent_role": "r1", "action": "a",
             "depends_on": [], "params_template": {},
             "feedback_to": ["audit"]},  # 'audit' is a LATER step, but valid
            {"name": "analyze", "agent_role": "r1", "action": "a",
             "depends_on": ["search"], "params_template": {}},
            {"name": "audit", "agent_role": "r1", "action": "a",
             "depends_on": ["analyze"], "params_template": {}},
        ],
        "variables": [],
    }
    ok, err = _validate_workflow_package(pkg)
    assert ok, f"valid package (forward feedback_to) rejected: {err}"


def test_validator_rejects_nonexistent_feedback_to():
    """feedback_to that references a step that doesn't exist in the
    workflow is rejected (existence check, not order check)."""
    from hermes_orch.api.workflows import _validate_workflow_package
    pkg = {
        "description": "test",
        "step_template": [
            {"name": "search", "agent_role": "r1", "action": "a",
             "depends_on": [], "params_template": {},
             "feedback_to": ["nonexistent-step"]},  # WRONG: doesn't exist
            {"name": "analyze", "agent_role": "r1", "action": "a",
             "depends_on": ["search"], "params_template": {}},
        ],
        "variables": [],
    }
    ok, err = _validate_workflow_package(pkg)
    assert not ok
    assert "feedback_to" in err or "nonexistent-step" in err


def test_validator_rejects_non_list_feedback_to():
    """feedback_to must be a list, not a string or dict."""
    from hermes_orch.api.workflows import _validate_workflow_package
    pkg = {
        "description": "test",
        "step_template": [
            {"name": "search", "agent_role": "r1", "action": "a",
             "depends_on": [], "params_template": {},
             "feedback_to": "audit"},  # WRONG: string, not list
            {"name": "audit", "agent_role": "r1", "action": "a",
             "depends_on": ["search"], "params_template": {}},
        ],
        "variables": [],
    }
    ok, err = _validate_workflow_package(pkg)
    assert not ok
    assert "feedback_to" in err or "list" in err
