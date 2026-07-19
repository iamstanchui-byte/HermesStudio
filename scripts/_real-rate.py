"""Find the actual rate of self-taught submissions using proper time filter."""
import sqlite3
from datetime import datetime, timedelta, timezone
con = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
con.row_factory = sqlite3.Row
cur = con.cursor()

# HKT = UTC+8
HKT = timezone(timedelta(hours=8))
now_hkt = datetime.now(HKT)
ten_min_ago = (now_hkt - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
print(f"now HKT: {now_hkt.isoformat()}")
print(f"filter: created_at >= '{ten_min_ago}'")
print()

print("=" * 70)
print("audit_log: events in last 10 min (HKT) by event_type")
print("=" * 70)
for r in cur.execute(f"""
    SELECT event_type, COUNT(*) AS n, MIN(created_at) AS first, MAX(created_at) AS last
    FROM audit_log
    WHERE created_at >= ?
    GROUP BY event_type
    ORDER BY n DESC
""", (ten_min_ago,)):
    print(f"  {r['n']:4}  {r['event_type']:40}  first={r['first']}  last={r['last']}")

print()
print("=" * 70)
print("profile_configs: rows in last 10 min (HKT) by file_path")
print("=" * 70)
for r in cur.execute(f"""
    SELECT file_path, COUNT(*) AS n, MIN(created_at) AS first, MAX(created_at) AS last
    FROM profile_configs
    WHERE created_at >= ?
    GROUP BY file_path
    ORDER BY n DESC
""", (ten_min_ago,)):
    print(f"  {r['n']:4}  {r['file_path']:50}  first={r['first']}  last={r['last']}")

print()
print("=" * 70)
print("Per-minute breakdown of profile.skill_submitted (last 15 min)")
print("=" * 70)
for r in cur.execute(f"""
    SELECT substr(created_at, 1, 16) AS minute, COUNT(*) AS n
    FROM audit_log
    WHERE event_type = 'profile.skill_submitted'
      AND created_at >= datetime(?, '-5 minutes')
    GROUP BY minute
    ORDER BY minute DESC
""", (ten_min_ago,)):
    print(f"  {r['minute']}  {r['n']:4}")
