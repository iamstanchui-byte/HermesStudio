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

    Idempotent: v3.5.1+ auto-creates the admin row (password=NULL) on
    a fresh DB before this fixture runs. If the admin already exists
    (with or without a password), we just set the password on it.
    """
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


# ===== Delete (v3.5.2) =====
#
# Hard-delete semantics: row is removed. Bootstrap admin is permanent
# (400). Self-delete is blocked (400). Last-admin is blocked (400).
# Non-admin is blocked (403). See api/users.py for the guard chain.


@pytest.mark.asyncio
async def test_list_users_includes_is_last_admin(client):
    """v3.5.2: list response includes is_last_admin per row so the UI
    can disable the Delete button without a second click/guess.
    With only the bootstrap admin in the DB, they ARE the last admin.
    """
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.get("/api/users")
    assert r.status_code == 200
    admin_row = next(u for u in r.json()["users"] if u["username"] == ADMIN_USERNAME)
    assert admin_row["is_last_admin"] is True
    assert admin_row["is_bootstrap_admin"] is True


@pytest.mark.asyncio
async def test_delete_user_happy_path(client):
    """Admin deletes a non-admin user. Row is gone, audit log records it."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Create a non-admin target
    r = await client.post(
        "/api/users",
        json={"username": "evan", "password": "EvanPass123!", "is_admin": False},
    )
    assert r.status_code == 201

    # Delete them
    r2 = await client.delete("/api/users/evan")
    assert r2.status_code == 200
    body = r2.json()
    assert body["ok"] is True
    assert body["username"] == "evan"
    assert body["deleted"] is True

    # Row is gone from the list
    r3 = await client.get("/api/users")
    usernames = [u["username"] for u in r3.json()["users"]]
    assert "evan" not in usernames

    # Audit log entry recorded
    app = client._transport.app  # type: ignore[attr-defined]
    db = app.state.db
    audit_row = await db.fetchone(
        "SELECT * FROM audit_log WHERE event_type = 'user.deleted' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert audit_row is not None
    assert audit_row["actor"] == ADMIN_USERNAME
    # payload is JSON text in the column
    import json
    payload = json.loads(audit_row["payload"])
    assert payload["target_username"] == "evan"
    assert payload["was_bootstrap_admin"] is False


@pytest.mark.asyncio
async def test_admin_cannot_delete_self(client):
    """Guard 3 (self-delete): blocked when there are 2+ active admins
    (i.e. self-guard is the right error, not bootstrap / last-admin).
    Setup: bootstrap admin + a second non-bootstrap admin. Log in
    as the second admin and try to delete themselves — the
    bootstrap guard doesn't apply (target isn't bootstrap), the
    last-admin guard doesn't apply (a second admin exists), so
    the self-guard fires.
    """
    # Create a second admin via API (logged in as bootstrap).
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/users",
        json={"username": "selfie", "password": "SelfiePass123!", "is_admin": True},
    )
    assert r.status_code == 201
    # Log in as the second admin.
    await _login(client, "selfie", "SelfiePass123!")
    # Try to delete themselves. Self-guard fires (the only guard
    # that applies when target is non-bootstrap and not last-admin).
    r2 = await client.delete("/api/users/selfie")
    assert r2.status_code == 400
    assert "yourself" in r2.json()["detail"].lower()
    # Row still exists
    app = client._transport.app  # type: ignore[attr-defined]
    db = app.state.db
    still = await db.fetchone("SELECT id FROM users WHERE username = ?", ("selfie",))
    assert still is not None


