"""v1.0.1 (new-user-activation) /api/settings/bind-host integration tests.

Tests cover:
    - GET  /api/settings/bind-host   returns {active, desired, lan_enabled, ...}
    - POST /api/settings/bind-host   sets restart-required + desired on disk
    - The endpoint is admin-only (per /api/server/restart policy)

Uses the in-process AsyncClient (httpx + ASGITransport) + per-test
in-memory DB via monkeypatch, following the test_users_api pattern.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch import main as main_mod
from hermes_orch.auth.cookie import ROLE_ADMIN, ROLE_USER, create_user
from hermes_orch.config import find_config_path
from hermes_orch.core.restart import is_restart_required
from hermes_orch.main import create_app

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


async def _bootstrap_admin(app) -> str:
    """Create the bootstrap admin with a known password. Returns user_id."""
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


async def _login(ac: AsyncClient, username: str, password: str) -> None:
    r = await ac.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Fresh app + AsyncClient with a bootstrap admin in place.

    Per-test DB at tmp_path/test.db, config at tmp_path/config.yaml.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "orchestrator:\n  port: 18765\n  bind_host: 127.0.0.1\n  log_level: INFO\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))

    # Per-test DB at tmp_path/test.db
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
            yield ac, cfg_path


# ===== GET /api/settings/bind-host =====

@pytest.mark.asyncio
async def test_get_bind_host_loopback_default(client):
    """A fresh config with bind_host=127.0.0.1 reports loopback + lan_enabled=false."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/api/settings/bind-host")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["active"] == "127.0.0.1"
    assert data["lan_enabled"] is False
    assert data["restart_required"] is False
    assert data["lan_url"] == ""  # LAN disabled → no URL


@pytest.mark.asyncio
async def test_get_bind_host_unauthenticated_is_401(client):
    """No session cookie -> 401 from user middleware before the endpoint runs."""
    ac, _ = client
    # Drop the session cookie
    ac.cookies.clear()
    r = await ac.get("/api/settings/bind-host")
    assert r.status_code == 401


# ===== POST /api/settings/bind-host =====

@pytest.mark.asyncio
async def test_post_bind_host_lan_enabled_sets_restart_flag(client):
    """POST {lan_enabled: true} writes 0.0.0.0 + sets the restart flag + persists to disk."""
    ac, cfg_path = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.post(
        "/api/settings/bind-host",
        json={"lan_enabled": True},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["desired"] == "0.0.0.0"
    assert data["restart_required"] is True
    assert "0.0.0.0" in data["restart_reason"]
    # Disk was updated (yaml writer may or may not quote the value — both forms parse the same)
    disk_cfg = cfg_path.read_text(encoding="utf-8")
    assert ('bind_host: "0.0.0.0"' in disk_cfg) or ('bind_host: 0.0.0.0' in disk_cfg)
    # Flag was set (file exists on disk)
    info = is_restart_required()
    assert info.required is True


@pytest.mark.asyncio
async def test_post_bind_host_lan_disabled_sets_restart_flag(client):
    """POST {lan_enabled: false} writes 127.0.0.1 + still sets the restart flag."""
    ac, cfg_path = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.post(
        "/api/settings/bind-host",
        json={"lan_enabled": False},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["desired"] == "127.0.0.1"
    assert data["restart_required"] is True
    disk_cfg = cfg_path.read_text(encoding="utf-8")
    assert ('bind_host: "127.0.0.1"' in disk_cfg) or ('bind_host: 127.0.0.1' in disk_cfg)


@pytest.mark.asyncio
async def test_post_bind_host_requires_admin(client):
    """Non-admin users get 403 (matches the /api/server/restart policy)."""
    ac, _ = client
    app = ac._transport.app
    db = app.state.db
    await create_user(
        db, username="alice", password="AlicePass123!", role=ROLE_USER
    )
    await _login(ac, "alice", "AlicePass123!")

    r = await ac.post(
        "/api/settings/bind-host",
        json={"lan_enabled": True},
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_post_bind_host_persists_across_reload(client, monkeypatch):
    """The bind_host value persists to disk and is re-read by a second load_config call."""
    ac, cfg_path = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.post(
        "/api/settings/bind-host",
        json={"lan_enabled": True},
    )
    assert r.status_code == 200
    # Drop the in-memory cache and re-read from disk
    monkeypatch.delenv("HERMES_ORCH_CONFIG", raising=False)
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))
    from hermes_orch.config import load_config
    cfg = load_config()
    assert cfg["orchestrator"]["bind_host"] == "0.0.0.0"
