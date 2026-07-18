"""Check current state of project proj-898f7179."""
import sqlite3
db = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
db.row_factory = sqlite3.Row
for r in db.execute("SELECT id, name, state, goal, created_at, updated_at FROM projects WHERE id='proj-898f7179'"):
    print(dict(r))
