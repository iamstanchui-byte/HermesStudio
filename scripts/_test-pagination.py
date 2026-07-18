"""Insert N fake tasks into a test project for pagination testing."""
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

db = str(Path.home() / ".hermes-orchestrator" / "hermes-orch.db")
n = int(sys.argv[1]) if len(sys.argv) > 1 else 50

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

# Find an existing project to use, or create one
pid = sys.argv[2] if len(sys.argv) > 2 else None
if not pid:
    # Use a project that already has the most tasks
    row = con.execute(
        "SELECT project_id, COUNT(*) as n FROM tasks GROUP BY project_id ORDER BY n DESC LIMIT 1"
    ).fetchone()
    pid = row["project_id"]
    print(f"using existing project: {pid} (currently {row['n']} tasks)")
else:
    print(f"using project: {pid}")

now = datetime.now(timezone.utc)
inserted = 0
for i in range(n):
    tid = "t-" + uuid.uuid4().hex[:8]
    created = (now - timedelta(minutes=i)).isoformat()
    con.execute(
        "INSERT INTO tasks (id, project_id, name, agent_role, status, depends_on, "
        "on_parent_failure, priority, retry_count, max_retries, timeout_seconds, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, pid, f"Bulk test task {i:03d}", "demo-runner",
         "completed" if i % 3 == 0 else "pending",
         json.dumps([]), "skip", "normal", 0, 2, 1800, created, created),
    )
    inserted += 1
con.commit()

total = con.execute(
    "SELECT COUNT(*) as n FROM tasks WHERE project_id = ?", (pid,)
).fetchone()["n"]
print(f"inserted {inserted} tasks; project now has {total} total tasks")
print(f"project_id={pid}")
