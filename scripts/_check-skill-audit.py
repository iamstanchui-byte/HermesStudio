"""Check recent profile_configs / audit_log entries for skills."""
import sqlite3
import os

db_path = os.path.expanduser(r"~\.hermes-orchestrator\hermes-orch.db")
db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

print("=== profile_configs (skills/) ===")
for r in db.execute(
    "SELECT id, file_path, status, error, desired_sha256, length(desired_content) as sz, created_at, applied_at "
    "FROM profile_configs WHERE file_path LIKE 'skills/%' ORDER BY created_at DESC LIMIT 10"
):
    print(dict(r))

print("\n=== audit_log (last 15) ===")
for r in db.execute(
    "SELECT id, event_type, actor, created_at, payload FROM audit_log ORDER BY id DESC LIMIT 15"
):
    d = dict(r)
    if d.get("payload"):
        d["payload"] = (d["payload"][:200] + "...") if len(d["payload"]) > 200 else d["payload"]
    print(d)
