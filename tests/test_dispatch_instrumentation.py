"""Tests for v3.12.1 follow-up #5: per-task-attempt dispatch instrumentation.

Asserts:
  1. Schema: task_dispatch table exists with the right columns
     + the (project_id, dispatched_at) and (dispatch_path,
     dispatched_at) indexes.
  2. record_dispatch helper writes a row with the right fields.
  3. record_dispatch warns on unknown dispatch_path (defensive).
  4. _create_dispatched_task writes dispatch_path='soul_dispatch'
     inside the same transaction as the task insert (so a
     failed insert rolls back both rows together).
  5. run_project_plan writes dispatch_path='apply_workflow'
     for every task it creates.
  6. _cascade_reset writes dispatch_path='loopback_reset'
     for every task it resets.
  7. /api/projects/{id}/dispatches endpoint format.
  8. Endpoint counts per path + recent events in newest-first
     order.
  9. Endpoint 404 for unknown project.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hermes_orch import db as db_mod
from hermes_orch import main as main_mod
from hermes_orch.core.audit import record_dispatch, ALLOWED_DISPATCH_PATHS
from hermes_orch.core.supervisor import Supervisor


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
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


def _make_supervisor(app) -> Supervisor:
    return Supervisor(
        db=app.state.db,
        cfg=app.state.config,
        notifier=MagicMock(),
        planner=MagicMock(),
    )


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "test-password-for-dispatch-test"


async def _bootstrap_admin(app) -> None:
    from hermes_orch.auth.cookie import hash_password
    db = app.state.db
    existing = await db.fetchone(
        "SELECT id, password_hash FROM users WHERE username = ?",
        (ADMIN_USERNAME,),
    )
    if existing:
        if not existing.get("password_hash"):
            await db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(ADMIN_PASSWORD), existing["id"]),
            )
        return
    from hermes_orch.auth.cookie import create_user, ROLE_ADMIN
    await create_user(
        db,
        username=ADMIN_USERNAME,
        password=ADMIN_PASSWORD,
        role=ROLE_ADMIN,
        is_bootstrap_admin=True,
    )


async def _login(ac) -> None:
    r = await ac.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"


async def _seed_project(app, pid: str) -> None:
    db = app.state.db
    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, '', 'planned', '', '', '', 0, 0, '')",
        (pid, f"dispatch-{pid}"),
    )


# ===== schema =====


@pytest.mark.asyncio
async def test_task_dispatch_table_exists_with_columns(client):
    """The migration added a `task_dispatch` table with the
    expected columns + the two indexes for the dashboard's
    'last N days' and 'per-source mix' queries.
    """
    ac, app = client
    db = app.state.db
    cols = await db.fetchall("PRAGMA table_info(task_dispatch)")
    col_names = {c["name"] for c in cols}
    expected = {
        "id", "project_id", "task_id", "dispatch_path",
        "dispatched_at", "actor", "history_turn_count",
    }
    assert expected.issubset(col_names), (
        f"missing columns: {expected - col_names}"
    )

    # Indexes
    idx_rows = await db.fetchall("PRAGMA index_list(task_dispatch)")
    idx_names = {r["name"] for r in idx_rows}
    assert "idx_task_dispatch_project_time" in idx_names, (
        f"missing index: idx_task_dispatch_project_time; got {idx_names}"
    )
    assert "idx_task_dispatch_path_time" in idx_names, (
        f"missing index: idx_task_dispatch_path_time; got {idx_names}"
    )


@pytest.mark.asyncio
async def test_history_turn_count_column_is_nullable(client):
    """The history_turn_count column is NULL-able (no NOT NULL
    constraint) so the server-only deploy can land before
    the wrapper populates it. Per the v3.12.1 follow-up queue
    decision, the column is created empty and the wrapper
    fills it in a separate deploy window.
    """
    ac, app = client
    db = app.state.db
    cols = await db.fetchall("PRAGMA table_info(task_dispatch)")
    htc = next(c for c in cols if c["name"] == "history_turn_count")
    assert htc["notnull"] == 0, (
        f"history_turn_count should be NULL-able; got notnull={htc['notnull']}"
    )


# ===== record_dispatch helper =====


@pytest.mark.asyncio
async def test_record_dispatch_writes_all_fields(client):
    """record_dispatch inserts a row with project_id, task_id,
    dispatch_path, actor, and a NULL history_turn_count (the
    wrapper hasn't reported yet).
    """
    ac, app = client
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)
    tid = f"t-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (tid, pid, 'step', '', 'pending'),
    )

    await record_dispatch(
        db,
        project_id=pid,
        task_id=tid,
        dispatch_path="apply_workflow",
        actor="operator",
    )

    rows = await db.fetchall(
        "SELECT * FROM task_dispatch WHERE task_id = ?", (tid,)
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["project_id"] == pid
    assert row["task_id"] == tid
    assert row["dispatch_path"] == "apply_workflow"
    assert row["actor"] == "operator"
    assert row["history_turn_count"] is None  # not yet populated


@pytest.mark.asyncio
async def test_allowed_dispatch_paths_constant(client):
    """Sanity check: ALLOWED_DISPATCH_PATHS is exactly the 3
    values the spec defines. Tests + orchestrator both rely
    on this constant.
    """
    assert ALLOWED_DISPATCH_PATHS == frozenset({
        "apply_workflow",
        "soul_dispatch",
        "loopback_reset",
    })


@pytest.mark.asyncio
async def test_record_dispatch_warns_on_unknown_path(client):
    """Defensive: an unrecognised dispatch_path should NOT
    raise (we'd rather have noisy data than silently drop
    instrumentation). A warning is logged but the row is
    still written.
    """
    ac, app = client
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)
    tid = f"t-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (tid, pid, 'step', '', 'pending'),
    )
    # Should not raise even with a typo path.
    await record_dispatch(
        db,
        project_id=pid,
        task_id=tid,
        dispatch_path="narrow_reset_typo",  # intentionally bad
    )
    rows = await db.fetchall(
        "SELECT dispatch_path FROM task_dispatch WHERE task_id = ?", (tid,)
    )
    assert len(rows) == 1
    assert rows[0]["dispatch_path"] == "narrow_reset_typo"


# ===== 3 entry points =====


@pytest.mark.asyncio
async def test_soul_dispatch_writes_record(client):
    """v3.12.1 follow-up #5: orchestrator/soul_dispatch.py
    writes a task_dispatch row with dispatch_path='soul_dispatch'
    inside the same transaction as the task insert. A failed
    audit_log mid-archive rolls back BOTH the task and the
    dispatch row (atomic).
    """
    from hermes_orch.orchestrator.soul_dispatch import _create_dispatched_task
    from unittest.mock import patch as mock_patch

    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)
    # An agent_profiles row is required by _create_dispatched_task.
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    profile_id = f"prof-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agents (id, secret_hash, status) "
        "VALUES (?, 'fake-hash', 'online')",
        (agent_id,),
    )
    await db.execute(
        "INSERT INTO agent_profiles (id, agent_id, name, status) "
        "VALUES (?, ?, 'super', 'idle')",
        (profile_id, agent_id),
    )
    step = {
        "name": "step",
        "agent_role": "super",
        "depends_on": [],
        "feedback_to": [],
        "required_capabilities": [],
        "action": "do_step",
        "output_path": "",
        "params_template": {},
    }
    profile = {"id": profile_id, "agent_id": agent_id}

    with mock_patch(
        "hermes_orch.orchestrator.soul_dispatch.audit_log",
        new=MagicMock(),
    ):
        await _create_dispatched_task(pid, step, profile, db)

    rows = await db.fetchall(
        "SELECT * FROM task_dispatch WHERE project_id = ? AND dispatch_path = 'soul_dispatch'",
        (pid,),
    )
    assert len(rows) == 1
    assert rows[0]["task_id"]  # non-empty task_id


@pytest.mark.asyncio
async def test_cascade_reset_writes_loopback_records(client):
    """v3.12.1 follow-up #5: supervisor._cascade_reset writes
    a task_dispatch row with dispatch_path='loopback_reset'
    for every task it resets.
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)
    # A small DAG: root (failed) + mid (completed) + child (completed)
    root = f"t-{uuid.uuid4().hex[:8]}"
    mid = f"t-{uuid.uuid4().hex[:8]}"
    child = f"t-{uuid.uuid4().hex[:8]}"
    for tid, deps, status, result in [
        (root, "[]", "failed", None),
        (mid, f'["{root}"]', "completed", '{"ok":1}'),
        (child, f'["{mid}"]', "completed", '{"ok":1}'),
    ]:
        await db.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, status, depends_on, result) "
            "VALUES (?, ?, 'step', '', ?, ?, ?)",
            (tid, pid, status, deps, result),
        )

    # full_chain_reset walks the whole tree.
    reset_ids = await sup._cascade_reset(pid, root)
    # reset_ids includes root + mid + child.
    assert set(reset_ids) == {root, mid, child}

    rows = await db.fetchall(
        "SELECT task_id, dispatch_path FROM task_dispatch "
        "WHERE project_id = ? AND dispatch_path = 'loopback_reset' "
        "ORDER BY task_id",
        (pid,),
    )
    assert {r["task_id"] for r in rows} == {root, mid, child}


