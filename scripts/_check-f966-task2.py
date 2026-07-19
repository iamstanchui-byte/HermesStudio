"""Check task 2 result to verify it saw L3 state + L2 memory."""
import sqlite3, json
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row
row = c.execute("SELECT result FROM tasks WHERE id = 't-phase2-1784446594-2'").fetchone()
if not row or not row['result']:
    print("no result yet")
else:
    data = json.loads(row['result'])
    s = data.get('summary', '')
    print(f"len: {len(s)} chars")
    with open(r'C:\Project\minimax code\hermes-orchestrator\out-task2-summary.txt', 'w', encoding='utf-8') as f:
        f.write('=== full summary ===\n')
        f.write(s)
        f.write('\n=== contains L3 state references? ===\n')
        f.write(f"  'Goal' in summary: {'Goal' in s}\n")
        f.write(f"  'Project State' in summary: {'Project State' in s}\n")
        f.write(f"  'Key Findings' in summary: {'Key Findings' in s}\n")
        f.write(f"  'PROJECT STATE' marker in summary: {'PROJECT STATE' in s}\n")
        f.write(f"  'Next Steps' in summary: {'Next Steps' in s}\n")
    print("written to out-task2-summary.txt")
    print()
    print("--- first 800 chars ---")
    print(s[:800])
