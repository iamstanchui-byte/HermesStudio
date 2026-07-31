"""Regression test for the v3.5.2 follow-up "depends_on reference mismatch"
bug (2026-07-31).

The bug:
  - MiniMax M3 (the configured LLM) is inconsistent about naming
    conventions across the same plan. The user hit this on
    proj-56c8e080 with the goal "LangGraph vs AutoGen vs CrewAI 分析...":
    the LLM returned 5 steps with kebab-case `name` ("research-langgraph")
    but Title Case + space `depends_on` references ("Research LangGraph").
  - The plan validated and saved, but the canvas couldn't draw wires
    because the depends_on strings didn't match any step name. 7
    dangling references, 0 visible wires.
  - Root cause: the planner's validation at core/planner.py:640 only
    checks "this ref exists earlier in the plan" — it accepts whatever
    naming the LLM is internally consistent with. The endpoint's
    `_to_kebab()` normalises the `name` field but leaves `depends_on`
    untouched, so the mismatch only appears after the conversion.

The fix (in api/plans.py):
  After building all PlanStep objects, walk each step's `depends_on` and
  `feedback_to` and resolve each reference:
    1. exact match against step names
    2. kebab-case the reference, then exact match
    3. case-insensitive match (after kebab-casing both sides)
  Drop dangling references with a logger.warning so the user gets a
  usable plan (just fewer wires for that step) and we have a trail
  for future LLM regressions.

This test uses the in-process test client (AsyncClient + create_app
with monkeypatched db_path) to mock the planner's plan() method
to return plans with the exact Title-Case-when-name-is-kebab pattern
the user saw, and verifies the endpoint returns the steps with all
depends_on references correctly resolved to the kebab-case step names.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch import main as main_mod
from hermes_orch.auth.cookie import ROLE_ADMIN, create_user
from hermes_orch.main import create_app


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


async def _bootstrap_admin(app) -> str:
    """Idempotent admin bootstrap (matches the test_users_api fixture)."""
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


async def _login(ac, username, password):
    r = await ac.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"


async def _create_test_project(ac):
    r = await ac.post(
        "/api/projects/",
        json={"name": "dep-resolution-test", "action": "do_step"},
    )
    assert r.status_code in (200, 201), f"create project failed: {r.status_code} {r.text}"
    return r.json()["id"]


async def _register_profile(ac, role_name: str = "super"):
    """Register an agent + profile so the from-llm endpoint has
    available roles.
    """
    app = ac._transport.app  # type: ignore[attr-defined]
    db = app.state.db
    existing = await db.fetchone("SELECT id FROM agents WHERE id = ?", ("test-host-1",))
    if not existing:
        await db.execute(
            "INSERT INTO agents (id, secret_hash, status, created_at) "
            "VALUES (?, 'test-hash', 'active', CURRENT_TIMESTAMP)",
            ("test-host-1",),
        )
    existing_p = await db.fetchone(
        "SELECT id FROM agent_profiles WHERE name = ?", (role_name,)
    )
    if not existing_p:
        await db.execute(
            "INSERT INTO agent_profiles (id, agent_id, name, status, capabilities, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, 'idle', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (f"ap-{role_name}", "test-host-1", role_name),
        )


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Fresh app + AsyncClient. Patches Database to use tmp_path."""
    from hermes_orch import db as db_mod

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


