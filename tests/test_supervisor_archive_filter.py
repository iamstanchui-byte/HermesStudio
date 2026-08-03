"""Tests for v3.10.7 + v3.12.1 supervisor task filter.

v3.10.7 (2026-08-02): filter `archived = 0` so the supervisor
doesn't re-dispatch archived tasks (re-pro on proj-e8106311).

v3.12.1 (2026-08-03): also dedupe by `name` — when there are
multiple pending tasks with the same step name, only return
the latest. The v3.10.7 fix handled the archive case but
missed the SOUL re-dispatch case (re-pro on proj-29b2990d:
loopback reset both `t-8c7634e3` (old check-total from
apply-workflow) AND `0407f925-...` (new check-total from
SOUL dispatch), then both got dispatched in parallel,
doubling the LLM cost per iteration).

This test asserts:
  1. _find_ready_tasks skips archived pending tasks  (v3.10.7)
  2. _promote_assigned_to_running skips archived assigned tasks (v3.10.7)
  3. Non-archived tasks of the same status are still picked up
     (no regression in the unarchived path)                  (v3.10.7)
  4. When multiple pending tasks share a step name, only the
     LATEST is returned                                       (v3.12.1)
  5. Mixed: unique-name tasks are unaffected, only duplicate-
     named ones get the latest-only filter                    (v3.12.1)
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


# ===== v3.12.1 dedupe by `name` =====


async def _seed_duplicate_name_project(app, *, names: list[str]) -> str:
    """Create a project with N pending tasks per name (created in
    the order given, so later items are the 'newest' instance).

    Returns the project_id. Use `name[i]` as the step name, with
    one task per call. The first call creates the oldest instance
    for that name, the last creates the newest.
    """
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, '', 'planned', '', '', '', 0, 0, '')",
        (pid, f"dedupe-{pid}"),
    )
    for step_name in names:
        # Fresh timestamp per INSERT so created_at is strictly
        # increasing (the dedupe query depends on this — see the
        # NOT EXISTS subquery's `t2.created_at > t.created_at`).
        now = _now_iso()
        tid = f"t-{uuid.uuid4().hex[:8]}"
        await db.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, "
            "depends_on, on_parent_failure, status, priority, action, "
            "params, retry_count, max_retries, timeout_seconds, "
            "output_path, required_capability, feedback_to, "
            "is_single_task, archived, created_at) "
            "VALUES (?, ?, ?, 'super', '', 'skip', 'pending', 'normal', "
            "'do_task', '{}', 0, 2, 1800, '', NULL, '[]', 0, 0, ?)",
            (tid, pid, step_name, now),
        )
        # Tiny async yield so SQLite's CURRENT_TIMESTAMP ticks over
        # (1s precision). Belt-and-suspenders; the explicit `now`
        # above should already differ between inserts.
        import asyncio
        await asyncio.sleep(0.01)
    return pid


@pytest.mark.asyncio
async def test_find_ready_tasks_dedupes_duplicate_names(client):
    """v3.12.1: when 2 pending tasks share a name, only the LATEST
    is returned. Regression: proj-29b2990d (2026-08-03) where
    loopback reset both old `t-8c7634e3` and new `0407f925-...`
    check-total tasks, and the supervisor dispatched BOTH in
    parallel, doubling the LLM cost per iteration."""
    ac, app = client
    sup = _make_supervisor(app)
    # Two pending check-total tasks (same name) plus one add-savings
    pid = await _seed_duplicate_name_project(
        app, names=["check-total", "check-total", "add-savings"],
    )
    # Names in the order: check-total(1), check-total(2), add-savings(3)
    # Expected: only check-total(2) + add-savings(3) returned
    ready = await sup._find_ready_tasks(pid)
    ready_names = [r["name"] for r in ready]
    assert sorted(ready_names) == ["add-savings", "check-total"], (
        f"expected 2 unique names back, got {ready_names}"
    )
    # Verify the check-total that came back is the LATEST instance
    # (highest created_at). The 3 inserts in seed created tids in
    # order; the latest one for check-total is the one we want.
    check_total_rows = [r for r in ready if r["name"] == "check-total"]
    assert len(check_total_rows) == 1
    # And it's the LAST one we inserted (highest created_at among
    # tasks with name='check-total')
    cur = await app.state.db.fetchall(
        "SELECT id, created_at FROM tasks WHERE project_id=? AND "
        "name='check-total' ORDER BY created_at",
        (pid,),
    )
    assert check_total_rows[0]["id"] == cur[-1]["id"], (
        f"expected latest check-total ({cur[-1]['id']}) to be dispatched, "
        f"got {check_total_rows[0]['id']}"
    )


@pytest.mark.asyncio
async def test_find_ready_tasks_three_duplicates_returns_latest_only(client):
    """3+ instances of the same name: only the LATEST is returned."""
    ac, app = client
    sup = _make_supervisor(app)
    pid = await _seed_duplicate_name_project(
        app, names=["step-x", "step-x", "step-x", "step-x"],
    )
    ready = await sup._find_ready_tasks(pid)
    assert len(ready) == 1
    assert ready[0]["name"] == "step-x"
    # Verify it's the last-inserted one
    cur = await app.state.db.fetchall(
        "SELECT id FROM tasks WHERE project_id=? AND name='step-x' "
        "ORDER BY created_at",
        (pid,),
    )
    assert ready[0]["id"] == cur[-1]["id"]


@pytest.mark.asyncio
async def test_find_ready_tasks_mixed_unique_and_duplicate(client):
    """Sanity: dedupe doesn't break unique-name dispatch."""
    ac, app = client
    sup = _make_supervisor(app)
    pid = await _seed_duplicate_name_project(
        app, names=["alpha", "beta", "beta", "gamma"],
    )
    # alpha + gamma are unique, beta has 2 instances (only latest)
    ready = await sup._find_ready_tasks(pid)
    ready_names = sorted(r["name"] for r in ready)
    assert ready_names == ["alpha", "beta", "gamma"]
    # And beta is the latest instance
    beta_rows = [r for r in ready if r["name"] == "beta"]
    cur = await app.state.db.fetchall(
        "SELECT id FROM tasks WHERE project_id=? AND name='beta' "
        "ORDER BY created_at",
        (pid,),
    )
    assert beta_rows[0]["id"] == cur[-1]["id"]


@pytest.mark.asyncio
async def test_find_ready_tasks_archived_dedup_combined(client):
    """Both filters apply: archived rows are skipped AND duplicate
    names are deduped. Mix archived + duplicate + live."""
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = await _seed_duplicate_name_project(
        app, names=["step-a", "step-a"],  # 2 live, both pending
    )
    # Add an archived step-a
    archived_tid = f"t-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, "
        "depends_on, on_parent_failure, status, priority, action, "
        "params, retry_count, max_retries, timeout_seconds, "
        "output_path, required_capability, feedback_to, "
        "is_single_task, archived, created_at) "
        "VALUES (?, ?, 'step-a', 'super', '', 'skip', 'pending', 'normal', "
        "'do_task', '{}', 0, 2, 1800, '', NULL, '[]', 0, 1, ?)",
        (archived_tid, pid, _now_iso()),
    )
    ready = await sup._find_ready_tasks(pid)
    # 2 live + 1 archived for step-a. After filter:
    #   archived skipped (v3.10.7), dup deduped to latest (v3.12.1)
    # → 1 row returned
    assert len(ready) == 1
    assert ready[0]["name"] == "step-a"
    assert ready[0]["archived"] == 0
