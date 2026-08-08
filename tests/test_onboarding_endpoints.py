# coding: utf-8
"""Tests for the v1.0.1 onboarding API endpoints (spec §5.3).

Endpoints exercised:
  - GET    /api/me/onboarding         current user's state
  - POST   /api/me/onboarding/skip    opt out of the checklist
  - POST   /api/me/onboarding/reset   admin-only: reset for re-demo
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch.auth.cookie import ROLE_ADMIN, ROLE_USER, create_user
from hermes_orch.core.onboarding import (
    SIGNAL_LLM_CONFIGURED,
    SIGNAL_PASSWORD_SET,
    set_signal,
    serialize_state,
    set_skipped,
    empty_state,
)
from hermes_orch.main import create_app

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


async def _bootstrap_admin(app) -> None:
    """Set the admin's password (auto-created on fresh DB).

    Uses the public `set_user_password` so the password_set onboarding
    signal gets flipped — otherwise the test would have to manually
    call set_user_signal to get the same effect.
    """
    from hermes_orch.auth.cookie import set_user_password
    db = app.state.db
    row = await db.fetchone(
        "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    await set_user_password(db, row["id"], ADMIN_PASSWORD)


async def _login(ac: AsyncClient, username: str, password: str) -> None:
    r = await ac.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Fresh app + AsyncClient with admin + alice."""
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
        # Regular non-admin user
        await create_user(
            app.state.db, username="alice", password="AlicePass123!",
            role=ROLE_USER,
        )
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


# ===== GET /api/me/onboarding =====

@pytest.mark.asyncio
async def test_get_onboarding_unauthenticated_is_401(client):
    ac, _ = client
    ac.cookies.clear()
    r = await ac.get("/api/me/onboarding")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_onboarding_fresh_user_should_show_checklist(client):
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/api/me/onboarding")
    assert r.status_code == 200
    data = r.json()
    # Fresh user (no signals flipped yet) should see the checklist
    assert data["should_show_checklist"] is True
    assert data["is_complete"] is False
    assert data["state"]["skipped"] is False


@pytest.mark.asyncio
async def test_get_onboarding_after_password_set(client):
    """After setting the password, the user has flipped password_set
    but still has 3 other steps left → checklist still shows."""
    from hermes_orch.auth.cookie import set_user_password
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    # The bootstrap admin password was set via _bootstrap_admin,
    # which already flipped password_set=True
    r = await ac.get("/api/me/onboarding")
    data = r.json()
    assert data["state"]["signals"][SIGNAL_PASSWORD_SET] is True
    # 3 of 4 still false → still shows
    assert data["is_complete"] is False
    assert data["should_show_checklist"] is True


