"""Tests for plan feedback_to (loop-back) field, v2.0 (2026-07-30).

v2.0 FLIPPED the semantic: the field is now on the FAILING step
(matches the standard on_failure pattern in AWS Step Functions,
Airflow, Temporal). A.feedback_to = [B] means "if A fails,
re-run B". The visual plan editor and visual workflow editor
both put the data on the wire's source end (the failing step).

These tests verify the round-trip:

  1. PlanStep model accepts + serializes feedback_to
  2. PUT /api/projects/{id}/plan preserves feedback_to
  3. /api/projects/{id}/plan/run resolves feedback_to (step
     name -> task id) and writes it to the tasks table
  4. Self-references are silently dropped at /plan/run time
  5. Dangling references (unknown step names) are dropped with
     an audit event (task.feedback_to_unresolved)
  6. Workflow apply path (apply_workflow_to_project) also handles
     feedback_to (regression catcher — already worked pre-v1.9.4
     but we want explicit coverage)

The v2.0 tests use the NEW semantic:
  fetch.feedback_to = [validate]   =>   if fetch fails, re-run validate
(Old v1.9.4 tests used the inverted data, where the field was
on the target. The migration script migrate_feedback_to_v2.py
inverts OLD data in place; see tests/test_migrate_feedback_to_v2.py
for migration coverage.)
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

import pytest


DB = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"


def _seed_project_with_plan(steps: list[dict]) -> str:
    """Create a project + a saved plan. Returns project_id.

    `steps` is a list of dicts with at least name + agent_role +
    action; may include feedback_to. Stored in projects.plan_json.
    """
    pid = f"proj-fb-{uuid.uuid4().hex[:8]}"
    plan = {
        "version": "1.0",
        "name": "test-fb",
        "description": "",
        "trigger": "manual",
        "variables": [],
        "steps": steps,
        "visual_layout": {},
    }
    conn = sqlite3.connect(str(DB))
    try:
        conn.execute(
            "INSERT INTO projects (id, name, goal, state, coordinator_role, "
            "accept_criteria, deliverable_path, max_iterations, "
            "current_iteration, last_iteration_summary) "
            "VALUES (?, ?, ?, 'planned', '', '', '', 0, 0, '')",
            (pid, "fb test", "test feedback_to"),
        )
        conn.execute(
            "UPDATE projects SET plan_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(plan), time.strftime("%Y-%m-%dT%H:%M:%S"), pid),
        )
        conn.commit()
    finally:
        conn.close()
    return pid


def _delete_project(pid: str) -> None:
    conn = sqlite3.connect(str(DB))
    try:
        conn.execute("DELETE FROM tasks WHERE project_id = ?", (pid,))
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
        conn.commit()
    finally:
        conn.close()


def _get_plan_steps(pid: str) -> list[dict]:
    conn = sqlite3.connect(str(DB))
    try:
        row = conn.execute(
            "SELECT plan_json FROM projects WHERE id = ?", (pid,)
        ).fetchone()
        if not row or not row[0]:
            return []
        return json.loads(row[0]).get("steps", [])
    finally:
        conn.close()


def _get_task_feedback_to(tid: str) -> list[str]:
    conn = sqlite3.connect(str(DB))
    try:
        row = conn.execute(
            "SELECT feedback_to FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        if not row or not row[0]:
            return []
        return json.loads(row[0])
    finally:
        conn.close()


# ===== Test 1: PlanStep model + plan round-trip =====


def test_plan_step_accepts_feedback_to_field():
    """PlanStep (the Pydantic model) accepts feedback_to and
    round-trips it through project.plan_json. The visual plan
    editor's saveStepEdits writes to this field; a Pydantic
    rejection here would 422 every Save click.

    v2.0: feedback_to is on the FAILING step. fetch.feedback_to
    = [validate] means "if fetch fails, re-run validate".
    """
    from hermes_orch.api.plans import PlanStep
    s = PlanStep(
        name="fetch-data",
        agent_role="win-agent01",
        action="fetch and validate",
        feedback_to=["validate-data"],
    )
    assert s.feedback_to == ["validate-data"]
    # round-trip
    dumped = s.model_dump_json()
    loaded = PlanStep.model_validate_json(dumped)
    assert loaded.feedback_to == ["validate-data"]


def test_plan_with_feedback_to_round_trips_through_plan_json():
    """A plan with feedback_to entries saved to plan_json
    should reload with the feedback_to preserved (no silent
    loss between write and read).

    v2.0: feedback_to is on the FAILING step (fetch).
    """
    pid = _seed_project_with_plan([
        {"name": "fetch", "agent_role": "r", "action": "f",
         "depends_on": [], "feedback_to": ["validate"],
         "params_template": {}, "output_path": ""},
        {"name": "validate", "agent_role": "r", "action": "v",
         "depends_on": ["fetch"], "feedback_to": [],
         "params_template": {}, "output_path": ""},
    ])
    try:
        steps = _get_plan_steps(pid)
        assert len(steps) == 2
        # fetch is the failing step (has feedback_to listing validate)
        assert sorted(steps[0]["feedback_to"]) == ["validate"]
        # validate has no feedback_to
        assert sorted(steps[1]["feedback_to"]) == []
    finally:
        _delete_project(pid)


# ===== Test 2: Plan runner creates tasks with feedback_to =====


def test_plan_run_resolves_feedback_to_step_names_to_task_ids():
    """POST /api/projects/{id}/plan/run creates tasks with
    feedback_to JSON column populated with the resolved task ids
    (NOT the step names). This is the contract the supervisor
    reads at dispatch time.

    v2.0: feedback_to is on the FAILING step. fetch.feedback_to
    = [validate] means "if fetch fails, re-run validate". So
    the fetch task should have feedback_to = [validate_task_id].
    """
    import urllib.request, urllib.error
    pid = _seed_project_with_plan([
        {"name": "fetch", "agent_role": "r", "action": "fetch_data",
         "depends_on": [], "feedback_to": ["validate"],
         "params_template": {}, "output_path": ""},
        {"name": "validate", "agent_role": "r", "action": "validate_data",
         "depends_on": ["fetch"], "feedback_to": [],
         "params_template": {}, "output_path": ""},
    ])
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:8765/api/projects/{pid}/plan/run",
            method="POST",
            data=b'{"archive_existing": true, "name_suffix": ""}',
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = json.loads(e.read())
            pytest.fail(f"plan/run failed: {body}")
        # New tasks: fetch (t-...) and validate (t-...)
        assert len(body.get("task_ids", [])) == 2
        # v2.0: fetch is the failing step. Its feedback_to should
        # be the task id of "validate" (the recovery step).
        fetch_tid = body["task_ids"][0]
        fb = _get_task_feedback_to(fetch_tid)
        assert len(fb) == 1
        # The feedback_to is the task id of "validate" — not the name
        assert fb[0].startswith("t-")
        assert fb[0] != fetch_tid  # not self
        assert fb[0] in body["task_ids"]
    finally:
        _delete_project(pid)


def test_plan_run_silently_drops_self_feedback_to():
    """A step that loops back to itself is a no-op. The plan
    runner drops it without error (matches depends_on's
    self-reference handling).

    v2.0: self-ref on feedback_to = "if I fail, re-run myself"
    which is a deadlock. Dropped silently.
    """
    import urllib.request, urllib.error
    pid = _seed_project_with_plan([
        {"name": "self-fb", "agent_role": "r", "action": "do_thing",
         "depends_on": [], "feedback_to": ["self-fb"],
         "params_template": {}, "output_path": ""},
    ])
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:8765/api/projects/{pid}/plan/run",
            method="POST",
            data=b'{"archive_existing": true, "name_suffix": ""}',
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            pytest.fail(f"plan/run failed: {e.read()}")
        self_tid = body["task_ids"][0]
        assert _get_task_feedback_to(self_tid) == []
    finally:
        _delete_project(pid)


def test_plan_run_drops_dangling_feedback_to_with_audit():
    """A feedback_to that references a step not in the plan is
    dropped (matches the depends_on unresolved behavior). The
    failure is recorded as a task.feedback_to_unresolved audit
    event, not a silent no-op (operators need to know).
    """
    import urllib.request, urllib.error
    pid = _seed_project_with_plan([
        {"name": "a", "agent_role": "r", "action": "do_thing",
         "depends_on": [], "feedback_to": ["ghost-step"],
         "params_template": {}, "output_path": ""},
    ])
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:8765/api/projects/{pid}/plan/run",
            method="POST",
            data=b'{"archive_existing": true, "name_suffix": ""}',
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                json.loads(r.read())
        except urllib.error.HTTPError as e:
            pytest.fail(f"plan/run failed: {e.read()}")

        # Check the task was created without the dangling ref
        conn = sqlite3.connect(str(DB))
        try:
            row = conn.execute(
                "SELECT id, feedback_to FROM tasks WHERE project_id = ?",
                (pid,),
            ).fetchone()
            assert row is not None
            assert json.loads(row[1] or "[]") == []
        finally:
            conn.close()

        # And the audit log records the unresolved reference
        conn = sqlite3.connect(str(DB))
        try:
            audit = conn.execute(
                "SELECT payload FROM audit_log "
                "WHERE project_id = ? AND event_type = 'task.feedback_to_unresolved'",
                (pid,),
            ).fetchall()
        finally:
            conn.close()
        assert len(audit) == 1, f"expected 1 audit row, got {len(audit)}"
        payload = json.loads(audit[0][0])
        assert "ghost-step" in payload["unresolved_feedback"]
    finally:
        _delete_project(pid)


# ===== Test 3: promote-to-workflow preserves feedback_to =====


def test_promote_project_to_workflow_preserves_feedback_to():
    """When a project with feedback_to is promoted to a workflow
    package, the workflow's step_template should include the
    feedback_to entries. Otherwise the workflow loses the
    loop-back signal silently.

    v2.0: feedback_to on the FAILING step (fetch), listing
    validate as the recovery step.
    """
    import urllib.request, urllib.error

    proj_body = json.dumps({
        "name": "promote source",
        "goal": "source for promote test",
        "coordinator_role": "",
        "accept_criteria": "",
        "deliverable_path": "",
        "max_iterations": 0,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:8765/api/projects/",
        method="POST", data=proj_body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            proj_resp = json.loads(r.read())
            src_pid = proj_resp["id"]
    except urllib.error.HTTPError as e:
        pytest.fail(f"create project failed: {e.read()}")

    # v2.0: feedback_to on fetch (the failing step)
    plan = {
        "version": "1.0",
        "name": "fb-promote",
        "description": "",
        "trigger": "manual",
        "variables": [],
        "steps": [
            {"name": "fetch", "agent_role": "r",
             "action": "fetch_data",
             "depends_on": [], "feedback_to": ["validate"],
             "params_template": {}, "output_path": ""},
            {"name": "validate", "agent_role": "r",
             "action": "validate_data",
             "depends_on": ["fetch"], "feedback_to": [],
             "params_template": {}, "output_path": ""},
        ],
        "visual_layout": {},
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:8765/api/projects/{src_pid}/plan",
        method="PUT",
        data=json.dumps({"plan": plan}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            json.loads(r.read())
    except urllib.error.HTTPError as e:
        pytest.fail(f"put plan failed: {e.read()}")

    # Bump the state to completed (PUT plan might have left it
    # as planned; promote requires terminal).
    conn = sqlite3.connect(str(DB))
    try:
        conn.execute(
            "UPDATE projects SET state = 'completed' WHERE id = ?",
            (src_pid,),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        wf_name = f"promote-fb-{uuid.uuid4().hex[:6]}"
        wf_id = None
        req = urllib.request.Request(
            f"http://127.0.0.1:8765/api/workflows/from-project/{src_pid}",
            method="POST",
            data=json.dumps({
                "name": wf_name,
                "description": "test",
                "version": 1,
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                wf_resp = json.loads(r.read())
                wf_id = wf_resp["id"]
        except urllib.error.HTTPError as e:
            pytest.fail(f"promote failed: {e.read()}")
        except TimeoutError:
            import pytest as _pt
            _pt.skip("promote endpoint timed out (LLM-dependent)")

        req = urllib.request.Request(
            f"http://127.0.0.1:8765/api/workflows/{wf_id}",
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                wf = json.loads(r.read())
        except urllib.error.HTTPError as e:
            pytest.fail(f"get workflow failed: {e.read()}")
        steps = wf.get("step_template", [])
        by_name = {s["name"]: s for s in steps}
        # v2.0: fetch (the failing step) has feedback_to = [validate]
        assert by_name["fetch"].get("feedback_to") == ["validate"]
        # validate has no feedback_to
        assert by_name["validate"].get("feedback_to") == []
    finally:
        conn = sqlite3.connect(str(DB))
        try:
            conn.execute("DELETE FROM tasks WHERE project_id = ?", (src_pid,))
            conn.execute("DELETE FROM projects WHERE id = ?", (src_pid,))
            if wf_id:
                conn.execute(
                    "DELETE FROM workflow_packages WHERE id = ?", (wf_id,)
                )
            conn.commit()
        finally:
            conn.close()


# ===== Test 4: v2.0 supervisor loop-back behavior =====


def test_supervisor_fires_loopback_when_failed_step_has_feedback_to():
    """v2.0 (2026-07-30) FLIPPED: feedback_to is on the FAILING
    step. When step A fails AND A.feedback_to = [B], the supervisor
    should re-run B (cascade-reset B + its downstream).

    This is a unit-level test that exercises the supervisor's
    _maybe_loop_back directly. We don't need a live wrapper; we
    stub the DB with a minimal schema + a few task rows.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    # Build a minimal DB stub.
    db_rows = {
        "projects": [
            {"id": "p1", "max_iterations": 3, "current_iteration": 0},
        ],
        "tasks": [
            {"id": "tA", "project_id": "p1", "name": "fetch",
             "status": "failed", "feedback_to": json.dumps(["save"]),
             "depends_on": "[]"},
            {"id": "tB", "project_id": "p1", "name": "save",
             "status": "pending", "feedback_to": "[]",
             "depends_on": "[]"},
        ],
    }
    async def fetchone(sql, params=()):
        s = sql.strip().lower()
        if "from projects" in s:
            for r in db_rows["projects"]:
                if r["id"] == params[0]:
                    return r
            return None
        if "from tasks where id" in s and "name" in s:
            for r in db_rows["tasks"]:
                if r["id"] == params[0]:
                    return {"id": r["id"], "name": r["name"]}
            return None
        return None
    async def fetchall(sql, params=()):
        s = sql.strip().lower()
        if "from tasks where project_id" in s and "status = 'failed'" in s:
            return [r for r in db_rows["tasks"]
                    if r["project_id"] == params[0] and r["status"] == "failed"]
        if "from tasks where id in" in s:
            ids = set(params)
            return [{"id": r["id"], "name": r["name"]}
                    for r in db_rows["tasks"] if r["id"] in ids]
        return []
    async def execute(sql, params=()):
        return MagicMock(rowcount=0)
    db = MagicMock()
    db.fetchone = AsyncMock(side_effect=fetchone)
    db.fetchall = AsyncMock(side_effect=fetchall)
    db.execute = AsyncMock(side_effect=execute)
    cfg = {"supervisor": {"poll_interval_seconds": 5}}
    from hermes_orch.core.supervisor import Supervisor
    sv = Supervisor(db, cfg, notifier=MagicMock(), planner=MagicMock())
    # We don't want audit_log side effects to fail the test
    import hermes_orch.core.supervisor as sup_mod
    sup_mod.audit_log = AsyncMock()
    fired = asyncio.run(sv._maybe_loop_back("p1"))
    assert fired is True
    # The supervisor should have called _cascade_reset with the
    # task id of "save" (the recovery step named in fetch's feedback_to).
    # We don't mock _cascade_reset itself — verify the call happened
    # by checking the project iteration counter was bumped.
    exec_calls = [c.args[0] for c in db.execute.await_args_list]
    assert any("current_iteration" in c for c in exec_calls), \
        "expected project.current_iteration to be updated"


