"""Was the NameError actually happening? Check if there were self-taught events between 18:40 and 19:37."""
import sqlite3
con = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=" * 70)
print("Per-minute profile.skill_submitted events from 18:30 to 19:40")
print("=" * 70)
for r in cur.execute("""
    SELECT substr(created_at, 1, 16) AS minute,
           COUNT(*) AS n,
           GROUP_CONCAT(DISTINCT agent_id) AS agents
    FROM audit_log
    WHERE event_type = 'profile.skill_submitted'
      AND created_at BETWEEN '2026-07-19T18:30' AND '2026-07-19T19:40'
    GROUP BY minute
    ORDER BY minute
"""):
    print(f"  {r['minute']}  {r['n']:4}  agents={r['agents']}")

print()
print("=" * 70)
print("If gap between 18:40 and 19:37 = 0 events, NameError was confirmed")
print("If events in that gap = NameError was NOT happening, my claim was wrong")
print("=" * 70)
gap = cur.execute("""
    SELECT COUNT(*) AS n
    FROM audit_log
    WHERE event_type = 'profile.skill_submitted'
      AND created_at > '2026-07-19T18:41' AND created_at < '2026-07-19T19:37'
""").fetchone()
print(f"  Gap events (18:41 - 19:37): {gap['n']}")

# Also check Linux wrapper status now
print()
print("=" * 70)
print("Linux wrapper self-taught submissions in last 24h (per agent_id)")
print("=" * 70)
for r in cur.execute("""
    SELECT agent_id, COUNT(*) AS n, MAX(created_at) AS last
    FROM audit_log
    WHERE event_type = 'profile.skill_submitted'
      AND created_at > datetime('now', '-1 day')
    GROUP BY agent_id
"""):
    print(f"  agent={r['agent_id']:15}  n={r['n']:5}  last={r['last']}")

# Check Linux profile_configs creation rate
print()
print("=" * 70)
print("Linux profile_configs created in last 2h (per file_path)")
print("=" * 70)
for r in cur.execute("""
    SELECT pc.file_path, COUNT(*) AS n, MIN(pc.created_at) AS first, MAX(pc.created_at) AS last
    FROM profile_configs pc
    JOIN agent_profiles ap ON ap.id = pc.profile_id
    WHERE ap.agent_id = 'linux-a-01'
      AND pc.created_at > datetime('now', '-2 hours')
    GROUP BY pc.file_path
    ORDER BY n DESC
"""):
    print(f"  {r['n']:4}  {r['file_path']:50}  first={r['first']}  last={r['last']}")
