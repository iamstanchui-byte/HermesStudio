"""Verify the new list_skills response shape (name field)."""
import time, hashlib, httpx
secret = open(r'C:\Users\stanley\.hermes-orchestrator\.secret-win-local-1').read().strip()
h = {
    'X-Agent-Id': 'win-local-1',
    'X-Timestamp': str(int(time.time())),
    'X-Signature': hashlib.sha256(secret.encode()).hexdigest(),
}
r = httpx.get('http://127.0.0.1:8765/api/agents/linux-a-01/profiles/super/skills', headers=h, timeout=5)
data = r.json()
print('count:', len(data))
for s in data:
    print(f"  name={s['name']!r:30s}  file_path={s['file_path']!r}")
