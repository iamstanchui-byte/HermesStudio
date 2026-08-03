"""Tests for v3.12.1 follow-up #3: runtime invariant check.

The supervisor's `_check_duplicate_running` runs every tick and
detects projects with >1 'running' rows sharing the same step
name. The check feeds a dashboard warning banner via
`get_duplicate_running_for_project()`.

This test asserts:
  1. Two pending tasks with the same name get marked running ->
     invariant is detected, cache populated, audit_log fired.
  2. Cache clears when the duplicate is resolved (one task
     completed) and a 'resolved' audit_log fires.
  3. No duplicate -> cache stays empty (the common case).
  4. Three duplicates on the same name still surface the name once
     in the cache (per-name, not per-row).
  5. Different projects' duplicates don't leak across (per-project
     isolation).
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
ADMIN_PASSWORD = "test-password-for-invariant-test"


async def _bootstrap_admin(app) -> None:
    """Mimic test_users_api.py: set the bootstrap admin password."""
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
    """Log in via the real /api/auth/login endpoint. Stashes the
    session cookie on the AsyncClient so subsequent requests pass
    the _RequireUserMiddleware."""
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
        (pid, f"invariant-{pid}"),
    )


async def _seed_task(
    app, *, pid: str, tid: str, name: str, status: str = "pending"
) -> None:
    db = app.state.db
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, "
        "depends_on, on_parent_failure, status, priority, action, "
        "params, retry_count, max_retries, timeout_seconds, "
        "output_path, required_capability, feedback_to, "
        "is_single_task, archived, created_at) "
        "VALUES (?, ?, ?, '', '[]', 'skip', ?, 'normal', "
        "'do_task', '{}', 0, 2, 1800, '', NULL, '[]', "
        "0, 0, ?)",
        (tid, pid, name, status, _now_iso()),
    )


# ===== invariant detection =====


@pytest.mark.asyncio
async def test_invariant_detects_two_running_same_name(client):
    """v3.12.1 follow-up #3: 2 'running' rows with the same
    name in the same project -> invariant violation, cache
    populated, audit_log fired.
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)
    await _seed_task(app, pid=pid, tid="t-a", name="check-total", status="running")
    await _seed_task(app, pid=pid, tid="t-b", name="check-total", status="running")

    # Cache is empty before the check.
    assert sup.get_duplicate_running_for_project(pid) == []

    # Run the check.
    await sup._check_duplicate_running()

    # Cache now flags the project with the duplicate step name.
    flagged = sup.get_duplicate_running_for_project(pid)
    assert flagged == ["check-total"], (
        f"expected ['check-total'] in cache, got {flagged}"
    )
    # Full map also reflects it.
    full_map = sup.get_duplicate_running_projects()
    assert full_map == {pid: ["check-total"]}, (
        f"full map mismatch: {full_map}"
    )

    # Audit log fired with the right event type + payload.
    rows = await db.fetchall(
        "SELECT event_type, project_id, payload FROM audit_log "
        "WHERE event_type = 'project.duplicate_running_detected' "
        "AND project_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (pid,),
    )
    assert rows, "expected audit_log entry for duplicate_running_detected"
    import json
    payload = json.loads(rows[0]["payload"])
    assert payload["name"] == "check-total"
    assert payload["count"] == 2
    assert set(payload["task_ids"]) == {"t-a", "t-b"}


@pytest.mark.asyncio
async def test_invariant_no_duplicate_cache_stays_empty(client):
    """Sanity check: 1 running task, no duplicate -> cache empty,
    no audit log. The common-case path; cheap scan should be
    near-zero overhead when invariant holds.
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)
    await _seed_task(app, pid=pid, tid="t-a", name="check-total", status="running")

    await sup._check_duplicate_running()
    assert sup.get_duplicate_running_for_project(pid) == []
    assert sup.get_duplicate_running_projects() == {}

    rows = await db.fetchall(
        "SELECT COUNT(*) AS n FROM audit_log "
        "WHERE event_type LIKE 'project.duplicate_running%' "
        "AND project_id = ?",
        (pid,),
    )
    assert rows[0]["n"] == 0, "no audit log should fire on a clean project"


@pytest.mark.asyncio
async def test_invariant_resolved_fires_resolved_audit_log(client):
    """v3.12.1 follow-up #3: when a previously-flagged project
    comes back to clean (one running row ended), the cache
    clears AND a 'resolved' audit log fires so operators can
    see when the warning cleared.
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)
    await _seed_task(app, pid=pid, tid="t-a", name="check-total", status="running")
    await _seed_task(app, pid=pid, tid="t-b", name="check-total", status="running")

    # First tick: detect.
    await sup._check_duplicate_running()
    assert sup.get_duplicate_running_for_project(pid) == ["check-total"]

    # Mark one task completed (the wrapper reported it done).
    await db.execute(
        "UPDATE tasks SET status = 'completed', ended_at = ? WHERE id = ?",
        (_now_iso(), "t-a"),
    )

    # Second tick: should resolve.
    await sup._check_duplicate_running()
    assert sup.get_duplicate_running_for_project(pid) == [], (
        f"expected empty after resolution, got "
        f"{sup.get_duplicate_running_for_project(pid)}"
    )

    rows = await db.fetchall(
        "SELECT event_type FROM audit_log "
        "WHERE event_type = 'project.duplicate_running_resolved' "
        "AND project_id = ?",
        (pid,),
    )
    assert rows, "expected a 'resolved' audit log entry"


