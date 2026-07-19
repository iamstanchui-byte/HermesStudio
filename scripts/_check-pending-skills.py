"""Diagnostic: check why specific skills show 'pending' on the dashboard."""
import sqlite3
from pathlib import Path

DB = r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=" * 60)
print("PENDING SKILL ROWS (latest 10)")
print("=" * 60)
for r in cur.execute("""
    SELECT pc.id, pc.profile_id, pc.file_path, pc.status, pc.error,
           pc.applied_at, pc.created_at, pc.desired_sha256,
           substr(coalesce(pc.desired_content, ''), 1, 80) AS content_preview
    FROM profile_configs pc
    WHERE pc.file_path LIKE 'skills/%/SKILL.md'
      AND pc.status IN ('pending', 'applying')
    ORDER BY pc.created_at DESC LIMIT 10
"""):
    print(f"  {r['created_at']}  {r['profile_id'][:12]}  {r['status']:8}  {r['file_path']}")
    print(f"    sha={r['desired_sha256'][:8]}  err={r['error']}  applied_at={r['applied_at']}")
    print(f"    content: {r['content_preview']!r}")

print()
print("=" * 60)
print("computer-use win-agent02 (latest 5 rows)")
print("=" * 60)
for r in cur.execute("""
    SELECT pc.id, pc.status, pc.error, pc.applied_at, pc.created_at,
           pc.desired_sha256,
           length(coalesce(pc.desired_content, '')) AS sz
    FROM profile_configs pc
    JOIN agent_profiles ap ON ap.id = pc.profile_id
    WHERE ap.agent_id = 'win-local-1' AND ap.name = 'win-agent02'
      AND pc.file_path = 'skills/computer-use/SKILL.md'
    ORDER BY pc.created_at DESC LIMIT 5
"""):
    print(f"  {r['created_at']}  {r['status']:8}  sz={r['sz']:6}  sha={r['desired_sha256'][:8]}")
    print(f"    applied_at={r['applied_at']}  err={r['error']}")

print()
print("=" * 60)
print("hk-weather win-agent01 (latest 5 rows)")
print("=" * 60)
for r in cur.execute("""
    SELECT pc.id, pc.status, pc.error, pc.applied_at, pc.created_at,
           pc.desired_sha256,
           length(coalesce(pc.desired_content, '')) AS sz
    FROM profile_configs pc
    JOIN agent_profiles ap ON ap.id = pc.profile_id
    WHERE ap.agent_id = 'win-local-1' AND ap.name = 'win-agent01'
      AND pc.file_path = 'skills/hk-weather/SKILL.md'
    ORDER BY pc.created_at DESC LIMIT 5
"""):
    print(f"  {r['created_at']}  {r['status']:8}  sz={r['sz']:6}  sha={r['desired_sha256'][:8]}")
    print(f"    applied_at={r['applied_at']}  err={r['error']}")

print()
print("=" * 60)
print("Audit log: recent skill-related events (last 30 min)")
print("=" * 60)
for r in cur.execute("""
    SELECT ts, event_type, actor, project_id, task_id,
           substr(json_extract(payload, '$'), 1, 200) AS payload
    FROM audit_log
    WHERE event_type LIKE '%skill%' OR event_type LIKE '%config%'
    ORDER BY ts DESC LIMIT 20
"""):
    print(f"  {r['ts']}  {r['event_type']:30}  actor={r['actor']}")
    print(f"    payload: {r['payload']}")

print()
print("=" * 60)
print("Wrapper state: pending configs that wrapper hasn't claimed")
print("=" * 60)
for r in cur.execute("""
    SELECT pc.id, pc.profile_id, pc.file_path, pc.status, pc.created_at,
           ap.agent_id, ap.name AS profile_name
    FROM profile_configs pc
    JOIN agent_profiles ap ON ap.id = pc.profile_id
    WHERE pc.status IN ('pending', 'applying')
      AND pc.file_path NOT LIKE 'skills/%'
    ORDER BY pc.created_at DESC LIMIT 10
"""):
    print(f"  {r['created_at']}  agent={r['agent_id']}  profile={r['profile_name']}  {r['file_path']}  {r['status']}")
