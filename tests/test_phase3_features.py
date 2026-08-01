"""Tests for v3.9.0 Phase 3 features (2026-08-01).

Three small, additive features in
`docs/soul-routing-design.md` §"Phased plan → Phase 3":

  Feature 1 — SOUL preset versioning
  Feature 2 — SOUL template library
  Feature 3 — Reset live SOUL

8 tests, all using the in-process app + ASGI client pattern from
`tests/test_phase2_ux.py` (FastAPI app, fresh tmpdir DB per test,
bootstrap admin already in place). The HTTP layer is what we care
about for these features (not the routing engine internals — those
are covered by `test_orchestrator_routing.py` / `_soul_dispatch.py`).
The test pattern is:

  @pytest_asyncio.fixture async def app_client(tmp_path):
      # patches Database.__init__ to use tmp_path, creates the app,
      # bootstraps the admin, yields (AsyncClient, app)

  # admin tests: `logged_in_client` fixture logs in as admin
  # non-admin tests: create a regular user, log in as them

Tests in this file:
  Versioning (3):
    1. test_preset_update_creates_new_version
    2. test_list_versions_returns_desc_order
    3. test_rollback_creates_new_version_with_old_content
  Template library (3):
    4. test_create_template_requires_admin
    5. test_create_preset_from_template
    6. test_list_templates_with_category_filter
  Reset (2):
    7. test_reset_live_soul_creates_empty_profile_config
    8. test_reset_audit_log
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch import db as db_mod
from hermes_orch import main as main_mod
from hermes_orch.auth.cookie import (
    ROLE_ADMIN, ROLE_USER, create_user, hash_password,
)
from hermes_orch.main import create_app


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"
REGULAR_USERNAME = "alice"
REGULAR_PASSWORD = "AlicePass123!"


# ===== In-process app fixtures (mirrors tests/test_phase2_ux.py) =====


async def _bootstrap_admin(app) -> str:
    """Create the bootstrap admin with a known password. Idempotent."""
    db = app.state.db
    existing = await db.fetchone(
        "SELECT id, password_hash FROM users WHERE username = ?",
        (ADMIN_USERNAME,),
    )
    if existing:
        if not existing.get("password_hash"):
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


async def _create_regular_user(app, username: str, password: str) -> str:
    """Create a regular (non-admin) user for the 403 test. Idempotent."""
    db = app.state.db
    existing = await db.fetchone(
        "SELECT id FROM users WHERE username = ?", (username,)
    )
    if existing:
        return existing["id"]
    return await create_user(
        db, username=username, password=password, role=ROLE_USER
    )


async def _login(ac: AsyncClient, username: str, password: str) -> None:
    r = await ac.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"


@contextmanager
def _patch_db_path(test_db_path):
    """Patch the Database class to use a per-test DB path under
    tmp_path. Returns the patched create_app so the caller can
    drive the app's lifespan manually."""
    orig_init = main_mod.create_app
    orig_db_init = db_mod.Database.__init__

    def patched_db_init(self, db_path):
        orig_db_init(self, test_db_path)

    db_mod.Database.__init__ = patched_db_init
    try:
        yield orig_init()
    finally:
        db_mod.Database.__init__ = orig_db_init


@pytest_asyncio.fixture
async def app_client(tmp_path):
    """Fresh app + AsyncClient with a bootstrap admin already in place."""
    test_db = tmp_path / "test.db"
    with _patch_db_path(test_db):
        app = create_app()
        async with app.router.lifespan_context(app):
            await _bootstrap_admin(app)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac, app


@pytest_asyncio.fixture
async def logged_in_client(app_client):
    """Yields (client, app) with the admin already logged in."""
    ac, app = app_client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    return ac, app


@pytest_asyncio.fixture
async def regular_client(app_client):
    """Yields (client, app) with a non-admin user created and logged in.

    The regular user is created on first call (idempotent) so the
    403-on-non-admin test has a stable non-admin to log in as.
    """
    ac, app = app_client
    await _create_regular_user(app, REGULAR_USERNAME, REGULAR_PASSWORD)
    await _login(ac, REGULAR_USERNAME, REGULAR_PASSWORD)
    return ac, app


# ===== Helpers =====