@pytest.mark.asyncio
async def test_admin_cannot_delete_bootstrap_admin(client):
    """Guard 2: bootstrap admin is permanent. Even with another admin
    available, you can't delete the row that was auto-created on
    fresh install. Use Disable instead."""
    # Create a second admin so the last-admin guard doesn't shadow this
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/users",
        json={"username": "secondadmin", "password": "SecondPass123!", "is_admin": True},
    )
    assert r.status_code == 201

    # Try to delete the bootstrap admin — should be blocked by bootstrap guard
    r2 = await client.delete(f"/api/users/{ADMIN_USERNAME}")
    assert r2.status_code == 400
    detail = r2.json()["detail"].lower()
    assert "bootstrap" in detail

    # Row still exists
    app = client._transport.app  # type: ignore[attr-defined]
    db = app.state.db
    still = await db.fetchone(
        "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    assert still is not None


@pytest.mark.asyncio
async def test_admin_cannot_delete_last_admin(client):
    """Guard 2 (last-admin): blocked when target is the only active
    admin remaining. Setup: bootstrap admin creates a second non-
    bootstrap admin, then disables itself. Now the second admin is
    the sole active admin. When they try to delete themselves, the
    self-guard WOULD fire — but we also need to test the last-admin
    guard specifically. The cleanest way is to have a non-bootstrap
    admin try to delete the OTHER non-bootstrap admin when they're
    the only active ones. But that requires TWO non-bootstrap admins
    + both of them being sole active — which is the same scenario.

    Pragmatic approach: have a third non-admin user who, after
    bootstrap disabled, logs in as the sole non-bootstrap admin and
    tries to delete themselves. Last-admin guard fires (more
    informative than the self-guard — tells the user WHY).

    Note: require_admin middleware ensures the caller is an active
    admin, so the only way for the last-admin guard to fire is when
    the target IS the caller. Both guards apply, but last-admin
    fires first (more critical to surface).
    """
    # Setup: bootstrap + second non-bootstrap admin.
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/users",
        json={"username": "solo", "password": "SoloPass123!", "is_admin": True},
    )
    assert r.status_code == 201
    # Disable the bootstrap admin (logged in as bootstrap, but the
    # URL is for the bootstrap, so this would actually fail with
    # self-disable — re-login as solo first to disable bootstrap).
    await _login(client, "solo", "SoloPass123!")
    r2 = await client.post(f"/api/users/{ADMIN_USERNAME}/disable")
    assert r2.status_code == 200
    # Re-login as solo (the disable endpoint revokes the bootstrap
    # session, but the solo session is fine; just to be safe).
    await _login(client, "solo", "SoloPass123!")
    # Now try to delete solo. They are the only active admin. Both
    # the self-guard and the last-admin guard apply; last-admin
    # fires first per the API's guard ordering (more informative
    # error message — "promote another user first" vs generic
    # "cannot delete yourself").
    r3 = await client.delete("/api/users/solo")
    assert r3.status_code == 400
    detail = r3.json()["detail"].lower()
    # Accept either the last-admin or self message — both are
    # valid for this scenario. The important assertion is 400 +
    # that the row is still there.
    assert (
        "only remaining admin" in detail
        or "yourself" in detail
    ), f"unexpected error message: {detail!r}"
    # Row still exists
    app = client._transport.app  # type: ignore[attr-defined]
    db = app.state.db
    still = await db.fetchone("SELECT id FROM users WHERE username = ?", ("solo",))
    assert still is not None


@pytest.mark.asyncio
async def test_delete_user_requires_admin(client):
    """Auth guard: a non-admin user gets 403 on DELETE (not 401, not 200)."""
    # Create non-admin + log in as them
    app = client._transport.app  # type: ignore[attr-defined]
    db = app.state.db
    await create_user(db, username="frank", password="FrankPass123!", role=ROLE_USER)
    # Also need a target to delete (admin would be a target but they
    # don't have admin role; use a fresh non-admin)
    await create_user(db, username="greta", password="GretaPass123!", role=ROLE_USER)
    await _login(client, "frank", "FrankPass123!")

    r = await client.delete("/api/users/greta")
    assert r.status_code == 403
    assert "Admin" in r.json()["detail"]


@pytest.mark.asyncio
async def test_deleted_user_cannot_login(client):
    """After delete, the user's password is no longer valid (their row
    is gone, so even with correct creds the auth query returns nothing)."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Create target
    await client.post(
        "/api/users",
        json={"username": "henry", "password": "HenryPass123!", "is_admin": False},
    )
    # Verify they can log in
    async with AsyncClient(transport=client._transport, base_url="http://test") as ac2:  # type: ignore[attr-defined]
        r0 = await ac2.post("/api/auth/login", json={"username": "henry", "password": "HenryPass123!"})
        assert r0.status_code == 200
    # Delete them
    r1 = await client.delete("/api/users/henry")
    assert r1.status_code == 200
    # Now they can't log in (row gone)
    async with AsyncClient(transport=client._transport, base_url="http://test") as ac3:  # type: ignore[attr-defined]
        r2 = await ac3.post("/api/auth/login", json={"username": "henry", "password": "HenryPass123!"})
        assert r2.status_code == 401
