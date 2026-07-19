"""Reset proj-f966b544 to ready so task 2 dispatches again."""
import sqlite3
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.execute("UPDATE projects SET state = 'ready' WHERE id = 'proj-f966b544'")
c.commit()
print("reset to ready")
