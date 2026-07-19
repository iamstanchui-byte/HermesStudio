"""Reset task 2 + project so we can re-test inject."""
import sqlite3
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.execute("""UPDATE tasks SET status='pending', assigned_agent_id=NULL,
              assigned_profile_id=NULL WHERE id='t-phase2-1784446594-2'""")
c.execute("UPDATE projects SET state='ready' WHERE id='proj-f966b544'")
c.commit()
print('reset done')
for r in c.execute("SELECT id, name, status FROM tasks WHERE project_id='proj-f966b544'"):
    print(r)
