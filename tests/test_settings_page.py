# coding: utf-8
"""Smoke tests for the v1.0.1 settings page (regression guard).

The settings page composes many sub-cards (LLM, Telegram, Project
Storage, Cleanup, HTTPS, Network, Onboarding). A missing import or
typo in any sub-card setup can blow up the whole page with a 500
Internal Server Error. These tests catch that class of bug fast.

Each test logs in as the bootstrap admin and hits /settings, then
checks the page rendered (200) + a marker string from the relevant
card. Specific-card tests live in their respective test files; this
is just the "does the page load at all" smoke guard.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch.auth.cookie import create_user
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
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


@pytest.mark.asyncio
async def test_settings_page_loads(client):
    """Regression: /settings returns 200 (not 500 from a missing import)."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/settings", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:500]}"


@pytest.mark.asyncio
async def test_settings_page_contains_onboarding_card(client):
    """The onboarding card (v1.0.1 §6.5) renders for admin."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/settings")
    body = r.text
    assert 'id="onboarding"' in body
    assert "Reset onboarding state" in body  # admin-only button


@pytest.mark.asyncio
async def test_settings_page_contains_network_card(client):
    """The network access card (v1.0.1 §3.1) renders."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/settings")
    body = r.text
    assert 'id="network"' in body
    assert "Network access" in body
