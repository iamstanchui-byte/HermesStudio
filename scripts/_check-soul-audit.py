"""Check audit log for recent SOUL preset attempts."""
import os
import sqlite3
db = sqlite3.connect(os.path.expanduser(r"~\.hermes-orchestrator\hermes-orch.db"))
db.row_factory = sqlite3.Row
print("=== audit (soul preset events, latest 10) ===")
for r in db.execute(
    "SELECT id, event_type, actor, created_at, substr(payload, 1, 200) as payload "
    "FROM audit_log WHERE event_type LIKE '%soul%' "
    "ORDER BY id DESC LIMIT 10"
):
    print(dict(r))
