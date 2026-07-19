"""Debug: check what's actually in the DB for the failed test."""
import sqlite3, json
con = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=== super profile capabilities ===")
for r in cur.execute("SELECT id, name, capabilities FROM agent_profiles WHERE name='super'"):
    print(f"  id={r['id']}  name={r['name']}  caps={r['capabilities']!r}")

print()
print("=== latest task with required_capability set ===")
for r in cur.execute("""
    SELECT id, name, agent_role, required_capability, status, error, assigned_profile_id
    FROM tasks WHERE required_capability IS NOT NULL
    ORDER BY created_at DESC LIMIT 5
"""):
    print(f"  id={r['id']}  role={r['agent_role']}  req_cap={r['required_capability']}  status={r['status']}  err={r['error']}  prof={r['assigned_profile_id']}")

print()
print("=== audit events for the latest test task ===")
tid = cur.execute("SELECT id FROM tasks WHERE required_capability IS NOT NULL ORDER BY created_at DESC LIMIT 1").fetchone()['id']
print(f"  task id: {tid}")
for r in cur.execute(f"SELECT event_type, actor, payload FROM audit_log WHERE task_id='{tid}' ORDER BY id"):
    p = (r['payload'] or '')[:200]
    print(f"  {r['event_type']:30}  {r['actor']:25}  {p}")
