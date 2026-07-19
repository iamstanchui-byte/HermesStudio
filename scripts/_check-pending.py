"""Check why hk-weather / computer-use show as pending."""
import sqlite3
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row
for r in c.execute("""
    SELECT id, profile_id, file_path, status, desired_sha256, created_at
    FROM profile_configs
    WHERE file_path LIKE '%hk-weather%' OR file_path LIKE '%computer-use%'
    ORDER BY created_at DESC LIMIT 10
"""):
    print(dict(r))
