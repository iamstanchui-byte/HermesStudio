"""Check status of one task."""
import sqlite3
import sys
from pathlib import Path

DB = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
task_id = sys.argv[1] if len(sys.argv) > 1 else "t-aeadd91a"

con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row
r = con.execute(
    "SELECT id, status, error, substr(result, 1, 200) as result_preview, "
    "last_liveness_at, updated_at FROM tasks WHERE id = ?",
    (task_id,),
).fetchone()
if r:
    print(f"  id: {r['id']}")
    print(f"  status: {r['status']}")
    print(f"  error: {r['error']}")
    print(f"  result_preview: {r['result_preview']}")
    print(f"  last_liveness: {r['last_liveness_at']}")
    print(f"  updated_at: {r['updated_at']}")
else:
    print(f"  not found: {task_id}")

# Also show recent audit events for this task
print()
print(f"--- recent audit for {task_id} ---")
for r in con.execute(
    "SELECT event_type, actor, created_at FROM audit_log WHERE task_id = ? ORDER BY id DESC LIMIT 5",
    (task_id,),
).fetchall():
    print(f"  {r['event_type']:30s} actor={r['actor']:20s} at={r['created_at']}")
