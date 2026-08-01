# coding: utf-8
"""Regression test: 'Open folder' button opens the deliverable dir, not the metadata dir.

Background (v3.10.4, 2026-08-02):
  User reported the 'Open folder' button on the project page 'did
  nothing'. Root cause was UX, not a missing feature: the button
  always opened the orchestrator's project metadata dir
  (C:/Project/minimax code/hermes-project/<id>), but the user
  expected to see the project's deliverable in the share folder
  (e.g. \\HERMES-WIN\\project_temp_folder). The button 'worked'
  (explorer.exe opened a window) but in the wrong directory, so
  the user saw no relevant files and assumed the feature was
  broken.

Fix: in `api/projects.py::open_project_folder`, the endpoint now
resolves the target path in this order:
  1. `projects.deliverable_path` (operator-curated) if it exists
  2. First `agent_profiles.storage_refs` of kind=local or kind=smb
     that exists, for any profile the project has used
  3. Fallback to the metadata dir (old behavior)

The response also includes `source`, `metadata_path`, and
`deliverable_opened` so the UI can tell the user which folder
opened and offer a one-click alternative.

This test verifies the resolution logic end-to-end via the
ASGITransport (no real file manager launches — `open_path` is
patched to return ok=True and record what it was called with).
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch import db as db_mod
from hermes_orch import main as main_mod
from hermes_orch.auth.cookie import ROLE_ADMIN, create_user


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


async def _bootstrap_admin(app) -> str:
    """Idempotent admin bootstrap with password_hash pre-set.

    The create_user() helper sets is_bootstrap_admin=True and leaves
    password_hash=NULL so the bootstrap flow (`/setup-password`)
    works in production. For tests we want a pre-hashed password
    so the standard `/api/auth/login` flow succeeds.
    """
    db = app.state.db
    from hermes_orch.auth.cookie import hash_password

    existing = await db.fetchone(
        "SELECT id, password_hash FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    if existing:
        if not existing.get("password_hash"):
            await db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(ADMIN_PASSWORD), existing["id"]),
            )
        return existing["id"]
    user_id = await create_user(
        db,
        username=ADMIN_USERNAME,
        password=ADMIN_PASSWORD,
        role=ROLE_ADMIN,
        is_bootstrap_admin=True,
    )
    # create_user() leaves password_hash NULL for bootstrap admin;
    # set it directly so login works.
    await db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(ADMIN_PASSWORD), user_id),
    )
    return user_id


async def _login_admin(ac: AsyncClient) -> None:
    r = await ac.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert r.status_code in (200, 201), r.text


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Fresh app + AsyncClient. Patches Database to use tmp_path
    AND patches config.projects.storage_root to a tmp dir so the
    metadata dir resolution works in isolation."""
    from hermes_orch import config as cfg_mod

    test_db = tmp_path / "test.db"
    test_projects_root = tmp_path / "projects"
    test_projects_root.mkdir()

    # Patch Database to use test_db
    orig_db_init = db_mod.Database.__init__

    def patched_db_init(self, db_path):
        orig_db_init(self, test_db)

    monkeypatch.setattr(db_mod.Database, "__init__", patched_db_init)

    # Patch config loader so projects.storage_root points to tmp.
    # main.py does `from hermes_orch.config import load_config`,
    # so we must patch the IMPORTED name in main_mod, not the
    # source module attribute.
    orig_load = cfg_mod.load_config

    def patched_load(*args, **kwargs):
        cfg = orig_load(*args, **kwargs)
        cfg["projects"]["storage_root"] = str(test_projects_root)
        return cfg

    monkeypatch.setattr("hermes_orch.main.load_config", patched_load)

    app = main_mod.create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        await _bootstrap_admin(app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, test_projects_root


# Capture calls to platform_compat.open_path so we can assert
# the resolved target. The real implementation would launch
# explorer.exe (and would fail in CI), so we always patch it.
@pytest.fixture
def captured_open_path(monkeypatch):
    """Returns a list that the patched open_path appends to. Each
    entry is (path_str,). Order = call order."""
    calls: list[tuple[str]] = []

    def fake_open_path(p):
        calls.append((str(p),))
        return True, None

    # Patch where it's used (inside the endpoint) — api/projects.py
    # imports `from hermes_orch.core.platform_compat import open_path`
    # at function call time, so we patch the module-level name.
    monkeypatch.setattr(
        "hermes_orch.core.platform_compat.open_path", fake_open_path
    )
    return calls


# ===== Tests for resolution order =====


@pytest.mark.asyncio
async def test_open_uses_deliverable_path_when_set(client, captured_open_path):
    """Priority 1: project.deliverable_path (operator-curated).
    The metadata dir is also created; we assert the deliverable
    wins."""
    ac, projects_root = client
    await _login_admin(ac)
    db = ac._transport.app.state.db  # type: ignore[attr-defined]

    deliverable = projects_root.parent / "deliverable-dir"
    deliverable.mkdir()
    (deliverable / "report.md").write_text("hi", encoding="utf-8")

    pid = f"proj-deliverable-{uuid.uuid4().hex[:8]}"
    metadata_dir = projects_root / pid
    metadata_dir.mkdir()

    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, ?, 'planned', '', '', ?, 0, 0, '')",
        (pid, "deliverable test", "x", str(deliverable)),
    )

    r = await ac.post(f"/api/projects/{pid}/open")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["path"] == str(deliverable)
    assert body["deliverable_opened"] is True
    assert body["source"] == "project.deliverable_path"
    assert body["metadata_path"] == str(metadata_dir)
    # And open_path was called with the deliverable, not metadata
    assert captured_open_path == [(str(deliverable),)]


