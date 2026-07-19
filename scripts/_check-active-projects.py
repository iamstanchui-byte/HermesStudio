"""Check current active projects and daemon state."""
import sqlite3
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row

print("=== Active projects (not terminal) ===")
rows = list(c.execute("SELECT id, name, state, current_iteration, max_iterations FROM projects WHERE state NOT IN ('completed','failed','cancelled') ORDER BY created_at DESC LIMIT 10").fetchall())
for r in rows:
    print(f"  {r['id']:18s} {r['state']:10s} iter={r['current_iteration']}/{r['max_iterations']} {r['name']}")

print()
print("=== Agent profiles ===")
rows = list(c.execute("SELECT id, agent_id, name, status, current_task_id, updated_at FROM agent_profiles").fetchall())
for r in rows:
    print(f"  {r['id']:30s} {r['agent_id']:15s} {r['status']:8s} task={r['current_task_id']}")

print()
print("=== Recent tasks (last 5) ===")
rows = list(c.execute("SELECT id, project_id, name, agent_role, status, created_at FROM tasks ORDER BY created_at DESC LIMIT 5").fetchall())
for r in rows:
    print(f"  {r['id']:30s} {r['project_id']:18s} {r['status']:10s} {r['name']}")
