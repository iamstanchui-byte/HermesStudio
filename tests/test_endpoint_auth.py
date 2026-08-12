# coding: utf-8
"""Endpoint auth + CSRF tests for the B12 / B10 security hotfix (2026-08-11).

Covers the §8 test matrix of
`docs/security/agent-endpoint-auth-hotfix-design.md`:

  - §8.1 Per-route admin matrix (7 admin-mutation routes × cases)
  - §8.2 Heartbeat / agent-self routes (HMAC auth, unchanged)
  - §8.3 B10 (legacy secret bootstrap) — 410 for everyone
  - §8.4 Cross-cutting audit assertions (actor format, payload)
  - §8.5 Negative / regression (no `actor_kind`, schema unchanged, etc.)
  - §6.1 CSRF helper cases (Origin / Referer fallback, prefix confusion,
    bare-origin enforcement, port-parsing error, etc.)

The test names are the acceptance contract per the operator
`-- Two implementation details are mandatory` directive:

  1. "Origin bare-origin validation must reject every non-empty path,
     including a single slash" — covered by
     `test_csrf_origin_with_trailing_slash_rejected_403` (the
     `Origin: http://192.168.2.152:8765/` regression test the operator
     explicitly requested).
  2. "Treat test names and zero pytest failures as acceptance criteria;
     do not treat the currently documented numeric test totals as a
     release contract." — every test below is named after the
     contract it pins.

Run with: `pytest tests/test_endpoint_auth.py -q --no-header`
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch import main as main_mod
from hermes_orch import db as db_mod
from hermes_orch.auth.cookie import ROLE_ADMIN, ROLE_USER, create_user
from hermes_orch.main import create_app


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"
NONADMIN_USERNAME = "alice"
NONADMIN_PASSWORD = "AlicePass123!"

# Canonical public origin (matches conftest.py `set_test_public_origin`
# so the lifespan doesn't fail-closed).
CANONICAL_ORIGIN = "http://127.0.0.1:8765"
CROSS_ORIGIN = "http://attacker.example:9999"


# ===== Bootstrap helpers (same pattern as test_users_api) =====


async def _bootstrap_admin(app) -> str:
    """Create the bootstrap admin with a known password."""
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
    r = await ac.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Fresh app + AsyncClient + bootstrap admin + non-admin user.

    Each test gets its own tmp DB. The autouse `inject_default_origin_header`
    in tests/conftest.py injects a default Origin header into every
    AsyncClient request — tests that exercise CSRF override the
    header per-request via `headers={"Origin": "..."}`.
    """
    import pathlib

    test_db = tmp_path / "test_endpoint_auth.db"
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
        db = app.state.db
        await create_user(
            db, username=NONADMIN_USERNAME, password=NONADMIN_PASSWORD, role=ROLE_USER
        )
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ===== HMAC helper for self-route tests =====


def _hmac_sign(secret: str, method: str, path: str, body: bytes, ts: str) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = f"{method.upper()}\n{path}\n{body_hash}\n{ts}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


# ====================================================================
# §8.1 — 7 admin-mutation routes: unauth / non-admin / admin matrix
# ====================================================================


# ---- POST /api/agents/ ----


@pytest.mark.asyncio
async def test_register_agent_unauthenticated_401(client):
    r = await client.post(
        "/api/agents/", json={"agent_id": "x", "max_concurrent_tasks": 1}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_register_agent_nonadmin_403(client):
    await _login(client, NONADMIN_USERNAME, NONADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/", json={"agent_id": "x", "max_concurrent_tasks": 1}
    )
    assert r.status_code == 403
    assert "Admin" in r.json()["detail"]


@pytest.mark.asyncio
async def test_register_agent_admin_allowed(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/", json={"agent_id": "admin-reg-1", "max_concurrent_tasks": 2}
    )
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_register_agent_hmac_does_not_grant_admin(client):
    """HMAC headers alone (no cookie) must NOT grant admin."""
    # No login — no cookie. But supply HMAC headers (with a dummy
    # secret). The endpoint should 401, not 200.
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "x", "max_concurrent_tasks": 1},
        headers={
            "X-Agent-Id": "x",
            "X-Timestamp": str(int(time.time())),
            "X-Signature": "0" * 64,
        },
    )
    assert r.status_code == 401