async def _create_test_project(ac: AsyncClient, name: str = "phase3-test") -> str:
    """Create a fresh project via the JSON API. Returns the project id."""
    r = await ac.post("/api/projects/", json={"name": name, "action": "do_step"})
    assert r.status_code in (200, 201), f"create project failed: {r.status_code} {r.text}"
    body = r.json()
    return body["id"]


async def _create_test_profile(
    app, agent_id: str | None = None, profile_name: str = "researcher"
) -> tuple[str, str]:
    """Create a real agent + agent_profile row directly via SQL so
    PUT /soul-presets has something to bind to. Returns (agent_id, profile_id)."""
    db = app.state.db
    aid = agent_id or f"agt-{uuid.uuid4().hex[:8]}"
    pid = f"prof-{uuid.uuid4().hex[:8]}"
    now = int(time.time())
    await db.execute(
        "INSERT INTO agents (id, ip, os_type, status, secret_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (aid, "127.0.0.1", "linux", "verified", "x" * 32, now),
    )
    await db.execute(
        "INSERT INTO agent_profiles "
        "(id, agent_id, name, description, status, skills, capabilities, "
        "mcp_servers, storage_refs, llm_model_default, llm_model_base_url, "
        "llm_model_provider, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pid, aid, profile_name, "test profile for phase3",
            "idle", json.dumps([]), json.dumps({}),
            json.dumps([]), json.dumps([]),
            None, None, None, now, now,
        ),
    )
    return aid, pid


async def _put_soul_preset(
    ac: AsyncClient, project_id: str, agent_id: str, profile_name: str,
    content: str, default_soul: str | None = None,
) -> dict:
    """PUT /api/projects/{id}/soul-presets. Returns the response body."""
    body: dict = {
        "agent_id": agent_id,
        "profile_name": profile_name,
        "content": content,
    }
    if default_soul is not None:
        body["default_soul"] = default_soul
    r = await ac.put(
        f"/api/projects/{project_id}/soul-presets",
        json=body,
    )
    assert r.status_code == 200, f"PUT soul-preset failed: {r.status_code} {r.text}"
    return r.json()


# =====================================================================
# Feature 1: SOUL Versioning
# =====================================================================


@pytest.mark.asyncio
async def test_preset_update_creates_new_version(logged_in_client):
    """PUT /soul-presets twice → 2 version rows exist, head is v2.

    Mirrors the spec: "Every time the preset's content or default_soul
    is updated, a new version row is inserted; version_number is the
    next sequential number for that preset_id; the 'head' columns stay
    in sync".
    """
    ac, app = logged_in_client
    pid = await _create_test_project(ac)
    agent_id, _profile_id = await _create_test_profile(app)
    # First PUT: creates the preset + v1.
    r1 = await _put_soul_preset(
        ac, pid, agent_id, "researcher", content="first content"
    )
    assert r1["content"] == "first content"
    preset_id = r1["id"]
    # Second PUT: same preset, different content → creates v2.
    r2 = await _put_soul_preset(
        ac, pid, agent_id, "researcher", content="second content"
    )
    assert r2["id"] == preset_id, "second PUT should be an upsert (same id)"
    assert r2["content"] == "second content"
    # Verify version rows directly.
    versions = await app.state.db.fetchall(
        "SELECT version_number, content FROM project_soul_preset_versions "
        "WHERE preset_id = ? ORDER BY version_number",
        (preset_id,),
    )
    assert len(versions) == 2, f"expected 2 versions, got {len(versions)}"
    assert [v["version_number"] for v in versions] == [1, 2]
    assert versions[0]["content"] == "first content"
    assert versions[1]["content"] == "second content"
    # Head columns on the preset row match the latest version.
    head = await app.state.db.fetchone(
        "SELECT content FROM project_soul_presets WHERE id = ?",
        (preset_id,),
    )
    assert head["content"] == "second content", (
        f"head content should match latest version; got {head['content']!r}"
    )


