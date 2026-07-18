import sqlite3
conn = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

print("=== PROJECT proj-48b50520 ===")
c.execute("SELECT id, name, state, current_iteration, max_iterations, last_iteration_summary, coordinator_role, deliverable_path, updated_at FROM projects WHERE id = ?", ("proj-48b50520",))
row = c.fetchone()
if row:
    for k in row.keys():
        v = row[k]
        if v and len(str(v)) > 300:
            v = str(v)[:300] + "..."
        print(f"  {k}: {v!r}")

print()
print("=== TASKS (all, ordered) ===")
c.execute("SELECT id, name, agent_role, state, status, created_at, started_at, completed_at FROM tasks WHERE project_id = ? ORDER BY created_at", ("proj-48b50520",))
for r in c.fetchall():
    print(f"  [{r['id'][:8]}] {r['name']}")
    print(f"    role={r['agent_role']} state={r['state']} status={r['status']}")
    print(f"    created={r['created_at']} started={r['started_at']} completed={r['completed_at']}")

print()
print("=== AUDIT (latest 25) ===")
c.execute("SELECT id, event_type, actor, created_at, payload_json FROM audit_log WHERE project_id = ? ORDER BY created_at DESC LIMIT 25", ("proj-48b50520",))
for r in c.fetchall():
    payload = (r["payload_json"] or "")[:150]
    print(f"  [{r['created_at']}] {r['event_type']} | actor={r['actor']} | {payload}")
