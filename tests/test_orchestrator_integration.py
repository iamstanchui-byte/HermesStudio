"""Integration tests for the Round-3 dispatch path (v3.9.0).

Covers the supervisor → `orchestrator.soul_dispatch.dispatch_step`
integration end-to-end. The dispatch flow per the design doc
(`docs/soul-routing-design.md` §"Modified files" + §"Migration
plan") is:

  1. Supervisor tick picks up pending workflow step tasks
  2. `_dispatch_via_soul_dispatch` builds a step dict and calls
     `dispatch_step(project_id, step, db)` which:
       - routes via the hybrid routing engine (routing.py)
       - ensures / reuses the project's SOUL preset
       - writes a `profile_configs` row for `soul.md`
       - waits for the wrapper to claim + ack
       - touches the preset's `last_applied_*` columns
       - inserts a NEW tasks row (status=pending) with the
         resolved `assigned_profile_id`
  3. The supervisor marks the ORIGINAL task as 'dispatched' so
     it isn't re-dispatched on subsequent ticks.
  4. The next supervisor tick's `_assign_task` picks up the new
     pending task (with `assigned_profile_id` already set) and
     transitions it to 'assigned'.

Two integration tests cover the happy path and the failure path:

  1. test_full_dispatch_with_soul_apply_via_supervisor
       - registers an agent + profile
       - creates a project with a workflow step
       - triggers the supervisor dispatch path
       - asserts: task is 'dispatched', preset is created,
         profile_configs has the SOUL.md, and the new task has
         the resolved `assigned_profile_id`

  2. test_dispatch_fails_when_no_profile_available
       - sets up a project with a step whose role doesn't match
         any registered profile
       - calls `_dispatch_via_soul_dispatch` directly
       - asserts: `NoProfileAvailable` is raised (the routing
         engine fails with an actionable hint) and the original
         task stays 'pending' (the supervisor's defer-on-no-
         profile semantic)

These tests use a fresh in-process Database (the same shape as
`tests/test_orchestrator_soul_dispatch.py`). A wrapper simulator
acks the `profile_configs` row so `dispatch_step`'s wait-for-ack
returns True (mirrors the live wrapper's behavior).

Pattern cribbed from:
  - tests/test_orchestrator_soul_dispatch.py (DB fixture, helpers)
  - tests/test_max_concurrent_tasks.py (wrapper simulator)
  - tests/test_chatbox_e2e.py (e2e naming conventions)
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from hermes_orch.db import Database
from hermes_orch.core.notifier import Notifier
from hermes_orch.core.planner import Planner
from hermes_orch.core.supervisor import Supervisor
from hermes_orch.orchestrator.routing import NoProfileAvailable
from hermes_orch.orchestrator.soul_dispatch import (
    SoulApplyError,
    dispatch_step,
)
from hermes_orch.utils import now_aware as _now_aware


# ===== Fixtures =====


@pytest_asyncio.fixture
async def db():
    """Fresh in-memory-ish DB per test (tmpfile for clean teardown).

    Same shape as `tests/test_orchestrator_soul_dispatch.py` and
    `tests/test_orchestrator_routing.py` — tempdir-scoped file so
    the path behaves identically to production and the tests don't
    leak state across event loops.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="orch_integration_test_"))
    database = Database(tmpdir / "test.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def _insert_agent(
    db: Database,
    agent_id: str,
    *,
    status: str = "verified",
    heartbeat_offset_s: float = 0.0,
) -> None:
    """Insert a parent `agents` row.

    `heartbeat_offset_s=0` means the heartbeat is "just now" (online
    for the routing engine's 90s window). Pass a positive value to
    simulate a stale wrapper — anything > 90s is considered offline
    by the routing engine.

    The `last_heartbeat_at` is set via `hermes_orch.utils.now_aware`
    so it carries the same timezone offset (Asia/Hong_Kong, +08:00
    in the test environment) that the routing engine uses for its
    cutoff. Mixing UTC and local time in the same ISO-8601 string
    produces lexicographic comparison bugs (UTC = `+00:00` always
    sorts before `+08:00`, so a "fresh" UTC heartbeat is treated as
    "stale" by the local-time cutoff). This caught a Round-3
    integration test on first run — keep this helper in sync with
    the routing engine's `_HEARTBEAT_STALE_S` window (90s) if you
    change one.
    """
    hb_iso = _now_aware().isoformat()
    await db.insert(
        "agents",
        {
            "id": agent_id,
            "secret_hash": "x" * 64,  # NOT NULL placeholder
            "status": status,
            "last_heartbeat_at": hb_iso,
        },
    )


