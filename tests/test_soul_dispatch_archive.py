"""Tests for v3.12.1 SOUL dispatch archive step.

v3.12.1: when `_create_dispatched_task` runs, it first archives any
older same-name live (non-running) task rows for the same
project. Prevents duplicate dispatch on loopback (repro on
proj-29b2990d: t-8c7634e3 + 0407f925-... both got dispatched).
"""
import asyncio
from unittest import mock

import pytest


class FakeDB:
    """Minimal fake db that records the archive UPDATE and the
    insert in the order they were called, plus a fetchone that
    returns the just-inserted row (mimics the post-insert SELECT
    in `_create_dispatched_task`).
    """
    def __init__(self, existing_tasks):
        self.existing = existing_tasks
        self.calls = []

    async def fetchall(self, sql, params=()):
        self.calls.append(("fetchall", sql, params))
        if "SELECT id, status FROM tasks" in sql and "archived = 0" in sql:
            project_id, step_name = params[0], params[1]
            return [
                {"id": t["id"], "status": t["status"]}
                for t in self.existing
                if t["project_id"] == project_id
                and t["name"] == step_name
                and t.get("archived", 0) == 0
                and t["status"] in (
                    "pending", "dispatched", "assigned", "failed", "skipped"
                )
            ]
        return []

    async def execute(self, sql, params=()):
        self.calls.append(("execute", sql, params))

    async def insert(self, table, row):
        self.calls.append(("insert", table, row))
        self.existing.append({**row, "archived": 0})

    async def fetchone(self, sql, params=()):
        self.calls.append(("fetchone", sql, params))
        if "SELECT * FROM tasks WHERE id = ?" in sql and params:
            target_id = params[0]
            for t in reversed(self.existing):
                if t.get("id") == target_id:
                    return t
        return None


def _make_profile():
    return {"id": "prof-1", "agent_id": "agent-1"}


def _make_step(name="check-total"):
    return {
        "name": name,
        "agent_role": "super",
        "depends_on": ["add-savings"],
        "feedback_to": ["add-savings"],
        "required_capabilities": [],
        "action": "check_threshold",
        "output_path": "",
        "params_template": {"file_path": "/tmp/x"},
    }


@pytest.mark.asyncio
async def test_create_dispatched_task_archives_pending_same_name():
    """v3.12.1: a 'pending' same-name task in the same project gets
    archived before the new task is inserted. Without this, the
    loopback reset would later dispatch both rows in parallel.
    """
    from hermes_orch.orchestrator.soul_dispatch import _create_dispatched_task

    db = FakeDB(existing_tasks=[
        {"id": "t-old", "project_id": "proj-x", "name": "check-total",
         "status": "pending", "archived": 0},
    ])
    with mock.patch(
        "hermes_orch.core.audit.audit_log",
        new=mock.AsyncMock(),
    ) as mock_audit:
        await _create_dispatched_task("proj-x", _make_step(), _make_profile(), db)

    execute_calls = [c for c in db.calls if c[0] == "execute"]
    assert any("UPDATE tasks SET archived = 1" in c[1] for c in execute_calls), (
        f"no archive UPDATE in execute calls: {[c[1][:80] for c in execute_calls]}"
    )
    insert_calls = [c for c in db.calls if c[0] == "insert"]
    assert insert_calls, "new task was not inserted"
    insert_idx = db.calls.index(insert_calls[0])
    archive_idx = next(
        i for i, c in enumerate(db.calls)
        if c[0] == "execute" and "archived = 1" in c[1]
    )
    assert archive_idx < insert_idx, (
        "archive must happen BEFORE insert (so the new task doesn't get "
        "caught in the same archive filter on the next dispatch)"
    )
    assert mock_audit.called, "expected audit_log to be called for archived task"


@pytest.mark.asyncio
async def test_create_dispatched_task_does_not_archive_running():
    """Don't archive a 'running' task — that's an in-flight wrapper
    we don't want to disrupt. The supervisor's v3.12.1 dedupe
    (NOT EXISTS) is the safety net.
    """
    from hermes_orch.orchestrator.soul_dispatch import _create_dispatched_task

    db = FakeDB(existing_tasks=[
        {"id": "t-running", "project_id": "proj-x", "name": "check-total",
         "status": "running", "archived": 0},
    ])
    with mock.patch(
        "hermes_orch.core.audit.audit_log",
        new=mock.AsyncMock(),
    ):
        await _create_dispatched_task("proj-x", _make_step(), _make_profile(), db)

    execute_calls = [c for c in db.calls if c[0] == "execute"]
    archive_updates = [
        c for c in execute_calls if "UPDATE tasks SET archived = 1" in c[1]
    ]
    assert not archive_updates, (
        f"archive fired on a 'running' task — would disrupt in-flight work: "
        f"{archive_updates}"
    )
    insert_calls = [c for c in db.calls if c[0] == "insert"]
    assert insert_calls


@pytest.mark.asyncio
async def test_create_dispatched_task_no_existing_no_op():
    """If no same-name task exists, the archive filter is a no-op
    and the new task is inserted normally.
    """
    from hermes_orch.orchestrator.soul_dispatch import _create_dispatched_task

    db = FakeDB(existing_tasks=[])
    with mock.patch(
        "hermes_orch.core.audit.audit_log",
        new=mock.AsyncMock(),
    ):
        await _create_dispatched_task("proj-x", _make_step(), _make_profile(), db)

    execute_calls = [c for c in db.calls if c[0] == "execute"]
    archive_updates = [
        c for c in execute_calls if "UPDATE tasks SET archived = 1" in c[1]
    ]
    assert not archive_updates
    insert_calls = [c for c in db.calls if c[0] == "insert"]
    assert insert_calls


@pytest.mark.asyncio
async def test_create_dispatched_task_multiple_old_all_archived():
    """3 old live tasks with the same name → all 3 get archived
    in a single UPDATE (with a multi-placeholder IN clause).
    """
    from hermes_orch.orchestrator.soul_dispatch import _create_dispatched_task

    db = FakeDB(existing_tasks=[
        {"id": "t-1", "project_id": "proj-x", "name": "step",
         "status": "pending", "archived": 0},
        {"id": "t-2", "project_id": "proj-x", "name": "step",
         "status": "dispatched", "archived": 0},
        {"id": "t-3", "project_id": "proj-x", "name": "step",
         "status": "failed", "archived": 0},
    ])
    with mock.patch(
        "hermes_orch.core.audit.audit_log",
        new=mock.AsyncMock(),
    ) as mock_audit:
        await _create_dispatched_task(
            "proj-x", _make_step(name="step"), _make_profile(), db
        )

    archive_updates = [
        c for c in db.calls
        if c[0] == "execute" and "UPDATE tasks SET archived = 1" in c[1]
    ]
    assert len(archive_updates) == 1
    sql, params = archive_updates[0][1], archive_updates[0][2]
    # 1 updated_at + 3 IDs in params; 4 question marks total
    assert len(params) == 4
    assert sql.count("?") == 4
    assert mock_audit.call_count == 3
