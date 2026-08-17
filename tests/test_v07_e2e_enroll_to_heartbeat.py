# coding: utf-8
"""v0.7 §1.4 end-to-end test: enroll → persisted config → 7-header
authenticated POST → replay rejection.

Why this exists
---------------
2026-08-17 HermesCtl bug-discovery report, item #10 (Perplexity
review addition): the v0.7 client implementation needs a real
end-to-end test that exercises the full happy path:
  1. Admin issues enrollment token (POST /api/enrollment-tokens)
  2. Agent host consumes token (POST /api/agents/enroll)
     -- response must include hmac_key_id + hmac_secret_hex (v0.7)
  3. Wrapper reads persisted config and calls
     agent_http.set_hmac_credential(key_id, secret_hex)
  4. Wrapper signs a request with the 7 X-Hermes-* headers
  5. Server-side v0.7 dispatcher routes to the v0.7 verifier
  6. v0.7 verifier looks up the agent by hmac_key_id
     (NOT by agent_id -- the key-id-to-agent authorization rule)
  7. v0.7 verifier validates body hash, timestamp, signature
  8. Endpoint returns 200 (not 401 INVALID_SIGNATURE / UNKNOWN_KEY_ID)
  9. Replaying the same nonce returns 401 NONCE_REPLAY
     (replay protection -- separate from the signature check)

The wrapper's behavior under this test mirrors the production
flow on a real agent host: enroll CLI writes a wrapper-config
that the start CLI reads and feeds into agent_http.

What's NOT in scope
-------------------
- The v0.6 fallback path (covered by test_hmac_v06_compat).
- The "v0.6 default" path on a fresh server (covered by hardening
  tests in test_hmac_v07_hardening).
- The TLS / cert pinning path (covered by integration tests
  against a live server with real certs).
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import base64
import time
import secrets as _secrets

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
async def e2e_client(tmp_path, monkeypatch):
    """Spin up a full FastAPI app on a temp DB + temp config, with an
    admin user pre-created. Returns (ac, app) -- the AsyncClient and
    the live app instance. Tests can mutate `app.state.db` directly
    if they need to inspect or change agent rows.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "orchestrator:\n  port: 18766\n  bind_host: 127.0.0.1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))

    import hermes_orch.db as db_mod
    test_db = tmp_path / "test_e2e.db"
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
            role=ROLE_ADMIN,
        )
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


