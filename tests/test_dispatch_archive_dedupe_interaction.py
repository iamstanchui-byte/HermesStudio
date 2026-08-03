"""Tests for v3.12.1 hardening #2: interaction between the two
duplicate-dispatch defense layers.

v3.12.1 fixes a duplicate-dispatch bug (repro on proj-29b2990d)
with a 2-layer defense:
  1. PRIMARY: `_create_dispatched_task` (orchestrator/soul_dispatch.py)
     archives older same-name live tasks BEFORE inserting the new
     one. Without this, the DB would accumulate ghost rows after
     every loopback reset.
  2. SAFETY NET: `_find_ready_tasks` (core/supervisor.py) uses a
     `NOT EXISTS` subquery to dedupe by `name`, keeping only the
     latest pending task per step name. Even if layer 1 is
     bypassed (a bug, a race, or a future dispatch path that
     forgets to archive), the supervisor never dispatches two
     rows for the same step.

The two layers are tested individually elsewhere:
  - `test_soul_dispatch_archive.py` covers the archive step
  - `test_supervisor_archive_filter.py` covers the dedupe

This file covers the INTERACTION: even when layer 1 is broken or
bypassed, layer 2 keeps the dispatch count to 1 per step name.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hermes_orch import db as db_mod
from hermes_orch import main as main_mod
from hermes_orch.core.supervisor import Supervisor


HK_TZ = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(HK_TZ).isoformat()


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Boot the FastAPI app with a fresh test DB so the schema
    is created via the normal init path. Same pattern as
    test_supervisor_archive_filter.py.
    """
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


async def _seed_two_pending_same_name(app, *, base_time: str | None = None) -> tuple[str, str, str]:
    """Create a project with 2 pending tasks sharing the same
    step name. The newer row has a later `created_at`. Returns
    (project_id, older_task_id, newer_task_id).
    """
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, '', 'planned', '', '', '', 0, 0, '')",
        (pid, f"interaction-{pid}"),
    )
    older = f"t-{uuid.uuid4().hex[:8]}"
    newer = f"t-{uuid.uuid4().hex[:8]}"
    # SQLite CURRENT_TIMESTAMP is 'YYYY-MM-DD HH:MM:SS' in UTC;
    # use a fixed offset so the ordering is deterministic.
    t_older = base_time or "2026-08-03 10:00:00"
    t_newer = "2026-08-03 10:01:00"
    for tid, ts in [(older, t_older), (newer, t_newer)]:
        await db.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, "
            "depends_on, on_parent_failure, status, priority, action, "
            "params, retry_count, max_retries, timeout_seconds, "
            "output_path, required_capability, feedback_to, "
            "is_single_task, archived, created_at) "
            "VALUES (?, ?, 'check-total', '', '[]', 'skip', 'pending', "
            "'normal', 'do_task', '{}', 0, 2, 1800, '', NULL, '[]', "
            "0, 0, ?)",
            (tid, pid, ts),
        )
    return pid, older, newer


# ====================================================================
# Interaction tests
# ====================================================================


@pytest.mark.asyncio
async def test_layer2_dedupe_works_when_layer1_archive_disabled(client):
    """v3.12.1 hardening #2: simulate the bug where layer 1
    (archive) is disabled or broken — the supervisor's NOT
    EXISTS subquery (layer 2) still picks exactly ONE row.

    Setup: 2 pending same-name tasks (older + newer, distinct
    created_at). Even if the dispatcher forgot to archive the
    older one, the supervisor dedupes by `created_at` and only
    dispatches the newer.
    """
    ac, app = client
    sup = _make_supervisor(app)
    pid, older, newer = await _seed_two_pending_same_name(app)

    # Simulate "layer 1 archive disabled": no archive was run.
    # We just confirm the supervisor's view of the world.
    db = app.state.db
    pre_dispatch = await db.fetchall(
        "SELECT id, name, status, archived, created_at FROM tasks "
        "WHERE project_id = ? AND name = 'check-total' "
        "ORDER BY created_at ASC",
        (pid,),
    )
    assert len(pre_dispatch) == 2
    # Both rows are still pending + unarchived (layer 1 did NOT run).
    assert all(t["archived"] == 0 for t in pre_dispatch)
    assert all(t["status"] == "pending" for t in pre_dispatch)

    # Layer 2: supervisor's _find_ready_tasks.
    ready = await sup._find_ready_tasks(pid)
    returned_ids = {r["id"] for r in ready}
    assert returned_ids == {newer}, (
        f"layer 2 dedupe failed: expected only the newer task "
        f"{newer}, got {returned_ids}"
    )
    # The older task is NOT in the ready set, so the supervisor
    # would only dispatch the newer.
    assert older not in returned_ids


