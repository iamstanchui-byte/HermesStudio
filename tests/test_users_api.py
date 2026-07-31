"""Tests for the admin user CRUD API (v3.5.0, 2026-07-31).

Exercises the full /api/users/* surface:
  - 401 without a session cookie
  - 403 when logged in as a non-admin
  - 200/201/4xx happy paths for list, create, reset password, disable, enable
  - Edge cases: duplicate username, self-disable, missing target user

Strategy:
  - Use the in-process AsyncClient so we don't need a running server.
  - Each test gets a fresh tmp DB (monkeypatches the db_path in main.py
    so tests don't share state and don't pollute ~/.hermes-orchestrator).
  - Bootstrap an admin user via the same path `hermes-orch init` uses.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch import main as main_mod
from hermes_orch.auth.cookie import ROLE_ADMIN, ROLE_USER, create_user
from hermes_orch.main import create_app


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


async def _bootstrap_admin(app) -> str:
    """Create the bootstrap admin with a known password. Returns user_id.

    Mimics `hermes-orch init` + first-login /setup-password (we just
    set the password directly here to skip the web flow).
    """
    db = app.state.db
    user_id = await create_user(
        db,
        username=ADMIN_USERNAME,
        password=ADMIN_PASSWORD,
        role=ROLE_ADMIN,
        is_bootstrap_admin=True,
    )
    return user_id


async def _login(ac: AsyncClient, username: str, password: str) -> None:
    """Helper: log in via the real /api/auth/login endpoint and store
    the session cookie on the AsyncClient."""
    r = await ac.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Fresh app + AsyncClient with a bootstrap admin already in place.

    Each test gets a unique tmp DB via the monkeypatch so we never
    touch ~/.hermes-orchestrator/hermes-orch.db (which would interfere
    with a dev server running on the same machine).
    """
    # Patch the db_path used by main.create_app's lifespan so the test
    # uses a per-test file under tmp_path.
    import pathlib
    from hermes_orch import db as db_mod

    test_db = tmp_path / "test.db"
    orig_init = main_mod.create_app

    def patched_init():
        # Wrap the original create_app so we can swap the db_path
        # before the lifespan starts. Easiest path: monkeypatch the
        # Database class to use the test path.
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


# ===== Auth guards =====

@pytest.mark.asyncio
async def test_list_users_requires_auth():
    """No session cookie → 401 from middleware before the endpoint runs."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/api/users")
    # Middleware returns 401 JSON for /api/* paths
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_users_requires_admin(client):
    """A regular (non-admin) user can log in but gets 403 on admin endpoints."""
    # Create a non-admin user via direct DB call (admin-self-test would
    # need admin to use the create endpoint — circular for this test).
    app = client._transport.app  # type: ignore[attr-defined]
    db = app.state.db
    await create_user(db, username="alice", password="AlicePass123!", role=ROLE_USER)
    await _login(client, "alice", "AlicePass123!")

    r = await client.get("/api/users")
    assert r.status_code == 403
    assert "Admin" in r.json()["detail"]


# ===== List =====

@pytest.mark.asyncio
async def test_list_users_as_admin(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.get("/api/users")
    assert r.status_code == 200
    users = r.json()["users"]
    assert len(users) == 1
    assert users[0]["username"] == ADMIN_USERNAME
    assert users[0]["role"] == ROLE_ADMIN
    assert users[0]["disabled"] is False
    assert users[0]["is_bootstrap_admin"] is True
    assert users[0]["has_password"] is True


# ===== Create =====

@pytest.mark.asyncio
async def test_create_user_as_admin(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/users",
        json={"username": "bob", "password": "BobPass123!", "is_admin": False},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["username"] == "bob"
    assert body["role"] == ROLE_USER

    # Verify it shows up in the list
    r2 = await client.get("/api/users")
    usernames = [u["username"] for u in r2.json()["users"]]
    assert "bob" in usernames


@pytest.mark.asyncio
async def test_create_user_admin_flag(client):
    """`is_admin: true` creates a user with the admin role."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/users",
        json={"username": "superbob", "password": "BobPass123!", "is_admin": True},
    )
    assert r.status_code == 201
    assert r.json()["role"] == ROLE_ADMIN