async def _insert_profile(
    db: Database,
    profile_id: str,
    agent_id: str,
    *,
    name: str = "cpi-analyst",
    skills: list[str] | None = None,
    status: str = "idle",
) -> None:
    """Insert an `agent_profiles` row with the v3.9.0 `skills`
    column populated (JSON list of capability tags).
    """
    await db.insert(
        "agent_profiles",
        {
            "id": profile_id,
            "agent_id": agent_id,
            "name": name,
            "status": status,
            "skills": json.dumps(skills if skills is not None else []),
        },
    )


async def _insert_project(
    db: Database,
    project_id: str,
    *,
    state: str = "ready",
) -> None:
    """Insert a project row in the given state. Default 'ready' so
    the supervisor's `_handle_execution` will pick it up.
    """
    await db.insert(
        "projects",
        {
            "id": project_id,
            "name": f"test {project_id}",
            "goal": "test goal",
            "state": state,
        },
    )


async def _insert_workflow_step(
    db: Database,
    task_id: str,
    project_id: str,
    *,
    name: str = "step-1",
    agent_role: str = "cpi-analyst",
    action: str = "analyse_cpi",
    default_soul: str | None = None,
    required_capability: str | None = None,
) -> None:
    """Insert a workflow step task (status=pending, deps=[]) for the
    supervisor's `_find_ready_tasks` to pick up.

    `default_soul` is stashed in the `params` JSON column under
    `default_soul` (the supervisor's `_dispatch_via_soul_dispatch`
    passes `params` as `params_template` to `dispatch_step`, and
    `soul_dispatch._step_default_soul` reads it from there). The
    tasks table doesn't have a top-level `default_soul` column
    yet (that's a v3.9.0 visual-editor addition), so this is the
    round-trip-safe way to exercise the preset auto-populate path.

    `required_capability` (singular) maps to the routing engine's
    `required_capabilities` list (plural) — the supervisor's
    adapter wraps the singular column into a one-element list.
    Setting a `required_capability` that the available profiles
    DON'T have is the standard way to force the routing engine's
    "no match" branch (e.g. for the negative-path test).
    """
    params: dict[str, Any] = {}
    if default_soul:
        params["default_soul"] = default_soul
    await db.insert(
        "tasks",
        {
            "id": task_id,
            "project_id": project_id,
            "name": name,
            "agent_role": agent_role,
            "depends_on": json.dumps([]),
            "on_parent_failure": "skip",
            "status": "pending",
            "priority": "normal",
            "action": action,
            "params": json.dumps(params),
            "required_capability": required_capability,
            "retry_count": 0,
            "max_retries": 2,
            "timeout_seconds": 1800,
        },
    )


async def _wrapper_simulator(db: Database, stop: asyncio.Event) -> None:
    """Background task that acks any `profile_configs` row the
    supervisor submits (Round-3 SOUL apply flow).

    Mirrors `tests/test_max_concurrent_tasks.py:_wrapper_simulator`
    (kept here for self-containment — the integration test file
    should be runnable without depending on test internals). Polls
    every 50ms and flips `pending → applied` for any new row whose
    profile belongs to a verified agent.
    """
    while not stop.is_set():
        try:
            rows = await db.fetchall(
                "SELECT pc.id FROM profile_configs pc "
                "JOIN agent_profiles ap ON ap.id = pc.profile_id "
                "JOIN agents a ON a.id = ap.agent_id "
                "WHERE pc.status = 'pending' AND a.status = 'verified'"
            )
            for r in rows:
                await db.execute(
                    "UPDATE profile_configs SET status = 'applied', "
                    "applied_at = ? WHERE id = ? AND status = 'pending'",
                    ("2026-08-01T00:00:00+00:00", r["id"]),
                )
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05)
        except asyncio.TimeoutError:
            pass


