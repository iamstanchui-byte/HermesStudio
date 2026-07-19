"""Test the dashboard delete flow: click X on a skill, verify it actually
goes away."""
import time, hashlib, httpx
secret = open(r'C:\Users\stanley\.hermes-orchestrator\.secret-win-local-1').read().strip()
def auth():
    return {
        'X-Agent-Id': 'win-local-1',
        'X-Timestamp': str(int(time.time())),
        'X-Signature': hashlib.sha256(secret.encode()).hexdigest(),
    }
c = httpx.Client(follow_redirects=True, timeout=10)

# Pick a throwaway skill: ridge-multicollinearity-on-small-n (hermes builtin
# but we can re-install later, this is just a flow test).
name = 'ridge-multicollinearity-on-small-n'
print(f'=== before delete: {name} ===')
r = c.get(f'http://127.0.0.1:8765/api/agents/linux-a-01/profiles/super/skills/{name}', headers=auth())
print(f'  GET status: {r.status_code}')

print()
print(f'=== DELETE {name} ===')
r = c.delete(f'http://127.0.0.1:8765/api/agents/linux-a-01/profiles/super/skills/{name}', headers=auth())
print(f'  status: {r.status_code}')
print(f'  body: {r.text[:200]}')

print()
print('=== wait 15s for wrapper to apply ===')
time.sleep(15)

print()
print(f'=== after delete: {name} ===')
r = c.get(f'http://127.0.0.1:8765/api/agents/linux-a-01/profiles/super/skills/{name}', headers=auth())
print(f'  GET status: {r.status_code} (404 = gone)')
if r.status_code == 200:
    d = r.json()
    print(f'  status field: {d.get("status")}  size: {d.get("size")}')

print()
print('=== list skills after delete ===')
r = c.get('http://127.0.0.1:8765/api/agents/linux-a-01/profiles/super/skills', headers=auth())
for s in r.json():
    marker = '<-- DELETED' if s['name'] == name else ''
    print(f"  {s['name']:40s} {s['file_path']:50s} {s['status']} {marker}")