@pytest.mark.asyncio
async def test_create_user_duplicate_returns_409(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Create once
    r1 = await client.post(
        "/api/users",
        json={"username": "bob", "password": "BobPass123!", "is_admin": False},
    )
    assert r1.status_code == 201
    # Duplicate (case-insensitive per schema)
    r2 = await client.post(
        "/api/users",
        json={"username": "BOB", "password": "OtherPass123!", "is_admin": False},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_create_user_password_too_short(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/users",
        json={"username": "shorty", "password": "abc", "is_admin": False},
    )
    assert r.status_code == 422  # Pydantic validation error


# ===== Reset password =====

@pytest.mark.asyncio
async def test_admin_reset_password(client):
    """Admin resets another user's password. They can log in with the new one."""
    # Create user via admin API
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/users",
        json={"username": "carol", "password": "OldPass123!", "is_admin": False},
    )
    assert r.status_code == 201

    # Reset their password
    r2 = await client.post(
        "/api/users/carol/password",
        json={"new_password": "NewPass456!", "confirm_password": "NewPass456!"},
    )
    assert r2.status_code == 200

    # Old password no longer works — log in as admin to clear cookies first
    # (logout via a fresh client would also work; here we just rely on
    #  AsyncClient session cookies being scoped per-client)
    async with AsyncClient(transport=client._transport, base_url="http://test") as ac2:  # type: ignore[attr-defined]
        r3 = await ac2.post("/api/auth/login", json={"username": "carol", "password": "OldPass123!"})
        assert r3.status_code == 401
        r4 = await ac2.post("/api/auth/login", json={"username": "carol", "password": "NewPass456!"})
        assert r4.status_code == 200


@pytest.mark.asyncio
async def test_reset_password_mismatch(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/users/nobody/password",
        json={"new_password": "NewPass456!", "confirm_password": "Different456!"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_reset_password_unknown_user(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/users/ghost/password",
        json={"new_password": "NewPass456!", "confirm_password": "NewPass456!"},
    )
    assert r.status_code == 404


# ===== Disable / Enable =====

@pytest.mark.asyncio
async def test_disable_then_enable(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Create target
    await client.post(
        "/api/users",
        json={"username": "dave", "password": "DavePass123!", "is_admin": False},
    )

    # Disable
    r = await client.post("/api/users/dave/disable")
    assert r.status_code == 200
    assert r.json()["disabled"] is True

    # List reflects it
    dave = next(u for u in (await client.get("/api/users")).json()["users"] if u["username"] == "dave")
    assert dave["disabled"] is True

    # Dave can't log in
    async with AsyncClient(transport=client._transport, base_url="http://test") as ac2:  # type: ignore[attr-defined]
        r2 = await ac2.post("/api/auth/login", json={"username": "dave", "password": "DavePass123!"})
        assert r2.status_code == 401

    # Re-enable
    r3 = await client.post("/api/users/dave/enable")
    assert r3.status_code == 200
    assert r3.json()["disabled"] is False

    # Dave can log in again
    async with AsyncClient(transport=client._transport, base_url="http://test") as ac3:  # type: ignore[attr-defined]
        r4 = await ac3.post("/api/auth/login", json={"username": "dave", "password": "DavePass123!"})
        assert r4.status_code == 200


@pytest.mark.asyncio
async def test_admin_cannot_disable_self(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(f"/api/users/{ADMIN_USERNAME}/disable")
    assert r.status_code == 400
    assert "yourself" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_disable_unknown_user(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post("/api/users/ghost/disable")
    assert r.status_code == 404
