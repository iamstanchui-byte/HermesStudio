"""Tests for v3.10.10 plan-run max_iterations cap.

Context (2026-08-02):
  The visual workflow Run modal exposes a "Loop-back cap" field so
  the operator can set max_iterations at run time. The visual PLAN
  editor (visual_plan.js generateTasks) used a bare `confirm()`
  dialog with NO way to set the cap, so projects ended up with
  max_iterations=0 and any step.feedback_to silently no-op'd.
  v3.10.10 added max_iterations to RunPlanBody and a Generate
  Tasks modal that lets the operator set the cap.

These tests cover the backend side:
  - POST /api/projects/{id}/plan/run with max_iterations=N updates
    the project's max_iterations column
  - Validation: max_iterations must be >= 0 (negative = 400)
  - When body.max_iterations is None, the project's existing
    max_iterations is left unchanged
  - The cap shows up in the audit log payload
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hermes_orch import db as db_mod
from hermes_orch import main as main_mod
from hermes_orch.api.plans import PlanStep, ProjectPlan
from hermes_orch.auth.cookie import ROLE_ADMIN, create_user, get_user_by_username, hash_password

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"
HK_TZ = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(HK_TZ).isoformat()


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    orig = db_mod.Database.__init__

    def patched(self, db_path):
        orig(self, test_db)

    monkeypatch.setattr(db_mod.Database, "__init__", patched)

    app = main_mod.create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        await _bootstrap_admin(app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


async def _bootstrap_admin(app):
    db = app.state.db
    existing = await get_user_by_username(db, ADMIN_USERNAME)
    if existing:
        await db.execute(
            "UPDATE users SET password_hash = ?, role = ?, disabled = 0 WHERE id = ?",
            (hash_password(ADMIN_PASSWORD), ROLE_ADMIN, existing["id"]),
        )
        return existing["id"]
    return await create_user(db, username=ADMIN_USERNAME,
                              password=ADMIN_PASSWORD, role=ROLE_ADMIN)


async def _login_admin(ac):
    r = await ac.post("/api/auth/login",
                      json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert r.status_code in (200, 201), r.text


async def _create_test_project(app, *, max_iterations: int = 0) -> str:
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, '', 'planned', '', '', '', ?, 0, '')",
        (pid, f"max-iter-test-{pid}", max_iterations),
    )
    return pid


async def _put_plan(ac, pid, steps):
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json={"plan": {"version": "1.0", "name": "t", "steps": steps}},
    )
    assert r.status_code == 200, r.text


async def _get_max_iter(app, pid: str) -> int:
    db = app.state.db
    row = await db.fetchone("SELECT max_iterations FROM projects WHERE id=?", (pid,))
    return int(row["max_iterations"] or 0)


# ===== Happy path =====


@pytest.mark.asyncio
async def test_run_plan_sets_max_iterations_when_provided(client):
    """v3.10.10: when body.max_iterations is set, the project
    column is updated to that value. Pre-fill default 3 matches
    the workflow Run modal's default."""
    ac, app = client
    await _login_admin(ac)
    pid = await _create_test_project(app, max_iterations=0)
    await _put_plan(ac, pid, [
        {"name": "step-a", "action": "do_a", "agent_role": "super"},
    ])
    r = await ac.post(
        f"/api/projects/{pid}/plan/run",
        json={"archive_existing": True, "name_suffix": "", "max_iterations": 3},
    )
    assert r.status_code == 200, r.text
    assert (await _get_max_iter(app, pid)) == 3


@pytest.mark.asyncio
async def test_run_plan_zero_disables_loopback(client):
    """0 is a legitimate opt-out (any step.feedback_to becomes a
    no-op). The backend must accept it and update the column."""
    ac, app = client
    await _login_admin(ac)
    pid = await _create_test_project(app, max_iterations=3)
    await _put_plan(ac, pid, [
        {"name": "step-a", "action": "do_a", "agent_role": "super"},
    ])
    r = await ac.post(
        f"/api/projects/{pid}/plan/run",
        json={"archive_existing": True, "name_suffix": "", "max_iterations": 0},
    )
    assert r.status_code == 200, r.text
    assert (await _get_max_iter(app, pid)) == 0


@pytest.mark.asyncio
async def test_run_plan_omitted_max_iterations_leaves_existing(client):
    """When body.max_iterations is None (the field is omitted),
    the project's existing max_iterations is preserved. This is
    the regression-safe default — re-running a plan shouldn't
    silently change the loop-back cap."""
    ac, app = client
    await _login_admin(ac)
    pid = await _create_test_project(app, max_iterations=5)
    await _put_plan(ac, pid, [
        {"name": "step-a", "action": "do_a", "agent_role": "super"},
    ])
    r = await ac.post(
        f"/api/projects/{pid}/plan/run",
        json={"archive_existing": True, "name_suffix": ""},  # no max_iterations
    )
    assert r.status_code == 200, r.text
    # Unchanged from the create-time value
    assert (await _get_max_iter(app, pid)) == 5


# ===== Validation =====


@pytest.mark.asyncio
async def test_run_plan_rejects_negative_max_iterations(client):
    """Negative max_iterations is meaningless (you can't loop -3
    times). Reject with 400 to fail fast at the API boundary."""
    ac, app = client
    await _login_admin(ac)
    pid = await _create_test_project(app, max_iterations=0)
    await _put_plan(ac, pid, [
        {"name": "step-a", "action": "do_a", "agent_role": "super"},
    ])
    r = await ac.post(
        f"/api/projects/{pid}/plan/run",
        json={"archive_existing": True, "name_suffix": "", "max_iterations": -1},
    )
    assert r.status_code == 400, r.text
    detail = r.json().get("detail", "")
    assert "max_iterations" in detail.lower()
    # The column was NOT touched
    assert (await _get_max_iter(app, pid)) == 0


# ===== Audit log =====


@pytest.mark.asyncio
async def test_run_plan_audit_includes_max_iterations_set(client):
    """The plan.ran audit log entry should include the
    max_iterations the operator set, so the audit trail shows
    why feedback_to did or didn't fire later."""
    import json
    ac, app = client
    await _login_admin(ac)
    pid = await _create_test_project(app, max_iterations=0)
    await _put_plan(ac, pid, [
        {"name": "step-a", "action": "do_a", "agent_role": "super"},
    ])
    r = await ac.post(
        f"/api/projects/{pid}/plan/run",
        json={"archive_existing": True, "name_suffix": "", "max_iterations": 5},
    )
    assert r.status_code == 200, r.text
    db = app.state.db
    rows = await db.fetchall(
        "SELECT event_type, payload FROM audit_log WHERE project_id=? "
        "AND event_type='project.plan.ran' ORDER BY rowid DESC LIMIT 1",
        (pid,),
    )
    assert len(rows) == 1
    payload = rows[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload.get("max_iterations_set") == 5
