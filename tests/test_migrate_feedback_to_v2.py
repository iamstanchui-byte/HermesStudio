"""Tests for scripts/migrate_feedback_to_v2.py.

The migration inverts the feedback_to data:
  OLD: T.feedback_to = [A, B] meant "if A or B fails, re-run T"
  NEW: A.feedback_to includes T, B.feedback_to includes T
       → "if A fails, re-run T" + "if B fails, re-run T" (same outcome)

We test:
  1. Inversion of a single project (T → [A] becomes A += [T])
  2. Multi-step rewires (multiple targets)
  3. Self-references are dropped (don't infinite-loop)
  4. Dangling references (target id doesn't exist) are skipped
  5. Idempotency: re-running the migration is a no-op
  6. Marker column is added on first run, present afterwards
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest


# Load the migration script as a module
SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "migrate_feedback_to_v2.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migrate_feedback_to_v2", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fresh_db() -> sqlite3.Connection:
    """Build a minimal in-memory schema matching the real DB shape
    for the columns we touch."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            _feedback_to_v2_migrated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            name TEXT,
            feedback_to TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            actor TEXT,
            project_id TEXT,
            task_id TEXT,
            agent_id TEXT,
            payload TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return conn


def _insert_tasks(conn, project_id, tasks):
    for t in tasks:
        conn.execute(
            "INSERT INTO tasks (id, project_id, name, feedback_to) "
            "VALUES (?, ?, ?, ?)",
            (t["id"], project_id, t["name"], json.dumps(t.get("feedback_to", []))),
        )


def test_migration_inverts_single_project():
    """OLD: t1.feedback_to = [t2_id] → "if t2 fails, re-run t1"
    NEW: t2.feedback_to += [t1_id] → "if t2 fails, re-run t1" (same outcome)
    """
    mod = _load_migration()
    conn = _fresh_db()
    conn.execute("INSERT INTO projects (id) VALUES ('p1')")
    _insert_tasks(conn, "p1", [
        {"id": "t1", "name": "fetch", "feedback_to": ["t2"]},
        {"id": "t2", "name": "save", "feedback_to": []},
    ])
    summary = mod.migrate_one_project(conn, "p1")
    assert summary["rewires"] == 1
    # v2.0 algorithm: 2 tasks are written — t1 is cleared, t2 gets
    # t1 added. The OLD listener (t1) and the NEW target (t2) are
    # both updated. (The v1.0 algorithm had a bug where if a task
    # was both listener and target, the clear step wiped the new
    # entry. v2.0 computes the final value in one pass per task.)
    assert summary["tasks_modified"] == 2
    rows = {
        r[0]: json.loads(r[1] or "[]")
        for r in conn.execute(
            "SELECT id, feedback_to FROM tasks ORDER BY id"
        ).fetchall()
    }
    # t1 (the OLD listener) is now empty
    assert rows["t1"] == []
    # t2 (the OLD trigger, now the failing step) lists t1
    assert rows["t2"] == ["t1"]


def test_migration_handles_multi_target_rewire():
    """OLD: t1.feedback_to = [t2, t3] → "if t2 or t3 fails, re-run t1"
    NEW: t2.feedback_to += [t1], t3.feedback_to += [t1]
    """
    mod = _load_migration()
    conn = _fresh_db()
    conn.execute("INSERT INTO projects (id) VALUES ('p1')")
    _insert_tasks(conn, "p1", [
        {"id": "t1", "name": "recovery", "feedback_to": ["t2", "t3"]},
        {"id": "t2", "name": "fail-a", "feedback_to": []},
        {"id": "t3", "name": "fail-b", "feedback_to": []},
    ])
    summary = mod.migrate_one_project(conn, "p1")
    assert summary["rewires"] == 2
    # t1 is cleared, t2 and t3 each get one entry
    assert summary["tasks_modified"] == 3
    rows = {
        r[0]: json.loads(r[1] or "[]")
        for r in conn.execute(
            "SELECT id, feedback_to FROM tasks ORDER BY id"
        ).fetchall()
    }
    assert rows["t1"] == []
    assert sorted(rows["t2"]) == ["t1"]
    assert sorted(rows["t3"]) == ["t1"]


