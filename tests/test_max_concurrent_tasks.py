"""Tests for v3.6.0 per-agent max_concurrent_tasks cap.

What this covers:
  1. Register with custom max_concurrent_tasks (1..32) persists
  2. Default is 1 (backward compatible)
  3. Validation: PUT rejects 0, 33, "abc", negative
  4. GET /api/agents/{id} returns the cap
  5. List /api/agents returns the cap
  6. Heartbeat response includes max_concurrent_tasks (so the wrapper
     can size its ThreadPoolExecutor from server state)
  7. Migration: fresh DB has the column (default 1)
  8. Supervisor: _assign_task skips when the agent is at cap
  9. Supervisor: dispatch resumes when a slot frees up

The first 7 are HTTP-level integration tests using the in-process
AsyncClient. Tests 8-9 exercise the supervisor directly with a real
DB so we can verify the cap query + retry behavior end-to-end.

The wrapper-side ThreadPoolExecutor isn't unit-tested here because it
would require mocking the entire daemon loop (httpx, hermes, file
uploads). The integration test in v3.6.0 verification
(`scripts/Temp/_verify_v36.py`) covers the live behavior — running 2
parallel hermes subprocesses from the same wrapper and confirming both
finish ~simultaneously rather than sequentially.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch import main as main_mod
from hermes_orch import db as db_mod
from hermes_orch.auth.cookie import ROLE_ADMIN, create_user
from hermes_orch.main import create_app


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


async def _wrapper_simulator(db, stop_event: asyncio.Event) -> None:
    """Background task that acks any `profile_configs` row the supervisor
    submits (v3.9.0 SOUL apply flow).

    Mirrors the wrapper's daemon loop: poll for `status='pending'`
    rows, claim them (status='applying' → 'applied') so
    `dispatch_step`'s wait-for-ack returns True. Without this
    simulator, the Round-3 dispatch path times out after 10s per
    task (the production default), which is far too slow for a
    unit test.

    Run via:
        stop = asyncio.Event()
        sim = asyncio.create_task(_wrapper_simulator(db, stop))
        try:
            ... test body ...
        finally:
            stop.set()
            await sim

    The simulator only acks rows whose profile belongs to a
    verified agent (so tests that intentionally set up an offline
    agent see the timeout path, not a false-positive ack).
    """
    while not stop_event.is_set():
        try:
            rows = await db.fetchall(
                "SELECT pc.id FROM profile_configs pc "
                "JOIN agent_profiles ap ON ap.id = pc.profile_id "
                "JOIN agents a ON a.id = ap.agent_id "
                "WHERE pc.status = 'pending' AND a.status = 'verified'"
            )
            for r in rows:
                await db.execute(
                    "UPDATE profile_configs SET status = 'applied', "
                    "applied_at = ? WHERE id = ? AND status = 'pending'",
                    ("2026-08-01T00:00:00+00:00", r["id"]),
                )
        except Exception:
            pass  # ignore transient DB errors in the simulator loop
        # Poll fast enough to keep the dispatch timeout (default 10s)
        # but slow enough to not hammer the DB.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.05)
        except asyncio.TimeoutError:
            pass



async def _bootstrap_admin(app) -> str:
    """Idempotent admin bootstrap (matches test_users_api / test_plan_dep_resolution)."""
    db = app.state.db
    existing = await db.fetchone(
        "SELECT id, password_hash FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    if existing:
        if not existing.get("password_hash"):
            from hermes_orch.auth.cookie import hash_password
            await db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(ADMIN_PASSWORD), existing["id"]),
            )
        return existing["id"]
    return await create_user(
        db,
        username=ADMIN_USERNAME,
        password=ADMIN_PASSWORD,
        role=ROLE_ADMIN,
        is_bootstrap_admin=True,
    )


async def _login(ac: AsyncClient, username: str, password: str) -> None:
    r = await ac.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Fresh app + AsyncClient with a bootstrap admin already in place.

    Same fixture pattern as test_users_api: monkeypatch Database.__init__
    so each test gets its own tmp DB.
    """
    import pathlib

    test_db = tmp_path / "test.db"
    orig_init = main_mod.create_app

    def patched_init():
        orig_db_init = db_mod.Database.__init__

        def patched_db_init(self, db_path):
            orig_db_init(self, test_db)

        monkeypatch.setattr(db_mod.Database, "__init__", patched_db_init)
        return orig_init()

    app = patched_init()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        await _bootstrap_admin(app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ===== Register validation =====
# Note: agents router is registered with @router.post("/", ...) so the
# path is /api/agents/ (trailing slash required). Same pattern as the
# dashboard's existing agent registration flow.

@pytest.mark.asyncio
async def test_register_defaults_cap_to_1(client):
    """Backward compatible: agents registered without max_concurrent_tasks
    default to 1 (sequential, one task at a time per process)."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "agent-default", "os_type": "linux"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["agent"]["max_concurrent_tasks"] == 1


@pytest.mark.asyncio
async def test_register_accepts_custom_cap(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "agent-cap4", "os_type": "linux", "max_concurrent_tasks": 4},
    )
    assert r.status_code == 201, r.text
    assert r.json()["agent"]["max_concurrent_tasks"] == 4


@pytest.mark.asyncio
async def test_register_accepts_cap_1_to_32(client):
    """Boundary check: 1 (lower bound) and 32 (upper bound) both work."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    for cap in (1, 32):
        r = await client.post(
            "/api/agents/",
            json={"agent_id": f"agent-cap-{cap}", "max_concurrent_tasks": cap},
        )
        assert r.status_code == 201, f"cap={cap}: {r.text}"
        assert r.json()["agent"]["max_concurrent_tasks"] == cap


@pytest.mark.asyncio
async def test_register_rejects_cap_below_1(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "agent-bad-low", "max_concurrent_tasks": 0},
    )
    assert r.status_code == 422  # Pydantic validation