# ---- PUT /api/agents/{id} ----


@pytest.mark.asyncio
async def test_update_agent_unauthenticated_401(client):
    r = await client.put("/api/agents/anything", json={"ip": "1.2.3.4"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_update_agent_nonadmin_403(client):
    await _login(client, NONADMIN_USERNAME, NONADMIN_PASSWORD)
    r = await client.put("/api/agents/anything", json={"ip": "1.2.3.4"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_update_agent_admin_allowed(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Pre-register so PUT finds the agent
    r1 = await client.post(
        "/api/agents/", json={"agent_id": "upd-1", "max_concurrent_tasks": 1}
    )
    assert r1.status_code == 201
    r2 = await client.put("/api/agents/upd-1", json={"ip": "1.2.3.4"})
    assert r2.status_code == 200, r2.text


# ---- DELETE /api/agents/{id} ---- (B12 highest priority)


@pytest.mark.asyncio
async def test_delete_agent_unauthenticated_401(client):
    r = await client.delete("/api/agents/anything")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_agent_nonadmin_403(client):
    await _login(client, NONADMIN_USERNAME, NONADMIN_PASSWORD)
    r = await client.delete("/api/agents/anything")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_delete_agent_admin_allowed(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r1 = await client.post(
        "/api/agents/", json={"agent_id": "del-1", "max_concurrent_tasks": 1}
    )
    assert r1.status_code == 201
    r2 = await client.delete("/api/agents/del-1")
    assert r2.status_code == 204


# ---- POST /api/agents/{id}/rotate-key ----


@pytest.mark.asyncio
async def test_rotate_key_unauthenticated_401(client):
    r = await client.post("/api/agents/anything/rotate-key")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_rotate_key_nonadmin_403(client):
    await _login(client, NONADMIN_USERNAME, NONADMIN_PASSWORD)
    r = await client.post("/api/agents/anything/rotate-key")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_rotate_key_admin_allowed(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r1 = await client.post(
        "/api/agents/", json={"agent_id": "rot-1", "max_concurrent_tasks": 1}
    )
    assert r1.status_code == 201
    r2 = await client.post("/api/agents/rot-1/rotate-key")
    assert r2.status_code == 200, r2.text
    assert "new_secret" in r2.json()


# ---- POST /api/agents/{id}/profiles ----


@pytest.mark.asyncio
async def test_add_profile_unauthenticated_401(client):
    r = await client.post(
        "/api/agents/anything/profiles",
        json={"name": "p", "description": "d"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_add_profile_nonadmin_403(client):
    await _login(client, NONADMIN_USERNAME, NONADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/anything/profiles",
        json={"name": "p", "description": "d"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_add_profile_admin_allowed(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r1 = await client.post(
        "/api/agents/", json={"agent_id": "prof-1", "max_concurrent_tasks": 1}
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/api/agents/prof-1/profiles",
        json={"name": "developer", "description": "dev profile"},
    )
    assert r2.status_code == 201, r2.text


# ---- DELETE /api/agents/{id}/profiles/{name} ----


@pytest.mark.asyncio
async def test_remove_profile_unauthenticated_401(client):
    r = await client.delete("/api/agents/anything/profiles/dev")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_remove_profile_nonadmin_403(client):
    await _login(client, NONADMIN_USERNAME, NONADMIN_PASSWORD)
    r = await client.delete("/api/agents/anything/profiles/dev")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_remove_profile_admin_allowed(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    await client.post(
        "/api/agents/", json={"agent_id": "prof-rm-1", "max_concurrent_tasks": 1}
    )
    await client.post(
        "/api/agents/prof-rm-1/profiles",
        json={"name": "developer", "description": "x"},
    )
    r = await client.delete("/api/agents/prof-rm-1/profiles/developer")
    assert r.status_code == 204


# ---- PATCH /api/agents/{id}/profiles/{name} ----


@pytest.mark.asyncio
async def test_update_profile_unauthenticated_401(client):
    r = await client.patch(
        "/api/agents/anything/profiles/dev", json={"description": "new"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_update_profile_nonadmin_403(client):
    await _login(client, NONADMIN_USERNAME, NONADMIN_PASSWORD)
    r = await client.patch(
        "/api/agents/anything/profiles/dev", json={"description": "new"}
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_update_profile_admin_allowed(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    await client.post(
        "/api/agents/", json={"agent_id": "prof-p-1", "max_concurrent_tasks": 1}
    )
    await client.post(
        "/api/agents/prof-p-1/profiles",
        json={"name": "developer", "description": "old"},
    )
    r = await client.patch(
        "/api/agents/prof-p-1/profiles/developer",
        json={"description": "new desc"},
    )
    assert r.status_code == 200, r.text


# ====================================================================
# §8.3 — B10: POST /api/agents/{id}/secret returns 410 unconditionally
# ====================================================================


@pytest.mark.asyncio
async def test_set_agent_secret_unauthenticated_410(client):
    r = await client.post(
        "/api/agents/anything/secret", json={"secret": "abcdef0123456789"}
    )
    assert r.status_code == 410


@pytest.mark.asyncio
async def test_set_agent_secret_nonadmin_410(client):
    await _login(client, NONADMIN_USERNAME, NONADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/anything/secret", json={"secret": "abcdef0123456789"}
    )
    assert r.status_code == 410


@pytest.mark.asyncio
async def test_set_agent_secret_admin_410(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/anything/secret", json={"secret": "abcdef0123456789"}
    )
    assert r.status_code == 410
    body = r.json()
    # B10 contract: body explains the deprecation (B11 = recovery track)
    assert "deprecated" in body["detail"].lower() or "410" in str(body["detail"])
    # IMPLEMENTATION TRAP guard: confirm `admin` is NOT bypassed —
    # the body must not echo a successful set / match.
    assert "set" not in body or "new_secret" not in body
    assert "match" not in body or "true" not in body.get("match", "")


@pytest.mark.asyncio
async def test_set_agent_secret_410_no_db_state_change(client):
    """410 must NOT mutate the agent's hmac_secret / secret_hash."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    await client.post(
        "/api/agents/", json={"agent_id": "b10-state-1", "max_concurrent_tasks": 1}
    )
    db = client._transport.app.state.db  # type: ignore[attr-defined]
    before = await db.fetchone(
        "SELECT hmac_secret, secret_hash FROM agents WHERE id = ?",
        ("b10-state-1",),
    )
    r = await client.post(
        "/api/agents/b10-state-1/secret",
        json={"secret": "ZZZZZZZZZZZZZZZZ"},
    )
    assert r.status_code == 410
    after = await db.fetchone(
        "SELECT hmac_secret, secret_hash FROM agents WHERE id = ?",
        ("b10-state-1",),
    )
    assert before["hmac_secret"] == after["hmac_secret"], (
        "B10 stub must not mutate hmac_secret"
    )
    assert before["secret_hash"] == after["secret_hash"], (
        "B10 stub must not mutate secret_hash"
    )


# ====================================================================
# §8.2 — Heartbeat / agent-self routes: HMAC unchanged, NOT admin-gated
# ====================================================================


@pytest.mark.asyncio
async def test_heartbeat_no_hmac_401(client):
    r = await client.post("/api/agents/anything/heartbeat", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_heartbeat_valid_hmac_200(client):
    """A valid HMAC-signed heartbeat (admin cookie NOT required)."""
    db = client._transport.app.state.db  # type: ignore[attr-defined]
    import secrets
    secret = secrets.token_urlsafe(24)
    agent_id = "hb-test-1"
    # Pre-register the agent via direct DB (same as production wrapper does)
    secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    await db.execute(
        "INSERT INTO agents (id, secret_hash, hmac_secret, status, created_at) "
        "VALUES (?, ?, ?, 'verified', ?)",
        (agent_id, secret_hash, secret, now),
    )
    path = f"/api/agents/{agent_id}/heartbeat"
    body = b"{}"
    ts = str(int(time.time()))
    sig = _hmac_sign(secret, "POST", path, body, ts)
    r = await client.post(
        path,
        content=body,
        headers={
            "X-Agent-Id": agent_id,
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_get_agent_self_no_hmac_401(client):
    r = await client.get("/api/agents/anything")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_agent_self_valid_hmac_200(client):
    db = client._transport.app.state.db  # type: ignore[attr-defined]
    import secrets
    secret = secrets.token_urlsafe(24)
    agent_id = "get-self-1"
    secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    await db.execute(
        "INSERT INTO agents (id, secret_hash, hmac_secret, status, created_at) "
        "VALUES (?, ?, ?, 'verified', ?)",
        (agent_id, secret_hash, secret, now),
    )
    path = f"/api/agents/{agent_id}"
    ts = str(int(time.time()))
    sig = _hmac_sign(secret, "GET", path, b"", ts)
    r = await client.get(
        path,
        headers={
            "X-Agent-Id": agent_id,
            "X-Timestamp": ts,
            "X-Signature": sig,
        },
    )
    assert r.status_code == 200, r.text


# ====================================================================
# §6.1 — CSRF: Origin / Referer validation
# ====================================================================


@pytest.mark.asyncio
async def test_csrf_missing_origin_rejected(client):
    """Admin cookie, no Origin, no Referer → 403 (state-changing method)."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "csrf-1", "max_concurrent_tasks": 1},
        headers={"Origin": ""},  # empty, not absent (the autouse
        # conftest sets the default Origin to CANONICAL_ORIGIN; we
        # explicitly override to empty to simulate a missing-header
        # scenario — but Starlette / httpx may strip the empty header.
        # Use the explicit override pattern below to fully clear it.)
    )
    # The autouse conftest adds Origin: CANONICAL_ORIGIN. We override
    # with an empty value above. ASGI/HTTP should treat that as
    # 'no header' but starlette may keep the empty value. Either way
    # the request must be rejected — Origin "" is not the canonical
    # origin.
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_csrf_malformed_origin_rejected(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "csrf-malformed", "max_concurrent_tasks": 1},
        headers={"Origin": "not-a-url"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_csrf_prefix_confusion_origin_rejected(client):
    """The exact attack the operator flagged: an Origin that LOOKS like
    the canonical origin but has extra characters after the port
    (e.g. `http://127.0.0.1:8765.attacker.example`). Must 403."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "csrf-prefix", "max_concurrent_tasks": 1},
        headers={"Origin": "http://127.0.0.1:8765.attacker.example"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_csrf_different_port_rejected(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "csrf-port", "max_concurrent_tasks": 1},
        headers={"Origin": "http://127.0.0.1:9999"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_csrf_different_scheme_rejected(client):
    """https://127.0.0.1:8765 is not the same as http://127.0.0.1:8765
    when the canonical origin is http. The CSRF check is strict on
    scheme."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "csrf-scheme", "max_concurrent_tasks": 1},
        headers={"Origin": "https://127.0.0.1:8765"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_csrf_exact_canonical_origin_accepted(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "csrf-ok", "max_concurrent_tasks": 1},
        headers={"Origin": CANONICAL_ORIGIN},
    )
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_csrf_referer_fallback_helper_origin_absent_valid_referer(client):
    """DETERMINISTIC helper-level test (R14 / operator 2026-08-11 review).

    Calls `require_same_origin` directly with a constructed ASGI
    request that has NO `Origin` header and a `Referer` matching
    the canonical public origin. The helper must accept this
    (fall back to Referer, matches scheme/host/port).

    A non-deterministic version of this check (`assert in (201, 403)`)
    was previously in the suite. It was REMOVED per operator
    review because a security control verification must not
    accept both pass and fail outcomes. This test pins the exact
    behavior at the helper level, where the contract is enforced.

    The end-to-end test `test_csrf_referer_fallback_end_to_end_...`
    below verifies the helper is wired into the route correctly.
    """
    from starlette.requests import Request

    from hermes_orch.auth.csrf import require_same_origin

    # The canonical public origin is set on `app.state` by the
    # lifespan hook. Use the real app (from the in-process client
    # fixture) so the helper reads the same origin the route would.
    real_app = client._transport.app  # type: ignore[attr-defined]
    assert getattr(real_app.state, "public_origin", None) == CANONICAL_ORIGIN

    # Build a minimal ASGI request with NO Origin and a valid Referer.
    # We pass the real app in the scope so `request.app.state.public_origin`
    # resolves to the canonical origin (the helper reads from there).
    referer_value = f"{CANONICAL_ORIGIN}/dashboard"
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/agents/",
        "headers": [(b"referer", referer_value.encode("ascii"))],
        "app": real_app,
    }
    request = Request(scope)

    # Should NOT raise — Origin absent, Referer matches canonical
    require_same_origin(request)


@pytest.mark.asyncio
async def test_csrf_referer_fallback_helper_origin_absent_invalid_referer(client):
    """DETERMINISTIC helper-level test (R14 / operator 2026-08-11 review).

    Same setup as the valid-Referer test, but the Referer is
    cross-origin. The helper MUST raise HTTPException(403).
    """
    from fastapi import HTTPException
    from starlette.requests import Request

    from hermes_orch.auth.csrf import require_same_origin

    real_app = client._transport.app  # type: ignore[attr-defined]
    referer_value = "http://attacker.example:9999/dashboard"
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/agents/",
        "headers": [(b"referer", referer_value.encode("ascii"))],
        "app": real_app,
    }
    request = Request(scope)

    with pytest.raises(HTTPException) as exc_info:
        require_same_origin(request)
    assert exc_info.value.status_code == 403, (
        f"Expected 403 for cross-origin Referer, got {exc_info.value.status_code}"
    )


@pytest.mark.asyncio
async def test_csrf_referer_fallback_end_to_end_no_origin_valid_referer(client):
    """DETERMINISTIC end-to-end test (R14 / operator 2026-08-11 review).

    Sends a state-changing POST to the admin gate with:
      - Valid admin session cookie
      - NO `Origin` header (bypasses the autouse Origin-injection
        wrapper by going directly to the ASGI app, not through
        httpx.AsyncClient.send)
      - Valid `Referer` matching the canonical public origin

    Expects: 201 (the route accepts the Referer-fallback path).

    This replaces the previous non-deterministic
    `assert r.status_code in (201, 403)` test. We use the ASGI
    app directly to bypass the autouse wrapper, since patching
    `httpx.AsyncClient.send` from inside a test cannot reach the
    REAL send (it's captured in the autouse's closure).
    """
    import json as _json

    real_app = client._transport.app  # type: ignore[attr-defined]
    # Extract the admin session cookie set by _login() above.
    # _login() already happened in the previous test step? No — this
    # test does NOT call _login() first. We need to log in via the
    # same path. But we want the cookie on a request that bypasses
    # the httpx autouse wrapper. So: log in via the real client,
    # then read the cookie from the client's cookie jar.
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    cookies = list(client.cookies.jar) if hasattr(client, "cookies") else []
    cookie_header = b""
    for c in cookies:
        cookie_header += f"{c.name}={c.value}; ".encode("ascii")
    if cookie_header:
        cookie_header = cookie_header.rstrip(b"; ")

    body_bytes = _json.dumps(
        {"agent_id": "csrf-ref-ok-2", "max_concurrent_tasks": 1}
    ).encode("utf-8")
    referer_value = f"{CANONICAL_ORIGIN}/dashboard".encode("ascii")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/agents/",
        "raw_path": b"/api/agents/",
        "query_string": b"",
        "server": ("testserver", 80),
        "headers": [
            (b"host", b"testserver"),
            (b"cookie", cookie_header),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body_bytes)).encode("ascii")),
            (b"referer", referer_value),
            # NO `origin` header — this is the key. We want the
            # Referer-fallback path to be exercised, which means
            # `request.headers.get("origin")` must return None.
        ],
        "app": real_app,
    }

    request_body_sent = False

    async def receive():
        nonlocal request_body_sent
        if not request_body_sent:
            request_body_sent = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        return {"type": "http.disconnect"}

    response_started = []
    response_body = []

    async def send(message):
        if message["type"] == "http.response.start":
            response_started.append(message)
        elif message["type"] == "http.response.body":
            response_body.append(message.get("body", b""))

    await real_app(scope, receive, send)

    assert response_started, "no response.start sent"
    status = response_started[0]["status"]
    body = b"".join(response_body)
    # Deterministic: must succeed because Referer matches canonical.
    assert status == 201, (
        f"Expected 201 (Referer matches canonical), "
        f"got {status}: {body.decode('utf-8', errors='replace')}"
    )


@pytest.mark.asyncio
async def test_csrf_referer_fallback_end_to_end_no_origin_invalid_referer(client):
    """DETERMINISTIC end-to-end test (R14 / operator 2026-08-11 review).

    Same setup as the valid-Referer end-to-end test, but the
    Referer is cross-origin. Expects: 403.
    """
    import json as _json

    real_app = client._transport.app  # type: ignore[attr-defined]
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    cookies = list(client.cookies.jar) if hasattr(client, "cookies") else []
    cookie_header = b""
    for c in cookies:
        cookie_header += f"{c.name}={c.value}; ".encode("ascii")
    if cookie_header:
        cookie_header = cookie_header.rstrip(b"; ")

    body_bytes = _json.dumps(
        {"agent_id": "csrf-ref-bad-2", "max_concurrent_tasks": 1}
    ).encode("utf-8")
    referer_value = b"http://attacker.example:9999/dashboard"

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/agents/",
        "raw_path": b"/api/agents/",
        "query_string": b"",
        "server": ("testserver", 80),
        "headers": [
            (b"host", b"testserver"),
            (b"cookie", cookie_header),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body_bytes)).encode("ascii")),
            (b"referer", referer_value),
        ],
        "app": real_app,
    }

    request_body_sent = False

    async def receive():
        nonlocal request_body_sent
        if not request_body_sent:
            request_body_sent = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        return {"type": "http.disconnect"}

    response_started = []
    response_body = []

    async def send(message):
        if message["type"] == "http.response.start":
            response_started.append(message)
        elif message["type"] == "http.response.body":
            response_body.append(message.get("body", b""))

    await real_app(scope, receive, send)

    assert response_started, "no response.start sent"
    status = response_started[0]["status"]
    body = b"".join(response_body)
    # Deterministic: must 403 because Referer doesn't match canonical.
    assert status == 403, (
        f"Expected 403 (Referer cross-origin), "
        f"got {status}: {body.decode('utf-8', errors='replace')}"
    )


@pytest.mark.asyncio
async def test_hmac_path_skips_csrf(client):
    """HMAC-authed agent requests don't carry cookies; they are not
    subject to the CSRF check. Verified by an HMAC-signed request
    that doesn't include an Origin header at all.

    Note: the autouse httpx wrapper injects Origin if absent. For
    this test we want to verify the request reaches the route even
    WITH a cross-origin Origin (since HMAC auth is sufficient).
    """
    db = client._transport.app.state.db  # type: ignore[attr-defined]
    import secrets
    secret = secrets.token_urlsafe(24)
    agent_id = "hmac-csrf-skip-1"
    secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    await db.execute(
        "INSERT INTO agents (id, secret_hash, hmac_secret, status, created_at) "
        "VALUES (?, ?, ?, 'verified', ?)",
        (agent_id, secret_hash, secret, now),
    )
    path = f"/api/agents/{agent_id}/heartbeat"
    body = b"{}"
    ts = str(int(time.time()))
    sig = _hmac_sign(secret, "POST", path, body, ts)
    r = await client.post(
        path,
        content=body,
        headers={
            "X-Agent-Id": agent_id,
            "X-Timestamp": ts,
            "X-Signature": sig,
            "Content-Type": "application/json",
            "Origin": CROSS_ORIGIN,  # pretend the request is cross-origin
        },
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_get_method_skips_csrf(client):
    """GET is a safe method — no CSRF check even from a cross-origin
    request. But the GET /api/agents/{id} endpoint is HMAC-authed,
    so we test a different GET: /api/agents/ (list) which is admin-
    gated but a safe method."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.get(
        "/api/agents/",
        headers={"Origin": CROSS_ORIGIN},
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_csrf_invalid_port_origin_rejected(client):
    """Origin with an unparseable port: 403 (NOT 500)."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "csrf-bad-port", "max_concurrent_tasks": 1},
        headers={"Origin": "http://127.0.0.1:not-a-port"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_csrf_structurally_broken_origin_rejected(client):
    """A structurally invalid Origin must 403, not 500."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "csrf-broken", "max_concurrent_tasks": 1},
        headers={"Origin": "://broken"},
    )
    assert r.status_code == 403


# ---- R14: Origin vs Referer distinction (allowlist bypass fix) ----


@pytest.mark.asyncio
async def test_csrf_origin_with_trailing_slash_rejected_403(client):
    """REGRESSION TEST (operator 2026-08-11 mandatory).

    `Origin: http://127.0.0.1:8765/` MUST be rejected with 403.

    The Origin header contract is "bare origin, no path" — a single
    trailing slash IS a path ("/") and Origin must never have one.
    `Origin: http://127.0.0.1:8765/` looks like a benign dashboard
    URL to a careless parser; the strict `if parsed.path != "": reject`
    guard catches it.

    Without this guard, an attacker could send an Origin that LOOKS
    like the canonical origin but with a trailing slash, bypassing
    the allowlist. This is the regression the operator explicitly
    pinned as a mandatory test.
    """
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "csrf-trailing-slash", "max_concurrent_tasks": 1},
        headers={"Origin": "http://127.0.0.1:8765/"},
    )
    assert r.status_code == 403, (
        f"Expected 403 for Origin with trailing slash, got {r.status_code}: {r.text}"
    )


@pytest.mark.asyncio
async def test_csrf_origin_with_path_rejected(client):
    """Origin with an explicit path: 403."""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "csrf-path", "max_concurrent_tasks": 1},
        headers={"Origin": "http://127.0.0.1:8765/attacker-path"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_csrf_origin_with_query_rejected(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "csrf-query", "max_concurrent_tasks": 1},
        headers={"Origin": "http://127.0.0.1:8765?x=1"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_csrf_origin_with_fragment_rejected(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "csrf-frag", "max_concurrent_tasks": 1},
        headers={"Origin": "http://127.0.0.1:8765#frag"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_csrf_origin_with_userinfo_rejected(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.post(
        "/api/agents/",
        json={"agent_id": "csrf-userinfo", "max_concurrent_tasks": 1},
        headers={"Origin": "http://user:pass@127.0.0.1:8765"},
    )
    assert r.status_code == 403


# ====================================================================
# §8.4 — Cross-cutting audit assertions
# ====================================================================


@pytest.mark.asyncio
async def test_audit_actor_admin_format(client):
    """Every admin-mutation audit has actor matching ^admin:.+$"""
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r1 = await client.post(
        "/api/agents/", json={"agent_id": "aud-1", "max_concurrent_tasks": 1}
    )
    assert r1.status_code == 201
    r2 = await client.delete("/api/agents/aud-1")
    assert r2.status_code == 204
    db = client._transport.app.state.db  # type: ignore[attr-defined]
    rows = await db.fetchall(
        "SELECT actor, event_type FROM audit_log "
        "WHERE event_type IN ('agent.registered', 'agent.deleted') "
        "ORDER BY created_at"
    )
    assert len(rows) >= 2
    for row in rows:
        actor = row["actor"] or ""
        assert actor.startswith("admin:"), (
            f"audit actor should match ^admin:.+$, got {actor!r}"
        )
        assert len(actor) > len("admin:"), (
            f"admin actor missing username: {actor!r}"
        )


@pytest.mark.asyncio
async def test_audit_payload_has_remote_addr_and_route(client):
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    await client.post(
        "/api/agents/", json={"agent_id": "aud-p-1", "max_concurrent_tasks": 1}
    )
    await client.delete("/api/agents/aud-p-1")
    db = client._transport.app.state.db  # type: ignore[attr-defined]
    rows = await db.fetchall(
        "SELECT event_type, payload FROM audit_log "
        "WHERE event_type IN ('agent.registered', 'agent.deleted') "
        "ORDER BY created_at"
    )
    assert len(rows) >= 2
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert "remote_addr" in payload, (
            f"audit payload missing remote_addr: {payload}"
        )
        assert "route" in payload, f"audit payload missing route: {payload}"


@pytest.mark.asyncio
async def test_audit_no_hardcoded_operator_in_b12_routes(client):
    """Grep: no admin-mutation audit call emits literal 'operator' as actor.

    We exercise the 7 B12 routes via the in-process client and inspect
    every audit_log row they created. The B12 §7.1 contract requires
    the actor to be `f"admin:{user['username']}"`.
    """
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Touch the 7 admin-mutation routes (or as many as we can without
    # a 500/400 leaking into audit). PUT requires a registered agent;
    # rotate-key too. We pre-register one and exercise the 7.
    await client.post(
        "/api/agents/", json={"agent_id": "aud-op-1", "max_concurrent_tasks": 1}
    )
    await client.put("/api/agents/aud-op-1", json={"max_concurrent_tasks": 2})
    await client.post("/api/agents/aud-op-1/rotate-key")
    await client.post(
        "/api/agents/aud-op-1/profiles",
        json={"name": "developer", "description": "x"},
    )
    await client.patch(
        "/api/agents/aud-op-1/profiles/developer", json={"description": "y"}
    )
    await client.delete("/api/agents/aud-op-1/profiles/developer")
    await client.delete("/api/agents/aud-op-1")

    db = client._transport.app.state.db  # type: ignore[attr-defined]
    rows = await db.fetchall(
        "SELECT actor, event_type FROM audit_log "
        "WHERE event_type IN ("
        "  'agent.registered', 'agent.max_concurrent_tasks_changed',"
        "  'agent.key_rotated', 'agent.profile_added',"
        "  'agent.profile_updated', 'agent.profile_removed',"
        "  'agent.deleted'"
        ") "
        "ORDER BY created_at"
    )
    assert len(rows) >= 6
    for row in rows:
        actor = row["actor"] or ""
        assert actor.startswith("admin:"), (
            f"B12 admin-mutation audit actor should be admin:<u>, got {actor!r} "
            f"for event {row['event_type']}"
        )


# ====================================================================
# §8.5 — Negative / regression
# ====================================================================


@pytest.mark.asyncio
async def test_audit_log_schema_unchanged(client):
    """B12 must NOT add a new column to audit_log."""
    db = client._transport.app.state.db  # type: ignore[attr-defined]
    rows = await db.fetchall("PRAGMA table_info(audit_log)")
    column_names = {r["name"] for r in rows}
    # The pre-hotfix schema has these columns. actor_kind is NOT
    # added in this hotfix (B11 territory).
    assert "actor_kind" not in column_names, (
        "B12 must NOT add actor_kind to audit_log (that's B11)"
    )
    # Sanity: the canonical columns are present.
    for expected in ("id", "event_type", "actor", "payload", "created_at"):
        assert expected in column_names, f"missing canonical column {expected}"


@pytest.mark.asyncio
async def test_health_endpoint_unaffected(client):
    """`/api/health` still 200 without auth (not in B12 scope)."""
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_enrollment_token_endpoints_unchanged(client):
    """Enrollment token routes (admin-only) are pre-existing and
    unchanged. Confirm they still 401/403/200 with admin."""
    # Unauth → 401
    r = await client.get("/api/enrollment-tokens")
    assert r.status_code == 401
    # Admin → 200
    await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await client.get("/api/enrollment-tokens")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_enroll_endpoint_unchanged(client):
    """POST /api/agents/enroll is anonymous-by-design (uses enrollment
    token). Should still work without admin."""
    r = await client.post(
        "/api/agents/enroll",
        json={
            "token": "etok-fake-not-a-real-token",
            "agent_id": "enroll-fake-1",
            "agent_name": "fake",
        },
    )
    # 404 / 401 / 410 — whatever the server returns for a fake token,
    # it should NOT be 403 (no admin gate). The exact code depends
    # on the enroll implementation.
    assert r.status_code != 403, r.text
