"""Final status summary."""
import sqlite3
from pathlib import Path

DB = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row

print("=== tasks by status ===")
for r in con.execute("SELECT status, COUNT(*) as n FROM tasks GROUP BY status ORDER BY n DESC").fetchall():
    print(f"  {r['status']:15s}  {r['n']}")

print()
print("=== recently touched tasks (last 3 minutes) ===")
for r in con.execute("""
    SELECT id, status, agent_role, assigned_agent_id, name, updated_at
    FROM tasks
    WHERE updated_at > datetime('now', '-3 minutes')
    ORDER BY updated_at DESC
""").fetchall():
    print(f"  {r['id']}  {r['status']:12s}  role={r['agent_role']:15s}  agent={r['assigned_agent_id']}  name={r['name']}")

print()
print("=== projects by state ===")
for r in con.execute("SELECT state, COUNT(*) as n FROM projects GROUP BY state").fetchall():
    print(f"  {r['state']:12s}  {r['n']}")
