"""Look at tasks.result column (raw JSON) for proj-5e899243."""
import sqlite3, json
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row
rows = list(c.execute("SELECT id, name, status, result FROM tasks WHERE project_id = 'proj-5e899243' ORDER BY created_at").fetchall())
for r in rows:
    print(f"--- {r['id']} {r['name']} ({r['status']}) ---")
    if r['result']:
        try:
            data = json.loads(r['result'])
            print('  summary (first 500):', repr((data.get('summary') or '')[:500]))
            print('  error:', repr((data.get('error') or '')[:200]))
            print('  artifacts count:', len(data.get('artifacts', [])))
            for a in data.get('artifacts', []):
                print('    -', a.get('path'), a.get('size_bytes'))
        except Exception as e:
            print('  parse error:', e)
    else:
        print('  (no result)')
    print()
