# coding: utf-8
"""Server-side healthcheck handler (v1.0.1 new-user-activation §3.5).

The system-health starter has a step with action
`_server_healthcheck`. The supervisor recognizes this magic action
and runs the handler below IN-PROCESS — no agent dispatch, no LLM
call. The handler:

  - 0 registered agents  → status=failed, summary="No agent connected yet"
  - 1 registered agent   → status=completed if heartbeat < 60s, else failed
  - 2+ registered agents → status=completed if ANY heartbeat < 60s, else failed;
                            summary lists each agent

Per spec §3.5.1 the zero-agent case flips `first_task_attempted`
but NOT `first_task_completed` (a health check against zero agents
cannot be a "successful" first task).

Runs in mock mode with no LLM key (the smoke test must work on a
fresh install per spec §3.5).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from hermes_orch.core.starters import SERVER_HEALTHCHECK_ACTION
from hermes_orch.core.onboarding import (
    SIGNAL_FIRST_TASK_ATTEMPTED,
    SIGNAL_FIRST_TASK_COMPLETED,
    set_signal_for_all_users,
)

# Heartbeat window: 60 seconds per spec §3.5.1.
HEARTBEAT_FRESH_SECONDS = 60

# How recent "created" the task must be for a fresh first_task_attempted
# to NOT collide with an older attempt. We just rely on the signal's
# idempotent set (no-op if already true).


def _isoformat(ts: float | int | str | None) -> str | None:
    """Normalize a heartbeat timestamp to ISO 8601 UTC.

    Handles both storage formats:
      - Unix timestamp (int/float, possibly fractional)
      - ISO 8601 string (the current DB default — `last_heartbeat_at`
        is stored as a TIMESTAMP column, which aiosqlite surfaces
        as ISO string)

    The supervisor's healthcheck branch must handle both because
    the agents table was created long before v1.0.1 and may have
    legacy rows in either format (pre/post v3.x schema evolution).
    """
    if ts is None or ts == "" or ts == 0:
        return None
    if isinstance(ts, str):
        # Already ISO — but we want to ensure it's a clean string,
        # not a SQLite-style "YYYY-MM-DD HH:MM:SS.SSSSSS" with a
        # space. Normalize by re-parsing + re-formatting.
        try:
            # SQLite returns "2026-08-08 23:56:05.720143" (space,
            # no timezone) for TIMESTAMP columns. Replace space → T
            # so datetime.fromisoformat can parse it.
            s = ts.strip()
            if " " in s and "T" not in s:
                s = s.replace(" ", "T", 1)
            # Handle "Z" suffix
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).isoformat()
        except (ValueError, TypeError):
            return ts  # best effort: return as-is
    # int / float — treat as unix timestamp
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _to_unix(ts: float | int | str | None) -> float | None:
    """Convert a heartbeat timestamp to a unix float for comparison.

    Returns None if unparseable. Used to decide whether the
    heartbeat is "fresh" (within HEARTBEAT_FRESH_SECONDS).
    """
    if ts is None or ts == "" or ts == 0:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        s = ts.strip()
        if " " in s and "T" not in s:
            s = s.replace(" ", "T", 1)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            # SQLite TIMESTAMP without explicit tz → assume UTC.
            # The codebase convention is to store heartbeats as
            # UTC (the wrapper's `datetime.utcnow().isoformat()`
            # at write time). aiosqlite surfaces this as a string
            # WITHOUT tz, which `fromisoformat` parses as naive
            # — we treat naive as UTC.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            return None
    return None


async def run_healthcheck(db) -> dict[str, Any]:
    """Run the server-side healthcheck. Returns the result dict.

    The dict has the same shape as a normal task result:
      {
        "status": "completed" | "failed",
        "summary": "<human-readable>",
        "details": { ... },
        "completed_at": ISO 8601,
      }
    """
    # Look up all registered agents (id, hostname, last_heartbeat)
    now_ts = time.time()
    fresh_after = now_ts - HEARTBEAT_FRESH_SECONDS
    rows = await db.fetchall(
        "SELECT id, name, ip, os_type, status, last_heartbeat_at "
        "FROM agents ORDER BY id"
    )

    # Spec §3.5.1: zero-agent case
    if not rows:
        return {
            "status": "failed",
            "summary": "No agent connected yet — connect one first.",
            "details": {
                "registered_agents": 0,
                "agent_count": 0,
            },
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

    # 1+ agents: build per-agent detail rows + a status aggregate
    per_agent: list[dict[str, Any]] = []
    fresh_count = 0
    for r in rows:
        hb_ts = r.get("last_heartbeat_at")
        hb_unix = _to_unix(hb_ts)
        hb_iso = _isoformat(hb_ts)
        # "fresh" = heartbeat within the last 60s. The agent's
        # status field is secondary — a heartbeat IS the liveness
        # signal. (We don't gate on `status = 'verified'` here
        # because a freshly-enrolled agent has status='verifying'
        # for a few seconds before its first auth'd heartbeat;
        # we don't want the smoke test to fail on that race.)
        is_fresh = bool(hb_unix and hb_unix > fresh_after)
        if is_fresh:
            fresh_count += 1
        per_agent.append({
            "agent_id": r["id"],
            "hostname": r.get("ip") or r.get("name") or r["id"],
            "last_heartbeat_at": hb_iso,
            "is_fresh": is_fresh,
        })

    # Status: completed if at least one agent has a fresh heartbeat
    overall_status = "completed" if fresh_count > 0 else "failed"
    if len(per_agent) == 1:
        # Single-agent: original spec wording (just that one agent)
        a = per_agent[0]
        if overall_status == "completed":
            summary = (
                f"Server ↔ agent connection OK. Last heartbeat: "
                f"{a['last_heartbeat_at']}."
            )
        else:
            summary = (
                f"Agent {a['agent_id']} has no heartbeat in the last "
                f"{HEARTBEAT_FRESH_SECONDS}s. The agent host may be "
                f"down or the wrapper daemon is not running."
            )
    else:
        # Multi-agent: list each
        n_fresh = sum(1 for a in per_agent if a["is_fresh"])
        n_total = len(per_agent)
        if overall_status == "completed":
            summary = (
                f"{n_fresh}/{n_total} agents reachable. "
                + ", ".join(
                    f"{a['agent_id']}: last heartbeat "
                    f"{a['last_heartbeat_at']}"
                    for a in per_agent
                )
                + "."
            )
        else:
            summary = (
                f"0/{n_total} agents have a fresh heartbeat. "
                f"All {n_total} agents may be down. "
                + ", ".join(
                    f"{a['agent_id']}: last heartbeat "
                    f"{a['last_heartbeat_at']}"
                    for a in per_agent
                )
                + "."
            )

    return {
        "status": overall_status,
        "summary": summary,
        "details": {
            "agent_count": len(per_agent),
            "fresh_count": fresh_count,
            "registered_agents": per_agent,
        },
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


async def run_and_record_healthcheck(db, task_id: str) -> dict[str, Any]:
    """Run the healthcheck AND record the result on the task row +
    flip the appropriate onboarding signal.

    Used by the supervisor's `_assign_task` magic-action branch:
    after this returns, the task row is updated to a terminal
    state and the audit log gets a `task.healthcheck` event.

    Per spec §3.5.1:
      - status=completed → flip first_task_completed (collapses the
        onboarding checklist step 4)
      - status=failed    → only flip first_task_attempted (informational;
        does NOT collapse the checklist)
    """
    result = await run_healthcheck(db)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Persist on the task row. The task row already exists (the
    # dispatch flow created it before reaching the supervisor).
    # The result field is JSON; we encode the dict.
    result_json = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
    await db.execute(
        "UPDATE tasks SET status = ?, result = ?, ended_at = ?, updated_at = ? "
        "WHERE id = ?",
        (result["status"], result_json, now_iso, now_iso, task_id),
    )

    # Flip onboarding signal. Wrapped in try/except — never let
    # signal bookkeeping fail the healthcheck.
    try:
        # Always flip attempted
        await set_signal_for_all_users(db, SIGNAL_FIRST_TASK_ATTEMPTED, True)
        # Only flip completed when the healthcheck actually passed
        if result["status"] == "completed":
            await set_signal_for_all_users(db, SIGNAL_FIRST_TASK_COMPLETED, True)
    except Exception:
        pass

    return result
