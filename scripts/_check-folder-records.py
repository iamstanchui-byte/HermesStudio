"""Check folder-path records after migration."""
import sqlite3
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row
print('=== distinct skill names with folder path (migrated + new) ===')
for r in c.execute("SELECT DISTINCT file_path FROM profile_configs WHERE file_path LIKE '%/SKILL.md' ORDER BY file_path"):
    print(' ', r['file_path'])
print()
n = c.execute("SELECT COUNT(*) FROM profile_configs WHERE file_path LIKE '%/SKILL.md'").fetchone()[0]
print(f'folder-path records: {n}')
