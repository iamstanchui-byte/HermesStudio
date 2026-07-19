"""Find all pending / applying / failed status records."""
import sqlite3
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row
for r in c.execute("""
    SELECT file_path, status, COUNT(*) as n
    FROM profile_configs
    WHERE status IN ('pending', 'applying', 'failed')
    GROUP BY file_path, status
    ORDER BY n DESC
    LIMIT 20
"""):
    print(dict(r))