@pytest.mark.asyncio
async def test_list_versions_returns_desc_order(logged_in_client):
    """GET .../versions returns the version list newest-first.

    The list endpoint is the operator's mental model: "most recent
    on top, oldest at the bottom". This matches git log, the spec
    for the rollback target picker, and the UI's "v3 of 5" pill.
    """
    ac, app = logged_in_client
    pid = await _create_test_project(ac)
    agent_id, _ = await _create_test_profile(app)
    # Create 3 versions.
    for i, content in enumerate(["v1 body", "v2 body", "v3 body"], start=1):
        await _put_soul_preset(ac, pid, agent_id, "researcher", content=content)
    # Fetch the list.
    r = await ac.get(
        f"/api/projects/{pid}/soul-presets/{agent_id}/researcher/versions"
    )
    assert r.status_code == 200, f"list versions failed: {r.status_code} {r.text}"
    versions = r.json()
    assert len(versions) == 3, f"expected 3 versions, got {len(versions)}"
    # Newest first: v3, v2, v1.
    assert [v["version_number"] for v in versions] == [3, 2, 1], (
        f"expected [3, 2, 1], got {[v['version_number'] for v in versions]}"
    )
    assert versions[0]["content"] == "v3 body"
    assert versions[1]["content"] == "v2 body"
    assert versions[2]["content"] == "v1 body"


@pytest.mark.asyncio
async def test_rollback_creates_new_version_with_old_content(logged_in_client):
    """Rollback to v1 creates a new version (v3) with v1's content.

    The spec is explicit: rollback does NOT delete the intervening
    versions. It creates a new head row whose content matches the
    target. This is the standard append-only history pattern
    (git commits, Notion page history). The user can re-rollback
    later, and nothing is ever lost.
    """
    ac, app = logged_in_client
    pid = await _create_test_project(ac)
    agent_id, _ = await _create_test_profile(app)
    # Create 3 versions.
    for content in ["alpha", "beta", "gamma"]:
        await _put_soul_preset(ac, pid, agent_id, "researcher", content=content)
    # Rollback to v1 ("alpha"). This should create v4 with v1's content.
    r = await ac.post(
        f"/api/projects/{pid}/soul-presets/{agent_id}/researcher/rollback/1"
    )
    assert r.status_code == 200, f"rollback failed: {r.status_code} {r.text}"
    new_version = r.json()
    assert new_version["version_number"] == 4, (
        f"expected v4, got v{new_version['version_number']}"
    )
    assert new_version["content"] == "alpha", (
        f"rolled-back content should match v1; got {new_version['content']!r}"
    )
    # All 4 versions still exist (append-only — nothing deleted).
    versions = await app.state.db.fetchall(
        "SELECT version_number, content FROM project_soul_preset_versions "
        "WHERE preset_id IN ("
        "  SELECT id FROM project_soul_presets "
        "  WHERE project_id = ? AND profile_id IN ("
        "    SELECT id FROM agent_profiles WHERE agent_id = ? AND name = ?"
        "  )"
        ") ORDER BY version_number",
        (pid, agent_id, "researcher"),
    )
    assert len(versions) == 4, f"expected 4 versions, got {len(versions)}"
    assert [v["version_number"] for v in versions] == [1, 2, 3, 4]
    assert [v["content"] for v in versions] == ["alpha", "beta", "gamma", "alpha"]
    # Head columns on the preset row reflect the rolled-back content.
    head = await app.state.db.fetchone(
        "SELECT content, last_applied_at FROM project_soul_presets sp "
        "JOIN agent_profiles ap ON ap.id = sp.profile_id "
        "WHERE sp.project_id = ? AND ap.agent_id = ? AND ap.name = ?",
        (pid, agent_id, "researcher"),
    )
    assert head["content"] == "alpha", (
        f"preset head should be 'alpha' after rollback; got {head['content']!r}"
    )
    # last_applied_at must be cleared so the next dispatch detects
    # drift and re-applies the rolled-back content.
    assert head["last_applied_at"] is None, (
        f"last_applied_at should be NULL after rollback; got {head['last_applied_at']!r}"
    )


# =====================================================================
# Feature 2: SOUL Template Library
# =====================================================================


@pytest.mark.asyncio
async def test_create_template_requires_admin(regular_client, app_client):
    """POST /api/soul-templates without admin role returns 403.

    The spec says templates are "admin-only" for the CRUD
    operations. Read (GET) is open; write (POST/PUT/DELETE) is
    admin-only. We verify the POST path here (the most dangerous
    of the three — anyone can create a template named
    'cpi-analyst' and pollute the library).
    """
    regular_ac, _regular_app = regular_client
    # The regular user is logged in. Try to create a template.
    r = await regular_ac.post(
        "/api/soul-templates",
        json={
            "name": "evil-template",
            "category": "evil",
            "content": "I am evil",
            "description": "should be rejected",
        },
    )
    assert r.status_code == 403, (
        f"non-admin POST should return 403; got {r.status_code} {r.text}"
    )
    # Confirm the row was NOT created (defense-in-depth).
    _admin_ac, admin_app = app_client
    row = await admin_app.state.db.fetchone(
        "SELECT id FROM project_soul_templates WHERE LOWER(name) = LOWER('evil-template')"
    )
    assert row is None, "evil-template should not have been created"


