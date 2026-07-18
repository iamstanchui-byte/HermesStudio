"""Check supervisor state for a project."""
import sqlite3
import sys
from pathlib import Path

pid = sys.argv[1] if len(sys.argv) > 1 else "proj-1e3ef5f7"
con = sqlite3.connect(str(Path.home() / ".hermes-orchestrator" / "hermes-orch.db"))
con.row_factory = sqlite3.Row

print(f"=== project {pid} ===")
for r in con.execute("SELECT id, state, goal, created_at, updated_at FROM projects WHERE id = ?", (pid,)):
    print("  ", dict(r))

print("--- tasks ---")
for r in con.execute("SELECT id, name, agent_role, status, assigned_agent_id FROM tasks WHERE project_id = ?", (pid,)):
    print("  ", dict(r))

print("--- recent audit ---")
for r in con.execute(
    "SELECT event_type, actor, created_at FROM audit_log WHERE project_id = ? ORDER BY id DESC LIMIT 10",
    (pid,),
):
    print(f"  {r['event_type']:30s} actor={r['actor']:12s} at={r['created_at']}")
