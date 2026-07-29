"""Task progress monitor — compute loop/stuck status for a running task.

Added 2026-07-29 (docs/task-progress-monitor.md, Phase 1).

The orchestrator has limited visibility into what an agent is
actually doing inside its hermes subprocess. We get:
  - task.started_at, task.last_liveness_at, task.status
    (from the tasks table — updated by /poll endpoint)
  - task.stuck_wrapper audit events
    (already emitted by the supervisor when the wrapper
    stops responding to heartbeats)

v1 status semantics (revised 2026-07-29, see design doc §6):
  - `ok`:    last_liveness_at within the last SLOW_THRESHOLD_S
  - `slow`:  no liveness for SLOW_THRESHOLD_S..STUCK_THRESHOLD_S
  - `stuck`: no liveness for > STUCK_THRESHOLD_S
            OR a `task.stuck_wrapper` audit event exists for this
            task in the last 5 minutes (the supervisor already
            detected the agent's wrapper died)
  - `looping`: NOT DETECTABLE in v1 — would require the agent to
            POST per-tool-call events (not implemented). We mark
            this as `unknown` for now; v1.1 will add an agent
            progress endpoint to enable real loop detection.
  - `unknown`: insufficient data (e.g. freshly started, no events)

All thresholds are tunable via the constants below; conservative
defaults match the design doc.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


# Tunable thresholds (see design doc §6)
SLOW_THRESHOLD_S = 30
STUCK_THRESHOLD_S = 120
LOOKBACK_FOR_STUCK_WRAPPER_S = 300  # 5 min
# Looping detection (v1.2, 2026-07-29): if the same (tool, signature)
# pair fires LOOP_MIN_REPEATS times within LOOP_WINDOW_S, we call it
# a loop. Conservative defaults — a real agent rarely calls the same
# tool with identical args 5+ times in 60s.
LOOP_WINDOW_S = 60
LOOP_MIN_REPEATS = 5


@dataclass
class LoopStatus:
    """The computed status for a task. The `status` field is one of
    'ok' / 'slow' / 'stuck' / 'looping' / 'unknown'. The `reason`
    is a human-readable explanation. The other fields provide
    context for the UI (icons, tooltips, etc.)."""
    status: str
    reason: str
    duration_s: int = 0
    last_event_age_s: int | None = None
    last_event_summary: str | None = None
    tool: str | None = None
    repeat_count: int = 0
    tools_recent: list[str] | None = None


def compute_loop_status(
    task: dict,
    db_path: Path,
    now_ts: float | None = None,
) -> LoopStatus:
    """Compute the live status of a running task.

    Args:
        task: row from the tasks table. Must have:
            - 'status'        ('running' / 'done' / 'failed' / 'cancelled')
            - 'started_at'    (ISO timestamp; may be None for never-started)
            - 'last_liveness_at' (ISO timestamp; may be None)
        db_path: path to the orchestrator's SQLite DB. Used to
            look up recent audit_log entries for the task.
        now_ts: current time in seconds (default: time.time()).

    Returns:
        LoopStatus dataclass with the computed status + reason.
        For non-running tasks, returns status='ok' with reason
        'task is <status>' (so the UI can render a single badge
        uniformly).
    """
    if now_ts is None:
        now_ts = time.time()

    # Only meaningful for running tasks
    status = task.get("status", "")
    if status != "running":
        return LoopStatus(
            status="ok",
            reason=f"task is {status}",
            duration_s=0,  # non-running tasks have no live "duration"
        )

    started_at_s = _iso_to_seconds(task.get("started_at"), now_ts)
    last_liveness_s = _iso_to_seconds(task.get("last_liveness_at"), now_ts)
    duration_s = int(now_ts - started_at_s) if started_at_s is not None else 0
    last_event_age_s = (
        int(now_ts - last_liveness_s) if last_liveness_s is not None else None
    )

    # 1. STUCK: supervisor already detected the wrapper is dead.
    #    Look for a recent task.stuck_wrapper event for this task.
    if _has_recent_stuck_wrapper_event(db_path, task["id"], now_ts):
        return LoopStatus(
            status="stuck",
            reason="agent wrapper not responding (supervisor flagged)",
            duration_s=duration_s,
            last_event_age_s=last_event_age_s,
        )

    # 1b. LOOPING: v1.2 (2026-07-29) — the agent is calling the same
    #     tool with the same args over and over. We can detect this
    #     now because the wrapper emits agent.tool_call events for
    #     each invocation. Highest priority after stuck_wrapper (a
    #     stuck wrapper isn't looping, it's dead).
    loop_info = _detect_loop(db_path, task["id"], now_ts)
    if loop_info is not None:
        tool, count = loop_info
        return LoopStatus(
            status="looping",
            reason=f"looped {count} times: {tool}",
            duration_s=duration_s,
            last_event_age_s=last_event_age_s,
            tool=tool,
            repeat_count=count,
        )

    # 2. STUCK: no liveness for > 2 min (independent of supervisor)
    if last_event_age_s is not None and last_event_age_s > STUCK_THRESHOLD_S:
        return LoopStatus(
            status="stuck",
            reason=f"no liveness for {last_event_age_s}s",
            duration_s=duration_s,
            last_event_age_s=last_event_age_s,
        )

    # 3. SLOW: no liveness for > 30s but < 2 min
    if last_event_age_s is not None and last_event_age_s > SLOW_THRESHOLD_S:
        return LoopStatus(
            status="slow",
            reason=f"no liveness for {last_event_age_s}s",
            duration_s=duration_s,
            last_event_age_s=last_event_age_s,
        )

    # 4. UNKNOWN: no liveness data yet (just started, or no poll yet)
    if last_liveness_s is None:
        return LoopStatus(
            status="unknown",
            reason="no liveness signal yet",
            duration_s=duration_s,
            last_event_age_s=None,
        )

    # 5. OK
    return LoopStatus(
        status="ok",
        reason="liveness OK",
        duration_s=duration_s,
        last_event_age_s=last_event_age_s,
    )


def _iso_to_seconds(iso_str: str | None, now_ts: float) -> int | None:
    """Convert an ISO-8601 timestamp to seconds since epoch.

    Accepts both naive and timezone-aware ISO strings (the kind
    produced by hermes_orch.utils.now_iso(), e.g.
    '2026-07-29T10:00:00.123456+08:00').

    Returns None on parse error or empty input.

    Note: `now_ts` is accepted for API symmetry with the public
    `compute_loop_status()` — it's not used by the parser itself.
    """
    if not iso_str:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso_str).timestamp()
    except (ValueError, TypeError):
        return None


def _has_recent_stuck_wrapper_event(
    db_path: Path, task_id: str, now_ts: float
) -> bool:
    """Check whether the supervisor has flagged this task as a
    stuck wrapper in the last LOOKBACK_FOR_STUCK_WRAPPER_S seconds.

    The supervisor writes a `task.stuck_wrapper` audit event when
    the wrapper stops responding. We use it as a strong signal
    that the agent's process is dead (vs just slow)."""
    try:
        # Look for the event in audit_log, filtering by recency.
        # We compare ISO timestamps lexicographically (works because
        # ISO 8601 is sortable as a string).
        from datetime import datetime, timezone
        cutoff = datetime.fromtimestamp(
            now_ts - LOOKBACK_FOR_STUCK_WRAPPER_S, tz=timezone.utc
        ).isoformat()
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE task_id = ? AND event_type = 'task.stuck_wrapper' "
                "AND created_at >= ?",
                (task_id, cutoff),
            )
            return cur.fetchone()[0] > 0
        finally:
            conn.close()
    except Exception:
        # Defensive: if the DB is locked or the schema changes,
        # don't crash the dashboard — just return False.
        return False


