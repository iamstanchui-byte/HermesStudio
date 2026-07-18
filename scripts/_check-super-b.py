"""Check if super-b profile is linked up to the agent and the wrapper."""
import sys
import sqlite3
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

conn = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== agents ===")
for r in cur.execute("SELECT id, os_type, last_heartbeat_at FROM agents ORDER BY id"):
    print(f"  {r['id']:18s} {r['os_type'] or '?':10s} hb_at={r['last_heartbeat_at'] or '-'}")

print()
print("=== agent_profiles ===")
for r in cur.execute("""
    SELECT ap.id, ap.name, ap.agent_id, ap.status, ap.created_at
    FROM agent_profiles ap
    ORDER BY ap.agent_id, ap.name
"""):
    print(f"  agent_id={r['agent_id']:18s}  /  {r['name']:15s} (id={r['id'][:8]})  status={r['status']}")

print()
print("=== profile_configs status counts ===")
for r in cur.execute("""
    SELECT ap.name AS profile, ap.agent_id, pc.status, COUNT(*) AS n
    FROM profile_configs pc
    JOIN agent_profiles ap ON pc.profile_id = ap.id
    GROUP BY ap.name, ap.agent_id, pc.status
    ORDER BY ap.agent_id, ap.name, pc.status
"""):
    print(f"  agent={r['agent_id']:18s} / {r['profile']:15s}  {r['status']:10s} count={r['n']}")
