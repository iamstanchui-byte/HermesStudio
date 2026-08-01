"""One-shot cleanup: delete artifacts matching a path prefix.

Used after the v3.10.3/v3.10.4 wrapper bug fixes to clean up
spurious artifact rows (e.g. node_modules/*, .pdfvenv/*) that
got uploaded before the skip list / glob pattern was in place.

Generalized from the original v3.10.3-specific script. Now
takes a `--prefix` arg to clean up any path prefix.

Examples:
  # Clean up all .pdfvenv/* artifacts (v3.10.4 follow-up bug)
  python scripts/_cleanup_variants_artifacts.py --prefix .pdfvenv/

  # Clean up a specific project's artifacts
  python scripts/_cleanup_variants_artifacts.py --prefix node_modules/ --yes

  # Clean up multiple prefixes (e.g. both v3.10.3 + v3.10.4 bugs)
  python scripts/_cleanup_variants_artifacts.py --prefix .pdfvenv/ --prefix node_modules/ --yes

The script:
  1. Backs up the DB to <db>.pre-prefix-cleanup
  2. Counts matches (artifact rows + audit log entries)
  3. Asks for confirmation (unless --yes / CLEANUP_YES=1)
  4. Deletes the matching rows
  5. Runs VACUUM to reclaim disk space

Safe to re-run (idempotent). Restoration: cp <backup> <db>.
"""
from __future__ import annotations

import argparse
import io
import shutil
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("C:/Users/stanley/.hermes-orchestrator/hermes-orch.db")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete artifacts matching a path prefix + their audit log entries."
    )
    parser.add_argument(
        "--prefix",
        action="append",
        required=True,
        help="Path prefix to match against artifacts.name (LIKE prefix%). "
             "Repeatable for multiple prefixes.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    else:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return 1

    backup = DB_PATH.with_suffix(".db.pre-prefix-cleanup")
    print(f"Backing up DB -> {backup}")
    shutil.copy2(DB_PATH, backup)
    print(f"  backup size: {backup.stat().st_size:,} bytes")

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # Build the WHERE clause for matching prefixes
    like_clauses = " OR ".join("name LIKE ?" for _ in args.prefix)
    like_params = [p + "%" for p in args.prefix]
    audit_like_clauses = " OR ".join(
        "json_extract(payload, '$.path') LIKE ?" for _ in args.prefix
    )

    # 1) Artifacts
    n_art = cur.execute(
        f"SELECT COUNT(*) FROM artifacts WHERE {like_clauses}", like_params
    ).fetchone()[0]
    print(f"\nArtifacts to delete: {n_art}")
    for r in cur.execute(
        f"SELECT id, project_id, name FROM artifacts WHERE {like_clauses} LIMIT 5",
        like_params,
    ).fetchall():
        print(f"  {r[0][:12]}  proj={r[1]}  {r[2]}")

    # 2) Audit log entries
    audit_params = [p + "%" for p in args.prefix]
    n_aud = cur.execute(
        f"SELECT COUNT(*) FROM audit_log WHERE event_type = 'artifact.registered' "
        f"AND ({audit_like_clauses})",
        audit_params,
    ).fetchone()[0]
    print(f"\nAudit log entries to delete: {n_aud}")

    cur.execute("SELECT COUNT(*) FROM artifacts")
    n_total_art = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM audit_log")
    n_total_aud = cur.fetchone()[0]
    print(f"\n  artifacts total before: {n_total_art}  ->  after: {n_total_art - n_art}")
    print(f"  audit_log total before: {n_total_aud}  ->  after: {n_total_aud - n_aud}")

    # 3) Confirm
    if not args.yes and not (sys.stdin and sys.stdin.isatty() and input("\nProceed? [y/N]: ").strip().lower() in ("y", "yes")):
        print("Aborted.")
        conn.close()
        return 0

    # 4) Delete
    cur.execute(f"DELETE FROM artifacts WHERE {like_clauses}", like_params)
    print(f"  deleted {cur.rowcount} artifact rows")
    cur.execute(
        f"DELETE FROM audit_log WHERE event_type = 'artifact.registered' AND ({audit_like_clauses})",
        audit_params,
    )
    print(f"  deleted {cur.rowcount} audit_log rows")
    conn.commit()

    # 5) VACUUM
    print("\nRunning VACUUM to reclaim space...")
    cur.execute("VACUUM")
    print("  done.")

    # 6) Verify
    cur.execute("SELECT COUNT(*) FROM artifacts")
    print(f"\nartifacts total after: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM audit_log")
    print(f"audit_log total after: {cur.fetchone()[0]}")

    conn.close()
    print(f"\nBackup at: {backup}")
    print("If anything looks wrong, restore with:")
    print(f"  cp {backup} {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
