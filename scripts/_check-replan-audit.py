"""Check audit log for replan events + planner result."""
import os
import sqlite3

db_path = os.path.expanduser(r"~\.hermes-orchestrator\hermes-orch.db")
db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

print("=== audit log (project.replan / project.plan / planner) ===")
for r in db.execute(
    "SELECT id, event_type, actor, created_at, substr(payload, 1, 200) as payload "
    "FROM audit_log WHERE event_type LIKE '%plan%' OR event_type LIKE '%replan%' "
    "ORDER BY id DESC LIMIT 10"
):
    print(dict(r))

print("\n=== tasks for proj-8b35fe88 ===")
for r in db.execute(
    "SELECT id, name, agent_role, status, depends_on FROM tasks WHERE project_id='proj-8b35fe88'"
):
    print(dict(r))
