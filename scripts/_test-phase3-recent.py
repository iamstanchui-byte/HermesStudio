"""Phase 3 E2E: recent.md generated + planner inject + delete rebuild."""
import sys, time, hashlib, os, json
import httpx
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

base = 'http://127.0.0.1:8765'
secret = open(r'C:\Users\stanley\.hermes-orchestrator\.secret-win-local-1', encoding='utf-8').read().strip()
c = httpx.Client(follow_redirects=True, timeout=60)

def auth():
    return {
        'X-Agent-Id': 'win-local-1',
        'X-Timestamp': str(int(time.time())),
        'X-Signature': hashlib.sha256(secret.encode()).hexdigest(),
    }

# 1. GET /projects/memory/recent before regen
print('=== GET /api/projects/memory/recent (before) ===')
r = c.get(f'{base}/api/projects/memory/recent', headers=auth())
print(f'  status: {r.status_code}')
d = r.json()
print(f'  exists: {d.get("exists")}')
print(f'  size: {d.get("size_bytes")}')

# 2. POST /memory/recent/regenerate
print()
print('=== POST /api/projects/memory/recent/regenerate ===')
r = c.post(f'{base}/api/projects/memory/recent/regenerate', headers=auth())
print(f'  status: {r.status_code}')
print(f'  body: {r.json()}')

# 3. Wait 5-10s for LLM call
print()
print('waiting 8s for LLM call...')
time.sleep(8)

# 4. GET /memory/recent after regen
r = c.get(f'{base}/api/projects/memory/recent', headers=auth())
d = r.json()
print(f'  status: {r.status_code}')
print(f'  exists: {d.get("exists")}')
print(f'  size: {d.get("size_bytes")} bytes')
if d.get('content'):
    with open(r'C:\Project\minimax code\hermes-orchestrator\out-recent.md', 'w', encoding='utf-8') as f:
        f.write(d['content'])
    print(f'  written to out-recent.md')
    print()
    print('--- recent.md (first 600) ---')
    print(d['content'][:600])
    print('---')

# 5. Validate structure
print()
print('=== structure check ===')
if d.get('content'):
    content = d['content']
    expected = ['# User Recent', '## Active projects', '## Recently completed',
                '## Patterns observed', '## Recurring failures', '## User preferences']
    for sec in expected:
        ok = sec in content
        print(f'  {sec:30s}: {"OK" if ok else "MISSING"}')
    sz = d.get('size_bytes', 0)
    print(f'  size: {sz} bytes (cap 4096) {"OK" if sz <= 4096 else "OVER"}')

# 6. Check archive
print()
print('=== archive ===')
archive_dir = r'C:\Users\stanley\.hermes-orchestrator\memory\recent_archive'
if os.path.isdir(archive_dir):
    files = sorted(os.listdir(archive_dir))
    print(f'  files ({len(files)}):')
    for f in files:
        p = os.path.join(archive_dir, f)
        print(f'    {f} ({os.path.getsize(p)} bytes)')

# 7. Test project delete -> recent regen
print()
print('=== test: project delete triggers recent regen ===')
r = c.post(f'{base}/api/projects', json={
    'name': 'Phase3 DeleteTest',
    'mode': 'manual',
    'goal': 'Test that delete triggers recent regen.',
}, headers=auth())
proj = r.json()
proj_id = proj['id']
print(f'  created: {proj_id}')

time.sleep(2)

r = c.delete(f'{base}/api/projects/{proj_id}', headers=auth())
print(f'  delete: {r.status_code}')

# Wait for delete-trigger regen
time.sleep(8)
print()
print('  recent.md (after delete, looking for Phase3 DeleteTest ref):')
r = c.get(f'{base}/api/projects/memory/recent', headers=auth())
d = r.json()
if d.get('content'):
    found = 'Phase3' in d['content'] or 'DeleteTest' in d['content']
    print(f'  Phase3/DeleteTest reference in recent: {found}')

# 8. Test planner inject
print()
print('=== test: planner sees recent.md ===')
r = c.post(f'{base}/api/projects', json={
    'name': 'Phase3 PlannerTest',
    'mode': 'auto',
    'goal': 'Test that the planner sees the recent context.',
}, headers=auth())
proj = r.json()
proj_id = proj['id']
print(f'  created: {proj_id}')
time.sleep(2)
r = c.get(f'{base}/api/projects/{proj_id}', headers=auth())
print(f'  state: {r.json().get("state")}')
