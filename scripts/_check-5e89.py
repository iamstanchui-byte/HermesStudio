"""Quick check: project state + tasks for proj-5e899243."""
import hashlib, time, httpx
base = 'http://127.0.0.1:8765'
secret = open(r'C:\Users\stanley\.hermes-orchestrator\.secret-win-local-1', encoding='utf-8').read().strip()
c = httpx.Client(follow_redirects=True, timeout=30)
h = {
    'X-Agent-Id': 'win-local-1',
    'X-Timestamp': str(int(time.time())),
    'X-Signature': hashlib.sha256(secret.encode()).hexdigest(),
}

r = c.get(f'{base}/api/tasks?project_id=proj-5e899243', headers=h)
data = r.json()
print('=== tasks ===')
for t in data['tasks']:
    print(f"  {t['id']:20s} {t['status']:10s} {t['agent_role']:8s} {t['name']}")

print()
print('=== project state ===')
r = c.get(f'{base}/api/projects/proj-5e899243', headers=h)
p = r.json()
print(f"  state: {p['state']}  iter: {p.get('current_iteration')}/{p.get('max_iterations')}")
print(f"  name:  {p.get('name')}")
print(f"  goal:  {p.get('goal', '')[:100]}...")
