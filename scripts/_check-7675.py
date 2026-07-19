"""Check proj-767595df raw result data to diagnose Bug 1 + Bug 2."""
import sqlite3, json
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row
print("=== tasks (proj-767595df) ===")
rows = list(c.execute("SELECT id, name, status, agent_role, result FROM tasks WHERE project_id = 'proj-767595df' ORDER BY created_at").fetchall())
for r in rows:
    print(f"\n--- {r['id']} {r['name']} ({r['status']}, role={r['agent_role']}) ---")
    if r['result']:
        data = json.loads(r['result'])
        s = data.get('summary', '')
        print(f"summary first 200: {s[:200]!r}")
        print(f"summary contains 'Query:': {'Query:' in s}")
        print(f"summary contains '--- PROJECT CONTEXT ---': {'--- PROJECT CONTEXT ---' in s}")
        print(f"summary contains 'SKILL SELF-TEACHING': {'SKILL SELF-TEACHING' in s}")
        print(f"artifacts: {data.get('artifacts', [])}")
