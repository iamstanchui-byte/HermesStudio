"""One-shot cleanup: remove duplicate artifact rows.

Background: a bug in agent_cli.py's auto-upload loop (fixed 2026-07-22,
commit pending) extended `result["artifacts"]` twice, so each file
appeared twice in the /result body. The server's /result endpoint
iterated body.artifacts and inserted one artifact row per entry, so
each file got 2-3 duplicate rows in the artifacts table.

This script keeps the OLDEST row per (task_id, name, checksum) and
deletes the rest, plus the matching audit_log events. Idempotent:
re-running on a clean DB is a no-op.

Before/after counts are printed so the operator can sanity-check.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")


def main() -> int:
    if not DB.exists():
        print(f"DB not found: {DB}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    before_total = cur.execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()["n"]
    before_audit = cur.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'artifact.registered'"
    ).fetchone()["n"]
    print(f"Before: artifacts={before_total}  audit events={before_audit}")

    # Find duplicate (task_id, name, checksum) groups with > 1 row
    dup_groups = cur.execute(
        """
        SELECT task_id, name, checksum, GROUP_CONCAT(id) AS ids,
               MIN(created_at) AS first_at,
               COUNT(*) AS n
        FROM artifacts
        GROUP BY task_id, name, checksum
        HAVING n > 1
        """
    ).fetchall()
    print(f"  duplicate groups: {len(dup_groups)} ({(sum(g['n'] for g in dup_groups) - len(dup_groups))} extra rows)")

    # For each group, keep the row whose created_at == MIN, delete the rest
    # Plus match the audit_log event by payload: the audit event stores
    # sha256[:12] in the payload, so we match on the first 12 chars.
    artifact_rows_deleted = 0
    audit_rows_deleted = 0
    for g in dup_groups:
        ids = g["ids"].split(",")
        # Find the kept id (oldest)
        first_id = cur.execute(
            "SELECT id FROM artifacts WHERE id IN ({}) ORDER BY created_at ASC LIMIT 1".format(
                ",".join("?" for _ in ids)
            ),
            ids,
        ).fetchone()["id"]
        # Delete the rest
        rest_ids = [i for i in ids if i != first_id]
        cur.execute(
            "DELETE FROM artifacts WHERE id IN ({})".format(",".join("?" for _ in rest_ids)),
            rest_ids,
        )
        artifact_rows_deleted += cur.rowcount
        # Match audit_log events: payload->>'$.sha256' starts with the same
        # 12 chars, payload->>'$.path' = g['name'], task_id = g['task_id']
        # Keep the first one (oldest by created_at), delete the rest.
        full_sha = g["checksum"]
        sha12 = full_sha[:12]
        audit_ids = [
            r["id"]
            for r in cur.execute(
                """
                SELECT id FROM audit_log
                WHERE event_type = 'artifact.registered'
                  AND task_id = ?
                  AND json_extract(payload, '$.path') = ?
                  AND substr(json_extract(payload, '$.sha256'), 1, 12) = ?
                ORDER BY created_at ASC
                """,
                (g["task_id"], g["name"], sha12),
            ).fetchall()
        ]
        if len(audit_ids) > 1:
            # Keep first, delete the rest
            rest_audit = audit_ids[1:]
            cur.execute(
                "DELETE FROM audit_log WHERE id IN ({})".format(
                    ",".join("?" for _ in rest_audit)
                ),
                rest_audit,
            )
            audit_rows_deleted += cur.rowcount

    conn.commit()

    after_total = cur.execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()["n"]
    after_audit = cur.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = 'artifact.registered'"
    ).fetchone()["n"]
    print(f"After:  artifacts={after_total}  audit events={after_audit}")
    print(f"Deleted: artifacts -={artifact_rows_deleted}  audit events -={audit_rows_deleted}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
