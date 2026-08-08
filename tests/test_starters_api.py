# coding: utf-8
"""Tests for v1.0.1 starter catalog API endpoints.

Covers:
  T1.8  Starter gallery shows ≥3 starters
  T1.9  "Use starter" clones into user's workflow_packages
  Clone creates a new workflow_packages row with the starter's
  step_template + variables, snapshot version, unique name
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch.auth.cookie import ROLE_ADMIN, create_user
from hermes_orch.main import create_app

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


async def _bootstrap_admin(app) -> None:
    from hermes_orch.auth.cookie import set_user_password
    db = app.state.db
    row = await db.fetchone("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,))
    await set_user_password(db, row["id"], ADMIN_PASSWORD)


async def _login(ac: AsyncClient, username: str, password: str) -> None:
    r = await ac.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "orchestrator:\n  port: 18765\n  bind_host: 127.0.0.1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))

    import hermes_orch.db as db_mod
    test_db = tmp_path / "test.db"
    orig_db_init = db_mod.Database.__init__

    def patched_db_init(self, db_path):
        orig_db_init(self, test_db)

    monkeypatch.setattr(db_mod.Database, "__init__", patched_db_init)

    app = create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        await _bootstrap_admin(app)
        await create_user(
            app.state.db, username="alice", password="AlicePass123!",
            role="user",
        )
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


# ===== Gallery =====

@pytest.mark.asyncio
async def test_gallery_lists_at_least_3_starters(client):
    """T1.8: gallery must show >= 3 starters."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/api/starters")
    assert r.status_code == 200
    items = r.json()
    assert len(items) >= 3, f"expected >=3, got {len(items)}: {items}"
    # Each item has the summary fields
    for item in items:
        assert "name" in item
        assert "title" in item
        assert "description" in item
        assert "icon" in item
        # And the heavy fields are NOT in the list view
        assert "step_template" not in item
        assert "variables" not in item


@pytest.mark.asyncio
async def test_gallery_includes_system_health(client):
    """system-health is the smoke-test starter — must be in the gallery."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/api/starters")
    items = r.json()
    names = {item["name"] for item in items}
    assert "system-health" in names


@pytest.mark.asyncio
async def test_gallery_includes_research_brief(client):
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/api/starters")
    names = {item["name"] for item in r.json()}
    assert "research-brief" in names


# ===== Single starter detail =====

@pytest.mark.asyncio
async def test_get_starter_detail_returns_template(client):
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/api/starters/research-brief")
    assert r.status_code == 200
    data = r.json()
    # The detail view has the heavy fields
    assert data["name"] == "research-brief"
    assert isinstance(data["step_template"], list)
    assert len(data["step_template"]) >= 2  # at least search + write
    assert isinstance(data["variables"], list)


@pytest.mark.asyncio
async def test_get_starter_unknown_is_404(client):
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/api/starters/does-not-exist")
    assert r.status_code == 404


# ===== Clone =====

@pytest.mark.asyncio
async def test_clone_creates_workflow_package_row(client):
    """T1.9: clone creates a user-owned workflow_packages row with the starter's content."""
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.post("/api/starters/research-brief/clone")
    assert r.status_code == 200
    data = r.json()
    assert data["cloned_from"] == "research-brief"
    assert data["workflow_name"].startswith("research-brief-")
    assert data["workflow_id"].startswith("wf-")

    # DB row exists
    row = await app.state.db.fetchone(
        "SELECT * FROM workflow_packages WHERE id = ?", (data["workflow_id"],)
    )
    assert row is not None
    # Snapshot the starter's content
    assert row["version"] == "0.1.0"
    # step_template + variables are JSON strings in the DB
    steps = json.loads(row["step_template"])
    assert len(steps) >= 2
    assert any("search" in s.get("name", "") for s in steps)
    vars_ = json.loads(row["variables"])
    assert any(v.get("name") == "topic" for v in vars_)


@pytest.mark.asyncio
async def test_clone_assigns_unique_names(client):
    """Two clones of the same starter get different names (UNIQUE constraint)."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r1 = await ac.post("/api/starters/research-brief/clone")
    r2 = await ac.post("/api/starters/research-brief/clone")
    assert r1.json()["workflow_name"] != r2.json()["workflow_name"]
    assert r1.json()["workflow_id"] != r2.json()["workflow_id"]


@pytest.mark.asyncio
async def test_clone_does_not_mutate_catalog(client):
    """The catalog YAML is read-only — clone does not write to it."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Snapshot the catalog file mtime + content
    import os
    from hermes_orch.core.starters import _catalog_dir
    catalog_path = _catalog_dir() / "research-brief.yaml"
    mtime_before = os.path.getmtime(catalog_path)
    content_before = catalog_path.read_text(encoding="utf-8")
    # Clone
    await ac.post("/api/starters/research-brief/clone")
    # File unchanged
    mtime_after = os.path.getmtime(catalog_path)
    content_after = catalog_path.read_text(encoding="utf-8")
    assert mtime_before == mtime_after
    assert content_before == content_after


@pytest.mark.asyncio
async def test_clone_unknown_starter_is_404(client):
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.post("/api/starters/does-not-exist/clone")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_clone_unauthenticated_is_401(client):
    ac, _ = client
    ac.cookies.clear()
    r = await ac.post("/api/starters/research-brief/clone")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_clone_system_health_creates_healthcheck_workflow(client):
    """Cloning the system-health starter creates a workflow whose
    step action is `_server_healthcheck`. The supervisor handles this
    action in-process (no agent dispatch)."""
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.post("/api/starters/system-health/clone")
    assert r.status_code == 200
    # Inspect the cloned workflow
    row = await app.state.db.fetchone(
        "SELECT step_template FROM workflow_packages WHERE id = ?",
        (r.json()["workflow_id"],),
    )
    steps = json.loads(row["step_template"])
    actions = [s.get("action") for s in steps]
    from hermes_orch.core.starters import SERVER_HEALTHCHECK_ACTION
    assert SERVER_HEALTHCHECK_ACTION in actions, (
        f"cloned system-health workflow should have the magic action, "
        f"got actions={actions}"
    )
