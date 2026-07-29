"""Tests for src/hermes_orch/core/loop_status.py (Task Progress Monitor, T1).

These tests are pure-function (with a temp SQLite for the audit_log
lookup). The thresholds are deliberately injected via now_ts so the
tests are deterministic — no sleeping, no real wall-clock.

Coverage:
  - non-running tasks always return status="ok" with a "task is X" reason
  - running with no liveness → "unknown"
  - running with recent liveness → "ok"
  - running with 30s < liveness_age ≤ 120s → "slow"
  - running with liveness_age > 120s → "stuck"
  - running + recent task.stuck_wrapper audit event → "stuck" (priority)
  - old task.stuck_wrapper audit event (>5min) → ignored
  - duration_s and last_event_age_s are computed correctly
  - "looping" is never returned in v1 (deferred to v1.1)
  - malformed ISO timestamps are tolerated (don't crash)
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_orch.core.loop_status import (
    LOOKBACK_FOR_STUCK_WRAPPER_S,
    SLOW_THRESHOLD_S,
    STUCK_THRESHOLD_S,
    LoopStatus,
    compute_loop_status,
)


# ===== Fixtures =====


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create a temp SQLite DB with the audit_log table.

    We don't need the full schema — only audit_log, because that's
    the only table compute_loop_status reads. Tasks come in as a
    dict argument."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE audit_log ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  event_type TEXT NOT NULL,"
            "  actor TEXT,"
            "  project_id TEXT,"
            "  task_id TEXT,"
            "  agent_id TEXT,"
            "  payload TEXT,"
            "  created_at TIMESTAMP"
            ")"
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _iso(ts: float) -> str:
    """Build an ISO-8601 timestamp that _iso_to_seconds can parse.

    Format: 2026-07-29T02:00:00.123456+00:00
    (matches hermes_orch.utils.now_iso() output, with UTC offset).
    """
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat()
    )


def _insert_audit(
    db_path: Path,
    task_id: str,
    event_type: str,
    ts: float,
) -> None:
    """Insert an audit_log row with a controlled created_at."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO audit_log "
            "(event_type, task_id, created_at) VALUES (?, ?, ?)",
            (event_type, task_id, _iso(ts)),
        )
        conn.commit()
    finally:
        conn.close()


# ===== Non-running task short-circuit =====


def test_non_running_done_task_returns_ok(tmp_path: Path):
    db = tmp_path / "noop.db"  # never opened
    task = {
        "id": "t1",
        "status": "done",
        "started_at": _iso(1000.0),
        "last_liveness_at": _iso(1100.0),
    }
    s = compute_loop_status(task, db, now_ts=1200.0)
    assert s.status == "ok"
    assert s.reason == "task is done"
    assert s.duration_s == 0  # non-running → no live "duration"


def test_non_running_failed_task_returns_ok(tmp_path: Path):
    db = tmp_path / "noop.db"
    task = {"id": "t1", "status": "failed"}
    s = compute_loop_status(task, db, now_ts=1200.0)
    assert s.status == "ok"
    assert s.reason == "task is failed"
    assert s.duration_s == 0  # no started_at


def test_non_running_cancelled_task_returns_ok(tmp_path: Path):
    db = tmp_path / "noop.db"
    task = {"id": "t1", "status": "cancelled"}
    s = compute_loop_status(task, db, now_ts=1200.0)
    assert s.status == "ok"
    assert s.reason == "task is cancelled"


# ===== Running task: no liveness data =====


