"""Check projects table schema."""
import sqlite3
db = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
for r in db.execute("PRAGMA table_info(projects)"):
    print(r[1], r[2])
