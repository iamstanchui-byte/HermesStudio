"""Regression test for the v3.5.2 "duplicate step name" bug (2026-07-31).

The bug:
  - MiniMax M3 (the configured LLM) sometimes returns two steps with
    the same `name` (e.g. two "langgraph" steps in one plan).
  - ProjectPlan's `_unique_step_names` field_validator rejected the
    duplicate with a Pydantic ValidationError.
  - The endpoint didn't catch it, so FastAPI's default 500 handler
    returned an HTML "Internal Server Error" page.
  - The frontend's `r.json()` then threw "Unexpected token 'I', 'Internal S'...
    is not valid JSON", which the user saw as a cryptic error in the
    "Generate plan" modal.

The fix (in api/plans.py):
  1. Dedupe step names BEFORE constructing ProjectPlan — rename
     duplicates to `<name>-2`, `<name>-3`, etc.
  2. Wrap the ProjectPlan constructor in a try/except that catches
     Pydantic ValidationError and returns a JSON 400 with a helpful
     message (so any future schema tightening doesn't 500 either).

This test uses the in-process test client (AsyncClient + create_app
with monkeypatched db_path) to mock the planner's plan() method
to return duplicate names, and verifies the endpoint handles it
gracefully with a 200 OK and unique step names.
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
        json={"name": "dedup-test", "action": "do_step"},
    )
    assert r.status_code in (200, 201), f"create project failed: {r.status_code} {r.text}"
    return r.json()["id"]


async def _register_profile(ac, role_name: str = "super"):
    """Register an agent + profile so the from-llm endpoint has
    available roles. Direct DB insert — no /api/agents/register needed
    for profile lookup.
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
async def test_plan_from_llm_dedupes_duplicate_step_names(client):
    """When the planner returns duplicate step names, the endpoint
    must dedupe them and return 200 OK with unique step names.

    Pre-fix, this scenario returned a 500 HTML page (the user saw
    "Failed: Unexpected token 'I', 'Internal S'... is not valid JSON"
    in the Generate plan modal).
    """
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    project_id = await _create_test_project(client)
    await _register_profile(client, "super")

    # Mock the planner to return duplicate step names (simulates
    # the MiniMax M3 behaviour that triggered the bug).
    mock_plan = AsyncMock(return_value=[
        {
            "name": "langgraph",
            "agent_role": "super",
            "action": "research_framework",
            "params": {"framework": "LangGraph"},
            "depends_on": [],
        },
        {
            "name": "langgraph",  # duplicate!
            "agent_role": "super",
            "action": "research_framework",
            "params": {"framework": "LangGraph", "sub": True},
            "depends_on": [],
        },
        {
            "name": "autogen",
            "agent_role": "super",
            "action": "research_framework",
            "params": {"framework": "AutoGen"},
            "depends_on": [],
        },
        {
            "name": "crewai",
            "agent_role": "super",
            "action": "research_framework",
            "params": {"framework": "CrewAI"},
            "depends_on": [],
        },
    ])
    client._transport.app.state.planner.plan = mock_plan  # type: ignore[attr-defined]

    r = await client.post(
        f"/api/projects/{project_id}/plan/from-llm",
        json={"goal": "Test duplicate step names"},
    )
    assert r.status_code == 200, (
        f"expected 200 OK after dedup, got {r.status_code}: {r.text[:500]}"
    )
    plan = r.json()["plan"]
    step_names = [s["name"] for s in plan["steps"]]
    # The fix: deduped. Without the fix this would have raised
    # ValidationError and the endpoint would have returned 500.
    assert len(step_names) == len(set(step_names)), (
        f"step names not unique after dedup: {step_names}"
    )
    # First occurrence keeps its name; the duplicate becomes "<name>-2".
    assert "langgraph" in step_names
    assert "langgraph-2" in step_names
    # Other steps unchanged.
    assert "autogen" in step_names
    assert "crewai" in step_names


@pytest.mark.asyncio
async def test_plan_from_llm_dedupes_three_duplicates(client):
    """Three steps with the same name should produce name, name-2, name-3."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    project_id = await _create_test_project(client)
    await _register_profile(client, "super")

    mock_plan = AsyncMock(return_value=[
        {"name": "research", "agent_role": "super", "action": "do_task",
         "params": {"i": 1}, "depends_on": []},
        {"name": "research", "agent_role": "super", "action": "do_task",
         "params": {"i": 2}, "depends_on": []},
        {"name": "research", "agent_role": "super", "action": "do_task",
         "params": {"i": 3}, "depends_on": []},
    ])
    client._transport.app.state.planner.plan = mock_plan  # type: ignore[attr-defined]

    r = await client.post(
        f"/api/projects/{project_id}/plan/from-llm",
        json={"goal": "Test triple duplicates"},
    )
    assert r.status_code == 200
    step_names = [s["name"] for s in r.json()["plan"]["steps"]]
    assert step_names == ["research", "research-2", "research-3"]