def test_migration_handles_task_that_is_both_listener_and_target():
    """v2.0 fix: a task that's BOTH a listener (has its own OLD
    feedback_to) AND a target (named in someone else's OLD
    feedback_to) must have its NEW feedback_to correctly set to
    the set of its NEW listeners — NOT wiped by the clear step.

    v1.0 of the migration had a bug where:
      T1.feedback_to = [T2]  (T1 listens for T2)
      T2.feedback_to = [T3]  (T2 listens for T3)
    ...would produce 0 entries in NEW because the clears would
    wipe the new additions on T2.

    v2.0 algorithm: compute the final value per task in one pass.
    T2 is both a listener (was_listener) and a target (new_fb[T2] = [T1]).
    T2's NEW feedback_to = [T1] (NOT wiped).
    """
    mod = _load_migration()
    conn = _fresh_db()
    conn.execute("INSERT INTO projects (id) VALUES ('p1')")
    _insert_tasks(conn, "p1", [
        {"id": "T1", "name": "first",  "feedback_to": ["T2"]},
        {"id": "T2", "name": "middle", "feedback_to": ["T3"]},
        {"id": "T3", "name": "last",   "feedback_to": []},
    ])
    summary = mod.migrate_one_project(conn, "p1")
    # T1 is cleared (its OLD target T2 is now the failing step
    # for T1's recovery). T2 is the OLD listener of T3, but
    # ALSO the NEW target of T1. T2's NEW value should be [T1]
    # (its new listener from T1), not []. T3 gets T2 added.
    assert summary["rewires"] == 2  # T2=[T1], T3=[T2]
    rows = {
        r[0]: json.loads(r[1] or "[]")
        for r in conn.execute(
            "SELECT id, feedback_to FROM tasks ORDER BY id"
        ).fetchall()
    }
    assert rows["T1"] == []  # cleared (was a listener only)
    assert rows["T2"] == ["T1"]  # NEW: if T2 fails, re-run T1
    assert rows["T3"] == ["T2"]  # NEW: if T3 fails, re-run T2


def test_migration_drops_self_references():
    """A task that references itself in feedback_to is a no-op
    in both OLD and NEW semantic. The migration skips it
    entirely (no real rewires, no clear needed because the
    self-ref remains a self-ref in the new format too)."""
    mod = _load_migration()
    conn = _fresh_db()
    conn.execute("INSERT INTO projects (id) VALUES ('p1')")
    _insert_tasks(conn, "p1", [
        {"id": "t1", "name": "self-fb", "feedback_to": ["t1"]},
    ])
    summary = mod.migrate_one_project(conn, "p1")
    # Self-ref is dropped before the rewire, so rewires=0
    assert summary["rewires"] == 0
    assert summary["tasks_modified"] == 0
    # The self-ref data is left alone (it would still be a
    # self-ref in the NEW format: t1's feedback_to = [t1]
    # would mean "if I fail, re-run me", which is still a no-op).
    # The migration only inverts relationships, not normalizes
    # meaningless ones.
    row = conn.execute(
        "SELECT feedback_to FROM tasks WHERE id = 't1'"
    ).fetchone()
    assert json.loads(row[0] or "[]") == ["t1"]


def test_migration_skips_dangling_target_ids():
    """If a task references a target that doesn't exist (orphaned
    by cleanup), the migration skips it without raising."""
    mod = _load_migration()
    conn = _fresh_db()
    conn.execute("INSERT INTO projects (id) VALUES ('p1')")
    _insert_tasks(conn, "p1", [
        {"id": "t1", "name": "fetch", "feedback_to": ["t-ghost"]},
    ])
    summary = mod.migrate_one_project(conn, "p1")
    # The dangling target t-ghost doesn't exist; we silently skip
    # adding to it. The clear still happens because t1's feedback_to
    # is non-empty (regardless of whether the target exists).
    assert summary["tasks_modified"] == 1
    row = conn.execute(
        "SELECT feedback_to FROM tasks WHERE id = 't1'"
    ).fetchone()
    assert json.loads(row[0] or "[]") == []


