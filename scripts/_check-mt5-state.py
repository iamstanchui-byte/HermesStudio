"""Check current state of mt5-bridge records."""
import sqlite3
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row
for r in c.execute("SELECT id, profile_id, file_path, status, desired_content, created_at FROM profile_configs WHERE file_path LIKE '%mt5-bridge%' ORDER BY created_at DESC"):
    content_preview = (r['desired_content'] or '')[:30].replace(chr(10), ' ')
    print(f"  {r['file_path']} status={r['status']} content={content_preview!r}")
