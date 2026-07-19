"""Get full task result content to see the structure of hermes output."""
import sqlite3, json
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row
row = c.execute("SELECT result FROM tasks WHERE id = 't-38085daa'").fetchone()
data = json.loads(row['result'])
summary = data.get('summary', '')
with open(r'C:\Project\minimax code\hermes-orchestrator\out-t-38085daa.txt', 'w', encoding='utf-8') as f:
    f.write('=== t-38085daa full summary ===\n')
    f.write(summary)
    f.write(f'\n\n=== length: {len(summary)} ===\n')
print(f"wrote {len(summary)} chars to out-t-38085daa.txt")
