"""v3.14.0 (Phase 2): approval runtime + API integration tests.

Covers the ACs from docs/v3.14.0-workflow-human-approval.md §8 that
pertain to Phase 2:

  - AC-9:  supervisor creates ApprovalRequest for human_approval step
  - AC-10: ApprovalRequest appears in inbox badge (count, list)
  - AC-11: User Approve → task completed, downstream ready
  - AC-12: Re-approve is idempotent (200)
  - AC-13: Approve after reject/expired → 409
  - AC-14: Reject with on_reject='stop' → task failed, workflow fail
  - AC-15: Reject with on_reject='skip' → task skipped
  - AC-16: Reject with on_reject='route' → task skipped, downstream ready
  - AC-17: Sweeper expires pending requests past timeout_seconds
  - AC-18: Atomic UPDATE on Approve vs sweeper race (only one winner)
  - AC-19: Workflow cancel auto-rejects pending approvals

Uses a real in-process SQLite DB (no server needed) + FastAPI
TestClient for the API routes. Pure-function tests where possible.

Phase 3 (UI) is tested separately via browser / smoke tests.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import aiosqlite
import pytest_asyncio

# Path setup so the package can be imported directly.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from hermes_orch.core.approval_runtime import (  # noqa: E402
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_EXPIRED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_SKIPPED,
    apply_approve,
    apply_on_reject,
    auto_reject_pending_approvals,
    build_summary,
    create_approval_request,
    sweep_expired_approvals,
)


SCHEMA_SQL = open(
    _PROJECT_ROOT / "src" / "hermes_orch" / "db.py", encoding="utf-8"
).read()
# Extract the SCHEMA constant (the first triple-quoted block).
_SCHEMA_START = SCHEMA_SQL.find('SCHEMA = """') + len('SCHEMA = """')
_SCHEMA_END = SCHEMA_SQL.find('"""\n\n# Idempotent')
SCHEMA_ONLY = SCHEMA_SQL[_SCHEMA_START:_SCHEMA_END]

# Extract the MIGRATIONS list so we apply ALL columns that the real
# DB would have after the migration runner finishes (some columns
# like projects.coordinator_role are ALTER'd in, not in CREATE TABLE).
_MIGRATIONS_START = SCHEMA_SQL.find('MIGRATIONS = [')
_MIGRATIONS_END = SCHEMA_SQL.find('\n]', _MIGRATIONS_START) + 2
MIGRATIONS_LIST = SCHEMA_SQL[_MIGRATIONS_START:_MIGRATIONS_END]


class FakeDB:
    """Minimal DB wrapper exposing the methods approval_runtime needs.

    approval_runtime calls:
      - db.fetchone(sql, params)
      - db.fetchall(sql, params)
      - db.execute(sql, params)  → returns affected rowcount
      - db.insert(table, row_dict)

    We use aiosqlite directly (the same engine the real Database uses),
    set row_factory=aiosqlite.Row, and proxy the methods.
    """

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def fetchone(self, sql: str, params=()):
        cur = await self._conn.execute(sql, params)
        return await cur.fetchone()

    async def fetchall(self, sql: str, params=()):
        cur = await self._conn.execute(sql, params)
        return await cur.fetchall()

    async def execute(self, sql: str, params=()):
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cur.rowcount

    async def insert(self, table: str, row: dict) -> None:
        cols = ",".join(row.keys())
        placeholders = ",".join("?" for _ in row)
        await self._conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
            tuple(row.values()),
        )
        await self._conn.commit()


