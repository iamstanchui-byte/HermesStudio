"""Mark a task as failed (for manual cleanup)."""
import sqlite3
import sys
from pathlib import Path

DB = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
task_id = sys.argv[1] if len(sys.argv) > 1 else "t-fd1bff2d"
reason = sys.argv[2] if len(sys.argv) > 2 else "daemon killed mid-task"

con = sqlite3.connect(str(DB))
con.execute(
    "UPDATE tasks SET status = 'failed', error = ? WHERE id = ? AND status IN ('running','assigned','pending')",
    (reason, task_id),
)
con.commit()
print(f"marked {task_id} as failed (reason: {reason})")
