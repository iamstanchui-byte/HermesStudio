# coding: utf-8
"""Tests for v1.0.1 enrollment API (issue + consume + revoke).

Covers the spec §3.3 contract:
  T1.5  Enrollment tokens: random 256-bit, stored hashed, 15-min
        expiry, single-use, atomic consume
  T1.5a Consume transaction writes used_by_agent_id back onto the
        token row in the same transaction
  T1.5b Agent-declared agent_name always wins over requested_agent_name
  T1.6  Used / expired enrollment tokens return 410 with clear error
        (no HMAC secret leak)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch.auth.cookie import ROLE_ADMIN, ROLE_USER, create_user
from hermes_orch.core.enrollment import hash_token
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
            role=ROLE_USER,
        )
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


# ===== Issue endpoint =====

@pytest.mark.asyncio
async def test_issue_token_unauthenticated_is_401_or_403(client):
    """Unauth gets 401 from the user-middleware; 403 only if a logged-in
    non-admin tries. We accept either for the unauth case (both are
    correct security responses).
    """
    ac, _ = client
    ac.cookies.clear()
    r = await ac.post("/api/enrollment-tokens", json={})
    assert r.status_code in (401, 403), f"got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_issue_token_non_admin_is_403(client):
    ac, _ = client
    await _login(ac, "alice", "AlicePass123!")
    r = await ac.post("/api/enrollment-tokens", json={})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_issue_token_admin_returns_plaintext_once(client):
    """Admin issues a token, gets plaintext + install_command back."""
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.post(
        "/api/enrollment-tokens",
        json={"label": "Home laptop", "requested_agent_name": "win-01"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["token"].startswith("etok-")
    assert data["label"] == "Home laptop"
    assert data["requested_agent_name"] == "win-01"
    assert "install_command" in data
    assert "hermes-orch-agent enroll" in data["install_command"]
    assert data["token"] in data["install_command"]
    # DB row has token_hash, NOT plaintext
    row = await app.state.db.fetchone(
        "SELECT token_hash, requested_agent_name FROM enrollment_tokens WHERE id = ?",
        (data["id"],),
    )
    assert row["token_hash"] == hash_token(data["token"])
    assert row["requested_agent_name"] == "win-01"


@pytest.mark.asyncio
async def test_issue_token_install_command_includes_pip_install_step(client):
    """v1.0.1 §3.3 contract: install_command is a full one-liner that
    works on a brand-new host with no hermes-orch-agent installed.

    The command must include BOTH:
      1. The pip install step (so a fresh host gets the CLI)
      2. The enroll step (consumes the token)
    """
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.post(
        "/api/enrollment-tokens",
        json={"label": "x", "requested_agent_name": "agent-1"},
    )
    cmd = r.json()["install_command"]
    # The install step — pulls the package (server + agent CLI) from
    # GitHub. Without this, the host has no `hermes-orch-agent` to run.
    assert "pip install" in cmd, f"missing pip install: {cmd!r}"
    assert "hermes-orchestrator" in cmd, f"missing package: {cmd!r}"
    # The enroll step — consumes the token, creates the agent row.
    assert "hermes-orch-agent enroll" in cmd
    # The token is inline (per spec §3.3.1 the brief ps-leak is
    # an accepted v1.0.1 limitation; we surface this in the UI
    # tooltip, not in the command itself).
    assert r.json()["token"] in cmd
    # The two steps are chained so the enroll can't run before
    # the install completes.
    assert "&&" in cmd, f"install + enroll must be chained: {cmd!r}"


@pytest.mark.asyncio
async def test_issue_token_install_command_is_tag_pinned(client):
    """v1.0.1 (P3.3 polish): the install URL is tag-pinned to v0.10.0
    (matches pyproject.toml). New users get the v0.10.0 source, not
    whatever happens to be on `main` today.

    Perplexity review (2026-08-08): the repo URL
    `github.com/iamstanchui-byte/HermesStudio` is correct (the project
    is named `hermes-orchestrator` but lives inside the user's
    umbrella repo). The tag pin is the safety belt: if the user
    hasn't pushed the `v0.10.0` tag, this exact command will fail
    with a clear pip error (NOT silently install a different version).
    """
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.post(
        "/api/enrollment-tokens",
        json={"label": "x", "requested_agent_name": "agent-1"},
    )
    cmd = r.json()["install_command"]
    # The exact form: pip install "<name> @ git+<url>@<tag>"
    assert "@v0.10.0" in cmd, f"install URL must be tag-pinned to v0.10.0: {cmd!r}"
    # The @ pinning must come AFTER the .git suffix (not just
    # somewhere in the URL).
    assert ".git@v0.10.0" in cmd, (
        f"tag pin must be after .git (PEP 508 git ref syntax): {cmd!r}"
    )


@pytest.mark.asyncio
async def test_issue_token_install_command_url_is_canonical(client):
    """The repo URL is `iamstanchui-byte/HermesStudio` — the user's
    umbrella repo. The project itself is named `hermes-orchestrator`
    in pyproject.toml. This test guards against accidental URL drift
    (e.g. pointing to a fork or wrong repo) — the URL must stay on
    the canonical `HermesStudio` repo.
    """
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.post(
        "/api/enrollment-tokens",
        json={"label": "x", "requested_agent_name": "agent-1"},
    )
    cmd = r.json()["install_command"]
    # Canonical URL — DO NOT change without coordinating with
    # the user. Perplexity flagged this as a P0 verification
    # point on 2026-08-08.
    assert "iamstanchui-byte/HermesStudio" in cmd, (
        f"URL must point to canonical repo HermesStudio: {cmd!r}"
    )


@pytest.mark.asyncio
async def test_issue_token_does_not_leak_plaintext_in_list(client):
    """The list endpoint must never include the plaintext."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Issue a token
    r1 = await ac.post(
        "/api/enrollment-tokens", json={"label": "Test"}
    )
    issued = r1.json()
    # List
    r2 = await ac.get("/api/enrollment-tokens")
    assert r2.status_code == 200
    items = r2.json()
    assert len(items) >= 1
    # The list item has no `token` field
    for item in items:
        assert "token" not in item
        # And the plaintext doesn't appear anywhere in the response
        assert issued["token"] not in r2.text


