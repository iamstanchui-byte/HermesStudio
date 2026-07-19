"""Check the latest mt5-bridge records in DB."""
import sqlite3
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row
for r in c.execute("SELECT id, file_path, status, desired_sha256 FROM profile_configs WHERE file_path LIKE '%mt5-bridge%' ORDER BY created_at DESC LIMIT 3"):
    print(f"  {r['file_path']} status={r['status']} sha={r['desired_sha256'][:12]}")
