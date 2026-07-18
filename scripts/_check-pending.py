"""Show current pending profile_configs for user to pick up."""
import sqlite3
from pathlib import Path

con = sqlite3.connect(str(Path.home() / ".hermes-orchestrator" / "hermes-orch.db"))
con.row_factory = sqlite3.Row

print("--- pending configs (waiting for wrapper to pick up) ---")
rows = con.execute(
    "SELECT pc.id, pc.file_path, pc.desired_content, pc.created_at, "
    "ap.agent_id, ap.name as profile_name "
    "FROM profile_configs pc "
    "JOIN agent_profiles ap ON pc.profile_id = ap.id "
    "WHERE pc.status = 'pending' "
    "ORDER BY pc.created_at DESC"
).fetchall()

if not rows:
    print("  none")
else:
    for r in rows:
        print(f"  agent={r['agent_id']:15s} profile={r['profile_name']:20s} file={r['file_path']:30s}")
        print(f"    config_id={r['id']}")
        print(f"    created={r['created_at']}")
        print(f"    content_preview={r['desired_content'][:80]!r}")
        print()

print("--- recent applying (in flight) ---")
for r in con.execute(
    "SELECT pc.id, pc.file_path, pc.created_at, ap.agent_id, ap.name as profile_name "
    "FROM profile_configs pc "
    "JOIN agent_profiles ap ON pc.profile_id = ap.id "
    "WHERE pc.status = 'applying' "
    "ORDER BY pc.created_at DESC"
).fetchall():
    print(f"  agent={r['agent_id']:15s} profile={r['profile_name']:20s} file={r['file_path']}")
