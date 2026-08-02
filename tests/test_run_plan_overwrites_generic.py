"""Tests for v3.10.6 run_project_plan overwrites generic placeholders.

Context (2026-08-02):
  v3.10.4 added auto-seed of generic SOUL presets at plan-save.
  v3.10.5 added LLM-driven SOUL generation at run_project_plan time
  (the "Generate task" button).

  Bug: run_project_plan called the seed helper with
  fill_empty_only=True, which meant the LLM-generated personas
  were computed but never written (the generic placeholders from
  plan-save were non-empty, so fill_empty_only=True skipped them).

  Real-world repro on proj-e8106311: user saved a plan (which
  auto-seeded generic SOULs), then clicked "Generate task". The
  LLM was called and produced project-specific personas, but the
  user still saw the generic text in the SOUL editor.

  Fix: run_project_plan now uses fill_empty_only=False so the LLM
  personas overwrite the generic placeholders. The "Generate SOUL"
  button (recovery use case) still uses fill_empty_only=True to
  preserve user edits between Generate Task and Generate SOUL.

This test asserts:
  1. plan-save creates generic placeholder presets
  2. run_project_plan (with LLM) overwrites them with LLM personas
  3. generate-soul (recovery button) preserves user edits
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hermes_orch import db as db_mod
from hermes_orch import main as main_mod
from hermes_orch.api.plans import PlanStep, ProjectPlan
from hermes_orch.auth.cookie import ROLE_ADMIN, create_user, get_user_by_username, hash_password

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


def _make_plan_json(step_specs):
    return {"plan": {"name": "test-plan", "steps": step_specs}}


async def _bootstrap_admin(app):
    db = app.state.db
    existing = await get_user_by_username(db, ADMIN_USERNAME)
    if existing:
        await db.execute(
            "UPDATE users SET password_hash = ?, role = ?, disabled = 0 WHERE id = ?",
            (hash_password(ADMIN_PASSWORD), ROLE_ADMIN, existing["id"]),
        )
        return existing["id"]
    return await create_user(db, username=ADMIN_USERNAME,
                              password=ADMIN_PASSWORD, role=ROLE_ADMIN)


async def _login_admin(ac):
    r = await ac.post("/api/auth/login",
                      json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert r.status_code in (200, 201), r.text


async def _create_project_with_profile(app, profile_name="super"):
    db = app.state.db
    from datetime import datetime, timezone, timedelta
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    await db.execute(
        "INSERT INTO agents (id, secret_hash, hmac_secret, status, "
        "last_heartbeat_at, created_at) VALUES (?, '', ?, 'verified', ?, ?)",
        (agent_id, "test-secret", now, now),
    )
    profile_id = f"prof-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agent_profiles (id, agent_id, name, description, status, "
        "capabilities, mcp_servers, storage_refs, skills) "
        "VALUES (?, ?, ?, '', 'idle', '{}', '[]', '[]', '[]')",
        (profile_id, agent_id, profile_name),
    )
    return agent_id, profile_id


async def _create_project(app, name="overwrite test"):
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, 'HK SME AI adoption research', 'planned', "
        "'', '', '', 0, 0, '')",
        (pid, name),
    )
    return pid


async def _get_presets(app, pid):
    db = app.state.db
    return await db.fetchall(
        "SELECT role_name, content, default_soul, length(content) as clen "
        "FROM project_soul_presets WHERE project_id = ? "
        "ORDER BY role_name",
        (pid,),
    )


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
        await _bootstrap_admin(app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


# ===== Unit test for the seed helper behavior =====


@pytest.mark.asyncio
async def test_run_plan_overwrites_generic_placeholder_with_llm(client, monkeypatch):
    """The proj-e8106311 repro: plan-save creates generic
    placeholders. run_project_plan calls LLM and overwrites
    them with project-specific personas.

    The LLM is mocked at the httpx layer so the test doesn't
    need a real API key.
    """
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="super")
    pid = await _create_project(app)

    # 1. Save plan via PUT. The plan-save path auto-seeds
    #    generic SOUL placeholders (v3.10.4 behavior).
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
        ]),
    )
    assert r.status_code == 200, r.text
    presets_after_save = await _get_presets(app, pid)
    assert len(presets_after_save) == 1
    # Generic placeholder (v3.10.4 fallback)
    assert "Adapt your expertise" in presets_after_save[0]["content"]

    # 2. Mock the LLM to return a project-specific persona.
    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, **kw):
            return _FakeResp(200, {
                "choices": [{
                    "message": {"content": json.dumps({
                        "souls": {"super": "LLM persona for HK SME research"}
                    })},
                    "finish_reason": "stop",
                }],
            })

    class _FakeResp:
        def __init__(self, sc, data):
            self.status_code = sc
            self._data = data
        def json(self):
            return self._data
        @property
        def text(self):
            return json.dumps(self._data)

    import json
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    # Ensure app config has a valid LLM block (run_project_plan reads it)
    app.state.config = {"llm": {
        "base_url": "https://api.minimax.io/anthropic",
        "api_key": "test-key",
        "model": "MiniMax-M3",
        "mock": False,
    }}

    # 3. Click "Generate task" (POST /plan/run).
    r = await ac.post(
        f"/api/projects/{pid}/plan/run",
        json={"archive_existing": True, "name_suffix": ""},
    )
    assert r.status_code == 200, r.text

    # 4. The SOUL should now be the LLM-generated one, not the
    #    generic placeholder. v3.10.6 fix: fill_empty_only=False
    #    overwrites the generic with the LLM persona.
    presets_after_run = await _get_presets(app, pid)
    assert len(presets_after_run) == 1
    assert presets_after_run[0]["content"] == "LLM persona for HK SME research"
    # default_soul is also overwritten with the LLM persona
    # (the seed helper sets default_soul to the new persona
    # text; a future "reset to default" UI would need a separate
    # "original preset" column to distinguish the original
    # generic placeholder from the latest LLM persona).


@pytest.mark.asyncio
async def test_generate_soul_button_preserves_user_edits(client, monkeypatch):
    """The recovery button ("Generate SOUL") must use
    fill_empty_only=True so user edits between Generate Task
    and Generate SOUL are preserved.
    """
    ac, app = client
    await _login_admin(ac)
    await _create_project_with_profile(app, profile_name="super")
    pid = await _create_project(app)

    # 1. Save plan.
    r = await ac.put(
        f"/api/projects/{pid}/plan",
        json=_make_plan_json([
            {"name": "step-a", "action": "do_a", "agent_role": "super"},
        ]),
    )
    assert r.status_code == 200

    # 2. User manually edits the preset to a custom persona.
    db = app.state.db
    await db.execute(
        "UPDATE project_soul_presets SET content = ?, default_soul = ? "
        "WHERE project_id = ? AND role_name = ?",
        ("USER'S CUSTOM PERSONA", "USER'S CUSTOM PERSONA", pid, "super"),
    )

    # 3. Mock LLM (would return something different).
    import json
    import httpx
    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, **kw):
            return _FakeResp(200, {
                "choices": [{
                    "message": {"content": json.dumps({
                        "souls": {"super": "LLM WOULD OVERWRITE USER!"}
                    })},
                    "finish_reason": "stop",
                }],
            })
    class _FakeResp:
        def __init__(self, sc, data):
            self.status_code = sc
            self._data = data
        def json(self):
            return self._data
        @property
        def text(self):
            return json.dumps(self._data)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    app.state.config = {"llm": {
        "base_url": "https://api.minimax.io/anthropic",
        "api_key": "test-key",
        "model": "MiniMax-M3",
        "mock": False,
    }}

    # 4. User clicks "Generate SOUL" (recovery button).
    r = await ac.post(f"/api/projects/{pid}/plan/generate-soul")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["llm_call_status"] == "ok"
    # The LLM was called, but the user's edit was preserved
    presets = await _get_presets(app, pid)
    assert len(presets) == 1
    assert presets[0]["content"] == "USER'S CUSTOM PERSONA"
    assert data["presets_skipped_existing"] == 1
