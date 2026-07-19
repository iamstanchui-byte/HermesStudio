"""Reset proj-6213a9f0 (Phase3 PlannerTest) for re-inject verify."""
import sqlite3
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.execute("UPDATE projects SET state='ready' WHERE id='proj-6213a9f0'")
c.execute("""UPDATE tasks SET status='pending', assigned_agent_id=NULL,
              assigned_profile_id=NULL WHERE id='t-7c3c5df4'""")
c.commit()
print('reset proj-6213a9f0 + t-7c3c5df4 (Investigate)')
