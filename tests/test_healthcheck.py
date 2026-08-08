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