def _make_supervisor(db: Database) -> Supervisor:
    """Build a Supervisor with empty cfg + mock notifier + mock
    planner. We never call .start() — these are direct method
    tests (mirrors `tests/test_cascade.py:_new_supervisor`).
    """
    tmpdir = Path(tempfile.gettempdir())
    cfg: dict[str, Any] = {
        "supervisor": {"poll_interval_seconds": 5},
        "projects": {"storage_root": str(tmpdir)},
        "cleanup": {},
    }
    notifier = Notifier({})  # disabled
    planner = Planner({}, db=db)
    return Supervisor(db, cfg, notifier, planner)


# ===== Test 1: full dispatch with SOUL apply via supervisor =====


@pytest.mark.asyncio
async def test_full_dispatch_with_soul_apply_via_supervisor(db: Database) -> None:
    """End-to-end Round-3 dispatch path:

      1. Register an agent + profile (with capability tags).
      2. Create a project + a workflow step task
         (agent_role='cpi-analyst', pending, no deps).
      3. Spawn a wrapper simulator (so the SOUL apply ack
         succeeds) and call `sup._drive_project(proj)` twice
         (tick 1 routes + applies SOUL + creates the new task;
         tick 2 transitions the new task to 'assigned').
      4. Assert:
         - The original task is 'dispatched' (Round-3 marker,
           not 'pending' and not 'assigned').
         - A new task exists with `assigned_profile_id` set
           to the resolved profile and status='assigned'.
         - The `project_soul_presets` table has a new row for
           the role with the step's `default_soul` as the
           initial content.
         - The `profile_configs` table has a row for `soul.md`
           with the role's composed SOUL (header + content).
    """
    # 1. Register agent + profile
    await _insert_agent(db, "int-agent-1")
    await _insert_profile(
        db,
        "int-profile-1",
        "int-agent-1",
        name="cpi-analyst",
        skills=["python", "pandas", "write_file"],
    )

    # 2. Create project + workflow step
    pid = "int-proj-1"
    await _insert_project(db, pid)
    step_default_soul = (
        "You are a CPI analyst. Use Python to fetch FRED CSV exports "
        "and produce a 1-page markdown report."
    )
    await _insert_workflow_step(
        db,
        "int-step-1",
        pid,
        name="fetch-and-analyse",
        agent_role="cpi-analyst",
        action="analyse_cpi",
        default_soul=step_default_soul,
    )

    # 3. Spawn wrapper simulator + run supervisor dispatch path
    sup = _make_supervisor(db)
    sim_stop = asyncio.Event()
    sim_task = asyncio.create_task(_wrapper_simulator(db, sim_stop))
    try:
        proj = await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,))
        # Tick 1: `_dispatch_via_soul_dispatch` → dispatch_step
        #         (routing + SOUL apply + create new task)
        await sup._drive_project(proj)
        # Tick 2: `_assign_task` picks up the new task (already
        #         has `assigned_profile_id` set) and transitions
        #         it to 'assigned'.
        await sup._drive_project(proj)
    finally:
        sim_stop.set()
        await sim_task

    # 4. Assertions
    rows = await db.fetchall(
        "SELECT id, status, assigned_profile_id, assigned_agent_id, "
        "agent_role, action, name FROM tasks "
        "WHERE project_id = ? ORDER BY created_at", (pid,)
    )
    by_id = {r["id"]: r for r in rows}

    # The original task is marked 'dispatched' (Round-3 marker)
    orig = by_id["int-step-1"]
    assert orig["status"] == "dispatched", (
        f"original task should be 'dispatched' after Round-3 dispatch, "
        f"got {orig['status']!r}"
    )
    # The original task's `assigned_profile_id` is also set (for
    # audit / debugging — the new task is the one the agent runs).
    assert orig["assigned_profile_id"] == "int-profile-1", (
        f"original task's assigned_profile_id should be set for audit, "
        f"got {orig['assigned_profile_id']!r}"
    )

    # A new task exists with the resolved profile + status='assigned'
    new_tasks = [r for r in rows if r["id"] != "int-step-1"]
    assert len(new_tasks) == 1, (
        f"exactly 1 new dispatched task should exist, got {len(new_tasks)}: "
        f"{[r['id'] for r in new_tasks]}"
    )
    new_task = new_tasks[0]
    assert new_task["status"] == "assigned", (
        f"new task should be 'assigned' after tick 2, got {new_task['status']!r}"
    )
    assert new_task["assigned_profile_id"] == "int-profile-1", (
        f"new task's assigned_profile_id should be the resolved profile, "
        f"got {new_task['assigned_profile_id']!r}"
    )
    assert new_task["assigned_agent_id"] == "int-agent-1", (
        f"new task's assigned_agent_id should be the resolved agent "
        f"(for per-agent cap check on the next tick), got "
        f"{new_task['assigned_agent_id']!r}"
    )
    # The new task carries the workflow step's fields (name, action, role)
    assert new_task["agent_role"] == "cpi-analyst"
    assert new_task["action"] == "analyse_cpi"
    assert new_task["name"] == "fetch-and-analyse"

    # `project_soul_presets` has a row for the role
    presets = await db.fetchall(
        "SELECT * FROM project_soul_presets WHERE project_id = ?",
        (pid,),
    )
    assert len(presets) == 1, (
        f"exactly 1 preset should be auto-populated, got {len(presets)}"
    )
    preset = presets[0]
    assert preset["role_name"] == "cpi-analyst"
    assert preset["profile_id"] == "int-profile-1"
    # The content is the step's default_soul (workflow author wins
    # over the generic template on first dispatch).
    assert step_default_soul in (preset["content"] or ""), (
        f"preset content should include the step's default_soul, "
        f"got {preset['content']!r}"
    )
    # The wrapper ack'd the SOUL apply, so last_applied_at is set
    assert preset["last_applied_at"] is not None, (
        f"preset last_applied_at should be set after a successful apply, "
        f"got {preset['last_applied_at']!r}"
    )

    # `profile_configs` has a row for `soul.md` with the role's SOUL
    cfgs = await db.fetchall(
        "SELECT * FROM profile_configs WHERE profile_id = ?",
        ("int-profile-1",),
    )
    assert len(cfgs) == 1, f"exactly 1 soul.md row expected, got {len(cfgs)}"
    cfg = cfgs[0]
    assert cfg["file_path"] == "soul.md"
    assert cfg["status"] == "applied", (
        f"soul.md row should be 'applied' (wrapper acked), "
        f"got {cfg['status']!r}"
    )
    # The composed SOUL has the standard 4-line header + the content
    assert "# ROLE: cpi-analyst" in cfg["desired_content"]
    assert f"# PROJECT: {pid}" in cfg["desired_content"]
    assert "# APPLIED_AT: " in cfg["desired_content"]
    assert "# ----" in cfg["desired_content"]
    assert step_default_soul in cfg["desired_content"]


