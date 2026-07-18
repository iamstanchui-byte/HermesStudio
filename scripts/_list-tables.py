"""List all tables in DB."""
import sqlite3
db = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    print(r[0])
