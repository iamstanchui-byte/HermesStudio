# coding: utf-8
"""Migrate v1.9.4 `feedback_to` data to v2.0 (FLIPPED semantic).

v1.9.4 (and earlier): B.feedback_to = [A] meant "if A fails, re-run B"
  (target subscribes to trigger's failure)

v2.0 (2026-07-30): A.feedback_to = [B] means "if A fails, re-run B"
  (failing step declares its recovery targets)

The DB column stays the same (feedback_to TEXT, JSON-encoded list of
task IDs). Only the INTERPRETATION of the list changes. To preserve
the OUTCOME, we invert the relationship:

  For each task T with feedback_to = [A_id, B_id, C_id, ...]:
    For each id X in T.feedback_to:
      Add T to X's feedback_to list (if not already there)

After migration:
  - OLD: T.feedback_to = [A] meant "if A fails, re-run T"
  - NEW: A.feedback_to includes T → "if A fails, re-run T"
  - Same outcome ✓

Idempotency: a project-level marker column `_feedback_to_v2_migrated_at`
is added (TEXT, ISO timestamp). Projects with a non-null value are
skipped. Run multiple times safely.

Run: `python scripts/migrate_feedback_to_v2.py`
Dry-run: `python scripts/migrate_feedback_to_v2.py --dry-run`

Touches:
  - tasks.feedback_to: in-place UPDATE for each affected task
  - projects._feedback_to_v2_migrated_at: column add + per-row stamp
  - audit_log: one 'feedback_to.v2_migrated' event per project
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

DB = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"


def _ensure_marker_column(conn: sqlite3.Connection) -> None:
    """Add the per-project migration marker column if it doesn't exist.

    Idempotent: SQLite ALTER TABLE ADD COLUMN fails with
    "duplicate column name" if we run twice. We check pragma
    table_info first to avoid the error.
    """
    cols = [
        r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()
    ]
    if "_feedback_to_v2_migrated_at" not in cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN "
            "_feedback_to_v2_migrated_at TEXT"
        )


def _audit(conn: sqlite3.Connection, project_id: str, payload: dict) -> None:
    """Write a single audit_log row for the migration event.

    audit_log schema: id, ts, actor, project_id, task_id, event_type,
    payload (TEXT JSON). We let `id` default (auto-rowid); ts is the
    current UTC time in the same format the rest of the system uses.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "INSERT INTO audit_log (ts, actor, project_id, task_id, event_type, payload) "
        "VALUES (?, 'migration', ?, NULL, 'feedback_to.v2_migrated', ?)",
        (ts, project_id, json.dumps(payload)),
    )


def _parse_fb(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, str) and x]