@pytest.mark.asyncio
async def test_create_preset_from_template(logged_in_client):
    """Admin creates a template, then a project creates a preset from it.

    The from-template endpoint:
      1. Looks up the template by name (case-insensitive).
      2. Validates project + profile exist.
      3. Inserts a project_soul_presets row with the template's
         content and role_name = template's name.
      4. Inserts a v1 version row (so the versioning UI shows
         "v1 of 1" immediately).
    """
    ac, app = logged_in_client
    # Step 1: admin publishes a template.
    r = await ac.post(
        "/api/soul-templates",
        json={
            "name": "cpi-analyst",
            "category": "finance",
            "content": "You are a CPI/PPI correlation analyst...",
            "description": "Macro analyst for inflation data",
        },
    )
    assert r.status_code == 201, f"create template failed: {r.status_code} {r.text}"
    template = r.json()
    assert template["name"] == "cpi-analyst"
    # Step 2: create a project + profile to instantiate the preset into.
    pid = await _create_test_project(ac)
    agent_id, _ = await _create_test_profile(app, profile_name="macro")
    # Step 3: instantiate.
    r = await ac.post(
        f"/api/projects/{pid}/soul-presets/from-template/cpi-analyst",
        json={"agent_id": agent_id, "profile_name": "macro"},
    )
    assert r.status_code == 201, (
        f"from-template failed: {r.status_code} {r.text}"
    )
    body = r.json()
    assert body["template_name"] == "cpi-analyst"
    assert body["role_name"] == "cpi-analyst"
    assert body["content"] == "You are a CPI/PPI correlation analyst..."
    # Step 4: verify the preset was inserted with the expected shape.
    preset = await app.state.db.fetchone(
        "SELECT sp.*, ap.agent_id, ap.name AS profile_name "
        "FROM project_soul_presets sp "
        "JOIN agent_profiles ap ON ap.id = sp.profile_id "
        "WHERE sp.project_id = ? AND ap.agent_id = ? AND ap.name = ?",
        (pid, agent_id, "macro"),
    )
    assert preset is not None, "preset row not found after from-template"
    assert preset["role_name"] == "cpi-analyst"
    assert preset["content"] == "You are a CPI/PPI correlation analyst..."
    # Step 5: verify the v1 version row exists.
    versions = await app.state.db.fetchall(
        "SELECT version_number, content FROM project_soul_preset_versions "
        "WHERE preset_id = ? ORDER BY version_number",
        (preset["id"],),
    )
    assert len(versions) == 1, f"expected 1 version, got {len(versions)}"
    assert versions[0]["version_number"] == 1
    assert versions[0]["content"] == "You are a CPI/PPI correlation analyst..."


@pytest.mark.asyncio
async def test_list_templates_with_category_filter(logged_in_client):
    """GET /api/soul-templates?category=finance returns only finance templates.

    The filter is case-insensitive (the column is COLLATE NOCASE;
    the query uses LOWER() for explicit safety). Templates in other
    categories must not appear.
    """
    ac, _app = logged_in_client
    # Publish 3 templates across 2 categories.
    for name, cat, body in [
        ("cpi-analyst", "Finance", "inflation body"),
        ("code-reviewer", "Engineering", "review body"),
        ("data-engineer", "Engineering", "data body"),
    ]:
        r = await ac.post(
            "/api/soul-templates",
            json={"name": name, "category": cat, "content": body, "description": name},
        )
        assert r.status_code == 201, f"create {name} failed: {r.status_code} {r.text}"
    # Filter by "finance" (case mismatch — must still match "Finance").
    r = await ac.get("/api/soul-templates?category=finance")
    assert r.status_code == 200, f"list filtered failed: {r.status_code} {r.text}"
    items = r.json()
    names = sorted(t["name"] for t in items)
    assert names == ["cpi-analyst"], (
        f"expected only cpi-analyst; got {names}"
    )
    # Filter by "engineering".
    r = await ac.get("/api/soul-templates?category=Engineering")
    items = r.json()
    names = sorted(t["name"] for t in items)
    assert names == ["code-reviewer", "data-engineer"], (
        f"expected 2 engineering templates; got {names}"
    )
    # No filter → all 3.
    r = await ac.get("/api/soul-templates")
    items = r.json()
    assert len(items) == 3, f"expected 3 templates; got {len(items)}"
    # Filter that matches nothing → empty list (not 404).
    r = await ac.get("/api/soul-templates?category=does-not-exist")
    assert r.status_code == 200
    assert r.json() == []


