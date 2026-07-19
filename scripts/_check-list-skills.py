"""Check the dedup'd skills list."""
import time, hashlib, httpx
secret = open(r'C:\Users\stanley\.hermes-orchestrator\.secret-win-local-1').read().strip()
h = {
    'X-Agent-Id': 'win-local-1',
    'X-Timestamp': str(int(time.time())),
    'X-Signature': hashlib.sha256(secret.encode()).hexdigest(),
}
c = httpx.Client(follow_redirects=True, timeout=5)
r = c.get('http://127.0.0.1:8765/api/agents/linux-a-01/profiles/super/skills', headers=h)
print('status:', r.status_code)
for s in r.json():
    print(f"  {s['name']:40s} {s['file_path']:50s} {s['status']}")
