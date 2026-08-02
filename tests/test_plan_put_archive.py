"""Tests for v3.10.6 PUT /plan archives stale tasks on plan change.

Context (2026-08-02):
  Without this, the user can edit a plan in the visual editor
  (which calls PUT /plan) and the old tasks stay in the DB.
  The supervisor then dispatches them with the old role
  assignments, executing the wrong agent for the new plan
  (proj-c7ad42e6 repro: cost-analysis was "super" then changed
  to "super-b" in the visual editor; the old "super" task
  79c31a24 was still in the DB and was dispatched, producing
  a "weird" result — the super agent just dumped an L1 trace
  summary instead of doing cost analysis).

  The fix: PUT /plan compares the old and new plan's (name, role)
  pairs. If they differ, any non-running, non-archived task whose
  (name, role) isn't in the new plan gets archived. Running
  tasks are untouched (would orphan the agent). Completed
  tasks get archived (their results are stale if the plan
  changed).

This test asserts:
  1. PUT same plan → no archive (idempotent)
  2. PUT with role change → old role task gets archived
  3. PUT with step removed → step's tasks get archived
  4. PUT with step added → no archive (new step is added)
  5. PUT with running task → running task is NOT archived
  6. PUT with completed task → completed task IS archived
  7. PUT first plan (no prior plan) → no archive (nothing to archive)
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Use the local import pattern so the test runs without needing
# the project on PYTHONPATH (the conftest sets it up, but
# being explicit here is safer).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hermes_orch import db as db_mod
from hermes_orch import main as main_mod
from hermes_orch.api.plans import (
    PlanStep,
    ProjectPlan,
    _archive_tasks,
)


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


# ===== Helpers (mirroring test_plan_soul_autoseed.py) =====


async def _bootstrap_admin(app) -> int:
    """Ensure the admin user exists with our known password. Returns
    user_id. The fresh-install bootstrap in db.py creates an admin
    row with no password; this just sets the password we want for
    the tests."""
    from hermes_orch.auth.cookie import (
        ROLE_ADMIN,
        create_user,
        get_user_by_username,
        hash_password,
    )
    db = app.state.db
    existing = await get_user_by_username(db, ADMIN_USERNAME)
    if existing:
        # Update the password to match our test constant.
        await db.execute(
            "UPDATE users SET password_hash = ?, role = ?, disabled = 0 "
            "WHERE id = ?",
            (hash_password(ADMIN_PASSWORD), ROLE_ADMIN, existing["id"]),
        )
        return existing["id"]
    user_id = await create_user(
        db, username=ADMIN_USERNAME, password=ADMIN_PASSWORD,
        role=ROLE_ADMIN,
    )
    return user_id


async def _login_admin(ac: AsyncClient) -> None:
    r = await ac.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert r.status_code in (200, 201), r.text


def _make_plan_json(step_specs: list[dict]) -> dict:
    """Build a ProjectPlan JSON body for /api/projects/{id}/plan PUT."""
    return {
        "plan": {
            "name": "test-plan",
            "steps": step_specs,
        }
    }


async def _create_project_with_profile(app, profile_name: str = "analyst"):
    """Create an agent + profile. Returns (agent_id, profile_id)."""
    db = app.state.db
    from datetime import datetime, timezone, timedelta
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    now_iso = datetime.now(timezone(timedelta(hours=8))).isoformat()
    await db.execute(
        "INSERT INTO agents (id, secret_hash, hmac_secret, status, "
        "last_heartbeat_at, created_at) VALUES (?, '', ?, 'verified', ?, ?)",
        (agent_id, "test-secret", now_iso, now_iso),
    )
    profile_id = f"prof-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agent_profiles (id, agent_id, name, description, status, "
        "capabilities, mcp_servers, storage_refs, skills) "
        "VALUES (?, ?, ?, '', 'idle', '{}', '[]', '[]', '[]')",
        (profile_id, agent_id, profile_name),
    )
    return agent_id, profile_id


async def _create_project_with_tasks(
    app, steps_spec: list[dict], tasks_spec: list[dict],
) -> str:
    """Create a project with a plan and pre-populated tasks.

    steps_spec: list of dicts with name, action, agent_role (for plan)
    tasks_spec: list of dicts with name, agent_role, status, action
        (for pre-populated tasks; archived defaults to 0)
    """
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, 'archive test', '', 'planned', '', '', '', 0, 0, '')",
        (pid,),
    )
    # Save the plan
    plan = ProjectPlan(
        name="test-plan",
        steps=[PlanStep(**s) for s in steps_spec],
    )
    await db.execute(
        "UPDATE projects SET plan_json = ? WHERE id = ?",
        (plan.model_dump_json(), pid),
    )
    # Create pre-populated tasks (so we can test archive behavior
    # without going through the full plan.ran flow)
    for t in tasks_spec:
        tid = "t-" + uuid.uuid4().hex[:8]
        await db.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, status, "
            "action, depends_on, archived, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, '[]', 0, ?, ?)",
            (tid, pid, t["name"], t["agent_role"], t["status"],
             t.get("action", "do_task"), _now_iso(), _now_iso()),
        )
    return pid


def _now_iso() -> str:
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


async def _get_open_tasks(app, pid: str) -> list[dict]:
    db = app.state.db
    return await db.fetchall(
        "SELECT id, name, agent_role, status, archived FROM tasks "
        "WHERE project_id = ? AND archived = 0 ORDER BY name",
        (pid,),
    )


async def _get_all_tasks(app, pid: str) -> list[dict]:
    db = app.state.db
    return await db.fetchall(
        "SELECT id, name, agent_role, status, archived FROM tasks "
        "WHERE project_id = ? ORDER BY name",
        (pid,),
    )


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    orig_db_init = db_mod.Database.__init__

    def patched_db_init(self, db_path):
        orig_db_init(self, test_db)

    monkeypatch.setattr(db_mod.Database, "__init__", patched_db_init)

    app = main_mod.create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        await _bootstrap_admin(app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


# ===== Unit tests for _archive_tasks =====


@pytest.mark.asyncio
async def test_archive_tasks_returns_zero_for_empty_list(client):
    ac, app = client
    await _login_admin(ac)
    archived = await _archive_tasks(app.state.db, [], _now_iso())
    assert archived == 0


@pytest.mark.asyncio
async def test_archive_tasks_skips_running(client):
    """Running tasks must NOT be archived (would orphan the agent)."""
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="super")
    pid = await _create_project_with_tasks(
        app,
        steps_spec=[
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
        ],
        tasks_spec=[
            {"name": "step-a", "agent_role": "super", "status": "running"},
            {"name": "step-a", "agent_role": "super", "status": "completed"},
        ],
    )
    # Archive the stale completed task only
    all_tasks = await _get_all_tasks(app, pid)
    completed_id = next(t["id"] for t in all_tasks if t["status"] == "completed")
    running_id = next(t["id"] for t in all_tasks if t["status"] == "running")
    archived = await _archive_tasks(
        app.state.db, [running_id, completed_id], _now_iso(),
    )
    assert archived == 1
    after = await _get_all_tasks(app, pid)
    running_after = next(t for t in after if t["id"] == running_id)
    completed_after = next(t for t in after if t["id"] == completed_id)
    assert running_after["archived"] == 0  # still open
    assert completed_after["archived"] == 1  # archived


# ===== Integration tests for PUT /plan archive behavior =====


@pytest.mark.asyncio
async def test_put_plan_idempotent_no_archive(client):
    """PUT with the same plan doesn't archive anything."""
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="super")
    pid = await _create_project_with_tasks(
        app,
        steps_spec=[
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
            {"name": "step-b", "action": "do_b", "agent_role": "super"},
        ],
        tasks_spec=[
            {"name": "step-a", "agent_role": "super", "status": "completed"},
            {"name": "step-b", "agent_role": "super", "status": "completed"},
        ],
    )
    # PUT the SAME plan
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
            {"name": "step-b", "action": "do_b", "agent_role": "super"},
        ]),
    )
    assert r.status_code == 200
    # Both tasks should still be open
    open_tasks = await _get_open_tasks(app, pid)
    assert len(open_tasks) == 2
    assert {t["name"] for t in open_tasks} == {"step-a", "step-b"}


