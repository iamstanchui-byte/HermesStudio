"""Delete orphan tasks (assigned to deleted agents)."""
import sqlite3
from pathlib import Path

DB = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
con = sqlite3.connect(str(DB))

# Find orphans
orphans = con.execute("""
    SELECT t.id, t.status, t.assigned_agent_id
    FROM tasks t LEFT JOIN agents a ON t.assigned_agent_id = a.id
    WHERE t.assigned_agent_id IS NOT NULL AND a.id IS NULL
""").fetchall()
print(f"Found {len(orphans)} orphan tasks")

# Show breakdown by status
from collections import Counter
by_status = Counter(r[1] for r in orphans)
print(f"By status: {dict(by_status)}")

# Delete
if orphans:
    ids = tuple(r[0] for r in orphans)
    placeholders = ",".join("?" * len(ids))
    cur = con.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", ids)
    con.commit()
    print(f"Deleted {cur.rowcount} tasks")

# Verify
remaining = con.execute("""
    SELECT COUNT(*) FROM tasks t LEFT JOIN agents a ON t.assigned_agent_id = a.id
    WHERE t.assigned_agent_id IS NOT NULL AND a.id IS NULL
""").fetchone()[0]
print(f"Remaining orphan tasks: {remaining}")

# Show what tasks remain
print()
print("--- remaining tasks by status ---")
for r in con.execute("SELECT status, COUNT(*) as n FROM tasks GROUP BY status ORDER BY n DESC").fetchall():
    print(f"  {r[0]:15s}  {r[1]}")