@pytest.mark.asyncio
async def test_get_onboarding_skipped_hides_checklist(client):
    """After POST /skip, the checklist is hidden even if not complete."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Skip
    r = await ac.post("/api/me/onboarding/skip")
    assert r.status_code == 200
    # Now GET should report should_show_checklist=False
    r = await ac.get("/api/me/onboarding")
    data = r.json()
    assert data["should_show_checklist"] is False
    assert data["state"]["skipped"] is True


# ===== POST /api/me/onboarding/skip =====

@pytest.mark.asyncio
async def test_skip_unauthenticated_is_401(client):
    ac, _ = client
    ac.cookies.clear()
    r = await ac.post("/api/me/onboarding/skip")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_skip_marks_skipped_true(client):
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.post("/api/me/onboarding/skip")
    assert r.status_code == 200
    assert r.json()["skipped"] is True


@pytest.mark.asyncio
async def test_skip_does_not_clear_signals(client):
    """Skip = hide UI. Signals already true stay true."""
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Set password (already done by _bootstrap_admin) + LLM manually
    from hermes_orch.core.onboarding import set_user_signal
    user = await app.state.db.fetchone(
        "SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,)
    )
    await set_user_signal(app.state.db, user["id"], SIGNAL_LLM_CONFIGURED, True)
    # Skip
    await ac.post("/api/me/onboarding/skip")
    # Signals must still be true
    r = await ac.get("/api/me/onboarding")
    data = r.json()
    assert data["state"]["signals"][SIGNAL_PASSWORD_SET] is True
    assert data["state"]["signals"][SIGNAL_LLM_CONFIGURED] is True


# ===== POST /api/me/onboarding/reset =====

@pytest.mark.asyncio
async def test_reset_unauthenticated_is_401(client):
    ac, _ = client
    ac.cookies.clear()
    r = await ac.post("/api/me/onboarding/reset")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_reset_non_admin_is_403(client):
    """Non-admin users cannot reset (it's a re-demo tool)."""
    ac, _ = client
    await _login(ac, "alice", "AlicePass123!")
    r = await ac.post("/api/me/onboarding/reset")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_reset_admin_succeeds(client):
    """Admin can reset their own state to all-false."""
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Confirm starting state is fresh-ish (password was set by bootstrap,
    # so password_set=True)
    r0 = await ac.get("/api/me/onboarding")
    assert r0.json()["state"]["signals"][SIGNAL_PASSWORD_SET] is True
    # Reset
    r = await ac.post("/api/me/onboarding/reset")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    # All signals are now false
    for sig, val in data["state"]["signals"].items():
        assert val is False, f"signal {sig} should be False after reset, got {val}"
    # And the checklist shows again
    r2 = await ac.get("/api/me/onboarding")
    assert r2.json()["should_show_checklist"] is True
    assert r2.json()["is_complete"] is False


@pytest.mark.asyncio
async def test_reset_preserves_password_hash(client):
    """Reset is a UI flag reset — it does NOT touch the user's password."""
    from hermes_orch.auth.cookie import hash_password
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Snapshot the password hash
    row = await app.state.db.fetchone(
        "SELECT password_hash FROM users WHERE username=?", (ADMIN_USERNAME,)
    )
    original_hash = row["password_hash"]
    # Reset
    await ac.post("/api/me/onboarding/reset")
    # Password hash unchanged
    row2 = await app.state.db.fetchone(
        "SELECT password_hash FROM users WHERE username=?", (ADMIN_USERNAME,)
    )
    assert row2["password_hash"] == original_hash


# ===== Signal hook tests =====

@pytest.mark.asyncio
async def test_llm_save_flips_llm_configured_signal(client, monkeypatch):
    """Saving LLM config (mock OR real) flips llm_configured for the user."""
    from hermes_orch.core.onboarding import SIGNAL_LLM_CONFIGURED
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Before: llm_configured should be False (no LLM saved yet)
    user = await app.state.db.fetchone(
        "SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,)
    )
    r0 = await ac.get("/api/me/onboarding")
    assert r0.json()["state"]["signals"][SIGNAL_LLM_CONFIGURED] is False
    # Save LLM config (mock mode)
    r = await ac.post(
        "/api/settings/llm",
        json={"mock": True, "model": "test"},
    )
    assert r.status_code == 200
    # After: llm_configured should be True
    r1 = await ac.get("/api/me/onboarding")
    assert r1.json()["state"]["signals"][SIGNAL_LLM_CONFIGURED] is True


@pytest.mark.asyncio
async def test_password_set_flips_password_set_signal(client, monkeypatch):
    """Setting a password (via /api/users/{id}/password or login flow)
    flips password_set for the user."""
    from hermes_orch.core.onboarding import SIGNAL_PASSWORD_SET
    ac, app = client
    # Create a fresh user with no password
    from hermes_orch.auth.cookie import create_user
    user_id = await create_user(
        app.state.db, username="newbie", password=None, role=ROLE_USER,
    )
    # Reset to default state
    from hermes_orch.core.onboarding import reset_state, serialize_state
    await app.state.db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(reset_state()), user_id),
    )
    # Login as newbie — first login triggers /setup-password (not a real
    # password set yet). The bootstrap admin sets the password.
    # Easier: directly call set_user_password (the public API)
    from hermes_orch.auth.cookie import set_user_password
    await set_user_password(app.state.db, user_id, "NewbiePass123!")
    # Check the state
    row = await app.state.db.fetchone(
        "SELECT onboarding_state FROM users WHERE id = ?", (user_id,)
    )
    import json
    state = json.loads(row["onboarding_state"])
    assert state["signals"][SIGNAL_PASSWORD_SET] is True
