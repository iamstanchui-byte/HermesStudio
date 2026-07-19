"""Trace who creates the 19:30:51 hk-weather row."""
import sqlite3, json
con = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=== profile_configs rows at 19:30:51 (hk-weather) and 19:30:54 (computer-use) ===")
for ts in ('2026-07-19T19:30:51.927530+08:00', '2026-07-19T19:30:54.092156+08:00'):
    r = cur.execute("SELECT * FROM profile_configs WHERE created_at=?", (ts,)).fetchone()
    if r:
        print(f"  id={r['id']}  {r['file_path']:50}  status={r['status']}  desired_sha={r['desired_sha256'][:12]}")

print()
print("=== ALL events in audit_log within 30 sec of 19:30:51 (any type) ===")
for r in cur.execute("""
    SELECT * FROM audit_log
    WHERE created_at BETWEEN '2026-07-19T19:30:20' AND '2026-07-19T19:31:10'
    ORDER BY created_at
"""):
    print(f"  {r['created_at']}  {r['event_type']:30}  actor={r['actor']:25}  agent={r['agent_id']}")
    if r['payload']:
        print(f"    {r['payload'][:250]}")

print()
print("=== profile_configs created in this window ===")
for r in cur.execute("""
    SELECT id, file_path, status, created_at, applied_at, desired_sha256
    FROM profile_configs
    WHERE created_at BETWEEN '2026-07-19T19:30:20' AND '2026-07-19T19:31:10'
    ORDER BY created_at
"""):
    print(f"  id={r['id'][:8]}  {r['file_path']:50}  {r['status']:10}  created={r['created_at']}  applied={r['applied_at']}")
