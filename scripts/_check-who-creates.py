"""Find who is creating the pending skill rows."""
import sqlite3
con = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=" * 70)
print("All event_types in audit_log (with counts, last 24h)")
print("=" * 70)
for r in cur.execute("""
    SELECT event_type, actor, COUNT(*) AS n, MAX(created_at) AS last
    FROM audit_log
    WHERE created_at > datetime('now', '-1 day')
    GROUP BY event_type, actor
    ORDER BY n DESC
"""):
    print(f"  {r['n']:5}  actor={r['actor']:20}  {r['event_type']:40}  last={r['last']}")

print()
print("=" * 70)
print("Non-claimed/acked events between 19:20 and 19:22")
print("=" * 70)
for r in cur.execute("""
    SELECT * FROM audit_log
    WHERE created_at BETWEEN '2026-07-19T19:20:00' AND '2026-07-19T19:22:00'
      AND event_type NOT IN ('profile.config_claimed', 'profile.config_acked')
    ORDER BY created_at
"""):
    print(f"  {r['created_at']}  {r['event_type']:30}  actor={r['actor']}  agent={r['agent_id']}")
    if r['payload']:
        print(f"    payload: {r['payload'][:300]}")

print()
print("=" * 70)
print("All events in last 5 min (any kind)")
print("=" * 70)
for r in cur.execute("""
    SELECT created_at, event_type, actor, agent_id,
           substr(payload, 1, 200) AS payload
    FROM audit_log
    WHERE created_at > datetime('now', '-5 minutes')
      AND event_type NOT IN ('profile.config_claimed', 'profile.config_acked')
    ORDER BY created_at DESC
    LIMIT 30
"""):
    print(f"  {r['created_at']}  {r['event_type']:30}  actor={r['actor']:12}  agent={r['agent_id']}")
    if r['payload']:
        print(f"    {r['payload']}")
