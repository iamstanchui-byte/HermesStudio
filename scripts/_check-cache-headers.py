"""Verify Cache-Control: no-store header is set on /api/agents/.../skills."""
import time, hashlib, httpx
secret = open(r'C:\Users\stanley\.hermes-orchestrator\.secret-win-local-1').read().strip()
h = {
    'X-Agent-Id': 'win-local-1',
    'X-Timestamp': str(int(time.time())),
    'X-Signature': hashlib.sha256(secret.encode()).hexdigest(),
}
c = httpx.Client(follow_redirects=True, timeout=10)
r = c.get('http://127.0.0.1:8765/api/agents/linux-a-01/profiles/super/skills', headers=h)
print('list skills response:')
print('  status:', r.status_code)
print('  Cache-Control:', r.headers.get('Cache-Control', '(none)'))
print()
r2 = c.get('http://127.0.0.1:8765/api/agents/linux-a-01/profiles/super/skills/dogfood', headers=h)
print('get single skill response:')
print('  status:', r2.status_code)
print('  Cache-Control:', r2.headers.get('Cache-Control', '(none)'))
print()
# Verify a non-skills endpoint does NOT have the header (only skills endpoints)
r3 = c.get('http://127.0.0.1:8765/api/agents/linux-a-01/profiles', headers=h)
print('list profiles (no /skills in path):')
print('  status:', r3.status_code)
print('  Cache-Control:', r3.headers.get('Cache-Control', '(none)'))
