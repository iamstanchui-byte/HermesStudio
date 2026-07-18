import sqlite3
conn = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Coord review tasks
print("=== COORD REVIEW TASKS ===")
c.execute("SELECT id, name, status, created_at, updated_at FROM tasks WHERE project_id = ? AND name LIKE '%coord%' ORDER BY created_at", ("proj-48b50520",))
for r in c.fetchall():
    print(f"  [{r['id'][:8]}] {r['name']} | status={r['status']} | created={r['created_at']} updated={r['updated_at']}")

# Replan/planning events
print()
print("=== REPLAN/PLANNING EVENTS ===")
c.execute("""SELECT created_at, actor, event_type, payload FROM audit_log
             WHERE project_id = ? AND (event_type LIKE '%replan%' OR event_type LIKE '%planning%' OR event_type LIKE '%plan_%')
             ORDER BY created_at""", ("proj-48b50520",))
for r in c.fetchall():
    print(f"  [{r['created_at']}] {r['event_type']} actor={r['actor']}")
    print(f"    payload: {(r['payload'] or '')[:250]}")

# Project state transitions
print()
print("=== PROJECT STATE TRANSITIONS ===")
c.execute("""SELECT created_at, actor, event_type, payload FROM audit_log
             WHERE project_id = ? AND event_type LIKE 'project.%'
             ORDER BY created_at""", ("proj-48b50520",))
for r in c.fetchall():
    print(f"  [{r['created_at']}] {r['event_type']:40s} actor={r['actor']}")
    print(f"    payload: {(r['payload'] or '')[:200]}")
