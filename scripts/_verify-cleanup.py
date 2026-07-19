"""Verify the fix: file sizes stable, audit log has skill_submitted events."""
import sqlite3
from datetime import datetime, timedelta, timezone
HKT = timezone(timedelta(hours=8))
con = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
con.row_factory = sqlite3.Row
cur = con.cursor()

# Check audit_log for skill_submitted events in last 5 min
since = (datetime.now(HKT) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
print("=" * 70)
print(f"profile.skill_submitted events since {since}")
print("=" * 70)
for r in cur.execute(f"""
    SELECT created_at, event_type, actor, agent_id,
           substr(payload, 1, 200) AS payload
    FROM audit_log
    WHERE event_type = 'profile.skill_submitted' AND created_at >= ?
    ORDER BY created_at DESC LIMIT 10
""", (since,)):
    print(f"  {r['created_at']}  actor={r['actor']:25}  agent={r['agent_id']}")
    print(f"    {r['payload']}")

print()
print("=" * 70)
print("Latest profile_configs for the 2 fixed skills (last 30 min)")
print("=" * 70)
for r in cur.execute("""
    SELECT pc.id, pc.file_path, pc.status, pc.desired_sha256, pc.created_at, pc.applied_at,
           ap.name AS profile_name
    FROM profile_configs pc
    JOIN agent_profiles ap ON ap.id = pc.profile_id
    WHERE pc.file_path IN ('skills/computer-use/SKILL.md', 'skills/hk-weather/SKILL.md')
      AND pc.created_at > datetime('now', '-30 minutes')
    ORDER BY pc.created_at DESC LIMIT 8
"""):
    print(f"  {r['created_at']}  {r['profile_name']:12}  {r['file_path']:35}  {r['status']:8}  desired_sha={r['desired_sha256'][:12]}")