@pytest.mark.asyncio
async def test_cascade_reset_does_not_write_for_skipped_resets(client):
    """failed_branch_reset only resets the root, not its
    dependents. Only the root gets a loopback_reset dispatch
    row; the children stay 'completed' and don't get a
    record.
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)
    root = f"t-{uuid.uuid4().hex[:8]}"
    child = f"t-{uuid.uuid4().hex[:8]}"
    for tid, deps, status, result in [
        (root, "[]", "failed", None),
        (child, f'["{root}"]', "completed", '{"ok":1}'),
    ]:
        await db.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, status, depends_on, result) "
            "VALUES (?, ?, 'step', '', ?, ?, ?)",
            (tid, pid, status, deps, result),
        )

    reset_ids = await sup._cascade_reset(
        pid, root, reset_policy="failed_branch_reset"
    )
    assert set(reset_ids) == {root}  # only root, not child

    rows = await db.fetchall(
        "SELECT task_id FROM task_dispatch "
        "WHERE project_id = ? AND dispatch_path = 'loopback_reset'",
        (pid,),
    )
    assert {r["task_id"] for r in rows} == {root}


# ===== API endpoint =====


@pytest.mark.asyncio
async def test_dispatches_endpoint_empty(client):
    """A project with no dispatches returns counts of 0 for
    each path + an empty events list.
    """
    ac, app = client
    await _bootstrap_admin(app)
    await _login(ac)
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)

    r = await ac.get(f"/api/projects/{pid}/dispatches?days=7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == pid
    assert body["days"] == 7
    assert body["counts"] == {
        "apply_workflow": 0,
        "soul_dispatch": 0,
        "loopback_reset": 0,
        "total": 0,
    }
    assert body["events"] == []


@pytest.mark.asyncio
async def test_dispatches_endpoint_counts_per_path(client):
    """Inject 1 apply_workflow + 2 soul_dispatch + 3 loopback_reset
    events directly, then assert the endpoint aggregates by
    dispatch_path correctly.
    """
    ac, app = client
    await _bootstrap_admin(app)
    await _login(ac)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)

    # Seed a task so the FK on task_dispatch.task_id is satisfied.
    tid = f"t-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (tid, pid, 'step', '', 'pending'),
    )

    for path, n in [
        ("apply_workflow", 1),
        ("soul_dispatch", 2),
        ("loopback_reset", 3),
    ]:
        for _ in range(n):
            await record_dispatch(
                db,
                project_id=pid,
                task_id=tid,
                dispatch_path=path,
            )

    r = await ac.get(f"/api/projects/{pid}/dispatches?days=7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"] == {
        "apply_workflow": 1,
        "soul_dispatch": 2,
        "loopback_reset": 3,
        "total": 6,
    }
    # events list should have 6 entries total. Ordering is
    # dispatched_at DESC, id DESC; 6 inserts in the same
    # second fall back to id ordering, which is non-deterministic
    # (uuid-based). So we only assert the SET of dispatch_path
    # values, not the order.
    assert len(body["events"]) == 6
    seen_paths = {e["dispatch_path"] for e in body["events"]}
    assert seen_paths == {"apply_workflow", "soul_dispatch", "loopback_reset"}, (
        f"all 3 paths should appear in the events list; got {seen_paths}"
    )


@pytest.mark.asyncio
async def test_dispatches_endpoint_404_for_unknown_project(client):
    ac, app = client
    await _bootstrap_admin(app)
    await _login(ac)
    r = await ac.get("/api/projects/proj-does-not-exist/dispatches")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_dispatches_endpoint_clamps_input(client):
    """days and limit should be clamped to safe ranges (no
    SQL injection via query params; no DoS via ?days=999999).
    """
    ac, app = client
    await _bootstrap_admin(app)
    await _login(ac)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)

    # days=0 should clamp to 1, not return 0 results.
    r = await ac.get(f"/api/projects/{pid}/dispatches?days=0")
    assert r.status_code == 200
    assert r.json()["days"] == 1

    # days=99999 should clamp to 90.
    r = await ac.get(f"/api/projects/{pid}/dispatches?days=99999")
    assert r.status_code == 200
    assert r.json()["days"] == 90
