"""Check if there are skill_submitted events being created in real time."""
import sqlite3, time
con = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=" * 70)
print("Latest 5 audit_log rows (any kind)")
print("=" * 70)
for r in cur.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 5"):
    print(f"  id={r['id']}  {r['created_at']}  {r['event_type']:30}  actor={r['actor']:20}  agent={r['agent_id']}")
    if r['payload']:
        print(f"    payload: {r['payload'][:200]}")

print()
print("=" * 70)
print("Latest 10 profile.skill_submitted events")
print("=" * 70)
for r in cur.execute("""
    SELECT * FROM audit_log WHERE event_type = 'profile.skill_submitted'
    ORDER BY id DESC LIMIT 10
"""):
    print(f"  id={r['id']}  {r['created_at']}  actor={r['actor']:20}  agent={r['agent_id']}")
    if r['payload']:
        print(f"    {r['payload'][:200]}")

print()
print("=" * 70)
print("Count of profile_configs created in last 10 min, by file_path")
print("=" * 70)
for r in cur.execute("""
    SELECT file_path, COUNT(*) AS n, MIN(created_at) AS first, MAX(created_at) AS last
    FROM profile_configs
    WHERE created_at > datetime('now', '-10 minutes')
    GROUP BY file_path
    ORDER BY n DESC
    LIMIT 20
"""):
    print(f"  {r['n']:3}  {r['file_path']:50}  first={r['first']}  last={r['last']}")

print()
print("=" * 70)
print("profile_configs created in last 10 min: who created them (no audit event_type = 'config_created' exists)")
print("=" * 70)
# Maybe the audit event_type is different. Let me list all unique event_types in last 10 min
for r in cur.execute("""
    SELECT event_type, COUNT(*) AS n
    FROM audit_log
    WHERE created_at > datetime('now', '-10 minutes')
    GROUP BY event_type
    ORDER BY n DESC
"""):
    print(f"  {r['n']:4}  {r['event_type']}")
