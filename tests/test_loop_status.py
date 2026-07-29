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

import json
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
    payload: str | None = None,
) -> None:
    """Insert an audit_log row with a controlled created_at."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO audit_log "
            "(event_type, task_id, created_at, payload) "
            "VALUES (?, ?, ?, ?)",
            (event_type, task_id, _iso(ts), payload),
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


# ===== Looping detection (v1.2, 2026-07-29) =====


def test_looping_detected_when_5_repeats_in_60s(db_path: Path):
    """5+ identical (tool, signature) pairs in 60s → looping."""
    now = 1_000_000.0
    task = {
        "id": "t-loop",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),  # healthy otherwise
    }
    # 5 tool calls, same tool+sig, all within the last 60s
    for i in range(5):
        _insert_audit(
            db_path,
            "t-loop",
            "agent.tool_call",
            now - (i * 2),  # 10s, 8s, 6s, 4s, 2s ago
            payload='{"tool": "shell", "signature": "abc123"}',
        )
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "looping"
    assert "shell" in s.reason
    assert s.tool == "shell"
    assert s.repeat_count == 5


def test_looping_not_detected_below_threshold(db_path: Path):
    """Only 4 repeats → NOT looping. The threshold is 5."""
    now = 1_000_000.0
    task = {
        "id": "t-loop",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    for i in range(4):
        _insert_audit(
            db_path,
            "t-loop",
            "agent.tool_call",
            now - (i * 2),
            payload='{"tool": "shell", "signature": "abc"}',
        )
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "ok"


def test_looping_not_detected_outside_window(db_path: Path):
    """Old repeats (>60s ago) don't count toward the loop."""
    now = 1_000_000.0
    task = {
        "id": "t-loop",
        "status": "running",
        "started_at": _iso(now - 200),
        "last_liveness_at": _iso(now - 1),
    }
    for i in range(5):
        _insert_audit(
            db_path,
            "t-loop",
            "agent.tool_call",
            now - 120 - (i * 2),  # all > 60s old
            payload='{"tool": "shell", "signature": "abc"}',
        )
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "ok"