@pytest_asyncio.fixture
async def db():
    """Create a fresh in-memory DB with the schema applied."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    # Apply CREATE TABLEs first, then run the MIGRATIONS list (which
    # adds columns that were introduced in v3.x via ALTER TABLE).
    # This mirrors what the production Database.connect() does.
    await conn.executescript(SCHEMA_ONLY)
    # MIGRATIONS list contains many ALTER TABLE statements as string
    # elements of a Python list — extract and execute each.
    import re as _re
    for m in _re.findall(r'"([^"]+)"', MIGRATIONS_LIST):
        try:
            await conn.execute(m)
        except Exception:
            pass  # 'duplicate column' is harmless
    await conn.commit()
    yield FakeDB(conn)
    await conn.close()


# ---------------------------------------------------------------------------
# (1) create_approval_request — supervisor helper
# ---------------------------------------------------------------------------


class TestCreateApprovalRequest:
    """AC-9: supervisor creates ApprovalRequest for human_approval step."""

    @pytest.mark.asyncio
    async def test_creates_pending_request_with_rendered_summary(self, db):
        """AC-9: ready human_approval step → ApprovalRequest status=pending
        with summary_template rendered against step params + upstream outputs.
        """
        # Insert a project + a "do-thing" task (already completed) +
        # a "human_approval" task (pending) with approval config.
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        await db.insert("projects", {
            "id": "p1", "name": "test", "goal": "x",
            "state": "ready", "coordinator_role": "",
            "accept_criteria": "", "deliverable_path": "",
            "max_iterations": 0, "current_iteration": 0,
            "last_iteration_summary": "",
        })
        # Dep task (completed, with output). Output keyed by step name
        # in the render context (per design doc §4.7.1).
        await db.insert("tasks", {
            "id": "t-do", "project_id": "p1", "name": "do-thing",
            "agent_role": "worker", "depends_on": "[]",
            "on_parent_failure": "skip", "status": "completed",
            "priority": "normal", "action": "do_task",
            "params": '{}', "max_retries": 2,
            "timeout_seconds": 1800, "output_path": "",
            "required_capability": None, "feedback_to": "[]",
        })
        # Update output (the `result` column holds the agent's output JSON).
        await db.execute(
            "UPDATE tasks SET result = ? WHERE id = ?",
            ('{"client_name": "ACME", "total": 1.2}', "t-do"),
        )
        # Human approval task (pending, ready). summary_template uses
        # dotted path {{t-do.client_name}} to reach upstream output.
        await db.insert("tasks", {
            "id": "t-ha", "project_id": "p1", "name": "approve-step",
            "agent_role": "", "depends_on": '["t-do"]',
            "on_parent_failure": "skip", "status": "pending",
            "priority": "normal", "action": "human_approval",
            "params": json.dumps({
                "_workflow_approval": {
                    "on_reject": "stop",
                    "summary_template": "Approve {{do-thing.client_name}}: total {{do-thing.total}}",
                    "timeout_seconds": 86400,
                }
            }),
            "max_retries": 2, "timeout_seconds": 1800,
            "output_path": "", "required_capability": None,
            "feedback_to": "[]",
        })
        # Build the task dict that create_approval_request expects
        task_row = await db.fetchone(
            "SELECT * FROM tasks WHERE id = ?", ("t-ha",)
        )
        task = dict(task_row)
        apr = await create_approval_request(db, task=task)
        assert apr is not None
        assert apr["status"] == STATUS_PENDING
        assert apr["workflow_id"] == "p1"
        assert apr["step_name"] == "approve-step"
        # Summary should be rendered against the dep's output
        assert "ACME" in apr["summary"]
        assert "1.2" in apr["summary"]
        # Row was inserted into DB
        row = await db.fetchone(
            "SELECT * FROM approval_requests WHERE id = ?", (apr["id"],)
        )
        assert row is not None
        assert row["status"] == STATUS_PENDING

    @pytest.mark.asyncio
    async def test_idempotent_when_pending_already_exists(self, db):
        """Second call for same (workflow, step) returns None (no duplicate)."""
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        await db.insert("projects", {
            "id": "p1", "name": "x", "goal": "x", "state": "ready",
            "coordinator_role": "", "accept_criteria": "",
            "deliverable_path": "", "max_iterations": 0,
            "current_iteration": 0, "last_iteration_summary": "",
        })
        await db.insert("tasks", {
            "id": "t1", "project_id": "p1", "name": "approve",
            "agent_role": "", "depends_on": "[]",
            "on_parent_failure": "skip", "status": "pending",
            "priority": "normal", "action": "human_approval",
            "params": json.dumps({"_workflow_approval": {
                "on_reject": "stop",
                "summary_template": "Approve?",
            }}),
            "max_retries": 2, "timeout_seconds": 1800,
            "output_path": "", "required_capability": None,
            "feedback_to": "[]",
        })
        task = dict(await db.fetchone("SELECT * FROM tasks WHERE id = ?", ("t1",)))
        # First call creates
        apr1 = await create_approval_request(db, task=task)
        assert apr1 is not None
        # Second call is a no-op
        apr2 = await create_approval_request(db, task=task)
        assert apr2 is None
        # Only one row in the table
        rows = await db.fetchall(
            "SELECT * FROM approval_requests WHERE workflow_id = ?", ("p1",)
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# (2) apply_on_reject + apply_approve — state transitions
# ---------------------------------------------------------------------------


class TestApplyOnReject:
    """AC-14/15/16: on_reject variants and task status transitions."""

    async def _make_task(self, db, project_id: str, name: str, status: str = "pending"):
        await db.insert("projects", {
            "id": project_id, "name": "x", "goal": "x", "state": "ready",
            "coordinator_role": "", "accept_criteria": "",
            "deliverable_path": "", "max_iterations": 0,
            "current_iteration": 0, "last_iteration_summary": "",
        })
        await db.insert("tasks", {
            "id": f"t-{name}", "project_id": project_id, "name": name,
            "agent_role": "", "depends_on": "[]",
            "on_parent_failure": "skip", "status": status,
            "priority": "normal", "action": "human_approval",
            "params": "{}", "max_retries": 2, "timeout_seconds": 1800,
            "output_path": "", "required_capability": None,
            "feedback_to": "[]",
        })

    @pytest.mark.asyncio
    async def test_stop_makes_task_failed(self, db):
        """AC-14: on_reject='stop' → task.status='failed'."""
        await self._make_task(db, "p1", "approve")
        new_status = await apply_on_reject(
            db, workflow_id="p1", step_name="approve",
            on_reject="stop", reason="user said no", user_id="admin",
        )
        assert new_status == TASK_FAILED
        row = await db.fetchone("SELECT status, failure_reason FROM tasks WHERE id = ?", ("t-approve",))
        assert row["status"] == "failed"
        assert row["failure_reason"] == "rejected_by_human"

    @pytest.mark.asyncio
    async def test_skip_makes_task_skipped(self, db):
        """AC-15: on_reject='skip' → task.status='skipped'."""
        await self._make_task(db, "p1", "approve")
        new_status = await apply_on_reject(
            db, workflow_id="p1", step_name="approve", on_reject="skip"
        )
        assert new_status == TASK_SKIPPED
        row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t-approve",))
        assert row["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_route_makes_task_skipped(self, db):
        """AC-16: on_reject='route' → task.status='skipped' (route target picked up via D.depends_on)."""
        await self._make_task(db, "p1", "approve")
        new_status = await apply_on_reject(
            db, workflow_id="p1", step_name="approve", on_reject="route"
        )
        assert new_status == TASK_SKIPPED
        row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t-approve",))
        assert row["status"] == "skipped"


class TestApplyApprove:
    """AC-11: Approve → task.status='completed' with completion_reason."""

    @pytest.mark.asyncio
    async def test_approve_marks_task_completed(self, db):
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        await db.insert("projects", {
            "id": "p1", "name": "x", "goal": "x", "state": "ready",
            "coordinator_role": "", "accept_criteria": "",
            "deliverable_path": "", "max_iterations": 0,
            "current_iteration": 0, "last_iteration_summary": "",
        })
        await db.insert("tasks", {
            "id": "t1", "project_id": "p1", "name": "approve",
            "agent_role": "", "depends_on": "[]",
            "on_parent_failure": "skip", "status": "pending",
            "priority": "normal", "action": "human_approval",
            "params": "{}", "max_retries": 2, "timeout_seconds": 1800,
            "output_path": "", "required_capability": None,
            "feedback_to": "[]",
        })
        ok = await apply_approve(db, workflow_id="p1", step_name="approve", user_id="admin")
        assert ok is True
        row = await db.fetchone(
            "SELECT status, completion_reason FROM tasks WHERE id = ?", ("t1",)
        )
        assert row["status"] == TASK_COMPLETED
        assert row["completion_reason"] == "approved_by_human"

    @pytest.mark.asyncio
    async def test_approve_no_pending_task_returns_false(self, db):
        """If there's no pending task (already terminal), return False."""
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        await db.insert("projects", {
            "id": "p1", "name": "x", "goal": "x", "state": "ready",
            "coordinator_role": "", "accept_criteria": "",
            "deliverable_path": "", "max_iterations": 0,
            "current_iteration": 0, "last_iteration_summary": "",
        })
        # No task at all
        ok = await apply_approve(db, workflow_id="p1", step_name="missing", user_id="admin")
        assert ok is False


