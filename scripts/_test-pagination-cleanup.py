"""Clean up bulk-inserted test tasks for a project."""
import sqlite3
import sys
from pathlib import Path

db = str(Path.home() / ".hermes-orchestrator" / "hermes-orch.db")
pid = sys.argv[1] if len(sys.argv) > 1 else "proj-7aa8665b"

con = sqlite3.connect(db)
n = con.execute(
    "DELETE FROM tasks WHERE project_id = ? AND name LIKE 'Bulk test task%'",
    (pid,),
).rowcount
con.commit()
print(f"deleted {n} bulk test tasks from {pid}")
