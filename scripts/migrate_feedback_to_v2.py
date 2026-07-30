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

    audit_log schema: id, event_type, actor, project_id, task_id,
    agent_id, payload (TEXT JSON), created_at (TIMESTAMP default
    CURRENT_TIMESTAMP). We let `id` and `created_at` default; the
    rest is set explicitly.
    """
    conn.execute(
        "INSERT INTO audit_log (event_type, actor, project_id, task_id, payload) "
        "VALUES ('feedback_to.v2_migrated', 'migration', ?, NULL, ?)",
        (project_id, json.dumps(payload)),
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

    v2.0 (2026-07-30) FLIPPED algorithm — fixed the v1.0 bug where
    a task that was BOTH a listener AND a target in OLD data would
    have its new entries wiped by the clear step. Now we compute
    the FINAL feedback_to for each task in one pass: it's the set
    of listeners who point to this task in OLD data. If the task
    had no listeners, its NEW feedback_to is []. If the task
    wasn't a target of anyone, it wasn't a listener either, so
    it stays unchanged.

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

    # Read all tasks' feedback_to
    rows = conn.execute(
        "SELECT id, name, feedback_to FROM tasks WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    summary["tasks_read"] = len(rows)

    # v2.0 FLIPPED algorithm:
    #   For each listener T with feedback_to = [X, Y, Z]:
    #     T becomes empty in NEW semantic (T was a listener, not a
    #     failing step in OLD). If T is also a target of some other
    #     listener U, then T's NEW feedback_to is the set of those U's
    #     (we add U to T). If T isn't targeted by anyone, T is empty.
    #   For each X in T's feedback_to (the OLD triggers):
    #     X becomes the failing step in NEW. X's NEW feedback_to is
    #     the set of all listeners (U) who point to X in OLD.
    #
    # So we compute: for each task_id, new_feedback_to = [U for each U
    # with X in U.feedback_to (OLD)]. Tasks that aren't in any U's
    # feedback_to AND had no OLD feedback_to themselves are unchanged.
    new_fb: dict[str, list[str]] = {}
    # Tracks which tasks had non-empty OLD feedback_to (so we know
    # they were listeners and need their feedback_to REPLACED with
    # the new value, not left as-is).
    was_listener: set[str] = set()

    for tid, tname, raw_fb in rows:
        fb = _parse_fb(raw_fb)
        if not fb:
            continue
        # Skip self-refs (no-op in both OLD and NEW)
        fb = [x for x in fb if x != tid]
        if not fb:
            # T's only feedback_to entries were self-refs. Leave T alone.
            continue
        was_listener.add(tid)
        # For each X in T's OLD feedback_to: T is a listener of X.
        # In NEW, X's feedback_to += [T].
        for x in fb:
            if x not in new_fb:
                new_fb[x] = []
            if tid not in new_fb[x]:
                new_fb[x].append(tid)

    # Apply: for each task in (new_fb union was_listener), write the
    # final value. Tasks in new_fb that aren't listeners (targets only)
    # get new_fb[x]. Tasks that are listeners (with no NEW listeners
    # of their own) get [].
    final_writes: dict[str, list[str]] = {}
    for tid, tname, raw_fb in rows:
        if tid in new_fb and tid in was_listener:
            # Task is both a target (has NEW listeners) and a listener
            # (had OLD feedback_to). Its NEW value is the set of NEW
            # listeners (the OLD feedback_to is dropped because the
            # semantic changed: T was listening to OLD triggers, but
            # in NEW, T's feedback_to lists its own recovery targets,
            # not the things it listens to).
            final_writes[tid] = new_fb[tid]
        elif tid in new_fb:
            # Task is a target only (no OLD feedback_to of its own).
            final_writes[tid] = new_fb[tid]
        elif tid in was_listener:
            # Task is a listener only (no NEW listeners).
            final_writes[tid] = []
        # else: task had no OLD feedback_to AND isn't a target of
        # anyone → leave its feedback_to unchanged.

    summary["tasks_modified"] = len(final_writes)
    summary["rewires"] = sum(len(v) for v in final_writes.values())

    if not final_writes:
        # Nothing to migrate; still stamp the marker
        if not dry_run:
            conn.execute(
                "UPDATE projects SET _feedback_to_v2_migrated_at = ? WHERE id = ?",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), project_id),
            )
            _audit(conn, project_id, {
                "project_id": project_id,
                "tasks_read": summary["tasks_read"],
                "rewires": 0,
                "tasks_modified": 0,
                "note": "no feedback_to entries; nothing to invert",
            })
        return summary

    if not dry_run:
        for tid, fb in final_writes.items():
            conn.execute(
                "UPDATE tasks SET feedback_to = ? WHERE id = ?",
                (json.dumps(fb), tid),
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
                print(f"  [err] {pid}: {s['error']}")
            else:
                if s["rewires"] > 0 or s["tasks_modified"] > 0:
                    grand["projects_migrated"] += 1
                grand["total_rewires"] += s["rewires"]
                if not args.dry_run and (s["rewires"] > 0 or s["tasks_modified"] > 0):
                    print(
                        f"  [ok] {pid}: read {s['tasks_read']} tasks, "
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
