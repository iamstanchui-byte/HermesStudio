"""Check audit log for iteration events."""
import os
import sqlite3
db = sqlite3.connect(os.path.expanduser(r"~\.hermes-orchestrator\hermes-orch.db"))
db.row_factory = sqlite3.Row
print("=== audit (iteration events) ===")
for r in db.execute(
    "SELECT id, event_type, actor, created_at, substr(payload, 1, 180) as payload "
    "FROM audit_log WHERE event_type LIKE '%iteration%' OR event_type='project.completed' "
    "ORDER BY id DESC LIMIT 10"
):
    print(dict(r))