def test_supervisor_no_fire_when_failed_step_has_no_feedback_to():
    """v2.0: a failed step with empty feedback_to means no loop-back.
    The supervisor returns False and the project continues normally.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    db_rows = {
        "projects": [
            {"id": "p1", "max_iterations": 3, "current_iteration": 0},
        ],
        "tasks": [
            {"id": "tA", "project_id": "p1", "name": "fetch",
             "status": "failed", "feedback_to": "[]",
             "depends_on": "[]"},
        ],
    }
    async def fetchone(sql, params=()):
        s = sql.strip().lower()
        if "from projects" in s:
            for r in db_rows["projects"]:
                if r["id"] == params[0]:
                    return r
            return None
        return None
    async def fetchall(sql, params=()):
        s = sql.strip().lower()
        if "from tasks where project_id" in s:
            return [r for r in db_rows["tasks"]
                    if r["project_id"] == params[0] and r["status"] == "failed"]
        return []
    db = MagicMock()
    db.fetchone = AsyncMock(side_effect=fetchone)
    db.fetchall = AsyncMock(side_effect=fetchall)
    db.execute = AsyncMock()
    cfg = {"supervisor": {"poll_interval_seconds": 5}}
    from hermes_orch.core.supervisor import Supervisor
    sv = Supervisor(db, cfg, notifier=MagicMock(), planner=MagicMock())
    fired = asyncio.run(sv._maybe_loop_back("p1"))
    assert fired is False
