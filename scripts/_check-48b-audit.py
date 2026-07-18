"""Check audit log for proj-48b50520."""
import os
import sqlite3
db = sqlite3.connect(os.path.expanduser(r"~\.hermes-orchestrator\hermes-orch.db"))
db.row_factory = sqlite3.Row
print("=== audit (last 15 events for proj-48b50520) ===")
for r in db.execute(
    "SELECT id, event_type, actor, created_at, substr(payload, 1, 200) as payload "
    "FROM audit_log WHERE project_id='proj-48b50520' "
    "ORDER BY id DESC LIMIT 15"
):
    print(dict(r))
