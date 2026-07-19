"""Find what creates the recent pending rows."""
import sqlite3
con = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=" * 70)
print("Latest 10 profile_configs rows (any status)")
print("=" * 70)
for r in cur.execute("""
    SELECT id, profile_id, file_path, status, error,
           desired_sha256, created_at, applied_at
    FROM profile_configs
    ORDER BY created_at DESC LIMIT 10
"""):
    print(f"  {r['created_at']}  {r['file_path']:50}  {r['status']:10}  err={r['error']}")
    print(f"    desired_sha={r['desired_sha256'][:12] if r['desired_sha256'] else None}  applied_at={r['applied_at']}")

print()
print("=" * 70)
print("Latest 5 acks — actual_sha256 from audit log")
print("=" * 70)
for r in cur.execute("""
    SELECT al.created_at, al.payload, pc.file_path, pc.desired_sha256
    FROM audit_log al
    LEFT JOIN profile_configs pc ON pc.id = json_extract(al.payload, '$.config_id')
    WHERE al.event_type = 'profile.config_acked'
      AND al.agent_id = 'win-local-1'
    ORDER BY al.created_at DESC LIMIT 5
"""):
    import json
    p = json.loads(r['payload']) if r['payload'] else {}
    print(f"  {r['created_at']}  {r['file_path']:50}")
    print(f"    desired_sha={r['desired_sha256'][:12] if r['desired_sha256'] else None}  actual_sha={p.get('actual_sha256', '')[:12]}")

print()
print("=" * 70)
print("Last 30 audit_log events (any type) — what is creating config rows?")
print("=" * 70)
for r in cur.execute("""
    SELECT created_at, event_type, actor, agent_id,
           substr(payload, 1, 300) AS payload
    FROM audit_log
    WHERE agent_id = 'win-local-1'
    ORDER BY created_at DESC LIMIT 30
"""):
    print(f"  {r['created_at']}  {r['event_type']:30}  actor={r['actor']:25}  {r['payload']}")