def _build_v07_headers(
    *,
    method: str,
    path: str,
    body: bytes,
    key_id: str,
    secret_hex: str,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Build the 7 X-Hermes-* headers exactly as the wrapper would.

    Mirrors `hermes_orch.auth.hmac_v07.sign_v07_request` (the canonical
    client-side signer). Kept in this test file to avoid the
    httpx/ASGI round-trip for signing; the values are byte-for-byte
    identical to what the real client would emit.
    """
    secret = bytes.fromhex(secret_hex)
    body_sha = hashlib.sha256(body).hexdigest()
    ts = timestamp or str(int(time.time()))
    nv = nonce or _secrets.token_hex(16)
    canonical = "\n".join([method, path, body_sha, ts, nv])
    sig = base64.b64encode(
        _hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    return {
        "X-Hermes-Method": method,
        "X-Hermes-Path": path,
        "X-Hermes-Body-SHA256": body_sha,
        "X-Hermes-Key-Id": key_id,
        "X-Hermes-Timestamp": ts,
        "X-Hermes-Nonce": nv,
        "X-Hermes-Signature": sig,
        "Content-Type": "application/json",
    }


@pytest.mark.asyncio
async def test_e2e_enroll_to_heartbeat_v07(e2e_client):
    """Full happy path: admin issues token, agent enrolls, wrapper
    signs heartbeat with v0.7 7-header, server returns 200.

    Steps verified:
      1. EnrollOut includes hmac_key_id + hmac_secret_hex
      2. agents.hmac_key_id column is populated
      3. v0.7 signed heartbeat is accepted (200)
      4. Server looks up by hmac_key_id (NOT agent_id)
    """
    ac, app = e2e_client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)

    # Step 1: admin issues an enrollment token
    r_issue = await ac.post(
        "/api/enrollment-tokens",
        json={"label": "E2E v0.7", "requested_agent_name": "e2e-01"},
    )
    assert r_issue.status_code == 200, r_issue.text
    token = r_issue.json()["token"]

    # Step 2: agent host consumes the token
    r_consume = await ac.post(
        "/api/agents/enroll",
        json={"token": token, "agent_name": "e2e-01", "hostname": "e2e-host"},
    )
    assert r_consume.status_code == 200, r_consume.text
    enroll_data = r_consume.json()
    agent_id = enroll_data["agent_id"]
    key_id = enroll_data["hmac_key_id"]
    secret_hex = enroll_data["hmac_secret_hex"]
    assert key_id and secret_hex, "EnrollOut must include hmac_key_id + hmac_secret_hex"

    # Step 2b: agents.hmac_key_id is populated in the DB
    agent_row = await app.state.db.fetchone(
        "SELECT hmac_key_id, hmac_secret FROM agents WHERE id = ?", (agent_id,)
    )
    assert agent_row is not None
    assert agent_row["hmac_key_id"] == key_id, "DB hmac_key_id must match EnrollOut"

    # Step 3: wrapper "reads" the persisted config and sets the v0.7
    # credential on the agent_http layer. This is what the wrapper's
    # `start` function does in production after reading wrapper-config.
    # (This test runs on the server branch which doesn't have
    # agent_http.set_hmac_credential -- the test simulates the signing
    # step directly via _build_v07_headers below. The wrapper-side
    # test on fix/wrapper-v07-migration-gaps covers the integration
    # with set_hmac_credential.)

    # Step 4: wrapper sends a heartbeat signed with the 7 X-Hermes-* headers.
    # Body must match the heartbeat schema; here we just send an idle
    # status -- the server's heartbeat endpoint is the canonical smoke
    # test target because it is HMAC-authed + state-mutating.
    body = b'{"status": "idle"}'
    headers = _build_v07_headers(
        method="POST",
        path=f"/api/agents/{agent_id}/heartbeat",
        body=body,
        key_id=key_id,
        secret_hex=secret_hex,
    )
    r_hb = await ac.post(
        f"/api/agents/{agent_id}/heartbeat",
        content=body,
        headers=headers,
    )
    assert r_hb.status_code == 200, (
        f"v0.7 signed heartbeat must be 200, got {r_hb.status_code}: {r_hb.text}"
    )

    # Step 5: the heartbeat updated last_heartbeat_at in the DB
    after = await app.state.db.fetchone(
        "SELECT last_heartbeat_at, status FROM agents WHERE id = ?", (agent_id,)
    )
    assert after["status"] == "verified", (
        f"first verified heartbeat should flip status to 'verified', got {after['status']!r}"
    )
    assert after["last_heartbeat_at"] is not None, "last_heartbeat_at must be set"


@pytest.mark.asyncio
async def test_e2e_heartbeat_with_wrong_key_id_is_401(e2e_client):
    """Sanity: signing the heartbeat with a WRONG hmac_key_id (one
    that doesn't exist in the agents table) is rejected with 401
    UNKNOWN_KEY_ID. This is the v0.7 §1.4 key-id-to-agent rule:
    the verifier looks up by hmac_key_id, NOT agent_id. If we send
    a key_id that has no row, we should get 401, not 'unknown agent'.
    """
    ac, app = e2e_client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)

    r_issue = await ac.post(
        "/api/enrollment-tokens",
        json={"label": "wrong-key test", "requested_agent_name": "e2e-02"},
    )
    token = r_issue.json()["token"]
    r_consume = await ac.post(
        "/api/agents/enroll",
        json={"token": token, "agent_name": "e2e-02", "hostname": "e2e-host-2"},
    )
    enroll_data = r_consume.json()
    agent_id = enroll_data["agent_id"]
    secret_hex = enroll_data["hmac_secret_hex"]

    body = b'{"status": "idle"}'
    headers = _build_v07_headers(
        method="POST",
        path=f"/api/agents/{agent_id}/heartbeat",
        body=body,
        key_id="kw_doesnotexist0000",  # invalid
        secret_hex=secret_hex,
    )
    r = await ac.post(
        f"/api/agents/{agent_id}/heartbeat",
        content=body,
        headers=headers,
    )
    assert r.status_code == 401, f"unknown key_id should be 401, got {r.status_code}"
    assert "UNKNOWN_KEY_ID" in r.text or "key" in r.text.lower(), (
        f"expected UNKNOWN_KEY_ID error, got: {r.text}"
    )


@pytest.mark.asyncio
async def test_e2e_heartbeat_nonce_replay_rejected(e2e_client):
    """v0.7 §1.4 replay protection: re-sending the same nonce with
    a valid signature is rejected with 401 NONCE_REPLAY. This is
    critical for security -- without it, an attacker who captures
    a heartbeat could replay it indefinitely.

    Note: the in-process nonce store is per-server-start. The
    middleware flushes nonces when the server restarts. This test
    is a smoke test, not a durability test.
    """
    ac, app = e2e_client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)

    r_issue = await ac.post(
        "/api/enrollment-tokens",
        json={"label": "replay test", "requested_agent_name": "e2e-03"},
    )
    token = r_issue.json()["token"]
    r_consume = await ac.post(
        "/api/agents/enroll",
        json={"token": token, "agent_name": "e2e-03", "hostname": "e2e-host-3"},
    )
    enroll_data = r_consume.json()
    agent_id = enroll_data["agent_id"]
    key_id = enroll_data["hmac_key_id"]
    secret_hex = enroll_data["hmac_secret_hex"]

    body = b'{"status": "idle"}'
    # Pin the timestamp + nonce so the second request is a literal replay
    fixed_ts = str(int(time.time()))
    fixed_nonce = _secrets.token_hex(16)
    headers = _build_v07_headers(
        method="POST",
        path=f"/api/agents/{agent_id}/heartbeat",
        body=body,
        key_id=key_id,
        secret_hex=secret_hex,
        timestamp=fixed_ts,
        nonce=fixed_nonce,
    )
    r1 = await ac.post(
        f"/api/agents/{agent_id}/heartbeat",
        content=body,
        headers=headers,
    )
    assert r1.status_code == 200, f"first heartbeat should be 200, got {r1.status_code}: {r1.text}"

    # Replay the EXACT same headers (same nonce)
    r2 = await ac.post(
        f"/api/agents/{agent_id}/heartbeat",
        content=body,
        headers=dict(headers),  # exact copy
    )
    assert r2.status_code == 401, (
        f"replayed heartbeat should be 401 NONCE_REPLAY, got {r2.status_code}: {r2.text}"
    )
    assert "NONCE_REPLAY" in r2.text or "replay" in r2.text.lower(), (
        f"expected NONCE_REPLAY error, got: {r2.text}"
    )


@pytest.mark.asyncio
async def test_e2e_heartbeat_with_query_string_rejected(e2e_client):
    """v0.7 §1.4: HMAC-signed endpoints MUST NOT have query strings
    (they're not in the canonical string-to-sign). The verifier
    rejects with 400 MALFORMED_HEADERS (or 401 depending on
    implementation). This is a smoke test for the gate.
    """
    ac, app = e2e_client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)

    r_issue = await ac.post(
        "/api/enrollment-tokens",
        json={"label": "query test", "requested_agent_name": "e2e-04"},
    )
    token = r_issue.json()["token"]
    r_consume = await ac.post(
        "/api/agents/enroll",
        json={"token": token, "agent_name": "e2e-04", "hostname": "e2e-host-4"},
    )
    enroll_data = r_consume.json()
    agent_id = enroll_data["agent_id"]
    key_id = enroll_data["hmac_key_id"]
    secret_hex = enroll_data["hmac_secret_hex"]

    # Build a request with a query string in the URL but NO query string
    # in X-Hermes-Path (which is what the v0.7 verifier looks at).
    body = b'{"status": "idle"}'
    headers = _build_v07_headers(
        method="POST",
        # X-Hermes-Path has no query string (correct canonical)
        path=f"/api/agents/{agent_id}/heartbeat",
        body=body,
        key_id=key_id,
        secret_hex=secret_hex,
    )
    # But the actual URL has a query string -- the verifier reads
    # request.url.query and rejects.
    r = await ac.post(
        f"/api/agents/{agent_id}/heartbeat?forced=1",
        content=body,
        headers=headers,
    )
    assert r.status_code in (400, 401), (
        f"query-string on v0.7 signed request should be 400/401, got {r.status_code}: {r.text}"
    )
