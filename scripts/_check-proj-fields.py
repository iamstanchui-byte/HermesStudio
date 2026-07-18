"""Check project fields for proj-ebf4cdea."""
import sqlite3
db = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
db.row_factory = sqlite3.Row
for r in db.execute("SELECT * FROM projects WHERE id='proj-ebf4cdea'"):
    d = dict(r)
    for k, v in d.items():
        print(f"  {k!s:<30} = {v!r}")
