"""Reset stuck project 48b50520."""
import sqlite3
db = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
db.row_factory = sqlite3.Row
for r in db.execute("SELECT id, name, state, updated_at FROM projects WHERE id='proj-48b50520'"):
    print("before:", dict(r))
db.execute("UPDATE projects SET state='ready' WHERE id='proj-48b50520'")
db.commit()
for r in db.execute("SELECT id, name, state, updated_at FROM projects WHERE id='proj-48b50520'"):
    print("after:", dict(r))