def _detect_loop(
    db_path: Path, task_id: str, now_ts: float
) -> tuple[str, int] | None:
    """Detect a tool-call loop: if any (tool, signature) pair has
    fired LOOP_MIN_REPEATS times in the last LOOP_WINDOW_S seconds,
    return (tool, count). Otherwise return None.

    Defensive: returns None on any DB error so a corrupt audit_log
    can't crash the dashboard.
    """
    try:
        from datetime import datetime, timezone
        cutoff = datetime.fromtimestamp(
            now_ts - LOOP_WINDOW_S, tz=timezone.utc
        ).isoformat()
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            # Use json_extract to pull the tool/signature out of the
            # payload column (stored as JSON text). GROUP BY the pair
            # to find repeated calls. LIMIT 1 + ORDER BY DESC picks
            # the worst offender.
            cur.execute(
                "SELECT json_extract(payload, '$.tool') AS tool, "
                "       COUNT(*) AS n "
                "FROM audit_log "
                "WHERE task_id = ? AND event_type = 'agent.tool_call' "
                "AND created_at >= ? "
                "AND json_extract(payload, '$.tool') IS NOT NULL "
                "AND json_extract(payload, '$.signature') IS NOT NULL "
                "GROUP BY json_extract(payload, '$.tool'), "
                "         json_extract(payload, '$.signature') "
                "ORDER BY n DESC LIMIT 1",
                (task_id, cutoff),
            )
            row = cur.fetchone()
            if not row:
                return None
            tool, count = row
            if count is None or count < LOOP_MIN_REPEATS:
                return None
            return (str(tool), int(count))
        finally:
            conn.close()
    except Exception:
        return None
