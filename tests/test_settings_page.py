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


# ===== Agents page: single-button (v1.0.1 §3.3 cleanup) =====
#
# The legacy "Register agent" button + modal was removed in
# v1.0.1 (commit fac1bce-followup) because the new "Add agent
# host" (enrollment token flow) covers the same use case with
# strictly less friction (no pre-declared agent_id, the agent
# host self-declares). Two near-duplicate buttons on the same
# page was confusing for new users.
#
# These tests guard against accidentally re-adding the old
# button or modal. The legacy POST /api/agents endpoint is
# still available for API consumers.

@pytest.mark.asyncio
async def test_agents_page_has_only_one_add_button(client):
    """The agents page header has exactly one "Add" button — the
    new v1.0.1 §3.3 enrollment-token flow. The legacy
    "+ Register agent" button must NOT be present."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/agents")
    body = r.text
    # The new button IS there
    assert "Add agent host" in body
    assert "showEnrollModal" in body
    # The legacy button is NOT
    assert "+ Register agent" not in body, (
        "Legacy 'Register agent' button must not be rendered "
        "— use 'Add agent host' (enrollment token flow) instead."
    )
    assert "showRegisterModal" not in body


@pytest.mark.asyncio
async def test_agents_page_does_not_have_register_modal(client):
    """The legacy #register-modal div must not be in the page
    (its elements were removed entirely; showEnrollModal +
    #enroll-modal is the new path)."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/agents")
    body = r.text
    assert 'id="register-modal"' not in body
    assert 'id="reg-agent-id"' not in body
    assert 'id="reg-success-secret"' not in body
    # The new modal IS there
    assert 'id="enroll-modal"' in body


@pytest.mark.asyncio
async def test_agents_page_legacy_handlers_removed(client):
    """Defensive: the legacy showRegisterModal handler must
    not be in the page script (it would error at runtime if
    anyone clicked a leftover trigger)."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/agents")
    body = r.text
    assert "function showRegisterModal" not in body
    assert "function closeRegisterModal" not in body
    assert "function submitRegisterAgent" not in body
    assert "function copySetupSecret" not in body


@pytest.mark.asyncio
async def test_agents_enroll_modal_still_renders(client):
    """Sanity: the new enrollment-token modal (the one we want
    to keep) IS rendered. Guards against accidentally removing
    the wrong modal during the cleanup."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/agents")
    body = r.text
    assert 'id="enroll-modal"' in body
    assert "enroll-label" in body
    assert "enroll-hint-name" in body
    assert "showEnrollModal" in body
    assert "submitEnrollToken" in body
