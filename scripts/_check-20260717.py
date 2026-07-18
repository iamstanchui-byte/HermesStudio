"""Check 20260717 project state and history."""
import os
import sqlite3
db_path = os.path.expanduser(r"~\.hermes-orchestrator\hermes-orch.db")
db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

print("=== project 20260717 (proj-52f9c897) ===")
for r in db.execute("SELECT id, name, state, goal, created_at, updated_at FROM projects WHERE name='20260717'"):
    print(dict(r))

print("\n=== tasks for proj-52f9c897 ===")
for r in db.execute("SELECT id, name, agent_role, status, depends_on FROM tasks WHERE project_id='proj-52f9c897'"):
    print(dict(r))

print("\n=== audit log for proj-52f9c897 ===")
for r in db.execute("SELECT id, event_type, actor, created_at, substr(payload, 1, 250) as payload FROM audit_log WHERE project_id='proj-52f9c897' ORDER BY id DESC LIMIT 20"):
    print(dict(r))
