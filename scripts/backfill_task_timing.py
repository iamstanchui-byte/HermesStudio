"""One-shot backfill: populate tasks.started_at and tasks.ended_at for
pre-migration tasks.

Before this script, those columns were NULL for every task, and the
dashboard hacked duration from `updated_at - last_liveness_at` —
which gave 1-30s for every task because both fields were written
within 1-2s of completion (last poll + final UPDATE).

Run this AFTER the server migration (`ALTER TABLE tasks ADD COLUMN
started_at / ended_at`) and after the server restart picks it up.

Heuristics for backfill:
- started_at: prefer `last_liveness_at` (set when wrapper first polled
  = approximately when the task started running). If NULL, fall back
  to `created_at` (lowest bound, but always present).
- ended_at: prefer `updated_at` IF status is terminal
  (completed/failed/cancelled/interrupted/skipped). For pending/assigned
  /running tasks, leave NULL.

Idempotent: re-running won't overwrite non-NULL values (uses
COALESCE on the SQL side).

Usage (from project root, with the venv active):

    .venv\\Scripts\\python.exe scripts\\backfill_task_timing.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")

# Must match the TERMINAL set in api/dashboard.py _compute_task_timing.
TERMINAL = ("completed", "failed", "cancelled", "interrupted", "skipped")


def main() -> int:
    if not DB.exists():
        print(f"DB not found: {DB}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Sanity check: columns must exist (i.e. server has been restarted
    # at least once since the migration was added to db.py).
    cols = {r["name"] for r in cur.execute("PRAGMA table_info(tasks)").fetchall()}
    if "started_at" not in cols or "ended_at" not in cols:
        print(
            "ERROR: tasks table is missing started_at / ended_at columns.\n"
            "Restart the server first so the migration in db.py runs, then\n"
            "re-run this script.",
            file=sys.stderr,
        )
        return 2

    # Counts before
    total = cur.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    before_started_null = cur.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE started_at IS NULL"
    ).fetchone()["n"]
    before_ended_null = cur.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE ended_at IS NULL"
    ).fetchone()["n"]
    print(
        f"Before: total={total}  started_at NULL={before_started_null}  "
        f"ended_at NULL={before_ended_null}"
    )

    # Backfill started_at.
    # - Use last_liveness_at if set (most accurate: first poll ≈ claim time)
    # - Else use created_at (always present, lower bound)
    # COALESCE on the SET side means we only update NULL rows.
    placeholders = ",".join("?" for _ in TERMINAL)
    cur.execute(
        f"""
        UPDATE tasks
        SET started_at = COALESCE(last_liveness_at, created_at)
        WHERE started_at IS NULL
        """,
    )
    started_updated = cur.rowcount
    print(f"  backfilled started_at on {started_updated} tasks")

    # Backfill ended_at. Only for terminal-state tasks; running/pending
    # /assigned are still in flight, leave NULL.
    cur.execute(
        f"""
        UPDATE tasks
        SET ended_at = updated_at
        WHERE ended_at IS NULL
          AND status IN ({placeholders})
        """,
        TERMINAL,
    )
    ended_updated = cur.rowcount
    print(f"  backfilled ended_at   on {ended_updated} tasks (terminal only)")

    conn.commit()

    # Counts after
    after_started_null = cur.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE started_at IS NULL"
    ).fetchone()["n"]
    after_ended_null = cur.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE ended_at IS NULL"
    ).fetchone()["n"]
    print(
        f"After:  total={total}  started_at NULL={after_started_null}  "
        f"ended_at NULL={after_ended_null}"
    )

    # Show a few samples so the operator can sanity-check
    print()
    print("Sample (proj-e4c9e5dd last 6 tasks):")
    print(
        f"  {'name':<24} {'status':<11} {'started_at':<20} {'ended_at':<20} "
        f"{'duration':>10}"
    )
    print("  " + "-" * 90)
    rows = cur.execute(
        """
        SELECT name, status, started_at, ended_at
        FROM tasks
        WHERE project_id = 'proj-e4c9e5dd'
        ORDER BY created_at
        LIMIT 6 OFFSET 18
        """
    ).fetchall()
    from datetime import datetime

    def _parse(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    for r in rows:
        s_dt = _parse(r["started_at"])
        e_dt = _parse(r["ended_at"])
        dur = (e_dt - s_dt).total_seconds() if (s_dt and e_dt) else None
        print(
            f"  {r['name'][:23]:<24} {r['status'][:10]:<11} "
            f"{(r['started_at'] or '-')[:19]:<20} "
            f"{(r['ended_at'] or '-')[:19]:<20} "
            f"{(f'{dur:.0f}s' if dur is not None else '-'):>10}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
