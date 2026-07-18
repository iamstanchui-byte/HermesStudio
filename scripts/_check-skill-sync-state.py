"""Check current state of skills in DB."""
import os
import sqlite3

db_path = os.path.expanduser(r"~\.hermes-orchestrator\hermes-orch.db")
db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

print("=== profile_configs (skills/ + sync marker) ===")
for r in db.execute(
    "SELECT id, file_path, status, length(desired_content) as sz, created_at, applied_at, error "
    "FROM profile_configs WHERE file_path LIKE 'skills/%' OR file_path = '__sync_skills__' "
    "ORDER BY created_at DESC LIMIT 8"
):
    print(dict(r))

print("\n=== audit_log (skill/sync events, latest 10) ===")
for r in db.execute(
    "SELECT id, event_type, actor, created_at, substr(payload, 1, 180) as payload "
    "FROM audit_log WHERE event_type LIKE '%skill%' OR event_type LIKE '%sync%' "
    "ORDER BY id DESC LIMIT 10"
):
    print(dict(r))