@pytest.mark.asyncio
async def test_plan_from_llm_resolves_title_case_depends_on_to_kebab(client):
    """User's exact proj-56c8e080 scenario: LLM returns kebab-case `name`
    but Title Case + space `depends_on` references. The endpoint must
    resolve those refs to the kebab-case step names so the canvas can
    draw the wires.

    Pre-fix: 7 dangling references, 0 wires drawn.
    Post-fix: 7 references resolved, all wires present.
    """
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    project_id = await _create_test_project(client)
    await _register_profile(client, "super")
    await _register_profile(client, "win-agent01")

    # The exact LLM output pattern from proj-56c8e080 (kebab names,
    # Title Case refs).
    mock_plan = AsyncMock(return_value=[
        {
            "name": "research-langgraph",
            "agent_role": "super",
            "action": "research_framework",
            "params": {"framework": "LangGraph",
                       "focus": "strengths architecture use_cases"},
            "depends_on": [],
        },
        {
            "name": "research-autogen",
            "agent_role": "super",
            "action": "research_framework",
            "params": {"framework": "AutoGen",
                       "focus": "strengths architecture use_cases"},
            "depends_on": [],
        },
        {
            "name": "research-crewai",
            "agent_role": "super",
            "action": "research_framework",
            "params": {"framework": "CrewAI",
                       "focus": "strengths architecture use_cases"},
            "depends_on": [],
        },
        {
            "name": "compare-frameworks",
            "agent_role": "super",
            "action": "compare_frameworks",
            "params": {"frameworks": "LangGraph,AutoGen,CrewAI",
                       "dimensions":
                           "orchestration,multi_agent,memory,tools,easy_of_use"},
            # BUG: these are Title Case + space; the actual step names
            # are kebab-case. Pre-fix the canvas would not draw wires
            # because no step is named "Research LangGraph".
            "depends_on": ["Research LangGraph", "Research AutoGen", "Research CrewAI"],
        },
        {
            "name": "write-beginner-report",
            "agent_role": "win-agent01",
            "action": "write_report_docx",
            "params": {"topic": "LangGraph vs AutoGen vs CrewAI",
                       "audience": "beginners",
                       "format": "docx",
                       "save_to": "project_temp_folder"},
            # Same issue: Title Case ref, kebab step name.
            "depends_on": ["Compare frameworks"],
        },
    ])
    client._transport.app.state.planner.plan = mock_plan  # type: ignore[attr-defined]

    r = await client.post(
        f"/api/projects/{project_id}/plan/from-llm",
        json={"goal": (
            "LangGraph vs AutoGen vs CrewAI 分析以上的技術, "
            "各有什麼強項, 各個技術的對比, 在現實中可以怎樣應用這些技術, "
            "出一個技術 report 給初接觸者理解"
        )},
    )
    assert r.status_code == 200, (
        f"expected 200 OK after dep resolution, got {r.status_code}: {r.text[:500]}"
    )
    plan = r.json()["plan"]
    steps = plan["steps"]
    step_names = [s["name"] for s in steps]
    assert step_names == [
        "research-langgraph",
        "research-autogen",
        "research-crewai",
        "compare-frameworks",
        "write-beginner-report",
    ]
    # The fix: all depends_on references resolved to the actual step names
    by_name = {s["name"]: s for s in steps}
    assert by_name["compare-frameworks"]["depends_on"] == [
        "research-langgraph",
        "research-autogen",
        "research-crewai",
    ], (
        "compare-frameworks should depend on the three kebab-case "
        "research step names, not the LLM's Title Case versions. "
        f"Got: {by_name['compare-frameworks']['depends_on']}"
    )
    assert by_name["write-beginner-report"]["depends_on"] == [
        "compare-frameworks",
    ], (
        "write-beginner-report should depend on the kebab-case "
        "compare-frameworks name. "
        f"Got: {by_name['write-beginner-report']['depends_on']}"
    )


@pytest.mark.asyncio
async def test_plan_from_llm_drops_dangling_depends_on(client):
    """If the LLM hallucinates a depends_on ref to a non-existent step,
    the endpoint drops the dangling ref with a warning rather than
    failing the whole plan generation. The user still gets a usable
    plan (just one fewer wire for that step).
    """
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    project_id = await _create_test_project(client)
    await _register_profile(client, "super")

    mock_plan = AsyncMock(return_value=[
        {"name": "alpha", "agent_role": "super", "action": "do_task",
         "params": {}, "depends_on": []},
        {"name": "beta", "agent_role": "super", "action": "do_task",
         "params": {},
         "depends_on": ["alpha", "ghost-step", "Alpha"]},  # ghost + casing
    ])
    client._transport.app.state.planner.plan = mock_plan  # type: ignore[attr-defined]

    r = await client.post(
        f"/api/projects/{project_id}/plan/from-llm",
        json={"goal": "test dangling refs"},
    )
    assert r.status_code == 200, (
        f"expected 200 OK with dangling refs dropped, got {r.status_code}: {r.text[:500]}"
    )
    steps = r.json()["plan"]["steps"]
    by_name = {s["name"]: s for s in steps}
    # "alpha" exact match. "ghost-step" dropped (no such step). "Alpha"
    # case-insensitive fallback hits "alpha" via _step_names_lower_index
    # — the resolver keeps both refs (we don't dedup the resolved list;
    # the planner treats depends_on as a set semantically anyway).
    assert by_name["beta"]["depends_on"] == ["alpha", "alpha"], (
        "beta.depends_on should resolve 'alpha' (exact) and 'Alpha' "
        "(case-insensitive fallback to 'alpha'), and drop 'ghost-step' "
        "as dangling. "
        f"Got: {by_name['beta']['depends_on']}"
    )


@pytest.mark.asyncio
async def test_plan_from_llm_resolves_feedback_to_refs_too(client):
    """Same resolver applies to feedback_to (v1.9.4 / v2.0 loop-back
    references). The LLM was just as inconsistent about that field's
    naming in v3.5.2 testing.
    """
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    project_id = await _create_test_project(client)
    await _register_profile(client, "super")

    mock_plan = AsyncMock(return_value=[
        {"name": "build-component", "agent_role": "super", "action": "build",
         "params": {}, "depends_on": []},
        {"name": "run-tests", "agent_role": "super", "action": "test",
         "params": {},
         "depends_on": ["Build component"],
         "feedback_to": ["Build component"]},  # both Title Case
    ])
    client._transport.app.state.planner.plan = mock_plan  # type: ignore[attr-defined]

    r = await client.post(
        f"/api/projects/{project_id}/plan/from-llm",
        json={"goal": "test feedback_to resolution"},
    )
    assert r.status_code == 200
    steps = r.json()["plan"]["steps"]
    by_name = {s["name"]: s for s in steps}
    assert by_name["run-tests"]["depends_on"] == ["build-component"]
    assert by_name["run-tests"]["feedback_to"] == ["build-component"]
