"""Regen proj-f966b544 state.md + reset task 2 for final inject verify."""
import time, hashlib, httpx, sqlite3
base = 'http://127.0.0.1:8765'
secret = open(r'C:\Users\stanley\.hermes-orchestrator\.secret-win-local-1', encoding='utf-8').read().strip()
c = httpx.Client(follow_redirects=True, timeout=30)
def auth():
    return {
        'X-Agent-Id': 'win-local-1',
        'X-Timestamp': str(int(time.time())),
        'X-Signature': hashlib.sha256(secret.encode()).hexdigest(),
    }
# 1. Regen state.md
r = c.post(f'{base}/api/projects/proj-f966b544/memory/state/regenerate', headers=auth())
print('regen:', r.json())
# 2. Reset project + task 2
db = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
db.execute("UPDATE projects SET state='ready' WHERE id='proj-f966b544'")
db.execute("""UPDATE tasks SET status='pending', assigned_agent_id=NULL,
              assigned_profile_id=NULL WHERE id='t-phase2-1784446594-2'""")
db.commit()
print('reset done')