def test_migration_is_idempotent():
    """Running the migration twice is a no-op the second time.
    The marker column is the idempotency key.
    """
    mod = _load_migration()
    conn = _fresh_db()
    conn.execute("INSERT INTO projects (id) VALUES ('p1')")
    _insert_tasks(conn, "p1", [
        {"id": "t1", "name": "fetch", "feedback_to": ["t2"]},
        {"id": "t2", "name": "save", "feedback_to": []},
    ])
    s1 = mod.migrate_one_project(conn, "p1")
    assert s1["rewires"] == 1
    # Snapshot the state after the first migration
    rows1 = {
        r[0]: json.loads(r[1] or "[]")
        for r in conn.execute(
            "SELECT id, feedback_to FROM tasks ORDER BY id"
        ).fetchall()
    }
    # Run again — should be skipped
    s2 = mod.migrate_one_project(conn, "p1")
    assert s2["skipped_already_migrated"] is True
    assert s2["rewires"] == 0
    # Data should be unchanged
    rows2 = {
        r[0]: json.loads(r[1] or "[]")
        for r in conn.execute(
            "SELECT id, feedback_to FROM tasks ORDER BY id"
        ).fetchall()
    }
    assert rows1 == rows2


def test_migration_dry_run_does_not_write():
    """With dry_run=True, the script returns the right summary
    but the DB is unchanged."""
    mod = _load_migration()
    conn = _fresh_db()
    conn.execute("INSERT INTO projects (id) VALUES ('p1')")
    _insert_tasks(conn, "p1", [
        {"id": "t1", "name": "fetch", "feedback_to": ["t2"]},
        {"id": "t2", "name": "save", "feedback_to": []},
    ])
    s = mod.migrate_one_project(conn, "p1", dry_run=True)
    assert s["rewires"] == 1
    # Marker should NOT be set in dry-run
    row = conn.execute(
        "SELECT _feedback_to_v2_migrated_at FROM projects WHERE id = 'p1'"
    ).fetchone()
    assert row[0] is None
    # Data should NOT be changed
    rows = {
        r[0]: json.loads(r[1] or "[]")
        for r in conn.execute(
            "SELECT id, feedback_to FROM tasks ORDER BY id"
        ).fetchall()
    }
    assert rows["t1"] == ["t2"]  # still OLD format
    assert rows["t2"] == []


def test_migration_marker_column_added_on_first_run():
    """The first migration adds the _feedback_to_v2_migrated_at
    column and stamps it on the migrated project."""
    mod = _load_migration()
    # Build a fresh DB without the marker column (simulating a
    # pre-v2.0 schema). We do this by NOT calling _fresh_db
    # (which already includes the column) and instead building
    # the schema manually with the marker column ABSENT.
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE projects (
            id TEXT PRIMARY KEY
        )
    """)
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            name TEXT,
            feedback_to TEXT
        )
    """)
    # Confirm the column does NOT exist yet
    cols_before = [
        r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()
    ]
    assert "_feedback_to_v2_migrated_at" not in cols_before
    # _ensure_marker_column should add it
    mod._ensure_marker_column(conn)
    cols_after = [
        r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()
    ]
    assert "_feedback_to_v2_migrated_at" in cols_after
    # Running again is a no-op (idempotent)
    mod._ensure_marker_column(conn)
    cols_still = [
        r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()
    ]
    assert "_feedback_to_v2_migrated_at" in cols_still


def test_migration_audit_event_written():
    """Each migrated project gets a feedback_to.v2_migrated audit row."""
    mod = _load_migration()
    conn = _fresh_db()
    conn.execute("INSERT INTO projects (id) VALUES ('p1')")
    _insert_tasks(conn, "p1", [
        {"id": "t1", "name": "fetch", "feedback_to": ["t2"]},
        {"id": "t2", "name": "save", "feedback_to": []},
    ])
    mod.migrate_one_project(conn, "p1")
    rows = conn.execute(
        "SELECT event_type, payload FROM audit_log WHERE project_id = 'p1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "feedback_to.v2_migrated"
    payload = json.loads(rows[0][1])
    assert payload["project_id"] == "p1"
    assert payload["rewires"] == 1
