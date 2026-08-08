# coding: utf-8
"""Integration tests for v1.0.1 onboarding backfill (spec §3.2.1).

The backfill runs once at server startup. For every user with
`onboarding_state = '{}'` (the SQL default), it computes the real
state from existing data and overwrites the column. Users with
non-default state are SKIPPED (in-progress onboarding is never
overwritten).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from hermes_orch.auth.cookie import create_user, hash_password
from hermes_orch.core.onboarding import (
    SIGNAL_AGENT_CONNECTED,
    SIGNAL_FIRST_TASK_ATTEMPTED,
    SIGNAL_FIRST_TASK_COMPLETED,
    SIGNAL_LLM_CONFIGURED,
    SIGNAL_PASSWORD_SET,
    parse_state,
)
from hermes_orch.db import Database


@pytest_asyncio.fixture
async def fresh_db(tmp_path, monkeypatch):
    """Fresh DB with a bootstrap admin + one regular user (both at default state).

    `Database.connect()` does TWO things relevant to tests:
      1. Auto-creates the bootstrap admin (with password=NULL).
      2. Runs the onboarding backfill (which rewrites the default `{}`
         state to the computed state for any existing user).

    We need control over the state before the test runs, so we:
      1. Set up the config + db
      2. Override the admin's password (so password_set=True)
      3. Create alice (no password)
      4. Reset BOTH users' onboarding_state to `{}` (the SQL default)
         so the backfill we're testing will rewrite them, not skip them.
    """
    # Write a minimal config so the backfill's `_has_llm_configured` check
    # has a config to read. Use a no-llm config — most tests want
    # llm_configured=False unless they explicitly set it.
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "orchestrator:\n  port: 18765\n  bind_host: 127.0.0.1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))

    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()

    # Admin was auto-created with password=NULL. Set a real password
    # so the password_set backfill signal is True.
    await db.execute(
        "UPDATE users SET password_hash = ? WHERE username = 'admin'",
        (hash_password("AdminPass123!"),),
    )

    # Regular user WITHOUT a password
    await create_user(
        db,
        username="alice",
        password=None,
        role="user",
    )

    # Reset BOTH users' onboarding_state to the SQL default `{}` so
    # the backfill we're about to test will rewrite them. (Without
    # this, the in-connect() backfill has already run and rewritten
    # admin to the all-false JSON state, which the backfill then
    # SKIPS — defeating the test.)
    await db.execute("UPDATE users SET onboarding_state = '{}'")

    yield db
    await db.close()


# ===== basic backfill =====

@pytest.mark.asyncio
async def test_backfill_sets_password_set_true_when_hash_exists(fresh_db):
    """User with password_hash → password_set=True after backfill."""
    db = fresh_db
    n = await db._run_onboarding_backfill()
    assert n == 2  # both users rewritten

    admin = await db.fetchone("SELECT onboarding_state FROM users WHERE username='admin'")
    alice = await db.fetchone("SELECT onboarding_state FROM users WHERE username='alice'")
    admin_state = parse_state(admin["onboarding_state"])
    alice_state = parse_state(alice["onboarding_state"])

    # admin has a password
    assert admin_state["signals"][SIGNAL_PASSWORD_SET] is True
    # alice does NOT
    assert alice_state["signals"][SIGNAL_PASSWORD_SET] is False


@pytest.mark.asyncio
async def test_backfill_sets_llm_configured_when_config_has_llm(tmp_path, monkeypatch):
    """If config.yaml has an `llm:` section, llm_configured=True for all users."""
    # Write a config with an llm section (the default after `hermes-orch init`)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "orchestrator:\n  port: 18765\n  bind_host: 127.0.0.1\n"
        "llm:\n  mock: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))

    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()
    await create_user(db, username="bob", password="BobPass123!", role="user")
    await db._run_onboarding_backfill()

    bob = await db.fetchone("SELECT onboarding_state FROM users WHERE username='bob'")
    state = parse_state(bob["onboarding_state"])
    assert state["signals"][SIGNAL_LLM_CONFIGURED] is True
    await db.close()


@pytest.mark.asyncio
async def test_backfill_sets_first_task_completed_when_tasks_table_has_completed(fresh_db):
    """A task with status='completed' in the DB → first_task_completed=True."""
    db = fresh_db
    # Insert a project + a completed task
    import time as _time
    now = int(_time.time())
    await db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("proj-1", "Test", now, now),
    )
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'completed', ?, ?)",
        ("task-1", "proj-1", "Test task", "default", now, now),
    )
    await db._run_onboarding_backfill()

    admin = await db.fetchone("SELECT onboarding_state FROM users WHERE username='admin'")
    state = parse_state(admin["onboarding_state"])
    assert state["signals"][SIGNAL_FIRST_TASK_COMPLETED] is True
    assert state["signals"][SIGNAL_FIRST_TASK_ATTEMPTED] is True


@pytest.mark.asyncio
async def test_backfill_sets_attempted_without_completing(fresh_db):
    """A task in any status (not just completed) → attempted=True, completed=False."""
    db = fresh_db
    import time as _time
    now = int(_time.time())
    await db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("proj-1", "Test", now, now),
    )
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'failed', ?, ?)",
        ("task-1", "proj-1", "Test task", "default", now, now),
    )
    await db._run_onboarding_backfill()

    admin = await db.fetchone("SELECT onboarding_state FROM users WHERE username='admin'")
    state = parse_state(admin["onboarding_state"])
    assert state["signals"][SIGNAL_FIRST_TASK_ATTEMPTED] is True
    assert state["signals"][SIGNAL_FIRST_TASK_COMPLETED] is False


# ===== idempotency + non-overwrite =====

@pytest.mark.asyncio
async def test_backfill_is_idempotent(fresh_db):
    """Running backfill twice → second call is a no-op."""
    db = fresh_db
    n1 = await db._run_onboarding_backfill()
    assert n1 == 2
    n2 = await db._run_onboarding_backfill()
    assert n2 == 0  # nobody left with `{}`


@pytest.mark.asyncio
async def test_backfill_does_not_overwrite_in_progress_state(fresh_db):
    """Users with non-default state are SKIPPED (never overwritten).

    Spec §3.2.1 contract: in-progress onboarding is never overwritten
    by the backfill. A user who has started (e.g. set password +
    skipped) keeps their state. Only `{}` (the SQL default) is
    eligible for backfill.
    """
    from hermes_orch.core.onboarding import (
        empty_state,
        serialize_state,
        set_signal,
        set_skipped,
    )

    db = fresh_db
    # Admin: already skipped the onboarding (non-default state)
    skipped_state = set_skipped(
        set_signal(empty_state(), SIGNAL_PASSWORD_SET, True),
        True,
    )
    await db.execute(
        "UPDATE users SET onboarding_state = ? WHERE username='admin'",
        (serialize_state(skipped_state),),
    )
    original = skipped_state
    # Now run the backfill — it should leave admin alone
    await db._run_onboarding_backfill()
    admin = await db.fetchone("SELECT onboarding_state FROM users WHERE username='admin'")
    new = parse_state(admin["onboarding_state"])
    # The skipped flag must be preserved (not reset by backfill)
    assert new["skipped"] is True
    # And the password signal preserved
    assert new["signals"][SIGNAL_PASSWORD_SET] is True
    # The serialized form should match (backfill did NOT touch this row)
    assert admin["onboarding_state"] == serialize_state(original)


@pytest.mark.asyncio
async def test_backfill_long_time_user_does_not_see_fresh_checklist(fresh_db, tmp_path, monkeypatch):
    """Spec §3.2.1: existing user with all 4 signals must NOT see fresh checklist.

    Setup: admin has password + a completed task + config has llm.
    (The agent_connected signal can't be set without a Phase 3
    `last_heartbeat` column, so we test with the 3 signals we can
    control + 1 mocked via a backfill override. The point of the
    test is "completed signal + config + password = no fresh
    checklist" — the full all-4 case is covered by the pure-function
    test in test_onboarding_state.py.)
    """
    from hermes_orch.core.onboarding import is_checklist_complete, should_show_checklist

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  mock: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))

    db = fresh_db
    # Add a completed task
    import time as _time
    now = int(_time.time())
    await db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("proj-1", "Test", now, now),
    )
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'completed', ?, ?)",
        ("task-1", "proj-1", "Test task", "default", now, now),
    )

    await db._run_onboarding_backfill()

    admin = await db.fetchone("SELECT onboarding_state FROM users WHERE username='admin'")
    state = parse_state(admin["onboarding_state"])
    # 3 of 4 success signals true (password, llm, first_task_completed).
    # agent_connected is False (Phase 3 column not present).
    assert state["signals"][SIGNAL_PASSWORD_SET] is True
    assert state["signals"][SIGNAL_LLM_CONFIGURED] is True
    assert state["signals"][SIGNAL_FIRST_TASK_COMPLETED] is True
    # Still partial — checklist must show (user has 1 step left)
    assert is_checklist_complete(state) is False
    assert should_show_checklist(state) is True


# ===== defensive: doesn't crash on missing tables =====

@pytest.mark.asyncio
async def test_backfill_does_not_crash_on_missing_agent_heartbeat_column(tmp_path, monkeypatch):
    """If the agent_profiles table is missing `last_heartbeat`, backfill still works.

    The pre-Phase 3 schema may not have this column. The backfill must
    handle that gracefully (just return False for has_connected_agent).
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("orchestrator:\n  port: 18765\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))

    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()
    # Don't add a completed task or heartbeat — fresh state
    await create_user(db, username="bob", password=None, role="user")
    # Backfill should NOT crash
    n = await db._run_onboarding_backfill()
    assert n >= 1
    bob = await db.fetchone("SELECT onboarding_state FROM users WHERE username='bob'")
    state = parse_state(bob["onboarding_state"])
    # agent_connected is False (no column = no recent heartbeat)
    assert state["signals"][SIGNAL_AGENT_CONNECTED] is False
    await db.close()