@pytest.mark.asyncio
async def test_put_plan_role_change_archives_old_role_task(client):
    """The proj-c7ad42e6 repro: change cost-analysis from 'super' to
    'super-b' in the visual editor. The old 'super' task gets
    archived so the supervisor doesn't dispatch it.

    Note: PUT only ARCHIVES stale tasks. It does NOT create new
    tasks with the new role — that happens when the user clicks
    "Generate tasks" (plan.ran). After PUT, the project has:
    - step-a (super) — still in new plan, kept open
    - step-b (super-b) — NEW role, NO task yet (waits for plan.ran)
    - step-b (super) — OLD role, ARCHIVED (no longer in plan)
    """
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="super-b")
    pid = await _create_project_with_tasks(
        app,
        steps_spec=[
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
            {"name": "step-b", "action": "do_b", "agent_role": "super"},
        ],
        tasks_spec=[
            # Old: step-b with super (the wrong role for the new plan)
            {"name": "step-a", "agent_role": "super", "status": "completed"},
            {"name": "step-b", "agent_role": "super", "status": "completed"},
        ],
    )
    # PUT new plan: step-b now super-b
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
            {"name": "step-b", "action": "do_b", "agent_role": "super-b"},
        ]),
    )
    assert r.status_code == 200
    # After PUT: only step-a (super) is still open. Old step-b (super)
    # is archived. New step-b (super-b) has NO task yet — that
    # comes from plan.ran.
    open_tasks = await _get_open_tasks(app, pid)
    open_keys = {(t["name"], t["agent_role"]) for t in open_tasks}
    assert ("step-a", "super") in open_keys
    assert ("step-b", "super-b") not in open_keys
    # Old step-b super task should be archived
    all_tasks = await _get_all_tasks(app, pid)
    old_step_b = next(t for t in all_tasks if t["name"] == "step-b" and t["agent_role"] == "super")
    assert old_step_b["archived"] == 1
    # step-a still open
    step_a = next(t for t in all_tasks if t["name"] == "step-a")
    assert step_a["archived"] == 0