@pytest.mark.asyncio
async def test_open_uses_profile_storage_refs_when_no_deliverable(
    client, captured_open_path
):
    """Priority 2: profile storage_refs of kind=local that exists."""
    ac, projects_root = client
    await _login_admin(ac)
    db = ac._transport.app.state.db  # type: ignore[attr-defined]

    # Set up a fake "share" dir that the agent profile points to
    share = projects_root.parent / "share-folder"
    share.mkdir()
    (share / "output.docx").write_text("docx content", encoding="utf-8")

    pid = f"proj-storage-{uuid.uuid4().hex[:8]}"
    metadata_dir = projects_root / pid
    metadata_dir.mkdir()

    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, ?, 'planned', '', '', '', 0, 0, '')",
        (pid, "storage test", "x"),
    )

    # Set up agent + profile with storage_refs pointing to share
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    profile_id = f"test-prof-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agents (id, secret_hash, hmac_secret, status, created_at) "
        "VALUES (?, '', '', 'verified', CURRENT_TIMESTAMP)",
        (agent_id,),
    )
    await db.execute(
        "INSERT INTO agent_profiles (id, agent_id, name, description, status, "
        "capabilities, mcp_servers, storage_refs, skills) "
        "VALUES (?, ?, ?, '', 'idle', '{}', '[]', ?, '[]')",
        (
            profile_id,
            agent_id,
            "test-profile",
            json.dumps([{"name": "share", "kind": "local", "ref": str(share)}]),
        ),
    )
    # Project has touched this profile (via task or session)
    task_id = f"t-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, "
        "assigned_profile_id, status) "
        "VALUES (?, ?, 'do-stuff', 'test-profile', ?, 'completed')",
        (task_id, pid, profile_id),
    )

    r = await ac.post(f"/api/projects/{pid}/open")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["path"] == str(share)
    assert body["deliverable_opened"] is True
    assert body["source"] == f"profile:test-profile:local"
    assert body["metadata_path"] == str(metadata_dir)
    assert captured_open_path == [(str(share),)]


@pytest.mark.asyncio
async def test_open_falls_back_to_metadata_when_no_deliverable_or_storage(
    client, captured_open_path
):
    """Priority 3: metadata dir. The OLD behavior — preserved as fallback."""
    ac, projects_root = client
    await _login_admin(ac)
    db = ac._transport.app.state.db  # type: ignore[attr-defined]

    pid = f"proj-fallback-{uuid.uuid4().hex[:8]}"
    metadata_dir = projects_root / pid
    metadata_dir.mkdir()

    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, ?, 'planned', '', '', '', 0, 0, '')",
        (pid, "fallback test", "x"),
    )

    r = await ac.post(f"/api/projects/{pid}/open")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # Falls back to metadata dir
    assert body["path"] == str(metadata_dir)
    assert body["deliverable_opened"] is False
    assert "source" not in body or body.get("source") is None
    assert body["metadata_path"] == str(metadata_dir)
    assert captured_open_path == [(str(metadata_dir),)]


