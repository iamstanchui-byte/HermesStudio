import sqlite3
conn = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Check tasks schema
print("=== TASKS schema ===")
c.execute("PRAGMA table_info(tasks)")
for r in c.fetchall():
    print(f"  {r['name']} {r['type']}")

print()
print("=== TASKS for proj-48b50520 (using correct columns) ===")
c.execute("SELECT id, name, agent_role, status, created_at, updated_at FROM tasks WHERE project_id = ? ORDER BY created_at", ("proj-48b50520",))
for r in c.fetchall():
    print(f"  [{r['id'][:8]}] {r['name']}")
    print(f"    role={r['agent_role']} status={r['status']}")
    print(f"    created={r['created_at']} updated={r['updated_at']}")

print()
print("=== AUDIT (latest 40) ===")
c.execute("SELECT id, event_type, actor, created_at, payload_json FROM audit_log WHERE project_id = ? ORDER BY created_at DESC LIMIT 40", ("proj-48b50520",))
for r in c.fetchall():
    payload = (r["payload_json"] or "")[:200]
    print(f"  [{r['created_at']}] {r['event_type']:35s} | actor={r['actor']:25s} | {payload}")