def test_looping_picks_worst_offender(db_path: Path):
    """If multiple (tool, sig) pairs repeat, we flag the one with
    the highest count."""
    now = 1_000_000.0
    task = {
        "id": "t-loop",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    # shell+sig_a x 6, shell+sig_b x 3
    for i in range(6):
        _insert_audit(
            db_path, "t-loop", "agent.tool_call", now - i,
            payload='{"tool": "shell", "signature": "sig_a"}',
        )
    for i in range(3):
        _insert_audit(
            db_path, "t-loop", "agent.tool_call", now - 30 - i,
            payload='{"tool": "shell", "signature": "sig_b"}',
        )
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "looping"
    assert s.tool == "shell"
    assert s.repeat_count == 6  # sig_a, the worst offender


def test_looping_higher_priority_than_slow(db_path: Path):
    """A task that's BOTH slow AND looping → looping wins."""
    now = 1_000_000.0
    task = {
        "id": "t-loop",
        "status": "running",
        "started_at": _iso(now - 200),
        "last_liveness_at": _iso(now - 60),  # 60s → slow
    }
    for i in range(5):
        _insert_audit(
            db_path, "t-loop", "agent.tool_call", now - i,
            payload='{"tool": "shell", "signature": "abc"}',
        )
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "looping"


def test_looping_lower_priority_than_stuck_wrapper(db_path: Path):
    """A task with a stuck_wrapper event AND tool loops → stuck wins
    (the wrapper is dead, so the loop is moot)."""
    now = 1_000_000.0
    task = {
        "id": "t-loop",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    _insert_audit(
        db_path, "t-loop", "task.stuck_wrapper", now - 30,
    )
    for i in range(5):
        _insert_audit(
            db_path, "t-loop", "agent.tool_call", now - i,
            payload='{"tool": "shell", "signature": "abc"}',
        )
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "stuck"  # stuck_wrapper > looping


def test_looping_other_event_types_dont_count(db_path: Path):
    """Only agent.tool_call events feed the loop detector. Other
    audit_log rows (e.g. output_chunk) don't."""
    now = 1_000_000.0
    task = {
        "id": "t-loop",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    for i in range(5):
        _insert_audit(
            db_path, "t-loop", "agent.output_chunk", now - i,
            payload='{"seq": 1, "text": "x", "stream": "stdout"}',
        )
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "ok"


def test_looping_scoped_to_task(db_path: Path):
    """Loops on task A don't affect task B."""
    now = 1_000_000.0
    task = {
        "id": "t-this",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    # 5 tool calls on a DIFFERENT task
    for i in range(5):
        _insert_audit(
            db_path, "t-other", "agent.tool_call", now - i,
            payload='{"tool": "shell", "signature": "abc"}',
        )
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "ok"


def test_looping_with_corrupt_payload_doesnt_crash(db_path: Path):
    """A tool_call row with invalid JSON payload should be ignored
    (json_extract returns NULL, so the row doesn't match the
    WHERE clause)."""
    now = 1_000_000.0
    task = {
        "id": "t-loop",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    for i in range(5):
        _insert_audit(
            db_path, "t-loop", "agent.tool_call", now - i,
            payload="not json",
        )
    s = compute_loop_status(task, db_path, now_ts=now)
    # The corrupt rows don't count as tool calls, so no loop
    assert s.status == "ok"


def test_looping_threshold_constant():
    """Sanity check on the v1.2 constants."""
    from hermes_orch.core.loop_status import (
        LOOP_WINDOW_S, LOOP_MIN_REPEATS,
    )
    assert LOOP_WINDOW_S == 60
    assert LOOP_MIN_REPEATS == 5


# ===== v1.7 per-tool thresholds =====
# Some tools fire much more often than others during normal
# operation. A real agent can legitimately read 10+ different files
# in a row, but running the same shell command 6+ times is a real
# loop. The v1.7 TOOL_LOOP_THRESHOLDS dict captures this — see
# docs/loop-detection-v1.7.md for the rationale.


def test_per_tool_thresholds_dict():
    """The thresholds dict has the expected tools + sane values."""
    from hermes_orch.core.loop_status import (
        TOOL_LOOP_THRESHOLDS, DEFAULT_LOOP_THRESHOLD,
    )
    # Must have entries for the common tools
    assert "shell" in TOOL_LOOP_THRESHOLDS
    assert "read" in TOOL_LOOP_THRESHOLDS
    assert "edit" in TOOL_LOOP_THRESHOLDS
    assert "search" in TOOL_LOOP_THRESHOLDS
    # read has a higher threshold than shell (more common during
    # normal operation; we don't want false positives)
    assert TOOL_LOOP_THRESHOLDS["read"] > TOOL_LOOP_THRESHOLDS["shell"]
    # All thresholds are positive
    for tool, n in TOOL_LOOP_THRESHOLDS.items():
        assert n >= 3, f"threshold for {tool!r} too low: {n}"
    assert DEFAULT_LOOP_THRESHOLD >= 3


def _seed_tool_calls(
    db_path: Path,
    task_id: str,
    tool: str,
    signature: str,
    count: int,
    now: float,
    *,
    seconds_ago_start: float = 30.0,
) -> None:
    """Insert `count` audit_log rows for the same (tool, signature)
    pair, spaced 1s apart starting `seconds_ago_start` ago.

    All rows stay inside LOOP_WINDOW_S (60s).
    """
    for i in range(count):
        _insert_audit(
            db_path,
            task_id,
            "agent.tool_call",
            now - seconds_ago_start + i,
            payload=json.dumps({"tool": tool, "signature": signature}),
        )


def test_shell_loop_threshold_5(db_path: Path):
    """shell: 5+ identical calls in 60s → looping."""
    now = 1_000_000.0
    task = {
        "id": "t-shell",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    _seed_tool_calls(db_path, "t-shell", "shell", "sigA", 5, now)
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "looping"
    assert s.tool == "shell"
    assert s.repeat_count == 5


def test_shell_below_threshold(db_path: Path):
    """shell: 4 calls is NOT a loop (threshold 5)."""
    now = 1_000_000.0
    task = {
        "id": "t-shell4",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    _seed_tool_calls(db_path, "t-shell4", "shell", "sigA", 4, now)
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "ok"


def test_read_loop_threshold_15(db_path: Path):
    """read: 6+ identical reads is NOT a loop (threshold 15)."""
    now = 1_000_000.0
    task = {
        "id": "t-read6",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    # 6 calls of the same read_file — typical "exploring the codebase"
    # behavior, NOT a loop
    _seed_tool_calls(db_path, "t-read6", "read", "sigA", 6, now)
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "ok", f"read x6 should not be looping: {s}"


def test_read_loop_threshold_15_at_threshold(db_path: Path):
    """read: 15 calls hits the threshold (boundary check)."""
    now = 1_000_000.0
    task = {
        "id": "t-read15",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    _seed_tool_calls(db_path, "t-read15", "read", "sigA", 15, now)
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "looping"
    assert s.tool == "read"


def test_edit_loop_threshold_5(db_path: Path):
    """edit (patch): 5+ identical patches is a loop."""
    now = 1_000_000.0
    task = {
        "id": "t-edit",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    _seed_tool_calls(db_path, "t-edit", "edit", "sigA", 5, now)
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "looping"
    assert s.tool == "edit"


def test_search_loop_threshold_8(db_path: Path):
    """search: 8+ identical searches is a loop (threshold 8)."""
    now = 1_000_000.0
    task = {
        "id": "t-search",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    _seed_tool_calls(db_path, "t-search", "search", "sigA", 8, now)
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "looping"
    assert s.tool == "search"


def test_search_below_threshold(db_path: Path):
    """search: 7 calls is NOT a loop (threshold 8)."""
    now = 1_000_000.0
    task = {
        "id": "t-search7",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    _seed_tool_calls(db_path, "t-search7", "search", "sigA", 7, now)
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "ok"


def test_unknown_tool_falls_back_to_default(db_path: Path):
    """Tool names not in TOOL_LOOP_THRESHOLDS use the fallback
    (LOOP_MIN_REPEATS=5). This covers new hermes tools we haven't
    categorized yet — be safe-by-default."""
    now = 1_000_000.0
    task = {
        "id": "t-future",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    _seed_tool_calls(db_path, "t-future", "future_tool_xyz", "sigA", 5, now)
    s = compute_loop_status(task, db_path, now_ts=now)
    # Falls back to LOOP_MIN_REPEATS=5, so 5 fires
    assert s.status == "looping"
    assert s.tool == "future_tool_xyz"


def test_unknown_tool_4_does_not_loop(db_path: Path):
    """Same as above: 4 < 5 default threshold, so no loop."""
    now = 1_000_000.0
    task = {
        "id": "t-future4",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    _seed_tool_calls(db_path, "t-future4", "future_tool_xyz", "sigA", 4, now)
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "ok"


def test_per_tool_picks_worst_offender(db_path: Path):
    """When multiple tools are looping, the one with the highest count
    wins (regardless of which has the higher threshold). Same as
    the v1.2 single-threshold behavior."""
    now = 1_000_000.0
    task = {
        "id": "t-multi",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    # 5 read calls (below threshold=15)
    _seed_tool_calls(db_path, "t-multi", "read", "sigR", 5, now)
    # 8 edit calls (at threshold=5) — should win
    _seed_tool_calls(db_path, "t-multi", "edit", "sigE", 8, now, seconds_ago_start=50.0)
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "looping"
    assert s.tool == "edit"
    assert s.repeat_count == 8


def test_per_tool_read_at_15_but_shell_at_5(db_path: Path):
    """Realistic scenario: agent reads 15 different files (loop!)
    AND has run 5 shell commands (also loop). Both should be
    detected but the higher-count one wins."""
    now = 1_000_000.0
    task = {
        "id": "t-mix",
        "status": "running",
        "started_at": _iso(now - 60),
        "last_liveness_at": _iso(now - 1),
    }
    _seed_tool_calls(db_path, "t-mix", "shell", "sigS", 5, now)
    _seed_tool_calls(db_path, "t-mix", "read", "sigR", 15, now, seconds_ago_start=50.0)
    s = compute_loop_status(task, db_path, now_ts=now)
    assert s.status == "looping"
    # ORDER BY n DESC: read (15) > shell (5), so read wins
    assert s.tool == "read"
    assert s.repeat_count == 15


def test_loop_status_dataclass_has_tool_field():
    """LoopStatus.tool is exposed in the dataclass so the UI can
    show the tool name in the badge (v1.7)."""
    s = LoopStatus(status="ok", reason="liveness OK", tool=None, repeat_count=0)
    assert s.tool is None
    s2 = LoopStatus(status="looping", reason="looped 5x: shell", tool="shell", repeat_count=5)
    assert s2.tool == "shell"
