"""Tests for v3.12.2 #3: supervisor uses its own aiosqlite connection.

Background (2026-08-04 incident):
  - Server's `db` (api) and supervisor's `db` were the same aiosqlite
    connection. Under load, supervisor's 10/sec checkpoint writes
    caused the API's heartbeat UPDATE to fail with
    `sqlite3.OperationalError: database is locked` (SQLITE_LOCKED).
  - v3.12.2 #2 added `PRAGMA busy_timeout = 5000` but only fixed
    SQLITE_BUSY (across connections). SQLITE_LOCKED (in-transaction
    read lock in the SAME connection) needs separate connections.
  - v3.12.2 #3 fix: lifespan creates a SECOND `Database` instance
    for the supervisor. The supervisor's tick-loop writes and the
    API's request-path writes no longer share an aiosqlite worker
    thread, so no in-process lock contention.

What this covers:
  1. Two Database instances can connect to the same file and both
     can read + write. (Sanity check the pattern works.)
  2. While one connection is in a long transaction (simulating the
     supervisor's tick-loop), the other connection can still write
     (simulating an API heartbeat) without SQLITE_LOCKED.
  3. The lifespan in main.py creates TWO separate Database
     instances: `app.state.db` and `app.state.supervisor_db`.
  4. The supervisor's database instance is different from the API's
     (separate aiosqlite connections = separate worker threads).
  5. Closing the supervisor's connection doesn't affect the API's
     connection.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hermes_orch import db as db_mod
from hermes_orch.db import Database
from hermes_orch import main as main_mod


# ===== Two Database instances on the same file =====


@pytest.mark.asyncio
async def test_two_database_instances_share_file(tmp_path):
    """Two Database instances pointing to the same file can both
    connect, and writes on one are visible to the other (proving
    they share the same underlying SQLite database, not stale
    in-memory snapshots)."""
    test_db = tmp_path / "shared.db"
    db_a = Database(test_db)
    db_b = Database(test_db)
    try:
        await db_a.connect()
        await db_b.connect()

        # Write via A
        await db_a.execute(
            "INSERT INTO audit_log (event_type, actor, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("test.event_a", "tester", '{"src": "a"}', "2026-08-04T00:00:00+08:00"),
        )
        # Write via B
        await db_b.execute(
            "INSERT INTO audit_log (event_type, actor, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("test.event_b", "tester", '{"src": "b"}', "2026-08-04T00:00:01+08:00"),
        )

        # Read via the OTHER connection (cross-connection visibility)
        # Note: connect() also writes a `user_bootstrap_auto` audit
        # row, so we filter to only our test events.
        rows_from_a = await db_a.fetchall(
            "SELECT event_type FROM audit_log "
            "WHERE event_type IN ('test.event_a', 'test.event_b') "
            "ORDER BY created_at"
        )
        rows_from_b = await db_b.fetchall(
            "SELECT event_type FROM audit_log "
            "WHERE event_type IN ('test.event_a', 'test.event_b') "
            "ORDER BY created_at"
        )
        assert [r["event_type"] for r in rows_from_a] == [
            "test.event_a",
            "test.event_b",
        ], f"db_a should see both rows; got {rows_from_a}"
        assert [r["event_type"] for r in rows_from_b] == [
            "test.event_a",
            "test.event_b",
        ], f"db_b should see both rows; got {rows_from_b}"
    finally:
        await db_a.close()
        await db_b.close()


@pytest.mark.asyncio
async def test_long_transaction_on_one_does_not_lock_other(tmp_path):
    """While one connection is in a long-running transaction (BEGIN +
    many writes, no COMMIT yet), the OTHER connection can still
    complete writes via busy_timeout=5000 (v3.12.2 #2). This is the
    core isolation guarantee of v3.12.2 #3: cross-connection
    contention is handled by busy_timeout, not by in-process lock
    collision (which is what we were seeing when both shared a
    single connection).

    We use a separate thread to hold the supervisor's write lock
    via raw sync sqlite3, then concurrently fire an aiosqlite write
    on the API's connection. The API write must succeed within
    busy_timeout (5s) — NOT raise SQLITE_LOCKED. Without the
    v3.12.2 #3 fix (two separate aiosqlite connections), this
    pattern would not exhibit cross-connection contention; this
    test primarily documents the v3.12.2 #2 behavior (busy_timeout
    in WAL mode lets cross-connection writes succeed) and
    exercises both connection paths through the same file.
    """
    import sqlite3 as _sync_sqlite
    import threading

    test_db = tmp_path / "iso.db"
    db_supervisor = Database(test_db)  # simulates supervisor (aiosqlite)
    db_api = Database(test_db)  # simulates API (aiosqlite)
    try:
        await db_supervisor.connect()
        await db_api.connect()

        # Spin up a thread that holds a write transaction on the
        # same file via SYNC sqlite3 (third connection, simulating
        # an external admin tool or the watchdog). This guarantees
        # cross-connection contention that the busy_timeout has to
        # ride out.
        holder_ready = threading.Event()
        holder_release = threading.Event()
        holder_done = threading.Event()
        holder_error: list[Exception] = []

        def _hold_lock():
            try:
                con = _sync_sqlite.connect(str(test_db), timeout=5)
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "INSERT INTO audit_log (event_type, actor, payload, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        "supervisor.holding_lock",
                        "supervisor",
                        '{"holding": true}',
                        "2026-08-04T00:00:00+08:00",
                    ),
                )
                holder_ready.set()
                # Hold the lock until the test signals release.
                holder_release.wait(timeout=10)
                con.commit()
                con.close()
            except Exception as e:
                holder_error.append(e)
            finally:
                holder_done.set()

        t = threading.Thread(target=_hold_lock, daemon=True)
        t.start()
        # Wait for the holder thread to have acquired the write lock.
        assert holder_ready.wait(timeout=5), (
            "holder thread failed to acquire the write lock within 5s"
        )

        try:
            # The API tries to write. With busy_timeout=5000 (v3.12.2
            # #2), this should wait up to 5s for the lock to be
            # released, then succeed. Without busy_timeout, it
            # would raise SQLITE_BUSY immediately.
            async def api_write():
                await db_api.execute(
                    "INSERT INTO audit_log (event_type, actor, payload, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        "api.heartbeat",
                        "api",
                        '{"status": "verified"}',
                        "2026-08-04T00:00:01+08:00",
                    ),
                )

            api_task = asyncio.create_task(api_write())
            # Give the API task a moment to start waiting on the
            # busy_timeout, then release the holder's lock.
            await asyncio.sleep(0.2)
            holder_release.set()
            # API write should now complete (no SQLITE_BUSY /
            # SQLITE_LOCKED exception).
            await asyncio.wait_for(api_task, timeout=5.0)

            # Verify both writes landed.
            rows = await db_api.fetchall(
                "SELECT event_type FROM audit_log "
                "WHERE event_type IN ('supervisor.holding_lock', 'api.heartbeat') "
                "ORDER BY created_at"
            )
            event_types = [r["event_type"] for r in rows]
            assert "supervisor.holding_lock" in event_types, (
                f"holder's row should be visible; got {event_types}"
            )
            assert "api.heartbeat" in event_types, (
                f"API's row should be visible; got {event_types}"
            )
        finally:
            holder_release.set()
            holder_done.wait(timeout=5)
            assert not holder_error, f"holder thread errored: {holder_error}"
    finally:
        await db_supervisor.close()
        await db_api.close()


# ===== Lifespan creates two separate Database instances =====


@pytest_asyncio.fixture
async def lifespan_client(tmp_path, monkeypatch):
    """Boot the FastAPI app with a tmp_path DB. Exposes the
    app.state objects so we can introspect the Database instances
    the lifespan created."""
    test_db = tmp_path / "lifespan.db"
    orig_init = db_mod.Database.__init__

    def patched_init(self, db_path):
        orig_init(self, test_db)

    monkeypatch.setattr(db_mod.Database, "__init__", patched_init)

    app = main_mod.create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


@pytest.mark.asyncio
async def test_lifespan_creates_two_database_instances(lifespan_client):
    """v3.12.2 #3: the lifespan in main.py creates two SEPARATE
    Database instances (`app.state.db` for the API, and
    `app.state.supervisor_db` for the supervisor)."""
    ac, app = lifespan_client
    assert hasattr(app.state, "db"), "app.state.db must exist"
    assert hasattr(app.state, "supervisor_db"), (
        "app.state.supervisor_db must exist (v3.12.2 #3)"
    )
    assert app.state.db is not app.state.supervisor_db, (
        "supervisor must NOT share the API's Database instance"
    )


@pytest.mark.asyncio
async def test_supervisor_isolated_db_in_lifespan(lifespan_client):
    """The supervisor constructor must receive the supervisor's
    own Database instance, not the API's. If a future refactor
    accidentally passes `db` instead of `supervisor_db`, this test
    will fail before the bug hits production."""
    ac, app = lifespan_client
    sup = app.state.supervisor
    assert sup.db is app.state.supervisor_db, (
        "Supervisor.db must point to app.state.supervisor_db "
        "(v3.12.2 #3 isolation), not app.state.db"
    )
    assert sup.db is not app.state.db, (
        "Supervisor.db must NOT be the same as app.state.db"
    )


@pytest.mark.asyncio
async def test_isolated_connections_have_separate_aiosqlite_workers(
    lifespan_client,
):
    """The two Database instances must have DIFFERENT aiosqlite
    connections (different `_conn` objects). If they share the
    same aiosqlite.Connection, the in-process lock contention
    bug is back."""
    ac, app = lifespan_client
    db_api = app.state.db
    db_sup = app.state.supervisor_db
    assert db_api._conn is not db_sup._conn, (
        "app.state.db and app.state.supervisor_db must have different "
        "aiosqlite.Connection objects (different worker threads)"
    )


@pytest.mark.asyncio
async def test_isolated_connections_share_underlying_sqlite_file(
    lifespan_client,
):
    """Both Database instances point to the same db_path. Writes
    on one are visible to the other (cross-connection visibility
    via WAL mode)."""
    ac, app = lifespan_client
    db_api = app.state.db
    db_sup = app.state.supervisor_db
    assert db_api.db_path == db_sup.db_path, (
        "Both Database instances must point to the same file"
    )

    # Write via supervisor
    marker = f"test.iso.{uuid.uuid4().hex[:8]}"
    await db_sup.execute(
        "INSERT INTO audit_log (event_type, actor, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (marker, "supervisor", '{"src": "sup"}', "2026-08-04T00:00:00+08:00"),
    )
    # Read via API
    rows = await db_api.fetchall(
        "SELECT event_type FROM audit_log WHERE event_type = ?",
        (marker,),
    )
    assert len(rows) == 1, (
        f"API should see supervisor's write; got {len(rows)} rows"
    )


@pytest.mark.asyncio
async def test_isolated_connections_have_busy_timeout_pragma(lifespan_client):
    """Both connections have `busy_timeout=5000` (v3.12.2 #2).
    This is the cross-connection SQLITE_BUSY handler; with two
    separate connections, this is what lets the API's heartbeat
    wait out a supervisor's write burst instead of failing
    immediately."""
    ac, app = lifespan_client
    for label, db in [
        ("api", app.state.db),
        ("supervisor", app.state.supervisor_db),
    ]:
        row = await db.fetchone("PRAGMA busy_timeout")
        # PRAGMA busy_timeout returns the timeout in milliseconds
        # as a single-row, single-column result.
        # aiosqlite's row is a Row object; the column name is
        # 'timeout' (SQLite convention for PRAGMA introspection).
        val = row["timeout"] if "timeout" in row else list(row)[0]
        assert val == 5000, (
            f"{label} connection should have busy_timeout=5000; got {val}"
        )