# ===== Consume endpoint =====

@pytest.mark.asyncio
async def test_consume_unknown_token_is_404(client):
    """A bogus token (never issued) returns 404 with no secret leak."""
    ac, _ = client
    r = await ac.post(
        "/api/agents/enroll",
        json={"token": "etok-this-was-never-issued", "agent_name": "x"},
    )
    assert r.status_code == 404
    # No hmac_secret anywhere in the response
    assert "hmac_secret" not in r.text
    assert "hmac" not in r.text.lower()


@pytest.mark.asyncio
async def test_consume_happy_path_creates_agent(client):
    """Admin issues a token, agent host consumes it, agent row is created."""
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r_issue = await ac.post(
        "/api/enrollment-tokens",
        json={"label": "Test host", "requested_agent_name": "win-01"},
    )
    token = r_issue.json()["token"]

    # Agent host consumes
    r_consume = await ac.post(
        "/api/agents/enroll",
        json={"token": token, "agent_name": "win-01", "hostname": "test-host"},
    )
    assert r_consume.status_code == 200
    data = r_consume.json()
    assert data["agent_id"].startswith("agent-")
    # hmac_secret is shown once and is 32+ bytes base64
    assert len(data["hmac_secret"]) >= 40

    # Agent row exists
    agent = await app.state.db.fetchone(
        "SELECT * FROM agents WHERE id = ?", (data["agent_id"],)
    )
    assert agent is not None
    assert agent["name"] == "win-01"
    # v0.7 IP fix (2026-08-17): `ip` is the actual TCP connection
    # source (request.client.host), `hostname` is the agent-declared
    # hostname. Previously the column-position was wrong and the
    # hostname string ended up in the `ip` column. The TestClient
    # connects from 127.0.0.1.
    assert agent["ip"] == "127.0.0.1"
    assert agent["hostname"] == "test-host"
    assert agent["status"] == "verifying"
    assert agent["hmac_secret"] == data["hmac_secret"]
    assert agent["secret_hash"] != data["hmac_secret"]  # hash != plaintext


