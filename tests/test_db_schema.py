"""Schema tests for v3.9.0 SOUL routing & dispatch columns.

What this covers (Phase 1, backend):
  1. agent_profiles.skills is a TEXT column with default '[]' (JSON
     list of capability tags) — used by the routing engine for
     capability-match dispatch.
  2. project_soul_presets gains three orchestration-side columns:
     default_soul (TEXT NULL), last_applied_at (TIMESTAMP NULL),
     last_applied_mtime (TEXT NULL).
  3. The MIGRATIONS list applies these columns to a pre-migration DB
     (existing DBs without these columns) — backward compat.
  4. Fresh INSERTs use the schema defaults — no caller has to set the
     new fields explicitly.

Pattern lifted from tests/test_max_concurrent_tasks.py
(test_migration_adds_column_to_existing_db) — uses Database(test_db)
+ db.connect() directly with PRAGMA table_info() to verify columns.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_orch.db import Database, SCHEMA


# ===== Helpers =====


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return column names for a table via PRAGMA (sync helper)."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


# ===== Fresh DB: CREATE TABLE includes the new columns =====


@pytest.mark.asyncio
async def test_fresh_db_has_all_new_columns(tmp_path):
    """A fresh DB built from SCHEMA must have all 4 new columns
    (no need to wait for MIGRATIONS to add them)."""
    test_db = tmp_path / "fresh.db"
    db = Database(test_db)
    await db.connect()

    # agent_profiles.skills
    agent_profiles_cols = await db.fetchall("PRAGMA table_info(agent_profiles)")
    ap_names = [r["name"] for r in agent_profiles_cols]
    assert "skills" in ap_names, (
        f"agent_profiles.skills missing from fresh DB; got {ap_names}"
    )
    # Default for skills is '[]' (JSON list of strings).
    ap_default = next(
        r["dflt_value"] for r in agent_profiles_cols if r["name"] == "skills"
    )
    assert ap_default == "'[]'", (
        f"agent_profiles.skills default should be '[]' (JSON), got {ap_default!r}"
    )

    # project_soul_presets: the 3 new columns
    psp_cols = await db.fetchall("PRAGMA table_info(project_soul_presets)")
    psp_names = [r["name"] for r in psp_cols]
    for col in ("default_soul", "last_applied_at", "last_applied_mtime"):
        assert col in psp_names, (
            f"project_soul_presets.{col} missing from fresh DB; got {psp_names}"
        )
    # All three default to NULL (we don't want to auto-populate).
    for col in ("default_soul", "last_applied_at", "last_applied_mtime"):
        dflt = next(r["dflt_value"] for r in psp_cols if r["name"] == col)
        assert dflt == "NULL", (
            f"project_soul_presets.{col} default should be NULL, got {dflt!r}"
        )

    await db.close()


# ===== Migration: pre-v3.9.0 DB gets the columns on connect =====


@pytest.mark.asyncio
async def test_migration_adds_columns_to_existing_db(tmp_path):
    """Simulate a pre-v3.9.0 DB (no skills / no 3 new soul columns),
    then run connect() which applies MIGRATIONS. The new columns
    must be present after connect() and old rows must keep working
    (DEFAULT '[]' for skills, DEFAULT NULL for the others)."""
    pre_db = tmp_path / "pre_v39.db"
    conn = sqlite3.connect(str(pre_db))
    conn.executescript(SCHEMA)
    # Insert a legacy agent + profile with the OLD shape (no skills
    # column, no default_soul / last_applied_* on the preset).
    conn.execute(
        "INSERT INTO agents (id, secret_hash, status) VALUES (?, ?, ?)",
        ("legacy-agent", "x" * 64, "verified"),
    )
    conn.execute(
        "INSERT INTO agent_profiles (id, agent_id, name) VALUES (?, ?, ?)",
        ("legacy-profile", "legacy-agent", "default"),
    )
    conn.execute(
        "INSERT INTO projects (id, name, state) VALUES (?, ?, ?)",
        ("legacy-proj", "legacy", "planning"),
    )
    conn.execute(
        "INSERT INTO project_soul_presets "
        "(id, project_id, profile_id, role_name, content) "
        "VALUES (?, ?, ?, ?, ?)",
        ("legacy-preset", "legacy-proj", "legacy-profile", "default", "old soul"),
    )
    conn.commit()
    conn.close()

    # Connect with the migration runner — this applies MIGRATIONS.
    db = Database(pre_db)
    await db.connect()

    # agent_profiles.skills column now exists
    ap_names = await db.fetchall("PRAGMA table_info(agent_profiles)")
    ap_col_names = [r["name"] for r in ap_names]
    assert "skills" in ap_col_names, (
        f"skills column missing after migration; got {ap_col_names}"
    )

    # project_soul_presets: the 3 new columns
    psp_names = await db.fetchall("PRAGMA table_info(project_soul_presets)")
    psp_col_names = [r["name"] for r in psp_names]
    for col in ("default_soul", "last_applied_at", "last_applied_mtime"):
        assert col in psp_col_names, (
            f"{col} column missing after migration; got {psp_col_names}"
        )

    # Legacy profile defaults to skills='[]' (JSON empty list).
    row = await db.fetchone(
        "SELECT skills FROM agent_profiles WHERE id = ?", ("legacy-profile",)
    )
    assert row["skills"] == "[]", (
        f"legacy profile should default to skills='[]', got {row['skills']!r}"
    )

    # Legacy preset has all 3 new columns NULL.
    psp_row = await db.fetchone(
        "SELECT default_soul, last_applied_at, last_applied_mtime "
        "FROM project_soul_presets WHERE id = ?",
        ("legacy-preset",),
    )
    assert psp_row["default_soul"] is None
    assert psp_row["last_applied_at"] is None
    assert psp_row["last_applied_mtime"] is None

    await db.close()


# ===== agent_profiles.skills: insert with custom skills, read back =====


@pytest.mark.asyncio
async def test_agent_profile_skills_insert_and_read(tmp_path):
    """Insert an agent_profile with skills=['web_search', 'python']
    and read it back. Verify the column is stored as JSON text and
    parses back to the same list (the routing engine reads it as a
    list[str])."""
    test_db = tmp_path / "skills.db"
    db = Database(test_db)
    await db.connect()

    # Need a parent agent first (FK).
    await db.insert(
        "agents",
        {"id": "a-skills", "secret_hash": "x" * 64, "status": "verified"},
    )
    skills_list = ["web_search", "python", "write_file"]
    await db.insert(
        "agent_profiles",
        {
            "id": "p-skills",
            "agent_id": "a-skills",
            "name": "researcher",
            "skills": json.dumps(skills_list),
        },
    )

    # Read back: column is JSON text, must round-trip exactly.
    row = await db.fetchone(
        "SELECT skills FROM agent_profiles WHERE id = ?", ("p-skills",)
    )
    assert row["skills"] == json.dumps(skills_list), (
        f"skills column should round-trip as JSON; got {row['skills']!r}"
    )
    parsed = json.loads(row["skills"])
    assert parsed == skills_list, (
        f"parsed skills should match input; got {parsed!r}"
    )

    # Also verify the schema default kicks in when skills is not
    # passed to insert (mimics the "old caller doesn't know about
    # skills" path).
    await db.insert(
        "agent_profiles",
        {
            "id": "p-default-skills",
            "agent_id": "a-skills",
            "name": "minimal",
        },
    )
    default_row = await db.fetchone(
        "SELECT skills FROM agent_profiles WHERE id = ?",
        ("p-default-skills",),
    )
    assert default_row["skills"] == "[]", (
        f"agent_profiles.skills default should be '[]'; "
        f"got {default_row['skills']!r}"
    )

    await db.close()


# ===== project_soul_presets: 3 new fields insert + read =====


@pytest.mark.asyncio
async def test_project_soul_preset_new_fields_insert_and_read(tmp_path):
    """Insert a project_soul_preset with all 3 new fields populated
    and read them back. Verifies the columns persist their values
    exactly (no silent JSON parsing / normalization on the server
    side)."""
    test_db = tmp_path / "preset.db"
    db = Database(test_db)
    await db.connect()

    # Parent rows for the FKs.
    await db.insert(
        "agents",
        {"id": "a-soul", "secret_hash": "x" * 64, "status": "verified"},
    )
    await db.insert(
        "agent_profiles",
        {
            "id": "p-soul",
            "agent_id": "a-soul",
            "name": "analyst",
            "skills": json.dumps(["python", "pandas"]),
        },
    )
    await db.insert(
        "projects",
        {"id": "proj-soul", "name": "soul test", "state": "ready"},
    )

    # The 3 new fields: workflow-supplied default + server-side
    # apply timestamp + host-side mtime string. The values are
    # arbitrary strings (the server doesn't normalize them) — what
    # matters is round-trip integrity.
    default_soul = "You are a careful CPI analyst. Verify data sources."
    last_applied_at = "2026-08-01T12:34:56+08:00"
    last_applied_mtime = "1722510896.789"  # wrapper-reported float-as-string
    await db.insert(
        "project_soul_presets",
        {
            "id": "preset-1",
            "project_id": "proj-soul",
            "profile_id": "p-soul",
            "role_name": "analyst",
            "content": "user-edited SOUL",
            "default_soul": default_soul,
            "last_applied_at": last_applied_at,
            "last_applied_mtime": last_applied_mtime,
        },
    )

    row = await db.fetchone(
        "SELECT default_soul, last_applied_at, last_applied_mtime "
        "FROM project_soul_presets WHERE id = ?",
        ("preset-1",),
    )
    assert row["default_soul"] == default_soul
    assert row["last_applied_at"] == last_applied_at
    assert row["last_applied_mtime"] == last_applied_mtime

    # And: a preset inserted WITHOUT the new fields should default
    # all three to NULL (backward compat — pre-v3.9.0 callers).
    # Needs a different (project_id, profile_id) pair because the
    # table has UNIQUE(project_id, profile_id) — see the table
    # comment for "one preset per (project, profile)".
    await db.insert(
        "agent_profiles",
        {
            "id": "p-soul-2",
            "agent_id": "a-soul",
            "name": "minimal",
            "skills": "[]",
        },
    )
    await db.insert(
        "project_soul_presets",
        {
            "id": "preset-2",
            "project_id": "proj-soul",
            "profile_id": "p-soul-2",
            "role_name": "minimal",
            "content": "just content",
        },
    )
    row2 = await db.fetchone(
        "SELECT default_soul, last_applied_at, last_applied_mtime "
        "FROM project_soul_presets WHERE id = ?",
        ("preset-2",),
    )
    assert row2["default_soul"] is None
    assert row2["last_applied_at"] is None
    assert row2["last_applied_mtime"] is None

    await db.close()


# ===== End-to-end: all 4 new columns survive a round trip =====


@pytest.mark.asyncio
async def test_all_new_columns_round_trip_together(tmp_path):
    """One profile + one preset, with all 4 new columns populated.
    Verifies the columns coexist (no constraint conflict) and that
    the ORCHESTRATOR-side flow (routing sees skills, dispatch
    updates last_applied_*) is structurally supported by the
    schema."""
    test_db = tmp_path / "round_trip.db"
    db = Database(test_db)
    await db.connect()

    await db.insert(
        "agents", {"id": "a-rt", "secret_hash": "x" * 64, "status": "verified"},
    )
    skills = ["web_search", "tradingview", "write_file"]
    await db.insert(
        "agent_profiles",
        {
            "id": "p-rt",
            "agent_id": "a-rt",
            "name": "trader",
            "skills": json.dumps(skills),
            "capabilities": json.dumps({"tradingview": True}),
        },
    )
    await db.insert(
        "projects", {"id": "proj-rt", "name": "rt", "state": "ready"},
    )
    await db.insert(
        "project_soul_presets",
        {
            "id": "preset-rt",
            "project_id": "proj-rt",
            "profile_id": "p-rt",
            "role_name": "trader",
            "content": "user content",
            "default_soul": "workflow default",
            "last_applied_at": "2026-08-01T10:00:00+08:00",
            "last_applied_mtime": "1722500000.0",
        },
    )

    # Single SELECT * that the API path uses (see
    # list_soul_presets + upsert_soul_preset in api/projects.py).
    join_row = await db.fetchone(
        "SELECT sp.id, sp.role_name, sp.content, sp.default_soul, "
        "       sp.last_applied_at, sp.last_applied_mtime, "
        "       ap.skills "
        "FROM project_soul_presets sp "
        "JOIN agent_profiles ap ON ap.id = sp.profile_id "
        "WHERE sp.id = ?",
        ("preset-rt",),
    )
    # Skills round-trips as JSON list.
    assert json.loads(join_row["skills"]) == skills
    # 3 new preset fields round-trip exactly.
    assert join_row["default_soul"] == "workflow default"
    assert join_row["last_applied_at"] == "2026-08-01T10:00:00+08:00"
    assert join_row["last_applied_mtime"] == "1722500000.0"
    # Sanity: the unchanged content field is still there.
    assert join_row["content"] == "user content"
    assert join_row["role_name"] == "trader"

    await db.close()
