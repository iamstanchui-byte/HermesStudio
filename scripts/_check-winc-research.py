"""Show win-c/research recent configs."""
import sqlite3
from pathlib import Path

con = sqlite3.connect(str(Path.home() / ".hermes-orchestrator" / "hermes-orch.db"))
con.row_factory = sqlite3.Row

print("--- win-c/research recent configs ---")
for r in con.execute(
    """
    SELECT pc.id, pc.status, pc.file_path, pc.desired_content, pc.applied_at, ap.name as profile_name
    FROM profile_configs pc
    JOIN agent_profiles ap ON pc.profile_id = ap.id
    JOIN agents a ON ap.agent_id = a.id
    WHERE a.id = 'win-c' AND ap.name = 'research'
    ORDER BY pc.created_at DESC LIMIT 5
    """
):
    print(f"  {r['id'][:12]} status={r['status']:10s} file={r['file_path']:20s} content={r['desired_content'][:40]!r}")