@pytest.mark.asyncio
async def test_invariant_three_duplicates_single_name_in_cache(client):
    """3 running rows with the same name -> the name appears
    ONCE in the cache list (per-name, not per-row). The count
    is in the audit payload, not the cache.
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)
    for tid in ("t-a", "t-b", "t-c"):
        await _seed_task(app, pid=pid, tid=tid, name="x", status="running")

    await sup._check_duplicate_running()
    flagged = sup.get_duplicate_running_for_project(pid)
    assert flagged == ["x"], (
        f"name should appear once in cache; got {flagged}"
    )

    # Audit payload has the count=3.
    import json
    rows = await db.fetchall(
        "SELECT payload FROM audit_log "
        "WHERE event_type = 'project.duplicate_running_detected' "
        "AND project_id = ? ORDER BY id DESC LIMIT 1",
        (pid,),
    )
    payload = json.loads(rows[0]["payload"])
    assert payload["count"] == 3
    assert set(payload["task_ids"]) == {"t-a", "t-b", "t-c"}


@pytest.mark.asyncio
async def test_invariant_per_project_isolation(client):
    """Two projects each with their own duplicate don't leak
    across. The cache is keyed by project_id, so the
    projects_list ⚠️ icon only fires for the affected one.
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid_a = f"proj-{uuid.uuid4().hex[:8]}"
    pid_b = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid_a)
    await _seed_project(app, pid_b)
    # Project A: 2 running same name
    await _seed_task(app, pid=pid_a, tid="t-a1", name="step", status="running")
    await _seed_task(app, pid=pid_a, tid="t-a2", name="step", status="running")
    # Project B: clean
    await _seed_task(app, pid=pid_b, tid="t-b1", name="step", status="running")

    await sup._check_duplicate_running()

    assert sup.get_duplicate_running_for_project(pid_a) == ["step"]
    assert sup.get_duplicate_running_for_project(pid_b) == []
    full_map = sup.get_duplicate_running_projects()
    assert full_map == {pid_a: ["step"]}, (
        f"only A should be in map: {full_map}"
    )


# ===== API endpoint =====


@pytest.mark.asyncio
async def test_diagnostics_endpoint_clean_project(client):
    """The /api/projects/{id}/diagnostics endpoint returns
    duplicate_running=false for a clean project. Uses the
    supervisor's cache (so requires the app.state.supervisor
    to be set, which the lifespan context does).
    """
    ac, app = client
    # Bootstrap admin + log in so the request passes the
    # _RequireUserMiddleware. The diagnostics endpoint itself
    # is dashboard-facing (no per-route auth), so the user
    # cookie is enough.
    await _bootstrap_admin(app)
    await _login(ac)
    # Wire the supervisor into app.state the way main.py does.
    sup = _make_supervisor(app)
    app.state.supervisor = sup
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)
    await _seed_task(app, pid=pid, tid="t-a", name="step", status="running")

    r = await ac.get(f"/api/projects/{pid}/diagnostics")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == pid
    assert body["duplicate_running"] is False
    assert body["duplicate_running_names"] == []
    assert body["duplicate_running_counts"] == {}


@pytest.mark.asyncio
async def test_diagnostics_endpoint_violation(client):
    """The endpoint surfaces the duplicate + count map when
    the invariant is violated. Operators can hit this URL
    directly to debug without going through the dashboard.
    """
    ac, app = client
    await _bootstrap_admin(app)
    await _login(ac)
    sup = _make_supervisor(app)
    app.state.supervisor = sup
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await _seed_project(app, pid)
    await _seed_task(app, pid=pid, tid="t-a", name="x", status="running")
    await _seed_task(app, pid=pid, tid="t-b", name="x", status="running")
    # Trigger the check to populate the supervisor's cache.
    await sup._check_duplicate_running()

    r = await ac.get(f"/api/projects/{pid}/diagnostics")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["duplicate_running"] is True
    assert body["duplicate_running_names"] == ["x"]
    assert body["duplicate_running_counts"] == {"x": 2}


@pytest.mark.asyncio
async def test_diagnostics_endpoint_404_for_unknown_project(client):
    ac, app = client
    await _bootstrap_admin(app)
    await _login(ac)
    r = await ac.get("/api/projects/proj-does-not-exist/diagnostics")
    assert r.status_code == 404
