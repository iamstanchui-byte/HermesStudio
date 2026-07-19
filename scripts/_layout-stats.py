"""Count records by layout type."""
import sqlite3
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row
flat_count = c.execute("SELECT COUNT(*) FROM profile_configs WHERE file_path LIKE 'skills/%.md' AND file_path NOT LIKE '%/SKILL.md'").fetchone()[0]
folder_count = c.execute("SELECT COUNT(*) FROM profile_configs WHERE file_path LIKE '%/SKILL.md'").fetchone()[0]
print(f'flat path records: {flat_count}')
print(f'folder/SKILL.md records: {folder_count}')
print()
print('=== distinct flat-path skill names ===')
for r in c.execute("SELECT DISTINCT file_path FROM profile_configs WHERE file_path LIKE 'skills/%.md' AND file_path NOT LIKE '%/SKILL.md' ORDER BY file_path LIMIT 20"):
    print(' ', r['file_path'])
