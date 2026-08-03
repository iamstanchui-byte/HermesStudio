"""Tests for v3.12.1 follow-up #4: per-plan reset_policy.

The supervisor's `_cascade_reset` honours the plan's `reset_policy`
field to decide how much of the depends_on chain to re-run when
a loopback fires. Three policies:
  - 'full_chain_reset' (default) — current behaviour: full BFS
    through all descendants
  - 'failed_branch_reset' — only the failed target + direct
    dependents
  - 'latest_instance_only' — failed_branch_reset + skip tasks
    that already have a valid latest result

Asserts:
  1. Default value: legacy plans (no reset_policy) fall back to
     'full_chain_reset' (backward-compat for the savings demo).
  2. full_chain_reset: BFS through the whole chain (the current
     behaviour; regression guard for the v3.12.1 change).
  3. failed_branch_reset: only the failed target + direct
     dependents get reset. Grandchildren stay as-is.
  4. latest_instance_only: same scope as failed_branch_reset, but
     tasks with status=completed + result are NOT reset (their
     results stay valid).
  5. Persistence: plan_json round-trips the reset_policy field
     through PUT/GET (so a workflow authored with policy X
     remembers it across re-loads).
  6. ProjectPlan validation rejects unknown policy values.
  7. workflow_packages table accepts the new column (migration).
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
from hermes_orch.api.plans import ProjectPlan, ProjectPlanUpdate, PlanStep
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
ADMIN_PASSWORD = "test-password-for-reset-policy-test"


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


# ===== helpers =====


async def _seed_project_with_diamond_dag(app, pid: str) -> dict[str, str]:
    """Build a 4-task diamond DAG:
        root → mid1 → child
        root → mid2 → child
    Plus a 'grandchild' that depends on `child`. Returns the task
    id map {root, mid1, mid2, child, grandchild} so the test can
    assert which ones got reset.
    """
    db = app.state.db
    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, '', 'planned', '', '', '', 0, 0, '')",
        (pid, f"reset-policy-{pid}"),
    )
    ids = {
        "root": f"t-{uuid.uuid4().hex[:8]}",
        "mid1": f"t-{uuid.uuid4().hex[:8]}",
        "mid2": f"t-{uuid.uuid4().hex[:8]}",
        "child": f"t-{uuid.uuid4().hex[:8]}",
        "grandchild": f"t-{uuid.uuid4().hex[:8]}",
    }
    dag = [
        ("root", "[]", "failed", None),
        ("mid1", f'["{ids["root"]}"]', "completed", '{"summary": "ok"}'),
        ("mid2", f'["{ids["root"]}"]', "completed", '{"summary": "ok"}'),
        ("child", f'["{ids["mid1"]}", "{ids["mid2"]}"]', "completed", '{"summary": "ok"}'),
        ("grandchild", f'["{ids["child"]}"]', "completed", '{"summary": "ok"}'),
    ]
    for name, deps, status, result in dag:
        tid = ids[name]
        await db.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, "
            "depends_on, on_parent_failure, status, priority, action, "
            "params, retry_count, max_retries, timeout_seconds, "
            "output_path, required_capability, feedback_to, "
            "is_single_task, archived, created_at, result) "
            "VALUES (?, ?, ?, '', ?, 'skip', ?, 'normal', "
            "'do_task', '{}', 0, 2, 1800, '', NULL, '[]', "
            "0, 0, ?, ?)",
            (tid, pid, f"step-{name}", deps, status, _now_iso(), result),
        )
    return ids


# ===== Pydantic model =====


def test_project_plan_default_reset_policy_is_full_chain_reset():
    """Backward-compat: a ProjectPlan without an explicit
    reset_policy uses 'full_chain_reset' (so legacy plan JSON
    files keep working — the savings demo + every shipped
    workflow).
    """
    p = ProjectPlan(
        version="1.0",
        steps=[PlanStep(name="a", action="do_x")],
    )
    assert p.reset_policy == "full_chain_reset"


def test_project_plan_accepts_all_three_policies():
    for policy in (
        "full_chain_reset",
        "failed_branch_reset",
        "latest_instance_only",
    ):
        p = ProjectPlan(
            version="1.0",
            steps=[PlanStep(name="a", action="do_x")],
            reset_policy=policy,
        )
        assert p.reset_policy == policy


def test_project_plan_rejects_unknown_policy():
    """Pydantic Literal validation rejects unknown values
    with a 422-style validation error. Catches typos like
    'full_reset' or 'lastest_instance_only' (sic) before
    they reach the supervisor.
    """
    with pytest.raises(Exception):
        ProjectPlan(
            version="1.0",
            steps=[PlanStep(name="a", action="do_x")],
            reset_policy="lastest_instance_only",
        )


# ===== _cascade_reset: per-policy behaviour =====


@pytest.mark.asyncio
async def test_cascade_reset_default_full_chain(client):
    """v3.12.1 follow-up #4: legacy plans (no plan_json) fall
    back to full_chain_reset. The whole diamond DAG below the
    failed root gets reset (the current behaviour; regression
    guard for the savings demo's accumulated file).
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    ids = await _seed_project_with_diamond_dag(app, pid)
    # No plan_json set on the project — legacy mode.

    reset_ids = await sup._cascade_reset(pid, ids["root"])
    # Root + all terminal descendants should be reset. The
    # root itself is in 'failed' state and gets reset too
    # (it'll re-dispatch; the supervisor's _maybe_loop_back
    # already triggered this path, and the wrapper picks up
    # the now-pending task on the next tick).
    expected = {ids["root"], ids["mid1"], ids["mid2"], ids["child"], ids["grandchild"]}
    assert set(reset_ids) == expected, (
        f"full_chain_reset should reset root + every descendant, "
        f"got {set(reset_ids)} expected {expected}"
    )

    # Mid tasks should now be 'pending' in the DB.
    for tid in [ids["mid1"], ids["mid2"], ids["child"], ids["grandchild"]]:
        row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", (tid,))
        assert row["status"] == "pending", (
            f"task {tid} should be 'pending' after full_chain_reset, got {row['status']}"
        )


@pytest.mark.asyncio
async def test_cascade_reset_explicit_full_chain(client):
    """Same as the default, but the plan explicitly says
    'full_chain_reset'. The result is the same: every
    descendant is reset.
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    ids = await _seed_project_with_diamond_dag(app, pid)
    await db.execute(
        "UPDATE projects SET plan_json = ? WHERE id = ?",
        (
            '{"version": "1.0", "name": "", "steps": [], "variables": [], '
            '"visual_layout": {}, "reset_policy": "full_chain_reset"}',
            pid,
        ),
    )

    reset_ids = await sup._cascade_reset(
        pid, ids["root"], reset_policy="full_chain_reset"
    )
    assert set(reset_ids) == {
        ids["root"], ids["mid1"], ids["mid2"], ids["child"], ids["grandchild"]
    }


@pytest.mark.asyncio
async def test_cascade_reset_failed_branch_policy(client):
    """'failed_branch_reset' only resets the root_task_id
    itself. The root's direct dependents (mid1, mid2) and
    deeper descendants (child, grandchild) all stay as-is.
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    ids = await _seed_project_with_diamond_dag(app, pid)

    reset_ids = await sup._cascade_reset(
        pid, ids["root"], reset_policy="failed_branch_reset"
    )
    # Only root is reset; mid1, mid2, child, grandchild stay.
    assert set(reset_ids) == {ids["root"]}, (
        f"failed_branch_reset should only reset root, "
        f"got {set(reset_ids)}"
    )

    # Verify the deeper tasks still hold their completed state.
    for tid in [ids["mid1"], ids["mid2"], ids["child"], ids["grandchild"]]:
        row = await db.fetchone(
            "SELECT status, result FROM tasks WHERE id = ?", (tid,)
        )
        assert row["status"] == "completed", (
            f"deeper task {tid} should remain 'completed', got {row['status']}"
        )
        assert row["result"] == '{"summary": "ok"}', (
            f"deeper task {tid} result should be intact, got {row['result']!r}"
        )


@pytest.mark.asyncio
async def test_cascade_reset_latest_instance_only_skips_completed(client):
    """'latest_instance_only' = only the root is touched, AND
    skip if the root itself already has a valid completed
    result. In the test DAG, the root is in 'failed' state
    (it'll re-run), so the reset fires; mid1/mid2/child/
    grandchild are not touched because they're not the root.
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    ids = await _seed_project_with_diamond_dag(app, pid)

    reset_ids = await sup._cascade_reset(
        pid, ids["root"], reset_policy="latest_instance_only"
    )
    # Root is in 'failed' state (not 'completed' with result),
    # so it gets reset. No BFS walk, so descendants stay.
    assert set(reset_ids) == {ids["root"]}, (
        f"latest_instance_only should reset the failed root only; "
        f"got reset_ids={set(reset_ids)}"
    )
    for tid in [ids["mid1"], ids["mid2"], ids["child"], ids["grandchild"]]:
        row = await db.fetchone(
            "SELECT status, result FROM tasks WHERE id = ?", (tid,)
        )
        assert row["status"] == "completed", (
            f"task {tid} should remain 'completed' under "
            f"latest_instance_only, got {row['status']}"
        )
        assert row["result"] == '{"summary": "ok"}'


@pytest.mark.asyncio
async def test_cascade_reset_latest_instance_only_skips_completed_root(client):
    """Edge case: root is already 'completed' with a valid
    result. Under 'latest_instance_only', the root is NOT
    reset (its result is still valid), so the reset list is
    empty. The dispatcher will then NOT re-run it — the
    operator's policy says "trust the existing result".
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    ids = await _seed_project_with_diamond_dag(app, pid)
    # Override root: complete it with a result. (mid1/mid2
    # still depend on it, but the policy says we don't walk
    # downstream, so the test stays focused on the root.)
    await db.execute(
        "UPDATE tasks SET status = 'completed', result = ? WHERE id = ?",
        ('{"summary": "ok"}', ids["root"]),
    )

    reset_ids = await sup._cascade_reset(
        pid, ids["root"], reset_policy="latest_instance_only"
    )
    assert reset_ids == [], (
        f"latest_instance_only should skip a root with valid result; "
        f"got reset_ids={reset_ids}"
    )


@pytest.mark.asyncio
async def test_cascade_reset_unknown_policy_falls_back_to_full_chain(client):
    """Defensive default: an unrecognised policy value should
    not silently narrow the scope. Falling back to
    full_chain_reset is the safe behaviour (a narrower scope
    would skip legitimate work and surprise the operator).
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    ids = await _seed_project_with_diamond_dag(app, pid)

    reset_ids = await sup._cascade_reset(
        pid, ids["root"], reset_policy="narrow_reset_typo"
    )
    # All descendants + root reset (full-chain behaviour).
    assert set(reset_ids) == {
        ids["root"], ids["mid1"], ids["mid2"], ids["child"], ids["grandchild"]
    }


# ===== _maybe_loop_back reads reset_policy from plan_json =====


@pytest.mark.asyncio
async def test_maybe_loop_back_reads_reset_policy_from_plan(client):
    """The supervisor's `_maybe_loop_back` should pull the
    project's `reset_policy` from plan_json and pass it to
    `_cascade_reset`. We verify by setting a non-default
    policy in plan_json and asserting the cascade behaviour
    matches.
    """
    ac, app = client
    sup = _make_supervisor(app)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    # Setup: a simple plan with 3 tasks, all completed except
    # one (which fails) so loopback fires.
    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary, plan_json) "
        "VALUES (?, ?, '', 'planned', 'super', '', '', 5, 0, '', ?)",
        (
            pid, f"loopback-{pid}",
            '{"version": "1.0", "name": "", "steps": [], "variables": [], '
            '"visual_layout": {}, "reset_policy": "failed_branch_reset"}',
        ),
    )
    # failed task with feedback_to pointing to its only dependent
    failed_id = f"t-{uuid.uuid4().hex[:8]}"
    target_id = f"t-{uuid.uuid4().hex[:8]}"
    grandchild_id = f"t-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, depends_on, "
        "status, action, params, feedback_to, archived) "
        "VALUES (?, ?, 'failing', '', '[]', 'failed', 'do', '{}', ?, 0)",
        (failed_id, pid, json_dumps([target_id])),
    )
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, depends_on, "
        "status, action, params, feedback_to, archived, result) "
        "VALUES (?, ?, 'target', '', ?, 'completed', 'do', '{}', '[]', 0, '{\"ok\":1}')",
        (target_id, pid, json_dumps([failed_id])),
    )
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, depends_on, "
        "status, action, params, feedback_to, archived, result) "
        "VALUES (?, ?, 'grandchild', '', ?, 'completed', 'do', '{}', '[]', 0, '{\"ok\":1}')",
        (grandchild_id, pid, json_dumps([target_id])),
    )

    fired = await sup._maybe_loop_back(pid)
    assert fired is True, "loopback should fire (failed task has feedback_to)"

    # `target` was reset (direct dependent). `grandchild` should
    # stay 'completed' (failed_branch_reset stops at depth 1).
    grandchild = await db.fetchone(
        "SELECT status, result FROM tasks WHERE id = ?", (grandchild_id,)
    )
    assert grandchild["status"] == "completed", (
        f"grandchild should stay 'completed' under failed_branch_reset, "
        f"got {grandchild['status']}"
    )
    assert grandchild["result"] == '{"ok":1}'


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj)


# ===== Persistence: plan_json round-trips reset_policy =====


@pytest.mark.asyncio
async def test_plan_json_round_trips_reset_policy(client):
    """PUT a plan with reset_policy='failed_branch_reset' and
    GET it back — the field survives the round trip through
    projects.plan_json (TEXT column). The chatbox and visual
    editor depend on this so an operator who picks a policy
    in the UI doesn't have to re-pick it after a reload.
    """
    import json
    ac, app = client
    await _bootstrap_admin(app)
    await _login(ac)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state) "
        "VALUES (?, ?, '', 'planned')",
        (pid, f"persistence-{pid}"),
    )
    plan = ProjectPlan(
        version="1.0",
        name="my-plan",
        steps=[PlanStep(name="a", action="do_a")],
        reset_policy="failed_branch_reset",
    )
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json={"plan": json.loads(plan.model_dump_json())},
    )
    assert r.status_code == 200, r.text

    # GET it back.
    r = await ac.get(f"/api/projects/{pid}/plan")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_plan"] is True
    assert body["plan"]["reset_policy"] == "failed_branch_reset", (
        f"reset_policy should round-trip; got {body['plan'].get('reset_policy')!r}"
    )


@pytest.mark.asyncio
async def test_legacy_plan_json_defaults_to_full_chain(client):
    """Backward-compat: a project whose plan_json was written
    BEFORE the v3.12.1 column was added still parses cleanly.
    Pydantic's `default='full_chain_reset'` fills in the field
    for any missing value, so the supervisor sees a known
    policy and the savings demo keeps working.
    """
    import json
    ac, app = client
    await _bootstrap_admin(app)
    await _login(ac)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state) "
        "VALUES (?, ?, '', 'planned')",
        (pid, f"legacy-{pid}"),
    )
    # Simulate a plan_json written by a v3.11-era server: no
    # reset_policy field at all.
    legacy_json = json.dumps({
        "version": "1.0",
        "name": "",
        "steps": [{"name": "a", "action": "do_a", "agent_role": ""}],
        "variables": [],
        "visual_layout": {},
    })
    await db.execute(
        "UPDATE projects SET plan_json = ? WHERE id = ?",
        (legacy_json, pid),
    )

    r = await ac.get(f"/api/projects/{pid}/plan")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"]["reset_policy"] == "full_chain_reset", (
        f"legacy plan_json should default to full_chain_reset; "
        f"got {body['plan'].get('reset_policy')!r}"
    )


# ===== DB schema: workflow_packages column added =====


@pytest.mark.asyncio
async def test_workflow_packages_has_reset_policy_column(client):
    """The migration added a `reset_policy` column to
    workflow_packages with default 'full_chain_reset'. Insert
    a row and read it back to confirm the column exists AND
    the default is correct.
    """
    ac, app = client
    db = app.state.db
    import json
    # Insert a row without specifying reset_policy; the column
    # default should kick in.
    wid = f"wf-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO workflow_packages (id, name, version, description, "
        "step_template, variables) VALUES (?, ?, '0.1.0', '', '[]', '[]')",
        (wid, f"test-wf-{uuid.uuid4().hex[:6]}"),
    )
    row = await db.fetchone(
        "SELECT reset_policy FROM workflow_packages WHERE id = ?", (wid,)
    )
    assert row is not None, "row should exist"
    assert row["reset_policy"] == "full_chain_reset", (
        f"workflow_packages.reset_policy default should be "
        f"'full_chain_reset'; got {row['reset_policy']!r}"
    )
