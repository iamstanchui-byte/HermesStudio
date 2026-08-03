"""Tests for v3.12.1 follow-up #6: per-task conversation-history window.

Asserts:
  1. Pydantic: ProjectPlan.max_history_turns default is None
     (use server default), accepts 0..200, rejects negative
     and >200.
  2. plan_json round-trips max_history_turns.
  3. Legacy plans (no max_history_turns) default to None.
  4. _create_dispatched_task writes `_max_history_turns` into
     task.params with the resolved effective value:
       a. step_dict override (highest)
       b. project plan_json override
       c. server default (6)
  5. /api/agents/{id}/max_history_config endpoint returns
     the server default.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hermes_orch import db as db_mod
from hermes_orch import main as main_mod
from hermes_orch.api.plans import ProjectPlan, PlanStep


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
ADMIN_PASSWORD = "test-password-for-history-test"


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


def test_project_plan_default_max_history_turns_is_none():
    """NULL means "use server default" — the plan is
    silent on the topic and the server's config.yaml value
    applies.
    """
    p = ProjectPlan(
        version="1.0",
        steps=[PlanStep(name="a", action="do_a")],
    )
    assert p.max_history_turns is None


def test_project_plan_accepts_valid_max_history_turns():
    """Explicit values 0..200 round-trip cleanly.
    0 = no cap (every call starts fresh; useful for
    long-context workflows). 200 = the upper bound to
    avoid accidentally OOM'ing the wrapper.
    """
    for v in (0, 1, 6, 50, 200):
        p = ProjectPlan(
            version="1.0",
            steps=[PlanStep(name="a", action="do_a")],
            max_history_turns=v,
        )
        assert p.max_history_turns == v


def test_project_plan_rejects_out_of_range_max_history_turns():
    """Negative or >200 values are almost certainly typos
    and would either OOM the wrapper or silently disable
    history. Pydantic validation catches them at the API
    boundary so they never reach the dispatch path.
    """
    for bad in (-1, 201, 1000, -100):
        with pytest.raises(Exception):
            ProjectPlan(
                version="1.0",
                steps=[PlanStep(name="a", action="do_a")],
                max_history_turns=bad,
            )


# ===== plan_json round-trip =====


@pytest.mark.asyncio
async def test_plan_json_round_trips_max_history_turns(client):
    """PUT a plan with max_history_turns=12 and GET it back
    — the field survives the round trip through
    projects.plan_json.
    """
    ac, app = client
    await _bootstrap_admin(app)
    await _login(ac)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state) "
        "VALUES (?, ?, '', 'planned')",
        (pid, f"history-{pid}"),
    )
    plan = ProjectPlan(
        version="1.0",
        name="my-plan",
        steps=[PlanStep(name="a", action="do_a")],
        max_history_turns=12,
    )
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json={"plan": json.loads(plan.model_dump_json())},
    )
    assert r.status_code == 200, r.text

    r = await ac.get(f"/api/projects/{pid}/plan")
    body = r.json()
    assert body["plan"]["max_history_turns"] == 12


@pytest.mark.asyncio
async def test_legacy_plan_json_defaults_to_none(client):
    """Backward compat: a v3.11-era plan_json (no
    max_history_turns field) parses cleanly with the field
    defaulting to None — the server's default_max_history_turns
    applies at dispatch time.
    """
    ac, app = client
    await _bootstrap_admin(app)
    await _login(ac)
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state) "
        "VALUES (?, ?, '', 'planned')",
        (pid, f"legacy-history-{pid}"),
    )
    legacy = json.dumps({
        "version": "1.0",
        "name": "",
        "steps": [{"name": "a", "action": "do_a", "agent_role": ""}],
        "variables": [],
        "visual_layout": {},
    })
    await db.execute(
        "UPDATE projects SET plan_json = ? WHERE id = ?",
        (legacy, pid),
    )

    r = await ac.get(f"/api/projects/{pid}/plan")
    body = r.json()
    assert body["plan"]["max_history_turns"] is None


# ===== dispatch resolution =====


@pytest.mark.asyncio
async def test_dispatch_writes_default_6_when_no_override(client):
    """If neither the step nor the plan sets
    max_history_turns, the dispatch path falls back to the
    defensive default of 6. (Future: read from the app's
    config; for now the hard-coded value is the documented
    default.)
    """
    from hermes_orch.orchestrator.soul_dispatch import _create_dispatched_task

    ac, app = client
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state) "
        "VALUES (?, ?, '', 'planned')",
        (pid, f"default-history-{pid}"),
    )
    # No plan_json — legacy / pre-#6 project.
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    profile_id = f"prof-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agents (id, secret_hash, status) "
        "VALUES (?, 'fake-hash', 'online')",
        (agent_id,),
    )
    await db.execute(
        "INSERT INTO agent_profiles (id, agent_id, name, status) "
        "VALUES (?, ?, 'super', 'idle')",
        (profile_id, agent_id),
    )
    step = {
        "name": "step",
        "agent_role": "super",
        "depends_on": [],
        "feedback_to": [],
        "required_capabilities": [],
        "action": "do_step",
        "output_path": "",
        "params_template": {},
    }
    profile = {"id": profile_id, "agent_id": agent_id}
    with patch("hermes_orch.orchestrator.soul_dispatch.audit_log", new=MagicMock()):
        await _create_dispatched_task(pid, step, profile, db)

    row = await db.fetchone(
        "SELECT params FROM tasks WHERE project_id = ? AND name = 'step'",
        (pid,),
    )
    params = json.loads(row["params"])
    assert params["_max_history_turns"] == 6, (
        f"expected default 6; got {params}"
    )


@pytest.mark.asyncio
async def test_dispatch_honours_plan_json_override(client):
    """If plan_json.max_history_turns is set, the dispatch
    path uses that value (per-workflow override).
    """
    from hermes_orch.orchestrator.soul_dispatch import _create_dispatched_task

    ac, app = client
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state) "
        "VALUES (?, ?, '', 'planned')",
        (pid, f"plan-override-{pid}"),
    )
    # Set a per-plan override.
    plan_json = json.dumps({
        "version": "1.0",
        "name": "",
        "steps": [],
        "variables": [],
        "visual_layout": {},
        "max_history_turns": 20,
    })
    await db.execute(
        "UPDATE projects SET plan_json = ? WHERE id = ?",
        (plan_json, pid),
    )
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    profile_id = f"prof-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agents (id, secret_hash, status) "
        "VALUES (?, 'fake-hash', 'online')",
        (agent_id,),
    )
    await db.execute(
        "INSERT INTO agent_profiles (id, agent_id, name, status) "
        "VALUES (?, ?, 'super', 'idle')",
        (profile_id, agent_id),
    )
    step = {
        "name": "step",
        "agent_role": "super",
        "depends_on": [],
        "feedback_to": [],
        "required_capabilities": [],
        "action": "do_step",
        "output_path": "",
        "params_template": {},
    }
    profile = {"id": profile_id, "agent_id": agent_id}
    with patch("hermes_orch.orchestrator.soul_dispatch.audit_log", new=MagicMock()):
        await _create_dispatched_task(pid, step, profile, db)

    row = await db.fetchone(
        "SELECT params FROM tasks WHERE project_id = ? AND name = 'step'",
        (pid,),
    )
    params = json.loads(row["params"])
    assert params["_max_history_turns"] == 20, (
        f"expected plan override 20; got {params}"
    )


@pytest.mark.asyncio
async def test_dispatch_honours_step_dict_override(client):
    """Per-step override (highest priority) wins over the
    plan-level override. Useful for a single step that needs
    extra context (e.g. the final synthesis step).
    """
    from hermes_orch.orchestrator.soul_dispatch import _create_dispatched_task

    ac, app = client
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state) "
        "VALUES (?, ?, '', 'planned')",
        (pid, f"step-override-{pid}"),
    )
    plan_json = json.dumps({
        "version": "1.0",
        "name": "",
        "steps": [],
        "variables": [],
        "visual_layout": {},
        "max_history_turns": 6,  # plan says 6
    })
    await db.execute(
        "UPDATE projects SET plan_json = ? WHERE id = ?",
        (plan_json, pid),
    )
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    profile_id = f"prof-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agents (id, secret_hash, status) "
        "VALUES (?, 'fake-hash', 'online')",
        (agent_id,),
    )
    await db.execute(
        "INSERT INTO agent_profiles (id, agent_id, name, status) "
        "VALUES (?, ?, 'super', 'idle')",
        (profile_id, agent_id),
    )
    step = {
        "name": "synthesis",
        "agent_role": "super",
        "depends_on": [],
        "feedback_to": [],
        "required_capabilities": [],
        "action": "do_step",
        "output_path": "",
        "params_template": {},
        "max_history_turns": 50,  # step says 50 (overrides plan)
    }
    profile = {"id": profile_id, "agent_id": agent_id}
    with patch("hermes_orch.orchestrator.soul_dispatch.audit_log", new=MagicMock()):
        await _create_dispatched_task(pid, step, profile, db)

    row = await db.fetchone(
        "SELECT params FROM tasks WHERE project_id = ? AND name = 'synthesis'",
        (pid,),
    )
    params = json.loads(row["params"])
    assert params["_max_history_turns"] == 50, (
        f"expected step override 50 to win over plan 6; got {params}"
    )


# ===== config endpoint =====


@pytest.mark.asyncio
async def test_max_history_config_endpoint_returns_default(client):
    """The wrapper's config-poll endpoint returns the server
    default (config.supervisor.default_max_history_turns).
    The default is 6 per the v3.12.1 #6 design (matches the
    measured 4x prompt growth we observed in commit 20fb097).
    """
    ac, app = client
    # The endpoint is HMAC-gated (wrapper-facing). Bypass auth
    # for the unit test by hitting the underlying function
    # directly with a fake request.
    from hermes_orch.api.agents import get_max_history_config
    from unittest.mock import AsyncMock

    # The endpoint reads from request.app.state.config. The
    # lifespan fixture sets up a real config, so the value
    # comes from DEFAULT_CONFIG (6).
    request = MagicMock()
    request.app = MagicMock()
    request.app.state.config = app.state.config

    result = await get_max_history_config("test-agent", request)
    assert result["agent_id"] == "test-agent"
    assert result["value"] == 6, (
        f"expected default 6 from DEFAULT_CONFIG; got {result}"
    )
    assert result["source"] == "default"