@pytest.mark.asyncio
async def test_consume_returns_v07_fields_with_correct_format(client):
    """v0.7 §1.4 (2026-08-17): EnrollOut includes hmac_secret_hex and
    hmac_key_id. The DB row has hmac_key_id populated, hex is 64
    lowercase chars, and hmac_key_id is `kw_` + 12 lowercase alnum.
    """
    import re as _re

    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r_issue = await ac.post(
        "/api/enrollment-tokens",
        json={"label": "v0.7 test", "requested_agent_name": "v07-01"},
    )
    token = r_issue.json()["token"]

    r_consume = await ac.post(
        "/api/agents/enroll",
        json={"token": token, "agent_name": "v07-01", "hostname": "v07-host"},
    )
    assert r_consume.status_code == 200, r_consume.text
    data = r_consume.json()

    # hmac_secret_hex: exactly 64 lowercase hex chars
    assert "hmac_secret_hex" in data, "EnrollOut missing hmac_secret_hex (v0.7 §1.4)"
    assert _re.fullmatch(r"[0-9a-f]{64}", data["hmac_secret_hex"]), (
        f"hmac_secret_hex must be 64 lowercase hex chars, got {data['hmac_secret_hex']!r}"
    )

    # hmac_key_id: 'kw_' + 12 lowercase alnum
    assert "hmac_key_id" in data, "EnrollOut missing hmac_key_id (v0.7 §1.4)"
    assert _re.fullmatch(r"kw_[a-z0-9]{12}", data["hmac_key_id"]), (
        f"hmac_key_id must be 'kw_' + 12 lowercase alnum, got {data['hmac_key_id']!r}"
    )

    # Invariant: hex secret and base64url secret must be the same 32 bytes
    import base64 as _b64
    hmac_secret_padded = data["hmac_secret"] + "=" * (-len(data["hmac_secret"]) % 4)
    base64_bytes = _b64.urlsafe_b64decode(hmac_secret_padded)
    hex_bytes = bytes.fromhex(data["hmac_secret_hex"])
    assert base64_bytes == hex_bytes, (
        "v0.6 hmac_secret (base64url) and v0.7 hmac_secret_hex must encode the same bytes"
    )

    # DB row: hmac_key_id populated, matches response
    agent = await app.state.db.fetchone(
        "SELECT hmac_key_id FROM agents WHERE id = ?", (data["agent_id"],)
    )
    assert agent is not None
    assert agent["hmac_key_id"] == data["hmac_key_id"], (
        "agents.hmac_key_id must match the EnrollOut.hmac_key_id"
    )


@pytest.mark.asyncio
async def test_consume_writes_used_by_agent_id_in_same_transaction(client):
    """T1.5a: used_by_agent_id is set on the token row, same transaction."""
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r_issue = await ac.post("/api/enrollment-tokens", json={"label": "x"})
    token_id = r_issue.json()["id"]
    token = r_issue.json()["token"]

    r_consume = await ac.post(
        "/api/agents/enroll",
        json={"token": token, "agent_name": "x"},
    )
    agent_id = r_consume.json()["agent_id"]

    # The token row now has used_by_agent_id set + used_at set
    row = await app.state.db.fetchone(
        "SELECT used_at, used_by_agent_id FROM enrollment_tokens WHERE id = ?",
        (token_id,),
    )
    assert row["used_at"] is not None
    assert row["used_by_agent_id"] == agent_id


