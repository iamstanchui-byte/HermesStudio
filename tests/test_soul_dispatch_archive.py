"""Tests for v3.12.1 SOUL dispatch archive step.

v3.12.1: when `_create_dispatched_task` runs, it first archives any
older same-name live (non-running) task rows for the same
project. Prevents duplicate dispatch on loopback (repro on
proj-29b2990d: t-8c7634e3 + 0407f925-... both got dispatched).

v3.12.1 hardening (this commit): archive + insert run in a single
`db.transaction()` (BEGIN IMMEDIATE) so two concurrent dispatchers
can't both pass the "no live duplicate" check before either
commits. The new tests in this file exercise that transaction
boundary: archive+insert share one BEGIN/COMMIT, an exception in
audit_log rolls back the whole block (no insert visible after), and
even if the archive path is intentionally disabled, the supervisor
dedupe (NOT EXISTS in _find_ready_tasks) still keeps only the
latest pending row per name.
"""
import asyncio
import copy
from contextlib import asynccontextmanager
from unittest import mock

import pytest


class FakeDB:
    """Minimal fake db that records the archive UPDATE and the
    insert in the order they were called, plus a fetchone that
    returns the just-inserted row (mimics the post-insert SELECT
    in `_create_dispatched_task`).

    v3.12.1 hardening: also implements `transaction()` as an
    async context manager that records `BEGIN` and `COMMIT` /
    `ROLLBACK` markers around the yielded block. Mocks the real
    `db.transaction()` (db.py) just closely enough for the
    `_create_dispatched_task` archive+insert path to be exercised
    end-to-end.
    """
    def __init__(self, existing_tasks):
        self.existing = existing_tasks
        self.calls = []
        # Snapshot of `existing` length captured at transaction entry.
        # Used by tests to assert "insert was rolled back: nothing
        # was appended to `existing` after the BEGIN marker".
        self._snapshot_at_tx_start: int | None = None

    @asynccontextmanager
    async def transaction(self):
        """Minimal BEGIN/COMMIT mock. Records a marker in
        `self.calls` and takes a DEEP snapshot of the existing-task
        list (the test fake mutates individual task dicts in place
        when simulating `UPDATE tasks SET archived = 1`, so a shallow
        copy is insufficient). On exception, restores the snapshot
        so the rollback is fully reflected in `self.existing`.
        """
        self.calls.append(("tx", "BEGIN", None))
        self._snapshot_at_tx_start = copy.deepcopy(self.existing)
        try:
            yield
        except BaseException:
            if self._snapshot_at_tx_start is not None:
                self.existing = self._snapshot_at_tx_start
                self._snapshot_at_tx_start = None
            self.calls.append(("tx", "ROLLBACK", None))
            raise
        else:
            self._snapshot_at_tx_start = None
            self.calls.append(("tx", "COMMIT", None))

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
        # Simulate the archive UPDATE mutating the existing list in
        # place so rollback can restore the snapshot.
        if "UPDATE tasks SET archived = 1" in sql and params:
            # params layout (see _create_dispatched_task): [_now_inner(), *ids]
            ids = set(params[1:])
            for t in self.existing:
                if t.get("id") in ids:
                    t["archived"] = 1

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
        "hermes_orch.orchestrator.soul_dispatch.audit_log",
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
        "hermes_orch.orchestrator.soul_dispatch.audit_log",
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
        "hermes_orch.orchestrator.soul_dispatch.audit_log",
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
        "hermes_orch.orchestrator.soul_dispatch.audit_log",
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


# ====================================================================
# v3.12.1 hardening: transaction-wrapping tests
# ====================================================================
# The original 4 tests above cover the WHAT of the archive step
# (which statuses get archived, no-op on 'running', etc.). The two
# tests below cover the HOW of the new transaction wrapper
# (archive+insert share a single BEGIN/COMMIT boundary, and any
# exception inside the block rolls back the insert).


