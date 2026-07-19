"""Phase 2 E2E test: state.md regen + content + size + format."""
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

proj_id = 'proj-5a6f548a'  # The Phase1 E2E we already ran, has L2 facts

# 1. Verify facts.md exists with content
pdir = rf'C:\Project\minimax code\hermes-project\{proj_id}'
facts = os.path.join(pdir, 'facts.md')
print(f'facts.md exists: {os.path.exists(facts)} ({os.path.getsize(facts) if os.path.exists(facts) else 0} bytes)')

# 2. Try GET /memory/state before regen (should be None)
print()
print('=== GET /memory/state (before regen) ===')
r = c.get(f'{base}/api/projects/{proj_id}/memory/state', headers=auth())
print(f'  status: {r.status_code}')
d = r.json()
print(f'  exists: {d.get("exists")}')
print(f'  size_bytes: {d.get("size_bytes")}')

# 3. POST /memory/state/regenerate
print()
print('=== POST /memory/state/regenerate ===')
r = c.post(f'{base}/api/projects/{proj_id}/memory/state/regenerate', headers=auth())
print(f'  status: {r.status_code}')
print(f'  body: {r.json()}')

# 4. Check state.md on disk
state_path = os.path.join(pdir, 'state.md')
print()
print(f'state.md exists: {os.path.exists(state_path)}')
if os.path.exists(state_path):
    size = os.path.getsize(state_path)
    print(f'state.md size: {size} bytes (cap 2048)')
    print()
    print('--- state.md content ---')
    print(open(state_path, encoding='utf-8').read())
    print('---')

# 5. Check archive (should be empty since first regen)
archive_dir = os.path.join(pdir, 'state_archive')
print()
print(f'state_archive exists: {os.path.exists(archive_dir)}')
if os.path.exists(archive_dir):
    print(f'  files: {os.listdir(archive_dir)}')

# 6. Re-trigger to test archive behavior
print()
print('=== second regen (should archive first) ===')
time.sleep(1)
r = c.post(f'{base}/api/projects/{proj_id}/memory/state/regenerate', headers=auth())
print(f'  status: {r.status_code} body: {r.json()}')
if os.path.exists(archive_dir):
    files = os.listdir(archive_dir)
    print(f'  archive files: {len(files)}')

# 7. Validate structure
print()
print('=== structure check ===')
if os.path.exists(state_path):
    content = open(state_path, encoding='utf-8').read()
    expected = ['# Project State:', '## Current Status', '## Goal',
                '## Open Questions', '## Key Findings', '## Next Steps']
    for sec in expected:
        ok = sec in content
        print(f'  {sec:25s}: {"OK" if ok else "MISSING"}')
    print(f'  size: {os.path.getsize(state_path)} bytes (cap 2048) {"OK" if os.path.getsize(state_path) <= 2048 else "OVER"}')
