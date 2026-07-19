"""Verify raw task summary is cleaned (not just L2 display)."""
import sqlite3, json
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row
out = []
out.append("=== proj-5a6f548a raw summaries ===")
for r in c.execute("SELECT id, name, result FROM tasks WHERE project_id = 'proj-5a6f548a' ORDER BY created_at").fetchall():
    if not r['result']:
        continue
    s = json.loads(r['result']).get('summary', '')
    out.append(f"\n--- {r['id']} {r['name']} ---")
    out.append(f"len: {len(s)} chars")
    out.append(f"first 150: {s[:150]!r}")
    out.append(f"contains 'Query:': {'Query:' in s}")
    out.append(f"contains 'PROJECT CONTEXT': {'PROJECT CONTEXT' in s}")
    out.append(f"contains 'SKILL SELF-TEACHING': {'SKILL SELF-TEACHING' in s}")
open(r'C:\Project\minimax code\hermes-orchestrator\out-check-5a6f.txt', 'w', encoding='utf-8').write('\n'.join(out))
print("written")
