import sqlite3
conn = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
conn.row_factory = sqlite3.Row
c = conn.cursor()

print("=== AUDIT (latest 50, proj-48b50520) ===")
c.execute("SELECT id, event_type, actor, created_at, payload FROM audit_log WHERE project_id = ? ORDER BY created_at DESC LIMIT 50", ("proj-48b50520",))
for r in c.fetchall():
    payload = (r["payload"] or "")[:180]
    print(f"  [{r['created_at']}] {r['event_type']:35s} | actor={r['actor']:25s} | {payload}")

# Check artifacts for v3
print()
print("=== ARTIFACTS for proj-48b50520 ===")
c.execute("SELECT name, kind, file_path, created_at FROM artifacts WHERE project_id = ? ORDER BY created_at", ("proj-48b50520",))
for r in c.fetchall():
    print(f"  {r['name']:50s} kind={r['kind']:10s} created={r['created_at']}")
    print(f"    file_path={r['file_path']}")
