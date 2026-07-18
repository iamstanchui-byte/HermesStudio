"""Check all agent_profiles records."""
import os
import sqlite3
db = sqlite3.connect(os.path.expanduser(r"~\.hermes-orchestrator\hermes-orch.db"))
db.row_factory = sqlite3.Row
for r in db.execute("SELECT id, agent_id, name, status FROM agent_profiles"):
    print(dict(r))