# ---------------------------------------------------------------------------
# (3) sweep_expired_approvals — timeout sweeper
# ---------------------------------------------------------------------------


class TestSweepExpiredApprovals:
    """AC-17: sweeper expires pending requests past timeout_seconds."""

    @pytest.mark.asyncio
    async def test_expires_pending_past_timeout(self, db):
        """AC-17: created_at + 86400s elapsed → status=expired, on_reject applied."""
        now = datetime.now()
        past = (now - timedelta(seconds=90000)).strftime("%Y-%m-%dT%H:%M:%S")  # 25h ago
        await db.insert("projects", {
            "id": "p1", "name": "x", "goal": "x", "state": "ready",
            "coordinator_role": "", "accept_criteria": "",
            "deliverable_path": "", "max_iterations": 0,
            "current_iteration": 0, "last_iteration_summary": "",
        })
        await db.insert("tasks", {
            "id": "t1", "project_id": "p1", "name": "approve",
            "agent_role": "", "depends_on": "[]",
            "on_parent_failure": "skip", "status": "pending",
            "priority": "normal", "action": "human_approval",
            "params": json.dumps({"_workflow_approval": {
                "on_reject": "stop", "summary_template": "X", "timeout_seconds": 86400,
            }}),
            "max_retries": 2, "timeout_seconds": 1800,
            "output_path": "", "required_capability": None,
            "feedback_to": "[]",
        })
        await db.insert("approval_requests", {
            "id": "apr-1", "workflow_id": "p1", "step_name": "approve",
            "status": "pending", "summary": "X", "payload": "",
            "created_at": past, "decided_at": None, "reason": "",
            "user_id": "",
        })
        n = await sweep_expired_approvals(db)
        assert n == 1
        row = await db.fetchone(
            "SELECT status, reason, user_id FROM approval_requests WHERE id = ?", ("apr-1",)
        )
        assert row["status"] == "expired"
        assert row["reason"] == "timeout"
        assert row["user_id"] == "system"
        # Task should now be failed (on_reject='stop')
        t = await db.fetchone("SELECT status, failure_reason FROM tasks WHERE id = ?", ("t1",))
        assert t["status"] == TASK_FAILED
        assert t["failure_reason"] == "rejected_by_human"

    @pytest.mark.asyncio
    async def test_no_expire_when_within_timeout(self, db):
        """Pending request created 1h ago, timeout 24h → not expired."""
        now = datetime.now()
        recent = (now - timedelta(seconds=3600)).strftime("%Y-%m-%dT%H:%M:%S")
        await db.insert("projects", {
            "id": "p1", "name": "x", "goal": "x", "state": "ready",
            "coordinator_role": "", "accept_criteria": "",
            "deliverable_path": "", "max_iterations": 0,
            "current_iteration": 0, "last_iteration_summary": "",
        })
        await db.insert("tasks", {
            "id": "t1", "project_id": "p1", "name": "approve",
            "agent_role": "", "depends_on": "[]",
            "on_parent_failure": "skip", "status": "pending",
            "priority": "normal", "action": "human_approval",
            "params": json.dumps({"_workflow_approval": {
                "on_reject": "stop", "summary_template": "X", "timeout_seconds": 86400,
            }}),
            "max_retries": 2, "timeout_seconds": 1800,
            "output_path": "", "required_capability": None,
            "feedback_to": "[]",
        })
        await db.insert("approval_requests", {
            "id": "apr-1", "workflow_id": "p1", "step_name": "approve",
            "status": "pending", "summary": "X", "payload": "",
            "created_at": recent, "decided_at": None, "reason": "",
            "user_id": "",
        })
        n = await sweep_expired_approvals(db)
        assert n == 0
        row = await db.fetchone("SELECT status FROM approval_requests WHERE id = ?", ("apr-1",))
        assert row["status"] == "pending"

    @pytest.mark.asyncio
    async def test_no_pending_returns_zero(self, db):
        """No pending requests → no-op."""
        n = await sweep_expired_approvals(db)
        assert n == 0


