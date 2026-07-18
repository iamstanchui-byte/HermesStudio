import sqlite3
import json
conn = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
c = conn.cursor()
c.execute("SELECT payload FROM audit_log WHERE project_id = ? AND event_type = 'project.iteration_completed' ORDER BY created_at", ("proj-48b50520",))
print("=== RAW audit payloads (proj-48b50520 iter_completed) ===")
for r in c.fetchall():
    try:
        p = json.loads(r[0])
        print(f"  current_iteration={p.get('current_iteration')!r} max_iterations={p.get('max_iterations')!r} decision={p.get('decision')!r}")
    except Exception as e:
        print(f"  raw: {r[0]!r}")
        print(f"  err: {e}")