@pytest.mark.asyncio
async def test_register_rejects_cap_above_32(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "agent-bad-high", "max_concurrent_tasks": 33},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_register_rejects_non_integer_cap(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "agent-bad-type", "max_concurrent_tasks": "abc"},
    )
    assert r.status_code == 422


# ===== PUT /api/agents/{id} =====

@pytest.mark.asyncio
async def test_put_updates_cap(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    await client.post(
        "/api/agents/", json={"agent_id": "agent-a", "max_concurrent_tasks": 1}
    )
    r = await client.put(
        "/api/agents/agent-a", json={"max_concurrent_tasks": 8}
    )
    assert r.status_code == 200, r.text
    assert r.json()["max_concurrent_tasks"] == 8

    # And it persists across a fresh GET
    r2 = await client.get("/api/agents/")
    assert r2.status_code == 200
    a = next(x for x in r2.json()["agents"] if x["id"] == "agent-a")
    assert a["max_concurrent_tasks"] == 8


@pytest.mark.asyncio
async def test_put_validates_cap_range(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    await client.post("/api/agents/", json={"agent_id": "agent-x"})
    for bad in (0, 33, -1, "abc"):
        r = await client.put(
            "/api/agents/agent-x", json={"max_concurrent_tasks": bad}
        )
        assert r.status_code == 422, f"cap={bad!r} should be rejected"


@pytest.mark.asyncio
async def test_put_with_no_cap_does_not_change_it(client):
    """Backwards-compat: PUT with only ip/os_type should not reset the cap."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    await client.post(
        "/api/agents/", json={"agent_id": "agent-y", "max_concurrent_tasks": 5}
    )
    r = await client.put("/api/agents/agent-y", json={"ip": "10.0.0.5"})
    assert r.status_code == 200
    assert r.json()["max_concurrent_tasks"] == 5  # unchanged


@pytest.mark.asyncio
async def test_put_emits_audit_event_only_on_change(client):
    """No-op PUTs (same value) should not write to the audit log."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    await client.post(
        "/api/agents/", json={"agent_id": "agent-z", "max_concurrent_tasks": 3}
    )
    # Re-PUT the same value
    r = await client.put(
        "/api/agents/agent-z", json={"max_concurrent_tasks": 3}
    )
    assert r.status_code == 200
    # And no audit row for the no-op
    app = client._transport.app  # type: ignore[attr-defined]
    rows = await app.state.db.fetchall(
        "SELECT id FROM audit_log WHERE event_type = ? AND agent_id = ?",
        ("agent.max_concurrent_tasks_changed", "agent-z"),
    )
    assert len(rows) == 0

    # Now actually change it
    await client.put("/api/agents/agent-z", json={"max_concurrent_tasks": 7})
    rows = await app.state.db.fetchall(
        "SELECT id, payload FROM audit_log WHERE event_type = ? AND agent_id = ?",
        ("agent.max_concurrent_tasks_changed", "agent-z"),
    )
    assert len(rows) == 1
    import json as _json
    payload = _json.loads(rows[0]["payload"]) if isinstance(rows[0]["payload"], str) else rows[0]["payload"]
    assert payload["old"] == 3
    assert payload["new"] == 7


# ===== List / GET / heartbeat =====

@pytest.mark.asyncio
async def test_list_includes_cap(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    await client.post(
        "/api/agents/", json={"agent_id": "agent-l1", "max_concurrent_tasks": 2}
    )
    await client.post(
        "/api/agents/", json={"agent_id": "agent-l2"}  # default 1
    )
    r = await client.get("/api/agents/")
    agents = {a["id"]: a for a in r.json()["agents"]}
    assert agents["agent-l1"]["max_concurrent_tasks"] == 2
    assert agents["agent-l2"]["max_concurrent_tasks"] == 1


@pytest.mark.asyncio
async def test_heartbeat_response_includes_cap(client):
    """The wrapper reads this on every heartbeat tick to size its
    ThreadPoolExecutor. Without it, the wrapper would always run
    with max_workers=1 (backward compatible), ignoring the operator's
    configured cap. Test directly via the in-process client with
    HMAC headers (matches how the real wrapper would call it)."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Register a fresh agent and capture the one-time setup secret
    reg = await client.post(
        "/api/agents/", json={"agent_id": "agent-hb", "max_concurrent_tasks": 6}
    )
    assert reg.status_code == 201, reg.text
    secret = reg.json()["setup_secret"]
    path = "/api/agents/agent-hb/heartbeat"
    body = b'{"status":"idle"}'
    # Build a valid HMAC-signed heartbeat (mimic what the wrapper does).
    import time as _time
    from hermes_orch.auth.hmac import compute_signature
    ts = str(int(_time.time()))
    sig = compute_signature(secret, "POST", path, body, ts)
    r = await client.post(
        path,
        headers={
            "X-Agent-Id": "agent-hb",
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
        content=body,
    )
    assert r.status_code == 200, r.text
    assert r.json()["max_concurrent_tasks"] == 6


# ===== Migration =====

@pytest.mark.asyncio
async def test_migration_adds_column_to_existing_db(tmp_path, monkeypatch):
    """Pre-migration DBs (no max_concurrent_tasks column) should get
    the column added on connect, and existing agents should default
    to 1 (backward compatible). Simulate by creating a DB with the
    pre-migration schema, then running connect() which runs MIGRATIONS."""
    import sqlite3
    from hermes_orch.db import Database, SCHEMA, MIGRATIONS

    test_db = tmp_path / "pre_v36.db"
    # Build a pre-v3.6 schema: same as SCHEMA but WITHOUT the
    # max_concurrent_tasks column on agents.
    pre_schema = SCHEMA.replace(
        "ALTER TABLE token_usage ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE token_usage ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0",
    )
    # Note: SCHEMA's CREATE TABLE for agents doesn't include
    # max_concurrent_tasks yet (we add the column only via MIGRATIONS).
    # So a fresh DB built from SCHEMA already has the CREATE TABLE
    # without the column. The MIGRATIONS list adds the column. To
    # simulate a pre-migration DB, we create a DB with the CREATE
    # TABLE from the current SCHEMA (which has no max_concurrent_tasks)
    # and only the MIGRATIONS UP TO the one before ours.
    conn = sqlite3.connect(str(test_db))
    conn.executescript(SCHEMA)
    # Roll back: drop the column if it was just added by the v3.1.2
    # cache_read migration... actually cache_read is also a MIGRATION.
    # To prove the migration adds the column we need to: (a) insert an
    # agent without max_concurrent_tasks, (b) run Database.connect()
    # which applies MIGRATIONS, (c) verify the column exists and the
    # existing row has the default 1.
    conn.execute(
        "INSERT INTO agents (id, secret_hash, status) VALUES (?, ?, ?)",
        ("legacy-agent", "x" * 64, "verified"),
    )
    conn.commit()
    conn.close()

    # Now connect with the migration runner
    db = Database(test_db)
    await db.connect()

    # Verify the column exists
    rows = await db.fetchall("PRAGMA table_info(agents)")
    col_names = [r["name"] for r in rows]
    assert "max_concurrent_tasks" in col_names, (
        f"max_concurrent_tasks column missing from migrated DB; "
        f"got columns: {col_names}"
    )

    # Verify the existing row defaulted to 1
    row = await db.fetchone(
        "SELECT max_concurrent_tasks FROM agents WHERE id = ?",
        ("legacy-agent",),
    )
    assert row["max_concurrent_tasks"] == 1, (
        f"pre-migration agent should default to cap=1, got {row['max_concurrent_tasks']}"
    )


# ===== Supervisor-side cap =====

@pytest.mark.asyncio
async def test_supervisor_skips_assignment_at_cap(client):
    """v3.6.0: the orchestrator's _assign_task must skip an agent that
    is at its max_concurrent_tasks (count of assigned+running tasks
    for the agent). The task should remain pending and be picked up
    when a slot frees. This is the orchestrator-side counterpart of
    the wrapper's ThreadPoolExecutor — without it, the orchestrator
    would over-assign and the wrapper would sit in 'assigned' state
    forever (the v3.5.2 middleware bug, different cause, same
    symptom).

    v3.9.0: the project dispatch path now goes through
    `orchestrator.soul_dispatch.dispatch_step` (routing + SOUL apply
    + create a new dispatched task). Two test-setup changes keep the
    legacy cap semantics visible:
      1. The agent gets a second profile (researcher-2). The
         routing engine's per-profile idle check would otherwise
         reject a single busy profile even when the agent is under
         cap, so we need a second idle slot for the resume case.
      2. A wrapper simulator acks the SOUL apply row (the Round-3
         dispatch path otherwise times out at 10s/task).
    """
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Set up: 1 agent, 1 role, cap=2
    await client.post(
        "/api/agents/",
        json={"agent_id": "agent-cap", "max_concurrent_tasks": 2},
    )
    app = client._transport.app  # type: ignore[attr-defined]
    db = app.state.db

    # Create TWO agents, each with a "researcher" profile. The
    # original test had one agent (sufficient for the legacy
    # per-agent cap check), but the Round-3 routing engine's
    # per-profile "idle" check needs a second idle slot so the
    # resume case can route somewhere. The schema's
    # `UNIQUE(agent_id, name)` constraint prevents two profiles
    # with the same role on one agent, so we add a second agent.
    # The cap on the second agent is the default (1) — enough for
    # one of the pending tasks to route there after t-cap-0 frees
    # up a slot on the first agent.
    import secrets as _secrets
    profile_id = "p-" + _secrets.token_hex(4)
    await db.insert("agent_profiles", {
        "id": profile_id,
        "agent_id": "agent-cap",
        "name": "researcher",
        "status": "idle",
    })
    await client.post(
        "/api/agents/",
        json={"agent_id": "agent-cap-2"},  # default cap=1
    )
    profile_id_2 = "p-" + _secrets.token_hex(4)
    await db.insert("agent_profiles", {
        "id": profile_id_2,
        "agent_id": "agent-cap-2",
        "name": "researcher",
        "status": "idle",
    })

    # Make the agents "verified" so the supervisor picks them. The
    # Round-3 dispatch path goes through orchestrator.soul_dispatch
    # which uses the routing engine — that engine considers a profile
    # "online" only if the parent agent's `last_heartbeat_at` is
    # within 90s. Setting it here (rather than relying on the
    # default NULL) keeps the legacy "verified ⇒ routable" semantic
    # that the cap test was written against.
    from datetime import datetime, timezone as _tz
    now_iso = datetime.now(_tz.utc).astimezone().isoformat()
    await db.execute(
        "UPDATE agents SET status = 'verified', hmac_secret = ?, "
        "last_heartbeat_at = ? WHERE id IN (?, ?)",
        ("dummysecret", now_iso, "agent-cap", "agent-cap-2"),
    )

    # Create a project + 4 tasks all needing role=researcher
    pid = "proj-cap-test"
    await db.insert("projects", {
        "id": pid, "name": "cap test", "goal": "x", "state": "ready",
    })
    for i in range(4):
        tid = f"t-cap-{i}"
        await db.insert("tasks", {
            "id": tid,
            "project_id": pid,
            "name": f"step-{i}",
            "agent_role": "researcher",
            "depends_on": "[]",
            "on_parent_failure": "skip",
            "status": "pending",
            "priority": "normal",
            "action": "noop",
            "params": "{}",
            "retry_count": 0,
            "max_retries": 0,
        })

    # Build a Supervisor with a no-op planner (we don't need LLM here)
    from hermes_orch.core.supervisor import Supervisor
    from hermes_orch.core.planner import Planner
    from hermes_orch.core.notifier import Notifier

    class _StubNotifier(Notifier):
        def __init__(self):
            # bypass Notifier.__init__ (we don't need telegram config)
            self.enabled = False

        async def send(self, *a, **kw): return None

    cfg = {"projects": {"storage_root": str(tmp_path := app.state.db.db_path + "_storage")}}
    import pathlib
    pathlib.Path(cfg["projects"]["storage_root"]).mkdir(parents=True, exist_ok=True)
    sup = Supervisor(
        db=db,
        cfg=cfg,
        notifier=_StubNotifier(),
        planner=Planner(cfg={"llm": {"mock": True}}, db=db),
    )

    # First, force-claim 2 tasks (assigned+running) to simulate the
    # wrapper having 2 tasks in flight. This is what the cap query
    # counts.
    for i in range(2):
        tid = f"t-cap-{i}"
        await db.execute(
            "UPDATE tasks SET status = 'assigned', assigned_agent_id = ?, "
            "assigned_profile_id = ? WHERE id = ?",
            ("agent-cap", profile_id, tid),
        )
        await db.execute(
            "UPDATE agent_profiles SET status = 'busy', current_task_id = ? "
            "WHERE id = ?",
            (tid, profile_id),
        )

    # Now run the supervisor's _drive_single_tasks (simpler path than
    # full _drive_project; same _assign_task under the hood)
    await sup._drive_single_tasks()

    # The first 2 tasks should be in 'assigned' (already were) and
    # the next 2 should still be in 'pending' — the supervisor
    # should NOT have assigned them because the agent is at cap=2.
    rows = await db.fetchall(
        "SELECT id, status FROM tasks WHERE project_id = ? ORDER BY id", (pid,)
    )
    by_status = {r["id"]: r["status"] for r in rows}
    assert by_status["t-cap-0"] == "assigned"
    assert by_status["t-cap-1"] == "assigned"
    assert by_status["t-cap-2"] == "pending", (
        f"task t-cap-2 should remain pending (agent at cap=2), got "
        f"{by_status['t-cap-2']}"
    )
    assert by_status["t-cap-3"] == "pending", (
        f"task t-cap-3 should remain pending (agent at cap=2), got "
        f"{by_status['t-cap-3']}"
    )

    # Now free one slot (mark t-cap-0 as completed) and re-run.
    # The supervisor should pick up t-cap-2 OR t-cap-3 (FIFO).
    await db.execute(
        "UPDATE tasks SET status = 'completed' WHERE id = 't-cap-0'"
    )
    await db.execute(
        "UPDATE agent_profiles SET status = 'idle', current_task_id = NULL "
        "WHERE current_task_id = 't-cap-0'"
    )
    # Project is still in 'ready' state so the supervisor will drive
    # the next ready task via _drive_project (the test setup used
    # state=ready, not the single-tasks flow). The cap check is
    # in _assign_task either way; we test that.
    # Mark one of the single-task-style flow to actually exercise
    # _assign_task directly via _drive_project.
    ready = await db.fetchall(
        "SELECT * FROM tasks WHERE project_id = ? AND status = 'pending' "
        "AND depends_on = '[]' ORDER BY created_at",
        (pid,),
    )
    # Force the 2 pending tasks to have no deps (they already do)
    # and run a single _assign_task against the project flow.
    # Simplest: re-call _drive_project for state=ready.
    proj = await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,))
    # v3.9.0: spawn a wrapper simulator so the Round-3 SOUL apply
    # succeeds (otherwise the dispatch path times out at 10s/task).
    sim_stop = asyncio.Event()
    sim_task = asyncio.create_task(_wrapper_simulator(db, sim_stop))
    try:
        # The Round-3 dispatch path is a two-tick flow:
        #   tick 1: `_dispatch_via_soul_dispatch` calls `dispatch_step`
        #           which routes + applies SOUL + creates a NEW tasks
        #           row (status=pending) with the resolved profile.
        #           The ORIGINAL workflow step task is marked
        #           'dispatched' so it doesn't get re-dispatched.
        #   tick 2: `_assign_task` picks up the new pending row,
        #           respects the pre-resolved `assigned_profile_id`,
        #           checks the per-agent cap, and transitions to
        #           'assigned'.
        # Run the project loop twice so the new task is actually
        # 'assigned' (not just 'pending') by the time we assert.
        await sup._drive_project(proj)
        await sup._drive_project(proj)
    finally:
        sim_stop.set()
        await sim_task

    rows = await db.fetchall(
        "SELECT id, status FROM tasks WHERE project_id = ? ORDER BY id", (pid,)
    )
    by_status = {r["id"]: r["status"] for r in rows}
    # t-cap-0 is still 'completed' (we just set it), t-cap-1 stays
    # 'assigned' (untouched). The Round-3 dispatch flow marks the
    # ORIGINAL workflow step task as 'dispatched' (not 'pending')
    # and creates a NEW task with the resolved profile. That new
    # task is the one that ends up 'assigned' after tick 2. So
    # we count the new 'assigned' tasks (not the originals) and
    # verify the cap-check deferred exactly one of the originals.
    new_assigned = [r for r in rows if r["status"] == "assigned"
                   and r["id"] not in ("t-cap-0", "t-cap-1")]
    assert len(new_assigned) == 1, (
        f"After freeing 1 slot, exactly 1 new dispatched task should be "
        f"assigned; got {len(new_assigned)}, by_status={by_status}"
    )
    # The other original task is also 'dispatched' (the Round-3
    # flow always marks the original as 'dispatched' after calling
    # dispatch_step, regardless of whether the new task was
    # successfully transitioned to 'assigned' on a follow-up
    # tick). The "deferred" signal here is: only 1 new task
    # became 'assigned' (not 2). The other original's new task
    # either doesn't exist (NoProfileAvailable → defer) or exists
    # but is 'pending' (waiting for a future tick to transition).
    # Both outcomes are valid; the test asserts the "at most 1
    # new assignment" invariant (cap=2, 1 slot was free).
    dispatched_originals = sum(
        1 for tid in ("t-cap-2", "t-cap-3") if by_status.get(tid) == "dispatched"
    )
    assert dispatched_originals == 2, (
        f"Both t-cap-2/t-cap-3 should be 'dispatched' (Round-3 marks the "
        f"original after dispatch_step); got {dispatched_originals}, "
        f"by_status={by_status}"
    )


@pytest.mark.asyncio
async def test_supervisor_dispatches_when_under_cap(client):
    """Counterpart to the at-cap test: when the agent is under cap,
    dispatch should happen normally. Without this, the cap check
    could be too aggressive (e.g. accidentally skipping when
    in_flight < cap)."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    await client.post(
        "/api/agents/",
        json={"agent_id": "agent-under", "max_concurrent_tasks": 4},
    )
    # Round-3 dispatch goes through the routing engine which requires
    # a fresh `last_heartbeat_at` (< 90s) for a profile to count as
    # "online". Compute it once here and reuse for the UPDATE below.
    from datetime import datetime as _dt, timezone as _tz2
    now_iso = _dt.now(_tz2.utc).astimezone().isoformat()
    app = client._transport.app  # type: ignore[attr-defined]
    db = app.state.db
    import secrets as _secrets
    profile_id = "p-" + _secrets.token_hex(4)
    await db.insert("agent_profiles", {
        "id": profile_id,
        "agent_id": "agent-under",
        "name": "writer",
        "status": "idle",
    })
    await db.execute(
        "UPDATE agents SET status = 'verified', hmac_secret = ?, "
        "last_heartbeat_at = ? WHERE id = ?",
        ("dummysecret", now_iso, "agent-under"),
    )
    pid = "proj-under"
    await db.insert("projects", {
        "id": pid, "name": "under test", "goal": "x", "state": "ready",
    })
    for i in range(2):
        tid = f"t-under-{i}"
        await db.insert("tasks", {
            "id": tid,
            "project_id": pid,
            "name": f"step-{i}",
            "agent_role": "writer",
            "depends_on": "[]",
            "on_parent_failure": "skip",
            "status": "pending",
            "priority": "normal",
            "action": "noop",
            "params": "{}",
            "retry_count": 0,
            "max_retries": 0,
        })

    from hermes_orch.core.supervisor import Supervisor
    from hermes_orch.core.planner import Planner
    from hermes_orch.core.notifier import Notifier

    class _StubNotifier(Notifier):
        def __init__(self):
            # bypass Notifier.__init__ (we don't need telegram config)
            self.enabled = False

        async def send(self, *a, **kw): return None

    import pathlib
    storage = pathlib.Path(str(app.state.db.db_path) + "_storage")
    storage.mkdir(parents=True, exist_ok=True)
    cfg = {"projects": {"storage_root": str(storage)}}
    sup = Supervisor(
        db=db, cfg=cfg,
        notifier=_StubNotifier(),
        planner=Planner(cfg={"llm": {"mock": True}}, db=db),
    )
    proj = await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,))
    # v3.9.0: spawn a wrapper simulator so the Round-3 SOUL apply
    # succeeds (otherwise the dispatch path times out at 10s/task).
    sim_stop = asyncio.Event()
    sim_task = asyncio.create_task(_wrapper_simulator(db, sim_stop))
    try:
        # Round-3 dispatch is a two-tick flow: tick 1 creates the
        # new dispatched task (status=pending), tick 2 transitions
        # it to 'assigned'. The originals are marked 'dispatched'
        # after tick 1, so we check the NEW tasks (not the
        # originals) for the 'assigned' status.
        await sup._drive_project(proj)
        await sup._drive_project(proj)
    finally:
        sim_stop.set()
        await sim_task
    rows = await db.fetchall(
        "SELECT id, status FROM tasks WHERE project_id = ? ORDER BY id", (pid,)
    )
    by_status = {r["id"]: r["status"] for r in rows}
    # The new dispatched tasks (with UUIDs) should be 'assigned';
    # the originals (t-under-0, t-under-1) should be 'dispatched'.
    new_assigned = [r for r in rows if r["status"] == "assigned"
                   and r["id"] not in ("t-under-0", "t-under-1")]
    assert len(new_assigned) == 2, (
        f"Both new dispatched tasks should be assigned (no prior in-flight, "
        f"cap=4); got {len(new_assigned)}, by_status={by_status}"
    )