def migrate_one_project(
    conn: sqlite3.Connection,
    project_id: str,
    dry_run: bool = False,
) -> dict:
    """Invert feedback_to on all tasks of `project_id`. Idempotent.

    Returns a summary dict for the caller to log.
    """
    summary = {
        "project_id": project_id,
        "tasks_read": 0,
        "tasks_modified": 0,
        "rewires": 0,  # total feedback_to entries that changed
        "skipped_already_migrated": False,
    }
    cur = conn.execute(
        "SELECT _feedback_to_v2_migrated_at FROM projects WHERE id = ?",
        (project_id,),
    )
    row = cur.fetchone()
    if row is None:
        summary["error"] = "project not found"
        return summary
    if row[0]:
        summary["skipped_already_migrated"] = True
        return summary

    # Read all tasks' feedback_to, group by name → id for the audit
    rows = conn.execute(
        "SELECT id, name, feedback_to FROM tasks WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    summary["tasks_read"] = len(rows)

    # Build the inverted relationship.
    # For each task T with feedback_to = [A_id, B_id, ...]:
    #   For each X in T.feedback_to: append T.id to X's feedback_to.
    #   Then clear T's feedback_to.
    additions: dict[str, list[str]] = {}  # target_id -> [task_ids to add]
    clears: set[str] = set()  # task_ids whose feedback_to should become []
    for tid, tname, raw_fb in rows:
        fb = _parse_fb(raw_fb)
        if not fb:
            continue
        # Self-ref: skip (the supervisor drops these at run time anyway)
        fb = [x for x in fb if x != tid]
        if not fb:
            continue
        for x in fb:
            additions.setdefault(x, []).append(tid)
        clears.add(tid)

    if not additions and not clears:
        # Nothing to migrate in this project; still stamp the marker
        # so we don't re-scan.
        if not dry_run:
            conn.execute(
                "UPDATE projects SET _feedback_to_v2_migrated_at = ? WHERE id = ?",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), project_id),
            )
            _audit(conn, project_id, {
                "project_id": project_id,
                "tasks_read": summary["tasks_read"],
                "rewires": 0,
                "note": "no feedback_to entries; nothing to invert",
            })
        return summary

    # Compute rewires count up front so it's reported even in dry-run.
    for to_add in additions.values():
        summary["rewires"] += len(to_add)
    summary["tasks_modified"] = len(clears)

    # Apply the inversion
    if not dry_run:
        for target_id, to_add in additions.items():
            cur_row = conn.execute(
                "SELECT feedback_to FROM tasks WHERE id = ?",
                (target_id,),
            ).fetchone()
            if cur_row is None:
                # The named target doesn't exist (task was deleted or
                # never created — probably an unresolved ref). Skip
                # silently; this matches the supervisor's behavior.
                continue
            existing = _parse_fb(cur_row[0])
            merged = list(existing)
            for x in to_add:
                if x not in merged:
                    merged.append(x)
            conn.execute(
                "UPDATE tasks SET feedback_to = ? WHERE id = ?",
                (json.dumps(merged), target_id),
            )

        # Clear the old listeners' feedback_to (they were the OLD
        # targets, now empty in the NEW semantic)
        for tid in clears:
            conn.execute(
                "UPDATE tasks SET feedback_to = '[]' WHERE id = ?",
                (tid,),
            )

        # Stamp the marker
        conn.execute(
            "UPDATE projects SET _feedback_to_v2_migrated_at = ? WHERE id = ?",
            (time.strftime("%Y-%m-%dT%H:%M:%S"), project_id),
        )
        _audit(conn, project_id, {
            "project_id": project_id,
            "tasks_read": summary["tasks_read"],
            "tasks_modified": summary["tasks_modified"],
            "rewires": summary["rewires"],
        })
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Migrate v1.9.4 feedback_to data to v2.0 (flipped semantic)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing to the DB",
    )
    ap.add_argument(
        "--db", default=str(DB),
        help=f"Path to the orchestrator DB (default: {DB})",
    )
    ap.add_argument(
        "--project", default=None,
        help="Migrate only this project id (default: all projects)",
    )
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        _ensure_marker_column(conn)
        if args.project:
            pids = [args.project]
        else:
            pids = [
                r[0] for r in conn.execute(
                    "SELECT id FROM projects ORDER BY id"
                ).fetchall()
            ]
        if not args.dry_run:
            conn.execute("BEGIN")
        grand = {
            "projects_scanned": 0,
            "projects_migrated": 0,
            "projects_skipped": 0,
            "total_rewires": 0,
        }
        for pid in pids:
            s = migrate_one_project(conn, pid, dry_run=args.dry_run)
            grand["projects_scanned"] += 1
            if s.get("skipped_already_migrated"):
                grand["projects_skipped"] += 1
            elif s.get("error"):
                print(f"  ! {pid}: {s['error']}")
            else:
                if s["rewires"] > 0 or s["tasks_modified"] > 0:
                    grand["projects_migrated"] += 1
                grand["total_rewires"] += s["rewires"]
                if not args.dry_run and (s["rewires"] > 0 or s["tasks_modified"] > 0):
                    print(
                        f"  ✓ {pid}: read {s['tasks_read']} tasks, "
                        f"modified {s['tasks_modified']}, "
                        f"rewires {s['rewires']}"
                    )
        if not args.dry_run:
            conn.commit()
        prefix = "[DRY-RUN] " if args.dry_run else ""
        print(
            f"{prefix}migrate_feedback_to_v2: "
            f"scanned {grand['projects_scanned']}, "
            f"migrated {grand['projects_migrated']}, "
            f"skipped {grand['projects_skipped']}, "
            f"rewires {grand['total_rewires']}"
        )
        return 0
    except Exception as e:
        conn.rollback()
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
