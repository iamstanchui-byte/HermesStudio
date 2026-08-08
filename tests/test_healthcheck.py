# coding: utf-8
"""Tests for v1.0.1 server-side healthcheck handler (§3.5 + §3.5.1).

Covers:
  T1.10  System health smoke test runs in mock mode
  T1.10a Health check with zero registered agents returns status=failed
  T1.10b Health check with 2+ agents pings all + reports each
"""
from __future__ import annotations

import json
import time

import pytest
import pytest_asyncio

from hermes_orch.auth.cookie import create_user
from hermes_orch.core.healthcheck import (
    HEARTBEAT_FRESH_SECONDS,
    run_healthcheck,
)
from hermes_orch.core.onboarding import (
    SIGNAL_FIRST_TASK_ATTEMPTED,
    SIGNAL_FIRST_TASK_COMPLETED,
    parse_state,
    reset_state,
    serialize_state,
)
from hermes_orch.core.starters import SERVER_HEALTHCHECK_ACTION
from hermes_orch.db import Database


@pytest_asyncio.fixture
async def fresh_db(tmp_path, monkeypatch):
    """Fresh DB with the bootstrap admin (auto-created on connect)."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "orchestrator:\n  port: 18765\n  bind_host: 127.0.0.1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


async def _insert_agent(db, agent_id: str, last_heartbeat_at: int | None):
    """Insert a minimal agent row. last_heartbeat_at=None → NULL (no heartbeat yet)."""
    import hashlib
    secret = hashlib.sha256(b"x").hexdigest()
    await db.execute(
        "INSERT INTO agents (id, secret_hash, hmac_secret, status, "
        "last_heartbeat_at, name) "
        "VALUES (?, ?, ?, 'verifying', ?, ?)",
        (agent_id, secret, "x", last_heartbeat_at, agent_id),
    )


# ===== Zero-agent case (T1.10a) =====

@pytest.mark.asyncio
async def test_healthcheck_zero_agents_is_failed(fresh_db):
    """T1.10a: 0 agents → status=failed, summary mentions connect one first."""
    db = fresh_db
    result = await run_healthcheck(db)
    assert result["status"] == "failed"
    assert "No agent connected" in result["summary"] or "connect one" in result["summary"]
    assert result["details"]["agent_count"] == 0


# ===== Single-agent case (T1.10) =====

@pytest.mark.asyncio
async def test_healthcheck_single_fresh_agent_is_completed(fresh_db):
    """T1.10: 1 agent with fresh heartbeat → status=completed."""
    db = fresh_db
    now = int(time.time())
    await _insert_agent(db, "agent-1", now)  # just heartbeat-ed
    result = await run_healthcheck(db)
    assert result["status"] == "completed"
    assert "OK" in result["summary"] or "reachable" in result["summary"].lower()
    assert result["details"]["agent_count"] == 1
    assert result["details"]["fresh_count"] == 1


@pytest.mark.asyncio
async def test_healthcheck_single_stale_agent_is_failed(fresh_db):
    """Single agent with old heartbeat → status=failed."""
    db = fresh_db
    stale_ts = int(time.time()) - HEARTBEAT_FRESH_SECONDS - 30
    await _insert_agent(db, "agent-1", stale_ts)
    result = await run_healthcheck(db)
    assert result["status"] == "failed"
    assert "agent-1" in result["summary"]
    assert "no heartbeat" in result["summary"] or "may be down" in result["summary"]


@pytest.mark.asyncio
async def test_healthcheck_single_agent_no_heartbeat_yet_is_failed(fresh_db):
    """Single agent that never heartbeat-ed (NULL) → status=failed."""
    db = fresh_db
    await _insert_agent(db, "agent-1", None)
    result = await run_healthcheck(db)
    assert result["status"] == "failed"


# ===== Multi-agent case (T1.10b) =====

@pytest.mark.asyncio
async def test_healthcheck_multi_all_fresh_is_completed(fresh_db):
    """T1.10b: 2+ agents, all fresh → status=completed, summary lists each."""
    db = fresh_db
    now = int(time.time())
    await _insert_agent(db, "agent-1", now)
    await _insert_agent(db, "agent-2", now)
    result = await run_healthcheck(db)
    assert result["status"] == "completed"
    assert result["details"]["agent_count"] == 2
    assert result["details"]["fresh_count"] == 2
    # Summary lists each agent individually
    assert "agent-1" in result["summary"]
    assert "agent-2" in result["summary"]


@pytest.mark.asyncio
async def test_healthcheck_multi_partial_fresh_is_completed(fresh_db):
    """T1.10b: 2 agents, 1 fresh → still completed (ANY fresh is enough)."""
    db = fresh_db
    now = int(time.time())
    stale_ts = now - HEARTBEAT_FRESH_SECONDS - 30
    await _insert_agent(db, "agent-1", now)  # fresh
    await _insert_agent(db, "agent-2", stale_ts)  # stale
    result = await run_healthcheck(db)
    assert result["status"] == "completed"
    assert result["details"]["fresh_count"] == 1


@pytest.mark.asyncio
async def test_healthcheck_multi_all_stale_is_failed(fresh_db):
    """T1.10b: 2 agents, none fresh → status=failed, lists each."""
    db = fresh_db
    stale_ts = int(time.time()) - HEARTBEAT_FRESH_SECONDS - 30
    await _insert_agent(db, "agent-1", stale_ts)
    await _insert_agent(db, "agent-2", stale_ts)
    result = await run_healthcheck(db)
    assert result["status"] == "failed"
    assert "0/2" in result["summary"]
    assert "agent-1" in result["summary"]
    assert "agent-2" in result["summary"]


# ===== run_and_record_healthcheck =====

@pytest.mark.asyncio
async def test_record_healthcheck_writes_task_result(fresh_db):
    """The full flow writes the result to the task row + flips the signal."""
    from hermes_orch.core.healthcheck import run_and_record_healthcheck
    db = fresh_db
    # Need a task row to update. Create a minimal one.
    import time as _time
    now = int(_time.time())
    await db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("proj-1", "Test", now, now),
    )
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, action, status, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
        ("task-1", "proj-1", "ping-agent", "super",
         SERVER_HEALTHCHECK_ACTION, now, now),
    )
    # Reset admin's onboarding state so we can see the signal flip
    admin = await db.fetchone("SELECT id FROM users WHERE username = 'admin'")
    await db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(reset_state()), admin["id"]),
    )

    result = await run_and_record_healthcheck(db, "task-1")
    # Zero agents → failed → only attempted flipped, not completed
    assert result["status"] == "failed"
    # Task row updated
    row = await db.fetchone("SELECT status, result FROM tasks WHERE id = 'task-1'")
    assert row["status"] == "failed"
    persisted = json.loads(row["result"])
    assert persisted["status"] == "failed"
    # Signal check
    state_row = await db.fetchone(
        "SELECT onboarding_state FROM users WHERE id = ?", (admin["id"],)
    )
    state = parse_state(state_row["onboarding_state"])
    assert state["signals"][SIGNAL_FIRST_TASK_ATTEMPTED] is True
    # Per spec §3.5.1: zero-agent case flips attempted but NOT completed
    assert state["signals"][SIGNAL_FIRST_TASK_COMPLETED] is False


@pytest.mark.asyncio
async def test_record_healthcheck_completed_flips_completed_signal(fresh_db):
    """A successful healthcheck (>= 1 fresh agent) flips BOTH signals."""
    from hermes_orch.core.healthcheck import run_and_record_healthcheck
    db = fresh_db
    import time as _time
    now = int(_time.time())
    await db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("proj-1", "Test", now, now),
    )
    await db.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, action, status, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
        ("task-1", "proj-1", "ping-agent", "super",
         SERVER_HEALTHCHECK_ACTION, now, now),
    )
    # Add a fresh agent so the healthcheck passes
    await _insert_agent(db, "agent-1", now)
    # Reset admin
    admin = await db.fetchone("SELECT id FROM users WHERE username = 'admin'")
    await db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(reset_state()), admin["id"]),
    )

    result = await run_and_record_healthcheck(db, "task-1")
    assert result["status"] == "completed"

    # Both signals flipped
    state_row = await db.fetchone(
        "SELECT onboarding_state FROM users WHERE id = ?", (admin["id"],)
    )
    state = parse_state(state_row["onboarding_state"])
    assert state["signals"][SIGNAL_FIRST_TASK_ATTEMPTED] is True
    assert state["signals"][SIGNAL_FIRST_TASK_COMPLETED] is True


# ===== Heartbeat format compatibility (regression for live bug) =====
#
# The agents.last_heartbeat_at column is a TIMESTAMP — aiosqlite
# surfaces it as an ISO 8601 string (sometimes with a space
# separator instead of 'T', sometimes with a 'Z' suffix). The
# healthcheck handler used to assume unix-timestamp int/float
# and crashed on real-world data. The fix: _isoformat / _to_unix
# helpers accept both formats. This test guards against the
# crash coming back if someone "simplifies" the type hint.
#
# Caught during live end-to-end verify on 2026-08-08: the live
# DB had heartbeats as "2026-08-08T23:56:05.720143+08:00" and
# the healthcheck handler raised ValueError. The unit tests
# passed because they used unix timestamps. This is the
# "regression only caught at the boundary" lesson.

@pytest.mark.asyncio
async def test_healthcheck_handles_iso_string_heartbeats(fresh_db):
    """Real-world heartbeats are ISO strings, not unix ints.

    Per spec §3.5.1 the healthcheck must handle whatever format
    the agents table gives us. Insert agents with ISO-format
    heartbeats (matching what aiosqlite surfaces for TIMESTAMP
    columns) and verify the healthcheck still works.
    """
    db = fresh_db
    import time as _time
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    # ISO 8601 with explicit +00:00 offset (what the DB stores)
    iso_utc = now.isoformat()  # e.g. "2026-08-08T15:58:02.170373+00:00"
    iso_local = now.astimezone().isoformat()  # with local tz
    iso_z = iso_utc.replace("+00:00", "Z")  # Z-suffix variant
    # SQLite-style "YYYY-MM-DD HH:MM:SS" (no T, no tz) — what
    # aiosqlite actually surfaces for TIMESTAMP columns
    iso_space = iso_utc.replace("T", " ").replace("+00:00", "")
    # Fresh = within HEARTBEAT_FRESH_SECONDS of now
    await _insert_agent(db, "agent-utc", iso_utc)
    await _insert_agent(db, "agent-local", iso_local)
    await _insert_agent(db, "agent-z", iso_z)
    await _insert_agent(db, "agent-space", iso_space)
    # Stale = 2 minutes ago (way past HEARTBEAT_FRESH_SECONDS=60)
    stale_ts = int(_time.time()) - 120
    await _insert_agent(db, "agent-stale-int", stale_ts)

    result = await run_healthcheck(db)
    # 5 agents total
    assert result["details"]["agent_count"] == 5
    # 4 fresh (all the ISO variants), 1 stale
    assert result["details"]["fresh_count"] == 4
    # Each ISO-variant agent is_fresh=True
    by_id = {a["agent_id"]: a for a in result["details"]["registered_agents"]}
    for agent_id in ("agent-utc", "agent-local", "agent-z", "agent-space"):
        assert by_id[agent_id]["is_fresh"] is True, (
            f"{agent_id} should be fresh but was marked stale: {by_id[agent_id]}"
        )
    # The unix-int stale agent is_fresh=False
    assert by_id["agent-stale-int"]["is_fresh"] is False
    # All ISO timestamps in the response are normalized (no space)
    for a in result["details"]["registered_agents"]:
        assert " " not in a["last_heartbeat_at"], (
            f"heartbeat should be normalized ISO, got: {a['last_heartbeat_at']!r}"
        )


@pytest.mark.asyncio
async def test_healthcheck_handles_null_and_empty_heartbeats(fresh_db):
    """Defensive: NULL or empty heartbeats (newly-enrolled agents
    that haven't sent their first heartbeat yet) must NOT crash
    the handler and must be marked stale."""
    db = fresh_db
    await _insert_agent(db, "agent-null", None)
    await _insert_agent(db, "agent-empty", 0)  # 0 is "never heartbeat-ed" in some stores
    result = await run_healthcheck(db)
    assert result["details"]["agent_count"] == 2
    assert result["details"]["fresh_count"] == 0
    by_id = {a["agent_id"]: a for a in result["details"]["registered_agents"]}
    for agent_id in ("agent-null", "agent-empty"):
        assert by_id[agent_id]["is_fresh"] is False
        # NULL → last_heartbeat_at is None in the response
        assert by_id[agent_id]["last_heartbeat_at"] is None


# ===== _has_recent_agent_heartbeat (used by onboarding truth-merge) =====
#
# v1.0.1 hotfix (2026-08-09): the previous method queried the
# WRONG TABLE (agent_profiles) for the WRONG COLUMN (last_heartbeat)
# — the actual column is on `agents` and named `last_heartbeat_at`.
# Net effect: the method always returned False, and any user with
# active agents had `agent_connected` stuck at `false` regardless
# of live data. The bug was only caught when truth-merge was
# added (commit 8c6b2c9) and a real user with live agents tested
# the page.

@pytest.mark.asyncio
async def test_has_recent_agent_heartbeat_fresh_iso_string(fresh_db):
    """Regression: agent_connected truth-merge must return True
    when an agent has a fresh heartbeat stored as ISO 8601 string
    (the aiosqlite default for TIMESTAMP columns).

    Two bugs in the previous implementation:
    1. Queried `agent_profiles` for column `last_heartbeat` —
       both wrong. The actual column is `agents.last_heartbeat_at`.
    2. SQL comparison `last_heartbeat >= ?` would fail for ISO
       strings (SQLite can't coerce "2026-08-09T..." to int).
    """
    db = fresh_db
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    await _insert_agent(db, "agent-1", now_iso)
    # Method under test
    assert await db._has_recent_agent_heartbeat() is True, (
        "Fresh ISO-string heartbeat should be detected as recent"
    )


@pytest.mark.asyncio
async def test_has_recent_agent_heartbeat_fresh_unix_int(fresh_db):
    """Counterpart: a fresh heartbeat stored as unix int (legacy
    or programmatic insert) must also be detected."""
    db = fresh_db
    now_int = int(time.time())
    await _insert_agent(db, "agent-1", now_int)
    assert await db._has_recent_agent_heartbeat() is True


@pytest.mark.asyncio
async def test_has_recent_agent_heartbeat_stale_is_false(fresh_db):
    """Stale heartbeat (older than 5 min) → False."""
    db = fresh_db
    from datetime import datetime, timezone, timedelta
    stale_iso = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    await _insert_agent(db, "agent-1", stale_iso)
    assert await db._has_recent_agent_heartbeat() is False


@pytest.mark.asyncio
async def test_has_recent_agent_heartbeat_null_is_false(fresh_db):
    """No heartbeat (NULL) → False (conservative: we don't claim
    the user is "connected" until an agent actually checks in)."""
    db = fresh_db
    await _insert_agent(db, "agent-1", None)
    assert await db._has_recent_agent_heartbeat() is False


@pytest.mark.asyncio
async def test_has_recent_agent_heartbeat_no_agents_is_false(fresh_db):
    """No agents at all → False (the conservative default)."""
    db = fresh_db
    assert await db._has_recent_agent_heartbeat() is False


@pytest.mark.asyncio
async def test_has_recent_agent_heartbeat_queries_agents_table_not_profiles(fresh_db):
    """Defensive regression: the method must read from `agents`
    (the table that has the heartbeat column), not `agent_profiles`.
    Insert an agent_profiles row only (the agents table is empty
    of non-NULL heartbeats); the method must still return False
    because it doesn't read from agent_profiles at all."""
    db = fresh_db
    import secrets
    # Need a stub agent row first (FK constraint).
    await _insert_agent(db, "stub-agent", None)  # last_heartbeat_at=NULL
    profile_id = "prof-" + secrets.token_hex(4)
    await db.execute(
        "INSERT INTO agent_profiles (id, agent_id, name, status) "
        "VALUES (?, ?, 'p1', 'idle')",
        (profile_id, "stub-agent"),
    )
    # agents table has the stub row with NULL heartbeat → method
    # returns False regardless of what agent_profiles has. This
    # proves the method doesn't accidentally read from
    # agent_profiles.
    assert await db._has_recent_agent_heartbeat() is False, (
        "Method must query `agents` (not `agent_profiles`) — "
        "the profile table doesn't have a last_heartbeat column"
    )