@pytest.mark.asyncio
async def test_consume_marks_token_used_atomically(client):
    """After a successful consume, the second consume returns 410."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r_issue = await ac.post("/api/enrollment-tokens", json={})
    token = r_issue.json()["token"]
    # First consume: 200
    r1 = await ac.post(
        "/api/agents/enroll", json={"token": token, "agent_name": "x"}
    )
    assert r1.status_code == 200
    # Second consume: 410 (already used)
    r2 = await ac.post(
        "/api/agents/enroll", json={"token": token, "agent_name": "x"}
    )
    assert r2.status_code == 410
    assert "already" in r2.text.lower() or "used" in r2.text.lower()
    # And no second agent row was created
    assert "hmac_secret" not in r2.text


@pytest.mark.asyncio
async def test_consume_expired_token_is_410(client, monkeypatch):
    """T1.6: An expired token returns 410 with a clear error."""
    from datetime import datetime, timezone
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r_issue = await ac.post("/api/enrollment-tokens", json={})
    token = r_issue.json()["token"]
    token_id = r_issue.json()["id"]

    # Manually expire the token by backdating expires_at
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    await app.state.db.execute(
        "UPDATE enrollment_tokens SET expires_at = ? WHERE id = ?",
        (expired, token_id),
    )

    r = await ac.post(
        "/api/agents/enroll", json={"token": token, "agent_name": "x"}
    )
    assert r.status_code == 410
    assert "expired" in r.text.lower()
    # No hmac_secret in response
    assert "hmac_secret" not in r.text


@pytest.mark.asyncio
async def test_consume_agent_name_wins_over_hint(client):
    """T1.5b: agent-declared name ALWAYS wins over requested_agent_name."""
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r_issue = await ac.post(
        "/api/enrollment-tokens",
        json={"label": "x", "requested_agent_name": "win-01"},
    )
    token = r_issue.json()["token"]

    # Agent host declares a different name
    r_consume = await ac.post(
        "/api/agents/enroll",
        json={"token": token, "agent_name": "win-02-different"},
    )
    assert r_consume.status_code == 200
    agent_id = r_consume.json()["agent_id"]
    # Agent's name is win-02-different (the agent's self-declared
    # name), NOT win-01 (the operator's hint)
    agent = await app.state.db.fetchone(
        "SELECT name FROM agents WHERE id = ?", (agent_id,)
    )
    assert agent["name"] == "win-02-different"


@pytest.mark.asyncio
async def test_consume_reports_when_requested_name_was_used(client):
    """If the agent's self-declared name matches the hint (or agent
    sent empty), requested_name_used=True."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r_issue = await ac.post(
        "/api/enrollment-tokens",
        json={"label": "x", "requested_agent_name": "win-01"},
    )
    token = r_issue.json()["token"]
    # Agent declares the SAME name as the hint
    r_consume = await ac.post(
        "/api/agents/enroll",
        json={"token": token, "agent_name": "win-01"},
    )
    assert r_consume.status_code == 200
    data = r_consume.json()
    # Same name → requested_name_used=True (the operator's hint was
    # the actual name used)
    assert data["requested_name_used"] is True


@pytest.mark.asyncio
async def test_consume_flips_agent_connected_for_issuer(client):
    """T1.7: after successful enroll, the user who issued the token
    gets agent_connected=true in their onboarding_state."""
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Reset admin state to all-false first
    from hermes_orch.core.onboarding import (
        SIGNAL_AGENT_CONNECTED, reset_state, serialize_state,
    )
    admin_row = await app.state.db.fetchone(
        "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    await app.state.db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(reset_state()), admin_row["id"]),
    )
    # Issue + consume
    r_issue = await ac.post("/api/enrollment-tokens", json={})
    token = r_issue.json()["token"]
    r_consume = await ac.post(
        "/api/agents/enroll", json={"token": token, "agent_name": "x"}
    )
    assert r_consume.status_code == 200
    # Check admin's onboarding state
    r_ob = await ac.get("/api/me/onboarding")
    data = r_ob.json()
    assert data["state"]["signals"][SIGNAL_AGENT_CONNECTED] is True


# ===== Revoke endpoint =====

@pytest.mark.asyncio
async def test_revoke_token_admin_succeeds(client):
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r_issue = await ac.post("/api/enrollment-tokens", json={"label": "to-revoke"})
    token_id = r_issue.json()["id"]
    r_del = await ac.delete(f"/api/enrollment-tokens/{token_id}")
    assert r_del.status_code == 200
    # Row is gone
    row = await app.state.db.fetchone(
        "SELECT id FROM enrollment_tokens WHERE id = ?", (token_id,)
    )
    assert row is None


@pytest.mark.asyncio
async def test_revoke_token_non_admin_is_403(client):
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r_issue = await ac.post("/api/enrollment-tokens", json={})
    token_id = r_issue.json()["id"]
    await _login(ac, "alice", "AlicePass123!")
    r_del = await ac.delete(f"/api/enrollment-tokens/{token_id}")
    assert r_del.status_code == 403


@pytest.mark.asyncio
async def test_revoke_makes_consume_404(client):
    """After revoke, the plaintext is useless (the row is gone)."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r_issue = await ac.post("/api/enrollment-tokens", json={})
    token = r_issue.json()["token"]
    token_id = r_issue.json()["id"]
    # Revoke
    await ac.delete(f"/api/enrollment-tokens/{token_id}")
    # Try to consume — should 404 (no row with this hash)
    r = await ac.post(
        "/api/agents/enroll", json={"token": token, "agent_name": "x"}
    )
    assert r.status_code == 404