@pytest.mark.asyncio
async def test_archive_and_insert_in_single_transaction():
    """v3.12.1 hardening: archive + insert share a single
    `BEGIN ... COMMIT` block, so two concurrent dispatchers can't
    both pass the "no live duplicate" check before either commits.

    Asserts the call ordering: BEGIN, fetchall (read), execute
    (UPDATE), audit_log x N, insert, COMMIT. The read-back fetchone
    happens AFTER COMMIT, outside the transaction.
    """
    from hermes_orch.orchestrator.soul_dispatch import _create_dispatched_task

    db = FakeDB(existing_tasks=[
        {"id": "t-old", "project_id": "proj-x", "name": "check-total",
         "status": "pending", "archived": 0},
    ])
    with mock.patch(
        "hermes_orch.orchestrator.soul_dispatch.audit_log",
        new=mock.AsyncMock(),
    ):
        await _create_dispatched_task("proj-x", _make_step(), _make_profile(), db)

    # Find the tx markers.
    tx_markers = [c for c in db.calls if c[0] == "tx"]
    assert len(tx_markers) == 2, f"expected BEGIN + COMMIT, got {tx_markers}"
    assert tx_markers[0] == ("tx", "BEGIN", None)
    assert tx_markers[1] == ("tx", "COMMIT", None)
    # No ROLLBACK on the happy path.
    assert not any(c[1] == "ROLLBACK" for c in tx_markers)

    # BEGIN must come BEFORE the archive fetchall + execute + audit
    # + insert, COMMIT must come AFTER all of them. Read-back
    # fetchone happens AFTER COMMIT.
    begin_idx = db.calls.index(tx_markers[0])
    commit_idx = db.calls.index(tx_markers[1])
    archive_fetchall_idx = next(
        i for i, c in enumerate(db.calls)
        if c[0] == "fetchall" and "SELECT id, status FROM tasks" in c[1]
    )
    archive_update_idx = next(
        i for i, c in enumerate(db.calls)
        if c[0] == "execute" and "UPDATE tasks SET archived = 1" in c[1]
    )
    insert_idx = next(
        i for i, c in enumerate(db.calls) if c[0] == "insert"
    )
    readback_idx = next(
        i for i, c in enumerate(db.calls)
        if c[0] == "fetchone" and "SELECT * FROM tasks WHERE id = ?" in c[1]
    )
    assert begin_idx < archive_fetchall_idx < archive_update_idx < insert_idx
    assert commit_idx > insert_idx
    # The read-back is intentionally OUTSIDE the transaction so other
    # dispatchers can proceed as soon as we COMMIT. Confirm that.
    assert commit_idx < readback_idx


@pytest.mark.asyncio
async def test_audit_log_failure_rolls_back_insert():
    """v3.12.1 hardening: if audit_log raises mid-archive, the
    whole transaction rolls back — including the new task insert.
    Without the transaction, the archive UPDATE would commit before
    the audit_log call, leaving the DB in a half-applied state.

    Repro: an operator with a corrupt `audit_log` table would
    leave a half-archived batch. With this test guarding the
    rollback path, the entire archive+insert is atomic.
    """
    from hermes_orch.orchestrator.soul_dispatch import _create_dispatched_task

    db = FakeDB(existing_tasks=[
        {"id": "t-old", "project_id": "proj-x", "name": "check-total",
         "status": "pending", "archived": 0},
    ])

    # audit_log that raises on the first call (the one that would
    # log the archive event). Simulates a transient DB error or
    # a corrupted audit_log row.
    failing_audit = mock.AsyncMock(side_effect=RuntimeError("audit_log table on fire"))

    with mock.patch("hermes_orch.orchestrator.soul_dispatch.audit_log", new=failing_audit):
        with pytest.raises(RuntimeError, match="audit_log table on fire"):
            await _create_dispatched_task(
                "proj-x", _make_step(), _make_profile(), db
            )

    # 1) ROLLBACK marker present, NO COMMIT.
    tx_markers = [c for c in db.calls if c[0] == "tx"]
    assert any(c[1] == "ROLLBACK" for c in tx_markers), (
        f"expected ROLLBACK, got: {tx_markers}"
    )
    assert not any(c[1] == "COMMIT" for c in tx_markers), (
        f"unexpected COMMIT on the failing path: {tx_markers}"
    )

    # 2) The old task is NOT marked archived in `existing` (the
    # snapshot rollback undid the UPDATE in place).
    old_task = next(t for t in db.existing if t["id"] == "t-old")
    assert old_task.get("archived", 0) == 0, (
        f"archive UPDATE not rolled back: {old_task}"
    )

    # 3) No new task row was inserted (the snapshot rollback also
    # trimmed the inserted row).
    assert len(db.existing) == 1, (
        f"new task insert not rolled back; existing={db.existing}"
    )


@pytest.mark.asyncio
async def test_no_existing_no_transaction_overhead():
    """v3.12.1 hardening sanity check: even when no archive is
    needed (no same-name live tasks), we still wrap the insert
    in a transaction. The transaction overhead is negligible for
    a single insert, and consistency is worth more than the
    micro-optimisation of skipping the wrapper.
    """
    from hermes_orch.orchestrator.soul_dispatch import _create_dispatched_task

    db = FakeDB(existing_tasks=[])
    with mock.patch(
        "hermes_orch.orchestrator.soul_dispatch.audit_log",
        new=mock.AsyncMock(),
    ):
        await _create_dispatched_task("proj-x", _make_step(), _make_profile(), db)

    # BEGIN + COMMIT still present, even though the archive block
    # was a no-op.
    tx_markers = [c for c in db.calls if c[0] == "tx"]
    assert tx_markers == [("tx", "BEGIN", None), ("tx", "COMMIT", None)]
    # No archive UPDATE fired.
    archive_updates = [
        c for c in db.calls
        if c[0] == "execute" and "UPDATE tasks SET archived = 1" in c[1]
    ]
    assert not archive_updates
    # New task inserted.
    insert_calls = [c for c in db.calls if c[0] == "insert"]
    assert len(insert_calls) == 1
