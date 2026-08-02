"""Tests for v3.10.7 supervisor archived-task filter.

Context (2026-08-02):
  The supervisor's `_find_ready_tasks` query and
  `_promote_assigned_to_running` query did not filter
  `archived = 0`. Archived tasks are historical rows the user
  explicitly archived (typically when the plan changed via
  PUT /plan or via the API).

  Real-world repro on proj-e8106311 (2026-08-02):
    - User saved a plan, then edited it, which archived the
      first batch of 4 tasks at 18:04:29.
    - User clicked Generate Task at 18:18:00, which created a
      new batch of 4 t-XXX tasks (also pending, also with
      names ram-ssd/step/it-si/step-2).
    - BUT the supervisor's _find_ready_tasks picked up the
      archived batch (status=pending, archived=1) and
      dispatched them, producing duplicate UUID tasks with the
      same step names running alongside the new batch.
    - Visible in the UI as 8f5c96cf ram-ssd (running) +
      8ad469de it-si (running) for step names that already
      had a 18:18 t-XXX task in the new batch.

  Fix: add `AND archived = 0` to the two supervisor queries
  that pick up tasks to act on (`_find_ready_tasks` and
  `_promote_assigned_to_running`). The other supervisor
  queries (count, propagate-failures dependents) are left
  as-is — they're observability queries or only act on
  the unarchived row anyway.

This test asserts:
  1. _find_ready_tasks skips archived pending tasks
  2. _promote_assigned_to_running skips archived assigned tasks
  3. Non-archived tasks of the same status are still picked up
     (no regression in the unarchived path)
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
from hermes_orch.core.supervisor import Supervisor


HK_TZ = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(HK_TZ).isoformat()


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Boot the FastAPI app with a fresh test DB so the schema
    is created via the normal init path."""
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
    """Construct a Supervisor with mock Notifier and Planner
    (neither is needed for the queries under test)."""
    return Supervisor(
        db=app.state.db,
        cfg=app.state.config,
        notifier=MagicMock(),
        planner=MagicMock(),
    )


async def _seed_project_with_tasks(app, *, archived: bool) -> tuple[str, str, str]:
    """Create a project with 2 pending tasks. The first is
    optionally archived. Returns (project_id, archived_task_id, live_task_id)."""
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, '', 'planned', '', '', '', 0, 0, '')",
        (pid, f"archive-filter-{pid}"),
    )
    now = _now_iso()
    archived_tid = f"t-{uuid.uuid4().hex[:8]}"
    live_tid = f"t-{uuid.uuid4().hex[:8]}"
    for tid, archived_flag in [(archived_tid, int(archived)), (live_tid, 0)]:
        await db.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, "
            "depends_on, on_parent_failure, status, priority, action, "
            "params, retry_count, max_retries, timeout_seconds, "
            "output_path, required_capability, feedback_to, "
            "is_single_task, archived, created_at) "
            "VALUES (?, ?, ?, '', '[]', 'skip', 'pending', 'normal', "
            "'do_task', '{}', 0, 2, 1800, '', NULL, '[]', 0, ?, ?)",
            (tid, pid, f"step-{tid}", archived_flag, now),
        )
    return pid, archived_tid, live_tid


# ===== _find_ready_tasks =====


@pytest.mark.asyncio
async def test_find_ready_tasks_skips_archived_pending(client):
    """Archived pending tasks must NOT be returned by
    `_find_ready_tasks` (they're historical, not work to do)."""
    ac, app = client
    sup = _make_supervisor(app)
    pid, archived_tid, live_tid = await _seed_project_with_tasks(
        app, archived=True,
    )

    ready = await sup._find_ready_tasks(pid)
    returned_ids = {r["id"] for r in ready}

    assert archived_tid not in returned_ids, (
        f"archived pending task {archived_tid} was returned — "
        f"supervisor would re-dispatch archived work"
    )
    assert live_tid in returned_ids, (
        f"non-archived pending task {live_tid} was NOT returned — "
        f"regression in the unarchived path"
    )


@pytest.mark.asyncio
async def test_find_ready_tasks_returns_all_when_none_archived(client):
    """Sanity check: with no archived rows, all pending tasks
    come back. Catches a regression where the WHERE clause
    becomes too strict."""
    ac, app = client
    sup = _make_supervisor(app)
    pid, _, live_tid = await _seed_project_with_tasks(
        app, archived=False,
    )

    ready = await sup._find_ready_tasks(pid)
    returned_ids = {r["id"] for r in ready}
    assert live_tid in returned_ids
    # Both rows are pending & unarchived → both come back.
    assert len(ready) == 2


# ===== _promote_assigned_to_running =====


@pytest.mark.asyncio
async def test_promote_assigned_skips_archived_assigned(client):
    """Archived assigned tasks must NOT be promoted to running
    (same rationale as _find_ready_tasks)."""
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, '', 'planned', '', '', '', 0, 0, '')",
        (pid, f"archive-promote-{pid}"),
    )
    now = _now_iso()
    archived_tid = f"t-{uuid.uuid4().hex[:8]}"
    live_tid = f"t-{uuid.uuid4().hex[:8]}"
    for tid, archived in [(archived_tid, 1), (live_tid, 0)]:
        await db.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, "
            "depends_on, on_parent_failure, status, priority, action, "
            "params, retry_count, max_retries, timeout_seconds, "
            "output_path, required_capability, feedback_to, "
            "is_single_task, archived, created_at) "
            "VALUES (?, ?, ?, '', '[]', 'skip', 'assigned', 'normal', "
            "'do_task', '{}', 0, 2, 1800, '', NULL, '[]', 0, ?, ?)",
            (tid, pid, f"step-{tid}", archived, now),
        )

    promoted = await sup._promote_assigned_to_running(pid)

    # Only the live one should be promoted.
    assert promoted == 1
    archived_row = await db.fetchone(
        "SELECT status FROM tasks WHERE id = ?", (archived_tid,),
    )
    live_row = await db.fetchone(
        "SELECT status FROM tasks WHERE id = ?", (live_tid,),
    )
    assert archived_row["status"] == "assigned", (
        f"archived task was promoted to {archived_row['status']!r} — "
        f"should have been left at 'assigned'"
    )
    assert live_row["status"] == "running"