def test_running_no_liveness_returns_unknown(db_path: Path):
    """Just-started running task that hasn't reported any liveness yet."""
    now = 1_000_000.0
    task = {
        "id": "t-running",
        "status": "running",
        "started_at": _iso(now - 5),
        "last_liveness_at": None,
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "unknown"
    assert "no liveness" in s.reason
    assert s.last_event_age_s is None
    assert s.duration_s == 5


def test_running_no_liveness_no_started_at(db_path: Path):
    """Edge case: status=running but started_at missing too."""
    now = 1_000_000.0
    task = {
        "id": "t-running",
        "status": "running",
        "started_at": None,
        "last_liveness_at": None,
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "unknown"
    assert s.duration_s == 0  # can't compute without started_at


# ===== Running task: recent liveness → ok =====


def test_running_fresh_liveness_returns_ok(db_path: Path):
    """Liveness 5s ago → well under SLOW_THRESHOLD."""
    now = 1_000_000.0
    task = {
        "id": "t-running",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 5),
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "ok"
    assert s.reason == "liveness OK"
    assert s.last_event_age_s == 5
    assert s.duration_s == 60


def test_running_liveness_exactly_at_slow_threshold(db_path: Path):
    """Boundary: liveness exactly SLOW_THRESHOLD_S old → still ok
    (the test is `> SLOW_THRESHOLD_S`, not `>=`)."""
    now = 1_000_000.0
    task = {
        "id": "t-running",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - SLOW_THRESHOLD_S),
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "ok"


# ===== Running task: slow (30s < age ≤ 120s) =====


def test_running_slow_threshold(db_path: Path):
    """Liveness 45s ago → between SLOW and STUCK → slow."""
    now = 1_000_000.0
    task = {
        "id": "t-running",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 45),
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "slow"
    assert "45s" in s.reason
    assert s.last_event_age_s == 45


def test_running_slow_just_above_threshold(db_path: Path):
    """Just past SLOW_THRESHOLD_S (31s) → slow."""
    now = 1_000_000.0
    task = {
        "id": "t-running",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - (SLOW_THRESHOLD_S + 1)),
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "slow"


def test_running_slow_just_below_stuck_threshold(db_path: Path):
    """119s — one second before stuck → still slow."""
    now = 1_000_000.0
    task = {
        "id": "t-running",
        "status": "running",
        "started_at": _iso(now - 200),
        "last_liveness_at": _iso(now - (STUCK_THRESHOLD_S - 1)),
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "slow"


# ===== Running task: stuck (age > 120s) =====


def test_running_stuck_threshold(db_path: Path):
    """Liveness 180s ago → over STUCK_THRESHOLD → stuck."""
    now = 1_000_000.0
    task = {
        "id": "t-running",
        "status": "running",
        "started_at": _iso(now - 300),
        "last_liveness_at": _iso(now - 180),
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "stuck"
    assert "180s" in s.reason
    assert s.last_event_age_s == 180


def test_running_stuck_just_above_threshold(db_path: Path):
    """121s — one second past stuck → stuck."""
    now = 1_000_000.0
    task = {
        "id": "t-running",
        "status": "running",
        "started_at": _iso(now - 200),
        "last_liveness_at": _iso(now - (STUCK_THRESHOLD_S + 1)),
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "stuck"


# ===== Running task: stuck_wrapper audit event (highest priority) =====


def test_running_stuck_wrapper_event_overrides_recent_liveness(db_path: Path):
    """If the supervisor already flagged the wrapper dead, the task
    is stuck regardless of how recent last_liveness_at looks.

    The supervisor's `task.stuck_wrapper` event is the strongest
    signal we have — it means the agent process died even though
    the last poll may have looked healthy."""
    now = 1_000_000.0
    task_id = "t-wedged"
    # Insert a stuck_wrapper event 30s ago (well within lookback)
    _insert_audit(db_path, task_id, "task.stuck_wrapper", ts=now - 30)
    task = {
        "id": task_id,
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 2),  # would be "ok" on its own
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "stuck"
    assert "supervisor" in s.reason.lower() or "wrapper" in s.reason.lower()


def test_running_stuck_wrapper_event_with_no_liveness(db_path: Path):
    """stuck_wrapper event takes priority even when liveness data is
    missing (e.g. wrapper died right after starting)."""
    now = 1_000_000.0
    task_id = "t-just-started"
    _insert_audit(db_path, task_id, "task.stuck_wrapper", ts=now - 60)
    task = {
        "id": task_id,
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": None,
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "stuck"


def test_old_stuck_wrapper_event_is_ignored(db_path: Path):
    """stuck_wrapper events older than LOOKBACK_FOR_STUCK_WRAPPER_S
    are stale (the supervisor may have recovered the wrapper) and
    should not count as stuck. Fall back to the liveness heuristic."""
    now = 1_000_000.0
    task_id = "t-recovered"
    # Event just outside the lookback window
    _insert_audit(
        db_path,
        task_id,
        "task.stuck_wrapper",
        ts=now - (LOOKBACK_FOR_STUCK_WRAPPER_S + 10),
    )
    task = {
        "id": task_id,
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 5),  # healthy now
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    # Should be ok, not stuck — old event ignored
    assert s.status == "ok"


def test_stuck_wrapper_event_for_different_task_ignored(db_path: Path):
    """The stuck_wrapper lookup filters by task_id, so a stuck event
    on another task doesn't bleed into this task's status."""
    now = 1_000_000.0
    # Stuck event for ANOTHER task
    _insert_audit(db_path, "t-other", "task.stuck_wrapper", ts=now - 10)
    task = {
        "id": "t-this-one",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 5),
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "ok"


def test_unrelated_audit_events_dont_count_as_stuck(db_path: Path):
    """Only `task.stuck_wrapper` triggers the supervisor path. Other
    events (task.started, task.completed, etc.) don't."""
    now = 1_000_000.0
    task_id = "t-normal"
    # Lots of normal events, but no stuck_wrapper
    _insert_audit(db_path, task_id, "task.created", ts=now - 60)
    _insert_audit(db_path, task_id, "task.started", ts=now - 55)
    _insert_audit(db_path, task_id, "task.assigned", ts=now - 50)
    task = {
        "id": task_id,
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 5),
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "ok"


# ===== LoopStatus dataclass & invariants =====


def test_looping_status_never_returned_in_v1(db_path: Path):
    """v1 cannot detect loops (no per-tool-call events). Verify the
    function never returns 'looping' across all plausible inputs —
    this is a safety net until v1.1 adds agent-side reporting."""
    now = 1_000_000.0
    # Try every combination
    cases = [
        {"id": "a", "status": "running", "started_at": _iso(now - 1),
         "last_liveness_at": _iso(now - 1)},
        {"id": "b", "status": "running", "started_at": _iso(now - 600),
         "last_liveness_at": _iso(now - 600)},
        {"id": "c", "status": "running", "started_at": None,
         "last_liveness_at": None},
    ]
    for t in cases:
        s = compute_loop_status(t, db_path, now_ts=now)
        assert s.status != "looping", f"looping returned for {t}"


def test_returned_dataclass_has_expected_fields():
    """Smoke test the dataclass shape — the UI relies on these names."""
    s = LoopStatus(status="ok", reason="x")
    assert s.status == "ok"
    assert s.reason == "x"
    assert s.duration_s == 0
    assert s.last_event_age_s is None
    assert s.last_event_summary is None
    assert s.tool is None
    assert s.repeat_count == 0
    assert s.tools_recent is None


# ===== Defensive: malformed inputs don't crash =====


def test_malformed_iso_timestamp_returns_unknown_or_zero_duration(db_path: Path):
    """If last_liveness_at is garbage, _iso_to_seconds returns None
    and we fall into the 'unknown' branch rather than crashing."""
    now = 1_000_000.0
    task = {
        "id": "t-bad",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": "not-an-iso-timestamp",
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "unknown"
    assert s.duration_s == 60  # started_at parsed fine


def test_empty_string_liveness_returns_unknown(db_path: Path):
    """Empty string for last_liveness_at should be treated as None."""
    now = 1_000_000.0
    task = {
        "id": "t-empty",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": "",
    }
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "unknown"


def test_corrupt_db_does_not_crash(tmp_path: Path):
    """If the audit_log query fails (DB missing/broken/locked), the
    helper swallows the exception and returns False. The caller then
    falls through to the liveness-based check."""
    now = 1_000_000.0
    corrupt = tmp_path / "corrupt.db"
    # Write garbage that's not a valid SQLite header
    corrupt.write_bytes(b"this is not a sqlite database")
    task = {
        "id": "t-x",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 5),
    }
    # Should not raise — falls through to ok/slow/stuck branch
    s = compute_loop_status(task, corrupt, now_ts=now)
    assert s.status == "ok"


def test_missing_db_file_does_not_crash(tmp_path: Path):
    """If the DB file doesn't exist, the helper should not crash."""
    now = 1_000_000.0
    missing = tmp_path / "does-not-exist.db"
    task = {
        "id": "t-x",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 5),
    }
    s = compute_loop_status(task, missing, now_ts=now)
    assert s.status == "ok"


# ===== Threshold values are what the design doc says =====


def test_thresholds_match_design_doc():
    """Sanity check: if anyone changes the constants, the design doc
    (§6 of docs/task-progress-monitor.md) must be updated to match."""
    assert SLOW_THRESHOLD_S == 30
    assert STUCK_THRESHOLD_S == 120
    assert LOOKBACK_FOR_STUCK_WRAPPER_S == 300