@pytest.mark.asyncio
async def test_layer1_archive_then_layer2_returns_one(client):
    """v3.12.1 happy path: layer 1 archives the older task, layer
    2 sees exactly 1 pending task. The two layers agree.

    This is what happens in production: the dispatcher archives
    the old row, inserts a fresh one, the supervisor picks the
    fresh one.
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid, older, newer = await _seed_two_pending_same_name(app)

    # Layer 1: archive the older task (simulating the dispatcher's
    # archive step in _create_dispatched_task).
    await db.execute(
        "UPDATE tasks SET archived = 1 WHERE id = ?",
        (older,),
    )
    # Add a new task (the dispatcher's insert) with the latest
    # created_at.
    fresh = f"t-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, "
        "depends_on, on_parent_failure, status, priority, action, "
        "params, retry_count, max_retries, timeout_seconds, "
        "output_path, required_capability, feedback_to, "
        "is_single_task, archived, created_at) "
        "VALUES (?, ?, 'check-total', '', '[]', 'skip', 'pending', "
        "'normal', 'do_task', '{}', 0, 2, 1800, '', NULL, '[]', "
        "0, 0, '2026-08-03 10:02:00')",
        (fresh, pid),
    )

    # Layer 2: supervisor dedupe.
    ready = await sup._find_ready_tasks(pid)
    returned_ids = {r["id"] for r in ready}
    assert returned_ids == {fresh}, (
        f"happy path failed: expected only the fresh task "
        f"{fresh}, got {returned_ids}"
    )
    # Both the older (archived) and the superseded `newer` should
    # NOT be in the ready set.
    assert older not in returned_ids
    assert newer not in returned_ids


@pytest.mark.asyncio
async def test_layer1_archive_layer2_invoked_via_soul_dispatch(client):
    """v3.12.1 hardening #2 (end-to-end): call the real
    `_create_dispatched_task` (layer 1) twice for the same step
    name, then ask the supervisor (layer 2) what to dispatch.

    Expected: only the SECOND insert is dispatched. The first
    insert got archived by layer 1 in the second call. Layer 2
    is the safety net: if layer 1's archive had failed silently
    (e.g. a transient DB error), the supervisor would still
    return only 1 row.
    """
    from hermes_orch.orchestrator.soul_dispatch import _create_dispatched_task

    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, '', 'planned', '', '', '', 0, 0, '')",
        (pid, f"e2e-{pid}"),
    )

    # Need an agent_profiles row for the dispatch to work.
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agents (id, secret_hash, status) "
        "VALUES (?, 'fake-hash', 'online')",
        (agent_id,),
    )
    profile_id = f"prof-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agent_profiles (id, agent_id, name, status, "
        "current_task_id, capabilities, skills) "
        "VALUES (?, ?, 'super', 'idle', NULL, '{}', '[]')",
        (profile_id, agent_id),
    )
    profile = {"id": profile_id, "agent_id": agent_id}

    step = {
        "name": "check-total",
        "agent_role": "super",
        "depends_on": [],
        "feedback_to": [],
        "required_capabilities": [],
        "action": "do_task",
        "output_path": "",
        "params_template": {},
    }

    with mock.patch(
        "hermes_orch.core.audit.audit_log",
        new=mock.AsyncMock(),
    ):
        # First dispatch — no archive, just insert.
        first = await _create_dispatched_task(pid, step, profile, db)
        # Second dispatch — should archive `first` and insert a fresh row.
        second = await _create_dispatched_task(pid, step, profile, db)

    assert first["id"] != second["id"]

    # The first row is now archived.
    first_row = await db.fetchone(
        "SELECT archived FROM tasks WHERE id = ?", (first["id"],)
    )
    assert first_row["archived"] == 1, (
        f"first task was not archived by layer 1: {first_row}"
    )

    # Layer 2 returns exactly 1 row: the second (latest) insert.
    ready = await sup._find_ready_tasks(pid)
    returned_ids = {r["id"] for r in ready}
    assert returned_ids == {second["id"]}, (
        f"layer 2 returned unexpected set: {returned_ids} "
        f"(expected only {second['id']})"
    )


@pytest.mark.asyncio
async def test_layer2_dedupe_handles_three_duplicates(client):
    """v3.12.1 hardening #2: if layer 1 is broken and THREE
    pending rows share a name, layer 2 still keeps only the
    latest. This is the worst-case scenario (rare but real) and
    guards against any future regression.
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, '', 'planned', '', '', '', 0, 0, '')",
        (pid, f"triple-{pid}"),
    )
    t1, t2, t3 = (
        f"t-{uuid.uuid4().hex[:8]}" for _ in range(3)
    )
    rows = [
        (t1, "2026-08-03 09:00:00"),
        (t2, "2026-08-03 09:01:00"),
        (t3, "2026-08-03 09:02:00"),
    ]
    for tid, ts in rows:
        await db.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, "
            "depends_on, on_parent_failure, status, priority, action, "
            "params, retry_count, max_retries, timeout_seconds, "
            "output_path, required_capability, feedback_to, "
            "is_single_task, archived, created_at) "
            "VALUES (?, ?, 'check-total', '', '[]', 'skip', 'pending', "
            "'normal', 'do_task', '{}', 0, 2, 1800, '', NULL, '[]', "
            "0, 0, ?)",
            (tid, pid, ts),
        )

    # Layer 2: only the latest (t3) is ready.
    ready = await sup._find_ready_tasks(pid)
    returned_ids = {r["id"] for r in ready}
    assert returned_ids == {t3}, (
        f"layer 2 dedupe failed with 3 duplicates: {returned_ids}"
    )
