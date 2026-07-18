import sqlite3
conn = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
conn.row_factory = sqlite3.Row
c = conn.cursor()
c.execute("PRAGMA table_info(projects)")
print("=== PROJECTS schema ===")
for r in c.fetchall():
    print(f"  {r['name']:30s} {r['type']:15s} default={r['dflt_value']!r}")

# Check proj-48b50520 row
print()
c.execute("SELECT id, current_iteration, max_iterations, state FROM projects WHERE id = ?", ("proj-48b50520",))
r = c.fetchone()
print(f"proj-48b50520: current_iteration={r['current_iteration']!r} (type={type(r['current_iteration']).__name__})")
