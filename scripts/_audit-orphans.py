"""Audit tasks for orphan references to deleted agents."""
import sqlite3
from pathlib import Path

DB = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row

# Orphan tasks
orphans = con.execute("""
    SELECT t.id, t.name, t.status, t.assigned_agent_id
    FROM tasks t LEFT JOIN agents a ON t.assigned_agent_id = a.id
    WHERE t.assigned_agent_id IS NOT NULL AND a.id IS NULL
""").fetchall()
print(f"Orphan tasks (assigned to deleted agents): {len(orphans)}")
for o in orphans[:10]:
    print(f"  {o['id']}  status={o['status']:12s}  agent={o['assigned_agent_id']}  name={o['name']}")

# Active projects
print()
print("Active projects (planning/ready/running):")
for r in con.execute("""
    SELECT id, name, state FROM projects
    WHERE state IN ('planning','ready','running')
    ORDER BY created_at DESC LIMIT 10
""").fetchall():
    print(f"  {r['id']}  state={r['state']:10s}  name={r['name']}")

# Profile count for win-local-1
print()
print("win-local-1 profile status:")
for r in con.execute("""
    SELECT ap.name, ap.status FROM agent_profiles ap
    WHERE ap.agent_id = 'win-local-1'
""").fetchall():
    print(f"  {r['name']}  status={r['status']}")
