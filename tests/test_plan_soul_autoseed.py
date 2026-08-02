# coding: utf-8
"""Regression test: v3.10.4 plan-save auto-seeds project_soul_presets.

Background (2026-08-02):
  Two related UX bugs in the Generate-plan / Generate-tasks flow:

  1. "Generate tasks" auto-dispatched (state → ready). The user
     had no chance to review/edit the SOUL before the agent
     started. Fix: leave state at 'planned' so the user has
     an explicit review step (click [▶ Run] on the project page).

  2. project_soul_presets only materialized AT dispatch time, so
     by the time the user could see them in the project page's
     "Show SOUL editor" toggle, the task was already running with
     the auto-generated content. Fix: pre-seed presets at
     plan-save time from each step's `default_soul`. The dispatch
     sees the existing preset and uses it (no override). The user
     can then edit the content on the project page BEFORE
     dispatching.

  + a fallback "Generate SOUL" button on the project page
    (POST /api/projects/{id}/plan/generate-soul) for the cases
    where the auto-seed couldn't run (no idle profile at save
    time, plan created before v3.10.4, etc.). Same idempotency
    guarantees as the auto-seed.

This test asserts:
  1. PlanStep has a `default_soul` field (v3.10.4 addition)
  2. plan-save auto-seeds presets from step.default_soul
  3. Idempotency: existing presets are not overwritten
  4. Steps without default_soul are skipped
  5. Roles that can't be routed are skipped (not errored)
  6. Same role appearing in multiple steps → one preset
  7. Legacy params_template["default_soul"] fallback still works
  8. "Generate tasks" no longer sets state='ready' (stays 'planned')
  9. POST /plan/generate-soul endpoint works (fallback path)
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch import db as db_mod
from hermes_orch import main as main_mod
from hermes_orch.api.plans import PlanStep, ProjectPlan
from hermes_orch.auth.cookie import ROLE_ADMIN, create_user


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


# ===== PlanStep default_soul field =====


def test_plan_step_has_default_soul_field():
    """v3.10.4: PlanStep now has a `default_soul` field for the
    LLM-drafted persona text. Pydantic round-trip must preserve it."""
    step = PlanStep(
        name="research-x",
        action="do_task",
        agent_role="analyst",
        default_soul="You are a research analyst.",
    )
    assert step.default_soul == "You are a research analyst."
    d = step.model_dump()
    assert d["default_soul"] == "You are a research analyst."


def test_plan_step_default_soul_optional():
    """`default_soul` is optional (default ""). The model still
    validates without it."""
    step = PlanStep(name="x", action="do_task")
    assert step.default_soul == ""


def test_project_plan_round_trip_with_default_soul():
    """A full ProjectPlan with default_soul on steps round-trips
    through Pydantic without losing the field."""
    plan = ProjectPlan(
        name="test-plan",
        steps=[
            PlanStep(name="s1", action="do_task", agent_role="r1",
                     default_soul="soul 1"),
            PlanStep(name="s2", action="do_task", agent_role="r2",
                     default_soul="soul 2"),
        ],
    )
    d = plan.model_dump()
    assert d["steps"][0]["default_soul"] == "soul 1"
    assert d["steps"][1]["default_soul"] == "soul 2"
    # round-trip
    plan2 = ProjectPlan.model_validate(d)
    assert plan2.steps[0].default_soul == "soul 1"
    assert plan2.steps[1].default_soul == "soul 2"


# ===== Integration: plan-save + auto-seed =====


async def _bootstrap_admin(app) -> str:
    db = app.state.db
    from hermes_orch.auth.cookie import hash_password
    existing = await db.fetchone(
        "SELECT id, password_hash FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    if existing:
        if not existing.get("password_hash"):
            await db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(ADMIN_PASSWORD), existing["id"]),
            )
        return existing["id"]
    user_id = await create_user(
        db, username=ADMIN_USERNAME, password=ADMIN_PASSWORD,
        role=ROLE_ADMIN, is_bootstrap_admin=True,
    )
    await db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(ADMIN_PASSWORD), user_id),
    )
    return user_id


async def _login_admin(ac: AsyncClient) -> None:
    r = await ac.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert r.status_code in (200, 201), r.text


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    orig_db_init = db_mod.Database.__init__

    def patched_db_init(self, db_path):
        orig_db_init(self, test_db)

    monkeypatch.setattr(db_mod.Database, "__init__", patched_db_init)

    app = main_mod.create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        await _bootstrap_admin(app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


def _make_plan_json(step_specs: list[dict]) -> dict:
    """Build a ProjectPlan JSON body for /api/projects/{id}/plan PUT.
    The plan name must be kebab-case (ProjectPlan validator) — no
    spaces, no uppercase. We use a generic name here; the project's
    human-readable name lives on the projects table."""
    return {
        "plan": {
            "name": "test-plan",
            "steps": step_specs,
        }
    }


async def _create_project_with_profile(
    app, agent_id: str | None = None, profile_name: str = "test-profile",
    storage_refs: list | None = None,
) -> tuple[str, str]:
    """Create an agent + profile, return (agent_id, profile_id).
    The profile has no special skills/capabilities — the routing
    engine should pick it as the only option for any role.

    The agent must be 'online' (last_heartbeat_at within 90s) for
    the routing engine to consider it. We set it to NOW explicitly
    using the same ISO-with-tz format that production uses. SQLite's
    CURRENT_TIMESTAMP returns a different format ('YYYY-MM-DD HH:MM:SS'
    no T, no tz) and the routing's `last_heartbeat_at >= cutoff`
    string comparison fails. This was a v3.10.4 integration test
    bug that took 30 min to find — the routing worked in production
    (Linux wrapper writes ISO format) but failed in tests (helper
    used CURRENT_TIMESTAMP).

    Each call creates a NEW agent (unique id by default) so tests
    that create multiple profiles for different roles don't
    collide on the agents.id UNIQUE constraint.
    """
    db = app.state.db
    if agent_id is None:
        agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    # Match the production timestamp format: ISO with timezone offset.
    # hermes_orch.utils.now_aware() returns this format, and
    # routing._list_online_profiles does a string-comparison
    # `last_heartbeat_at >= cutoff` — if the formats mismatch,
    # the agent is treated as offline and the routing fails.
    from datetime import datetime, timezone, timedelta
    now_iso = datetime.now(timezone(timedelta(hours=8))).isoformat()
    await db.execute(
        "INSERT INTO agents (id, secret_hash, hmac_secret, status, "
        "last_heartbeat_at, created_at) "
        "VALUES (?, '', ?, 'verified', ?, ?)",
        (agent_id, "test-secret", now_iso, now_iso),
    )
    profile_id = f"prof-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agent_profiles (id, agent_id, name, description, status, "
        "capabilities, mcp_servers, storage_refs, skills) "
        "VALUES (?, ?, ?, '', 'idle', '{}', '[]', ?, '[]')",
        (
            profile_id, agent_id, profile_name,
            json.dumps(storage_refs or []),
        ),
    )
    return agent_id, profile_id


async def _get_presets(app, project_id: str) -> list[dict]:
    db = app.state.db
    return await db.fetchall(
        "SELECT * FROM project_soul_presets WHERE project_id = ?",
        (project_id,),
    )


@pytest.mark.asyncio
async def test_plan_save_auto_seeds_presets_from_default_soul(client):
    """The happy path: LLM generates a plan with default_soul on
    each step → save the plan → presets exist for each role."""
    ac, app = client
    await _login_admin(ac)
    # Create profiles for BOTH roles in the plan. The routing
    # engine needs at least one idle+online profile per role;
    # otherwise the role is skipped (see test_plan_save_skips_
    # roles_with_no_routable_profile for that branch).
    await _create_project_with_profile(app, profile_name="analyst")
    await _create_project_with_profile(app, profile_name="writer")
    pid = f"proj-soul-{uuid.uuid4().hex[:8]}"
    # Insert a project (no plan yet, state=planned)
    await app.state.db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, 'soul test', 'x', 'planned', '', '', '', 0, 0, '')",
        (pid,),
    )
    # Save a plan with two steps, each with a default_soul
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "s1", "action": "do_task", "agent_role": "analyst",
             "default_soul": "You are a research analyst."},
            {"name": "s2", "action": "do_task", "agent_role": "writer",
             "default_soul": "You are a technical writer."},
        ]),
    )
    assert r.status_code == 200, r.text
    # Presets should exist for both roles
    presets = await _get_presets(app, pid)
    assert len(presets) == 2, f"expected 2 presets, got {len(presets)}: {presets}"
    role_to_soul = {p["role_name"]: p["content"] for p in presets}
    assert role_to_soul["analyst"] == "You are a research analyst."
    assert role_to_soul["writer"] == "You are a technical writer."
    # default_soul is also stored (for future "reset to default")
    for p in presets:
        assert p["default_soul"] == p["content"]


@pytest.mark.asyncio
async def test_plan_save_does_not_overwrite_existing_presets(client):
    """Idempotency: if a preset already exists (e.g., user manually
    edited it), the auto-seed must NOT overwrite it."""
    ac, app = client
    await _login_admin(ac)
    _, profile_id = await _create_project_with_profile(
        app, profile_name="analyst",
    )
    pid = f"proj-soul-{uuid.uuid4().hex[:8]}"
    await app.state.db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, 'soul test', 'x', 'planned', '', '', '', 0, 0, '')",
        (pid,),
    )
    # Manually pre-create a preset with a USER-EDITED content
    user_preset_id = str(uuid.uuid4())
    await app.state.db.execute(
        "INSERT INTO project_soul_presets (id, project_id, profile_id, "
        "role_name, content, default_soul) "
        "VALUES (?, ?, ?, 'analyst', 'I am a manually edited soul.', '')",
        (user_preset_id, pid, profile_id),
    )
    # Now save a plan with a default_soul for the same role
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "s1", "action": "do_task", "agent_role": "analyst",
             "default_soul": "You are a research analyst. (LLM default)"},
        ]),
    )
    assert r.status_code == 200, r.text
    # The user's preset content must be preserved
    presets = await _get_presets(app, pid)
    assert len(presets) == 1
    assert presets[0]["content"] == "I am a manually edited soul.", (
        f"user's manually edited soul was overwritten by auto-seed! "
        f"content={presets[0]['content']!r}"
    )


@pytest.mark.asyncio
async def test_plan_save_skips_steps_without_default_soul(client):
    """Steps without `default_soul` are skipped (the dispatch path
    will use a generic role template as fallback)."""
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="analyst")
    pid = f"proj-soul-{uuid.uuid4().hex[:8]}"
    await app.state.db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, 'soul test', 'x', 'planned', '', '', '', 0, 0, '')",
        (pid,),
    )
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            # has default_soul
            {"name": "s1", "action": "do_task", "agent_role": "analyst",
             "default_soul": "analyst soul"},
            # no default_soul — should be skipped
            {"name": "s2", "action": "do_task", "agent_role": "writer",
             "default_soul": ""},
            # missing field entirely — should be skipped
            {"name": "s3", "action": "do_task", "agent_role": "reviewer"},
        ]),
    )
    assert r.status_code == 200, r.text
    presets = await _get_presets(app, pid)
    role_names = {p["role_name"] for p in presets}
    assert "analyst" in role_names
    assert "writer" not in role_names
    assert "reviewer" not in role_names


@pytest.mark.asyncio
async def test_plan_save_dedupes_same_role_across_steps(client):
    """The same role appearing in multiple steps with the same
    default_soul → only ONE preset is created (deterministic)."""
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="analyst")
    pid = f"proj-soul-{uuid.uuid4().hex[:8]}"
    await app.state.db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, 'soul test', 'x', 'planned', '', '', '', 0, 0, '')",
        (pid,),
    )
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "s1", "action": "do_task", "agent_role": "analyst",
             "default_soul": "shared analyst soul"},
            {"name": "s2", "action": "do_task", "agent_role": "analyst",
             "default_soul": "shared analyst soul"},
            {"name": "s3", "action": "do_task", "agent_role": "analyst",
             "default_soul": "shared analyst soul"},
        ]),
    )
    assert r.status_code == 200, r.text
    presets = await _get_presets(app, pid)
    analyst_presets = [p for p in presets if p["role_name"] == "analyst"]
    assert len(analyst_presets) == 1, (
        f"expected 1 preset for repeated 'analyst' role, got "
        f"{len(analyst_presets)}: {analyst_presets}"
    )


@pytest.mark.asyncio
async def test_plan_save_skips_roles_with_no_routable_profile(client):
    """If the routing engine has no profile for a role (no
    matching agent in the fleet), the auto-seed must NOT crash
    — it skips that role silently. The dispatch path will create
    the preset lazily when an agent is available."""
    ac, app = client
    await _login_admin(ac)
    # Create a profile for "analyst" but NOT for "ghost-role"
    await _create_project_with_profile(app, profile_name="analyst")
    pid = f"proj-soul-{uuid.uuid4().hex[:8]}"
    await app.state.db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, 'soul test', 'x', 'planned', '', '', '', 0, 0, '')",
        (pid,),
    )
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "s1", "action": "do_task", "agent_role": "analyst",
             "default_soul": "analyst soul"},
            # This role has no agent — routing should fail
            {"name": "s2", "action": "do_task", "agent_role": "ghost-role",
             "default_soul": "ghost soul"},
        ]),
    )
    assert r.status_code == 200, r.text  # no 500 crash
    presets = await _get_presets(app, pid)
    role_names = {p["role_name"] for p in presets}
    assert "analyst" in role_names
    assert "ghost-role" not in role_names  # skipped


@pytest.mark.asyncio
async def test_plan_save_falls_back_to_generic_template(client):
    """v3.10.4 follow-up: when a plan step has no `default_soul`
    (because the plan was generated before the planner prompt
    required it, OR because the LLM skipped it), the auto-seed
    should fall back to `_generic_role_template(role)` so the
    user gets a usable starting point. Previously these roles
    were silently skipped and the user had no presets to edit
    — they had to either re-plan or hand-write every SOUL.

    The fallback is the same generic template the dispatch
    path uses (see orchestrator.soul_dispatch._ensure_soul_preset).
    The user can edit it on the project page once it appears.
    """
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="analyst")
    pid = f"proj-soul-{uuid.uuid4().hex[:8]}"
    await app.state.db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, 'soul test', 'x', 'planned', '', '', '', 0, 0, '')",
        (pid,),
    )
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            # Step with NO default_soul — should use generic fallback
            {"name": "s1", "action": "do_task", "agent_role": "analyst"},
        ]),
    )
    assert r.status_code == 200, r.text
    presets = await _get_presets(app, pid)
    assert len(presets) == 1, (
        f"expected 1 preset (with generic fallback), got {len(presets)}: {presets}"
    )
    # The content is the generic template (not empty, not the LLM's output)
    assert "analyst" in presets[0]["content"].lower()
    # The default_soul field is also set (same as content, so a
    # future "reset to default" can find it)
    assert presets[0]["default_soul"] == presets[0]["content"]


@pytest.mark.asyncio
async def test_generate_soul_endpoint_uses_generic_fallback(client):
    """v3.10.4 follow-up: the fallback endpoint also uses the
    generic template when the plan has no default_soul. The
    response includes `roles_used_generic_fallback` so the UI
    can mention which presets need custom editing."""
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="analyst")
    pid = f"proj-fallback-gen-{uuid.uuid4().hex[:8]}"
    await app.state.db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, 'fallback test', 'x', 'planned', '', '', '', 0, 0, '')",
        (pid,),
    )
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "s1", "action": "do_task", "agent_role": "analyst"},
        ]),
    )
    assert r.status_code == 200
    # Clear the auto-seeded preset
    await app.state.db.execute(
        "DELETE FROM project_soul_presets WHERE project_id = ?", (pid,)
    )
    # Call the fallback endpoint
    r = await ac.post(f"/api/projects/{pid}/plan/generate-soul")
    assert r.status_code == 200, r.text
    data = r.json()
    # The endpoint reports the generic fallback count
    assert data["roles_used_generic_fallback"] == 1, (
        f"expected 1 role using generic fallback, got "
        f"{data['roles_used_generic_fallback']}: {data}"
    )
    assert data["presets_created"] == 1
    # Preset content is the generic template
    presets = await _get_presets(app, pid)
    assert len(presets) == 1
    assert "analyst" in presets[0]["content"].lower()


@pytest.mark.asyncio
async def test_plan_save_supports_legacy_params_template_default_soul(client):
    """Pre-v3.10.4 plans stored the SOUL in
    `params_template["default_soul"]`. Backwards compat: the
    auto-seed still picks that up."""
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="analyst")
    pid = f"proj-soul-{uuid.uuid4().hex[:8]}"
    await app.state.db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, 'soul test', 'x', 'planned', '', '', '', 0, 0, '')",
        (pid,),
    )
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "s1", "action": "do_task", "agent_role": "analyst",
             "params_template": {"default_soul": "legacy-form soul"}},
        ]),
    )
    assert r.status_code == 200, r.text
    presets = await _get_presets(app, pid)
    assert len(presets) == 1
    assert presets[0]["content"] == "legacy-form soul"


# ===== No-auto-dispatch: Generate tasks leaves state='planned' =====


@pytest.mark.asyncio
async def test_plan_run_does_not_set_state_to_ready(client):
    """v3.10.4: `Generate tasks` must NOT set state='ready'. The
    project stays in 'planned' so the user can review the
    auto-seeded SOUL before clicking the green [▶ Run] button."""
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="analyst")
    pid = f"proj-norun-{uuid.uuid4().hex[:8]}"
    await app.state.db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, 'no-run test', 'x', 'planned', '', '', '', 0, 0, '')",
        (pid,),
    )
    # Save a plan
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "s1", "action": "do_task", "agent_role": "analyst",
             "default_soul": "analyst soul"},
        ]),
    )
    assert r.status_code == 200, r.text
    # Generate tasks (POST /plan/run)
    r = await ac.post(
        f"/api/projects/{pid}/plan/run",
        json={"archive_existing": True, "name_suffix": ""},
    )
    assert r.status_code == 200, r.text
    # CRITICAL: state must NOT have flipped to 'ready'/'running'
    proj = await app.state.db.fetchone(
        "SELECT state FROM projects WHERE id = ?", (pid,)
    )
    assert proj["state"] == "planned", (
        f"Generate tasks flipped project state to {proj['state']!r} — "
        f"v3.10.4 expects 'planned' so the user can review SOUL "
        f"before clicking [▶ Run]. If the user wants this back to "
        f"auto-dispatch, see the v3.10.4 design doc and don't "
        f"silently revert."
    )
    # Tasks WERE created
    tasks = await app.state.db.fetchall(
        "SELECT id, name, status FROM tasks WHERE project_id = ?", (pid,)
    )
    assert len(tasks) == 1


# ===== Fallback endpoint: POST /plan/generate-soul =====


@pytest.mark.asyncio
async def test_generate_soul_endpoint_seeds_from_existing_plan(client):
    """The fallback endpoint POST /plan/generate-soul re-seeds
    from the project's existing plan_json. Idempotent: skips
    existing presets."""
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="analyst")
    pid = f"proj-fallback-{uuid.uuid4().hex[:8]}"
    await app.state.db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, 'fallback test', 'x', 'planned', '', '', '', 0, 0, '')",
        (pid,),
    )
    # Save a plan WITHOUT a soul (simulate "plan was created
    # before v3.10.4" — no auto-seed happened, and the plan_json
    # has no default_soul fields). But for the fallback to
    # work, we need at least one default_soul. So we add one.
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "s1", "action": "do_task", "agent_role": "analyst",
             "default_soul": "fallback soul"},
        ]),
    )
    assert r.status_code == 200
    # Manually delete the preset that was auto-seeded (simulate
    # the user already had a plan, then deleted the preset, then
    # wants to re-seed)
    await app.state.db.execute(
        "DELETE FROM project_soul_presets WHERE project_id = ?", (pid,)
    )
    assert len(await _get_presets(app, pid)) == 0
    # Call the fallback endpoint
    r = await ac.post(f"/api/projects/{pid}/plan/generate-soul")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["presets_created"] == 1
    assert data["presets_skipped_existing"] == 0
    # Preset is back
    presets = await _get_presets(app, pid)
    assert len(presets) == 1
    assert presets[0]["content"] == "fallback soul"


@pytest.mark.asyncio
async def test_generate_soul_endpoint_404_for_missing_project(client):
    ac, _ = client
    await _login_admin(ac)
    r = await ac.post("/api/projects/proj-does-not-exist/plan/generate-soul")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_generate_soul_endpoint_409_for_project_with_no_plan(client):
    ac, app = client
    await _login_admin(ac)
    pid = f"proj-noplan-{uuid.uuid4().hex[:8]}"
    await app.state.db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, 'no plan', 'x', 'planned', '', '', '', 0, 0, '')",
        (pid,),
    )
    r = await ac.post(f"/api/projects/{pid}/plan/generate-soul")
    assert r.status_code == 409
    assert "No plan" in r.json()["detail"]