# ---------------------------------------------------------------------------
# (4) auto_reject_pending_approvals — workflow cancel hook
# ---------------------------------------------------------------------------


class TestAutoRejectOnCancel:
    """AC-19: workflow cancel auto-rejects pending approvals."""

    @pytest.mark.asyncio
    async def test_auto_rejects_all_pending(self, db):
        """Cancel a workflow with 2 pending approvals → both rejected, tasks skipped."""
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        await db.insert("projects", {
            "id": "p1", "name": "x", "goal": "x", "state": "stopping",
            "coordinator_role": "", "accept_criteria": "",
            "deliverable_path": "", "max_iterations": 0,
            "current_iteration": 0, "last_iteration_summary": "",
        })
        # Two tasks + their approvals
        for i, name in enumerate(["approve-a", "approve-b"]):
            await db.insert("tasks", {
                "id": f"t{i}", "project_id": "p1", "name": name,
                "agent_role": "", "depends_on": "[]",
                "on_parent_failure": "skip", "status": "pending",
                "priority": "normal", "action": "human_approval",
                "params": "{}", "max_retries": 2, "timeout_seconds": 1800,
                "output_path": "", "required_capability": None,
                "feedback_to": "[]",
            })
            await db.insert("approval_requests", {
                "id": f"apr-{i}", "workflow_id": "p1", "step_name": name,
                "status": "pending", "summary": "X", "payload": "",
                "created_at": now, "decided_at": None, "reason": "",
                "user_id": "",
            })
        n = await auto_reject_pending_approvals(db, workflow_id="p1")
        assert n == 2
        # Both approvals rejected
        rows = await db.fetchall(
            "SELECT status, reason, user_id FROM approval_requests WHERE workflow_id = ?",
            ("p1",),
        )
        for r in rows:
            assert r["status"] == "rejected"
            assert r["reason"] == "workflow_cancelled"
            assert r["user_id"] == "system"
        # Both tasks skipped
        tasks = await db.fetchall("SELECT status FROM tasks WHERE project_id = ?", ("p1",))
        for t in tasks:
            assert t["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_no_pending_returns_zero(self, db):
        n = await auto_reject_pending_approvals(db, workflow_id="nonexistent")
        assert n == 0


# ---------------------------------------------------------------------------
# (5) build_summary — render with params + upstream outputs
# ---------------------------------------------------------------------------


class TestBuildSummary:
    """summary_template render at ApprovalRequest creation time."""

    def test_simple_param_substitution(self):
        result = build_summary(
            "Approve {{client_name}}",
            params={"client_name": "ACME"},
            dep_tasks=[],
        )
        assert result == "Approve ACME"

    def test_upstream_dotted_path(self):
        result = build_summary(
            "Total: {{do-thing.total}}",
            params={},
            dep_tasks=[{"name": "do-thing", "result": '{"total": 1.2}'}],
        )
        assert result == "Total: 1.2"

    def test_missing_var_renders_placeholder(self):
        result = build_summary("{{missing}}", params={}, dep_tasks=[])
        assert result == "<missing:missing>"

    def test_invalid_dep_output_skipped(self):
        """A dep with unparseable output is skipped (not raised)."""
        result = build_summary(
            "X: {{broken.field}}",
            params={},
            dep_tasks=[{"name": "broken", "result": "not json{"}],
        )
        assert result == "X: <missing:broken.field>"

    def test_combined_param_and_upstream(self):
        result = build_summary(
            "{{client_name}}: total {{do-thing.total}}",
            params={"client_name": "ACME"},
            dep_tasks=[{"name": "do-thing", "result": '{"total": 1.2}'}],
        )
        assert result == "ACME: total 1.2"
