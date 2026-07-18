import sqlite3
conn = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
c = conn.cursor()
c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'")
for r in c.fetchall():
    print(r[0])

print()
# Show raw created_at values
c.execute("SELECT created_at, event_type FROM audit_log WHERE project_id = 'proj-48b50520' ORDER BY created_at DESC LIMIT 10")
print("=== RAW created_at (last 10) ===")
for r in c.fetchall():
    print(f"  {r[0]!r:50s}  {r[1]}")
