"""Review the 2 Phase3 PlannerTest projects."""
import sqlite3, json
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row
for pid in ['proj-e37455d2', 'proj-6213a9f0']:
    print(f'=== {pid} ===')
    for r in c.execute('SELECT id, name, state, current_iteration, max_iterations, coordinator_role, created_at FROM projects WHERE id = ?', (pid,)):
        print(f'  state: {r["state"]}  iter: {r["current_iteration"]}/{r["max_iterations"]}  coord: {r["coordinator_role"]}')
        print(f'  name: {r["name"]}')
    for t in c.execute('SELECT id, name, status, agent_role, assigned_agent_id, error FROM tasks WHERE project_id = ?', (pid,)):
        err = (t['error'] or '')[:60] if t['error'] else ''
        agent = t['assigned_agent_id'] or '-'
        print(f'  task {t["id"]}: {t["status"]:10s} role={t["agent_role"]:8s} agent={agent:15s} {t["name"]} {err}')
    # audit for that project
    for a in c.execute("SELECT event_type, actor, created_at FROM audit_log WHERE project_id = ? ORDER BY created_at DESC LIMIT 5", (pid,)):
        print(f'  audit {a["created_at"]} {a["event_type"]} actor={a["actor"]}')
    print()
