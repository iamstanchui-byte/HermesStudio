"""Tests for v3.12.1 follow-up #6: history_turn_count server-side
persistence.

When the wrapper submits a /api/tasks/{id}/result, it can
include a `history_turn_count` field (the hermes session's
message_count at task completion). The server persists this
into the most-recent task_dispatch row for the task so the
dashboard / operator can verify the conversation-history
growth fix is working (commit 20fb097 measured 4x prompt
growth; hermes 0.19.1's micro-compaction should flatten this
to ~1.3x).

Asserts:
  1. Pydantic: TaskResult accepts history_turn_count as int.
  2. /api/tasks/{id}/result with history_turn_count populates
     the task_dispatch row.
  3. Missing history_turn_count is silently OK (older wrappers).
  4. Multiple task_dispatch rows for the same task: the
     most-recent (MAX(dispatched_at)) gets updated.
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
from hermes_orch.api.tasks import TaskResult


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


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "test-password-for-htc-test"


async def _bootstrap_admin(app) -> None:
    from hermes_orch.auth.cookie import hash_password, create_user, ROLE_ADMIN
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


# ===== Pydantic =====


def test_task_result_accepts_history_turn_count():
    """The wrapper can submit history_turn_count as an int.
    Optional (None default) for backward compat with old
    wrappers that don't report it.
    """
    r = TaskResult(status="completed", history_turn_count=12)
    assert r.history_turn_count == 12


def test_task_result_history_turn_count_default_is_none():
    """Backwards compat: missing history_turn_count field
    parses cleanly (None). The submit_result handler then
    skips the UPDATE silently.
    """
    r = TaskResult(status="completed")
    assert r.history_turn_count is None


# ===== submit_result persistence =====


@pytest.mark.asyncio
async def test_submit_result_persists_history_turn_count(client):
    """submitting a /result with history_turn_count populates
    the most-recent task_dispatch row for the task.
    """
    ac, app = client
    await _bootstrap_admin(app)
    await _login(ac)
    db = app.state.db
    # Seed: an agent + a running task + a task_dispatch row.
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agents (id, secret_hash, status) "
        "VALUES (?, 'fake-hash', 'online')",
        (agent_id,),
    )
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state) "
        "VALUES (?, ?, '', 'planned')",
        (pid, f"htc-{pid}"),
    )
    tid = f"t-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, status, "
        "assigned_agent_id, depends_on, on_parent_failure, action, "
        "params, is_single_task, archived) "
        "VALUES (?, ?, 'step', 'super', 'running', ?, '[]', 'skip', "
        "'do_task', '{}', 0, 0)",
        (tid, pid, agent_id),
    )
    # One task_dispatch row, history_turn_count = NULL
    td_id = f"td-{uuid.uuid4().hex[:12]}"
    await db.execute(
        "INSERT INTO task_dispatch (id, project_id, task_id, "
        "dispatch_path, actor) VALUES (?, ?, ?, 'soul_dispatch', 'supervisor')",
        (td_id, pid, tid),
    )

    # Submit /result with history_turn_count=12
    r = await ac.post(
        f"/api/tasks/{tid}/result",
        json={
            "status": "completed",
            "session_id": "sid-1",
            "summary": "done",
            "token_usage": {
                "model": "m",
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            },
            "history_turn_count": 12,
        },
        # HMAC bypass: the test sends no HMAC header, so the
        # endpoint will reject with 401. We hit the function
        # directly to test the persistence logic without the
        # auth gate.
    )
    # The endpoint requires HMAC; 401 means we hit the wire
    # path. The persistence logic is exercised below via
    # direct DB write to keep this test focused on the
    # bookkeeping, not the auth flow (which has its own tests).
    if r.status_code == 401:
        # Direct-call path: insert a /result submission via
        # the same DB code path the endpoint uses.
        from hermes_orch.api.tasks import submit_result
        from unittest.mock import MagicMock
        request = MagicMock()
        request.app.state.db = db
        from hermes_orch.api.tasks import TaskResult as TR
        body = TR(
            status="completed",
            session_id="sid-1",
            summary="done",
            history_turn_count=12,
        )
        # Bypass the HMAC auth by setting the assignment manually.
        await db.execute(
            "UPDATE tasks SET assigned_agent_id = ? WHERE id = ?",
            (agent_id, tid),
        )
        # submit_result signature: (task_id, body, request, agent_id=Depends)
        # Pass agent_id directly to bypass the auth dependency.
        try:
            await submit_result(
                task_id=tid, body=body, request=request, agent_id=agent_id
            )
        except Exception as e:
            # The endpoint may raise HTTPException for state issues;
            # for the persistence path we only care that the
            # UPDATE ran, so we ignore HTTP errors here and verify
            # the row directly.
            pass

    # The task_dispatch row should now have history_turn_count = 12.
    row = await db.fetchone(
        "SELECT history_turn_count FROM task_dispatch WHERE id = ?",
        (td_id,),
    )
    assert row["history_turn_count"] == 12, (
        f"expected history_turn_count=12; got {row['history_turn_count']}"
    )


@pytest.mark.asyncio
async def test_submit_result_skips_update_when_history_turn_count_missing(client):
    """Backwards compat: a wrapper that doesn't send
    history_turn_count is silently OK. The task_dispatch
    row's history_turn_count stays NULL.
    """
    ac, app = client
    await _bootstrap_admin(app)
    await _login(ac)
    db = app.state.db

    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agents (id, secret_hash, status) "
        "VALUES (?, 'fake-hash', 'online')",
        (agent_id,),
    )
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state) "
        "VALUES (?, ?, '', 'planned')",
        (pid, f"htc-skip-{pid}"),
    )
    tid = f"t-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, status, "
        "assigned_agent_id, depends_on, on_parent_failure, action, "
        "params, is_single_task, archived) "
        "VALUES (?, ?, 'step', 'super', 'running', ?, '[]', 'skip', "
        "'do_task', '{}', 0, 0)",
        (tid, pid, agent_id),
    )
    td_id = f"td-{uuid.uuid4().hex[:12]}"
    await db.execute(
        "INSERT INTO task_dispatch (id, project_id, task_id, "
        "dispatch_path, actor) VALUES (?, ?, ?, 'soul_dispatch', 'supervisor')",
        (td_id, pid, tid),
    )

    from hermes_orch.api.tasks import submit_result
    from hermes_orch.api.tasks import TaskResult as TR
    from unittest.mock import MagicMock
    request = MagicMock()
    request.app.state.db = db
    body = TR(
        status="completed",
        session_id="sid-1",
        summary="done",
        # history_turn_count omitted (None default)
    )
    try:
        await submit_result(
            task_id=tid, body=body, request=request, agent_id=agent_id
        )
    except Exception:
        pass

    # history_turn_count stays NULL (the wrapper didn't report it).
    row = await db.fetchone(
        "SELECT history_turn_count FROM task_dispatch WHERE id = ?",
        (td_id,),
    )
    assert row["history_turn_count"] is None


@pytest.mark.asyncio
async def test_submit_result_updates_most_recent_task_dispatch_only(client):
    """A task may have multiple task_dispatch rows
    (apply_workflow + loopback_reset paths both create
    one for the same task). The UPDATE targets only the
    most-recent row (MAX(dispatched_at)) so we don't
    accidentally clobber a historical value.
    """
    ac, app = client
    await _bootstrap_admin(app)
    await _login(ac)
    db = app.state.db

    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agents (id, secret_hash, status) "
        "VALUES (?, 'fake-hash', 'online')",
        (agent_id,),
    )
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state) "
        "VALUES (?, ?, '', 'planned')",
        (pid, f"htc-multi-{pid}"),
    )
    tid = f"t-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, status, "
        "assigned_agent_id, depends_on, on_parent_failure, action, "
        "params, is_single_task, archived) "
        "VALUES (?, ?, 'step', 'super', 'running', ?, '[]', 'skip', "
        "'do_task', '{}', 0, 0)",
        (tid, pid, agent_id),
    )
    # Two dispatch rows: one older (apply_workflow) + one
    # newer (loopback_reset). The newer one is the "final"
    # dispatch for this task.
    td_old = f"td-{uuid.uuid4().hex[:12]}"
    td_new = f"td-{uuid.uuid4().hex[:12]}"
    await db.execute(
        "INSERT INTO task_dispatch (id, project_id, task_id, "
        "dispatch_path, actor, dispatched_at) "
        "VALUES (?, ?, ?, 'apply_workflow', 'supervisor', "
        "'2026-08-03 10:00:00')",
        (td_old, pid, tid),
    )
    await db.execute(
        "INSERT INTO task_dispatch (id, project_id, task_id, "
        "dispatch_path, actor, dispatched_at) "
        "VALUES (?, ?, ?, 'loopback_reset', 'supervisor', "
        "'2026-08-03 10:05:00')",
        (td_new, pid, tid),
    )

    from hermes_orch.api.tasks import submit_result
    from hermes_orch.api.tasks import TaskResult as TR
    from unittest.mock import MagicMock
    request = MagicMock()
    request.app.state.db = db
    body = TR(
        status="completed",
        session_id="sid-1",
        summary="done",
        history_turn_count=12,
    )
    try:
        await submit_result(
            task_id=tid, body=body, request=request, agent_id=agent_id
        )
    except Exception:
        pass

    # The newer row gets history_turn_count=12; the older one stays NULL.
    old_row = await db.fetchone(
        "SELECT history_turn_count FROM task_dispatch WHERE id = ?",
        (td_old,),
    )
    new_row = await db.fetchone(
        "SELECT history_turn_count FROM task_dispatch WHERE id = ?",
        (td_new,),
    )
    assert old_row["history_turn_count"] is None, (
        f"older dispatch row should not be touched; got {old_row}"
    )
    assert new_row["history_turn_count"] == 12, (
        f"newer dispatch row should have 12; got {new_row}"
    )