@pytest.mark.asyncio
async def test_put_plan_step_removed_archives_its_tasks(client):
    """If a step is removed from the plan, its tasks get archived."""
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="super")
    pid = await _create_project_with_tasks(
        app,
        steps_spec=[
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
            {"name": "step-b", "action": "do_b", "agent_role": "super"},
            {"name": "step-c", "action": "do_c", "agent_role": "super"},
        ],
        tasks_spec=[
            {"name": "step-a", "agent_role": "super", "status": "completed"},
            {"name": "step-b", "agent_role": "super", "status": "completed"},
            {"name": "step-c", "agent_role": "super", "status": "completed"},
        ],
    )
    # PUT new plan: step-c removed
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
            {"name": "step-b", "action": "do_b", "agent_role": "super"},
        ]),
    )
    assert r.status_code == 200
    open_tasks = await _get_open_tasks(app, pid)
    assert {t["name"] for t in open_tasks} == {"step-a", "step-b"}
    # step-c is archived
    all_tasks = await _get_all_tasks(app, pid)
    step_c = next(t for t in all_tasks if t["name"] == "step-c")
    assert step_c["archived"] == 1


@pytest.mark.asyncio
async def test_put_plan_step_added_does_not_archive_existing(client):
    """If a step is added to the plan, no archive happens (existing
    tasks still match their steps; the new step has no tasks yet)."""
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="super")
    pid = await _create_project_with_tasks(
        app,
        steps_spec=[
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
        ],
        tasks_spec=[
            {"name": "step-a", "agent_role": "super", "status": "completed"},
        ],
    )
    # PUT new plan: add step-b
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
            {"name": "step-b", "action": "do_b", "agent_role": "super"},
        ]),
    )
    assert r.status_code == 200
    # step-a stays (still in new plan)
    open_tasks = await _get_open_tasks(app, pid)
    assert {t["name"] for t in open_tasks} == {"step-a"}


@pytest.mark.asyncio
async def test_put_plan_running_task_not_archived(client):
    """Running tasks must NOT be archived — the agent is actively
    processing them. Archiving would orphan the agent."""
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="super")
    pid = await _create_project_with_tasks(
        app,
        steps_spec=[
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
        ],
        tasks_spec=[
            {"name": "step-a", "agent_role": "super", "status": "running"},
        ],
    )
    # Change the role of step-a
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "step-a", "action": "do_a", "agent_role": "super-b"},
        ]),
    )
    assert r.status_code == 200
    # Running task should NOT be archived
    all_tasks = await _get_all_tasks(app, pid)
    assert len(all_tasks) == 1
    assert all_tasks[0]["status"] == "running"
    assert all_tasks[0]["archived"] == 0


@pytest.mark.asyncio
async def test_put_plan_completed_task_archived(client):
    """Completed tasks ARE archived (their results are stale if the
    plan changed; the new plan.ran will create fresh tasks)."""
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="super")
    pid = await _create_project_with_tasks(
        app,
        steps_spec=[
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
        ],
        tasks_spec=[
            {"name": "step-a", "agent_role": "super", "status": "completed"},
        ],
    )
    # Change the role
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "step-a", "action": "do_a", "agent_role": "super-b"},
        ]),
    )
    assert r.status_code == 200
    all_tasks = await _get_all_tasks(app, pid)
    assert all_tasks[0]["archived"] == 1


@pytest.mark.asyncio
async def test_put_first_plan_no_archive(client):
    """First PUT (no prior plan) doesn't archive anything — there's
    nothing to archive."""
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="super")
    pid = f"proj-first-{uuid.uuid4().hex[:8]}"
    # No plan yet
    await app.state.db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, 'first plan', '', 'planned', '', '', '', 0, 0, '')",
        (pid,),
    )
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
        ]),
    )
    assert r.status_code == 200
    # No tasks at all
    all_tasks = await _get_all_tasks(app, pid)
    assert all_tasks == []