# ===== Test 2: dispatch fails when no profile available =====


@pytest.mark.asyncio
async def test_dispatch_fails_when_no_profile_available(db: Database) -> None:
    """Negative path: a workflow step with `agent_role='nonexistent-role'`
    has no matching profile. The supervisor's
    `_dispatch_via_soul_dispatch` calls `dispatch_step` which calls
    the routing engine → `NoProfileAvailable`. The supervisor's
    contract is: defer (leave the original task 'pending' for the
    next tick to retry), do NOT mark the task 'failed' — the
    "no profile" case is a transient condition (the operator
    could register a profile later).

    The user's spec (`docs/soul-routing-design.md` Q3) calls for a
    "lazy migration" semantic: warn at run time if `agent_role`
    has no profile, prompt to register. The supervisor's defer
    behavior is the runtime half of that — don't fail the
    workflow; let the next tick try again after the operator
    has had a chance to register a profile.

    Asserts:
      - `dispatch_step` raises `NoProfileAvailable` (routing
        engine's failure mode).
      - The routing engine's `.hint` attribute is populated
        (actionable message — the operator can register a
        profile or add `target_profiles` to the step).
      - The original task stays 'pending' (not 'failed', not
        'dispatched') — the supervisor's defer semantic.
    """
    # Register an agent + profile with a DIFFERENT role so the
    # nonexistent-role step can't accidentally match. We also
    # set a `required_capability` the profile DOESN'T have, so
    # the routing engine's strategy 3 (capability match) can't
    # route to the "researcher" profile either — the only way
    # the step can succeed is with a profile for "nonexistent-
    # role" + the "mt5-fictional-feed" capability, which doesn't
    # exist anywhere.
    await _insert_agent(db, "neg-agent-1")
    await _insert_profile(
        db,
        "neg-profile-1",
        "neg-agent-1",
        name="researcher",  # ≠ "nonexistent-role"
        skills=["python"],
    )

    # Create a project + a step with an unmapped role + capability
    pid = "neg-proj-1"
    await _insert_project(db, pid)
    await _insert_workflow_step(
        db,
        "neg-step-1",
        pid,
        name="do-something-without-a-role",
        agent_role="nonexistent-role",
        action="do_step",
        required_capability="mt5-fictional-feed",  # no profile has this
    )

    # Call dispatch_step directly to verify the routing engine
    # raises the expected exception with an actionable hint.
    # This is the most surgical check — no supervisor / wrapper
    # in the loop, just the routing engine's verdict on an
    # un-mapped role. The step also carries a `required_capability`
    # that the available profile DOESN'T have, so even strategy 3
    # (capability match) can't route the step to the wrong
    # profile — the test would otherwise hit the SOUL apply
    # path and timeout (10s) instead of getting the routing
    # verdict.
    step = {
        "name": "do-something-without-a-role",
        "agent_role": "nonexistent-role",
        "action": "do_step",
        "required_capabilities": ["mt5-fictional-feed"],
    }
    with pytest.raises(NoProfileAvailable) as excinfo:
        await dispatch_step(pid, step, db)

    err = excinfo.value
    # The hint is a non-empty actionable message — the operator
    # can either register a profile with the right role, or add
    # `target_profiles` to the workflow step.
    assert err.hint, "NoProfileAvailable.hint should be populated"
    assert "nonexistent-role" in err.hint or "role" in err.hint.lower(), (
        f"hint should reference the failing role or 'role', got {err.hint!r}"
    )
    # The exception message references the project + role for log
    # correlation.
    assert pid in str(err) or "nonexistent-role" in str(err), (
        f"exception message should mention the project or role, got {err!r}"
    )

    # Also exercise the supervisor's path: the original task
    # should stay 'pending' (defer semantic, not 'failed').
    # The supervisor's `_dispatch_via_soul_dispatch` catches
    # `NoProfileAvailable` and returns False without updating
    # the task's status. (We DON'T add a profile for
    # 'nonexistent-role' here — the point is that the routing
    # engine can't find one and the supervisor defers.)
    sup = _make_supervisor(db)
    proj = await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,))
    await sup._drive_project(proj)

    # Re-read the original task
    orig = await db.fetchone(
        "SELECT id, status, error FROM tasks WHERE id = ?",
        ("neg-step-1",),
    )
    assert orig is not None, "original task should still exist"
    assert orig["status"] == "pending", (
        f"original task should stay 'pending' (defer-on-no-profile), "
        f"got {orig['status']!r}"
    )
    # No error message — the defer semantic is "try again next tick"
    # not "this task is broken".
    assert not orig["error"], (
        f"deferred task should not have an error message, "
        f"got {orig['error']!r}"
    )

    # No preset or profile_configs row was created (dispatch_step
    # raised before reaching the preset/profile creation steps).
    presets = await db.fetchall(
        "SELECT * FROM project_soul_presets WHERE project_id = ?", (pid,)
    )
    assert len(presets) == 0, (
        f"no preset should be created when routing fails, got {len(presets)}"
    )
    cfgs = await db.fetchall(
        "SELECT * FROM profile_configs WHERE profile_id = ?",
        ("neg-profile-1",),
    )
    assert len(cfgs) == 0, (
        f"no profile_configs row should be created when routing fails, "
        f"got {len(cfgs)}"
    )