# =====================================================================
# Feature 3: Reset Live SOUL
# =====================================================================


@pytest.mark.asyncio
async def test_reset_live_soul_creates_empty_profile_config(logged_in_client):
    """POST /soul/reset inserts a profile_configs row with empty content.

    The wrapper's existing claim→ack loop (api/agents.py::
    claim_pending_config) will pick this up on the next tick and
    write an empty SOUL.md on the agent host. We verify the
    DB-side state (the row exists with the right shape) — the
    wrapper side is a separate, pre-existing flow that's tested
    elsewhere (the e2e tests in test_orchestrator_soul_dispatch.py).
    """
    import hashlib
    ac, app = logged_in_client
    agent_id, _ = await _create_test_profile(app, profile_name="cleared-profile")
    # Reset the live SOUL.
    r = await ac.post(
        f"/api/agents/{agent_id}/profiles/cleared-profile/soul/reset"
    )
    assert r.status_code == 201, f"reset failed: {r.status_code} {r.text}"
    body = r.json()
    # The response should look like a normal ProfileConfig with
    # the empty-file metadata.
    assert body["file_path"] == "SOUL.md"
    assert body["desired_content"] == ""
    expected_sha = hashlib.sha256(b"").hexdigest()
    assert body["desired_sha256"] == expected_sha, (
        f"sha256 should be of empty string; got {body['desired_sha256']!r}"
    )
    assert body["status"] == "pending"
    # Verify the row is in the DB.
    cfg = await app.state.db.fetchone(
        "SELECT pc.* FROM profile_configs pc "
        "JOIN agent_profiles ap ON ap.id = pc.profile_id "
        "WHERE ap.agent_id = ? AND ap.name = ? "
        "ORDER BY pc.created_at DESC LIMIT 1",
        (agent_id, "cleared-profile"),
    )
    assert cfg is not None, "profile_configs row not found after reset"
    assert cfg["desired_content"] == ""
    assert cfg["desired_sha256"] == expected_sha
    assert cfg["file_path"] == "SOUL.md"
    assert cfg["status"] == "pending"


@pytest.mark.asyncio
async def test_reset_audit_log(logged_in_client):
    """The reset writes a profile.soul_reset audit log entry with
    actor=admin, agent_id, and profile_name in the payload.

    The audit shape is the most-tested contract of any admin
    action (compliance + debugging). Verify all required fields
    are present so the dashboard's audit page renders correctly.
    """
    import hashlib
    ac, app = logged_in_client
    agent_id, _ = await _create_test_profile(app, profile_name="audited-profile")
    r = await ac.post(
        f"/api/agents/{agent_id}/profiles/audited-profile/soul/reset"
    )
    assert r.status_code == 201, f"reset failed: {r.status_code} {r.text}"
    # Find the audit log entry.
    rows = await app.state.db.fetchall(
        "SELECT event_type, actor, agent_id, payload FROM audit_log "
        "WHERE event_type = 'profile.soul_reset' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert len(rows) == 1, f"expected 1 audit row, got {len(rows)}"
    row = rows[0]
    assert row["event_type"] == "profile.soul_reset"
    # Actor should be the logged-in admin (NOT 'orch_server' or 'operator').
    assert row["actor"] == ADMIN_USERNAME, (
        f"actor should be the admin username; got {row['actor']!r}"
    )
    assert row["agent_id"] == agent_id
    # Payload is JSON-encoded text.
    payload = json.loads(row["payload"]) if row["payload"] else {}
    assert payload.get("profile_name") == "audited-profile", (
        f"payload should include profile_name; got {payload!r}"
    )
    assert payload.get("config_id"), "payload should include config_id"
    expected_sha = hashlib.sha256(b"").hexdigest()
    assert payload.get("desired_sha256") == expected_sha, (
        f"payload should include sha256 of empty string; got {payload.get('desired_sha256')!r}"
    )
    assert payload.get("size") == 0
