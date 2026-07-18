"""Quick verification of profile_configs and related audit events."""
import sqlite3
from pathlib import Path

DB = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row

print("--- profile_configs rows ---")
for r in con.execute(
    "SELECT id, status, file_path, substr(desired_content,1,40) as preview, applied_at "
    "FROM profile_configs ORDER BY created_at DESC"
):
    print(f"  {r['id'][:12]:14s} {r['status']:10s} {r['file_path']:30s} {r['preview']!r:50s} applied={r['applied_at'] or '-'}")

print()
print("--- profile.* audit events (last 10) ---")
for r in con.execute(
    "SELECT event_type, actor, created_at FROM audit_log "
    "WHERE event_type LIKE 'profile.%' ORDER BY id DESC LIMIT 10"
):
    print(f"  {r['event_type']:30s} actor={r['actor'] or '?':10s} at={r['created_at']}")