@pytest.mark.asyncio
async def test_open_skips_url_only_storage_refs(client, captured_open_path):
    """gdrive / http / s3 refs CAN'T be opened in the file manager
    — only local / smb. Verify URL-only refs are ignored and we
    fall back to metadata dir."""
    ac, projects_root = client
    await _login_admin(ac)
    db = ac._transport.app.state.db  # type: ignore[attr-defined]

    pid = f"proj-url-{uuid.uuid4().hex[:8]}"
    metadata_dir = projects_root / pid
    metadata_dir.mkdir()

    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, ?, 'planned', '', '', '', 0, 0, '')",
        (pid, "url test", "x"),
    )

    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    profile_id = f"test-prof-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agents (id, secret_hash, hmac_secret, status, created_at) "
        "VALUES (?, '', '', 'verified', CURRENT_TIMESTAMP)",
        (agent_id,),
    )
    # Only URL refs (gdrive) — should be skipped
    await db.execute(
        "INSERT INTO agent_profiles (id, agent_id, name, description, status, "
        "capabilities, mcp_servers, storage_refs, skills) "
        "VALUES (?, ?, ?, '', 'idle', '{}', '[]', ?, '[]')",
        (
            profile_id,
            agent_id,
            "gdrive-only-profile",
            json.dumps([{"name": "gdrive", "kind": "gdrive",
                          "ref": "https://drive.google.com/..."}]),
        ),
    )
    task_id = f"t-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, "
        "assigned_profile_id, status) "
        "VALUES (?, ?, 'do-stuff', 'gdrive-only-profile', ?, 'completed')",
        (task_id, pid, profile_id),
    )

    r = await ac.post(f"/api/projects/{pid}/open")
    assert r.status_code == 200, r.text
    body = r.json()
    # URL ref is ignored, falls back to metadata
    assert body["deliverable_opened"] is False
    assert body["path"] == str(metadata_dir)
    assert captured_open_path == [(str(metadata_dir),)]


@pytest.mark.asyncio
async def test_open_skips_nonexistent_local_storage(client, captured_open_path):
    """If the local ref points to a non-existent path, fall back
    to the metadata dir (don't open a window to a missing folder)."""
    ac, projects_root = client
    await _login_admin(ac)
    db = ac._transport.app.state.db  # type: ignore[attr-defined]

    pid = f"proj-missing-{uuid.uuid4().hex[:8]}"
    metadata_dir = projects_root / pid
    metadata_dir.mkdir()

    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, ?, 'planned', '', '', '', 0, 0, '')",
        (pid, "missing test", "x"),
    )

    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    profile_id = f"test-prof-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO agents (id, secret_hash, hmac_secret, status, created_at) "
        "VALUES (?, '', '', 'verified', CURRENT_TIMESTAMP)",
        (agent_id,),
    )
    # local ref to a path that doesn't exist
    await db.execute(
        "INSERT INTO agent_profiles (id, agent_id, name, description, status, "
        "capabilities, mcp_servers, storage_refs, skills) "
        "VALUES (?, ?, ?, '', 'idle', '{}', '[]', ?, '[]')",
        (
            profile_id,
            agent_id,
            "missing-profile",
            json.dumps([{"name": "ghost", "kind": "local",
                          "ref": str(projects_root.parent / "does-not-exist")}]),
        ),
    )
    task_id = f"t-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, "
        "assigned_profile_id, status) "
        "VALUES (?, ?, 'do-stuff', 'missing-profile', ?, 'completed')",
        (task_id, pid, profile_id),
    )

    r = await ac.post(f"/api/projects/{pid}/open")
    assert r.status_code == 200, r.text
    body = r.json()
    # Missing local path is skipped, falls back to metadata
    assert body["deliverable_opened"] is False
    assert body["path"] == str(metadata_dir)
    assert captured_open_path == [(str(metadata_dir),)]


@pytest.mark.asyncio
async def test_open_returns_404_for_missing_project(client, captured_open_path):
    """404 when the project metadata dir doesn't exist. The endpoint
    delegates to _project_dir which checks dir existence; this is
    the existing behavior (a project with no on-disk folder is
    treated as missing). If this changes, callers need to update.
    """
    ac, _projects_root = client
    await _login_admin(ac)
    r = await ac.post("/api/projects/proj-nonexistent-12345/open")
    assert r.status_code == 404
    # And open_path was never called (resolution failed at _project_dir)
    assert captured_open_path == []
