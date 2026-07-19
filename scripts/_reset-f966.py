"""Reset proj-f966b544 to 'ready' so supervisor dispatches pending task 2."""
import sqlite3
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.execute("UPDATE projects SET state = 'ready' WHERE id = 'proj-f966b544'")
c.commit()
print('reset proj-f966b544 to ready')
c.row_factory = sqlite3.Row
for t in c.execute("SELECT id, name, status FROM tasks WHERE project_id = 'proj-f966b544'"):
    print(dict(t))
