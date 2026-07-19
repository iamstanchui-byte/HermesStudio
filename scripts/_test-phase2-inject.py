"""Phase 2 E2E: verify L3 state.md is INJECTED into task prompts.

Strategy:
1. Create new project, run task 1 (write fib script)
2. After task 1 done, manually regen state.md
3. Add task 2 (depends on task 1) that just reads the file
4. Watch Linux daemon log for "prompt=..." line which should include
   [PROJECT STATE (L3: state.md)] section (truncated to ~200 chars in
   log, but we can verify the section markers appear).
"""
import sys, time, hashlib, os, json
import httpx
import sqlite3
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

base = 'http://127.0.0.1:8765'
secret = open(r'C:\Users\stanley\.hermes-orchestrator\.secret-win-local-1', encoding='utf-8').read().strip()
c = httpx.Client(follow_redirects=True, timeout=30)

def auth():
    return {
        'X-Agent-Id': 'win-local-1',
        'X-Timestamp': str(int(time.time())),
        'X-Signature': hashlib.sha256(secret.encode()).hexdigest(),
    }

# 1. Create project
print('=' * 60)
print('Phase 2: L3 state inject E2E')
print('=' * 60)
r = c.post(f'{base}/api/projects', json={
    'name': 'Phase2 State Inject',
    'mode': 'manual',  # we control task creation directly
    'goal': 'Test that L3 state.md is injected into subsequent task prompts.',
}, headers=auth())
proj = r.json()
proj_id = proj['id']
print(f'project: {proj_id} state={proj["state"]}')

# 2. Insert task 1: write a file
db = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
now = time.strftime('%Y-%m-%d %H:%M:%S')
task1_id = f't-phase2-{int(time.time())}'
db.execute("""
    INSERT INTO tasks (id, project_id, name, agent_role, action, status,
                       depends_on, params, output_path,
                       created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, NULL, ?, ?)
""", (task1_id, proj_id, 'write_hello_file', 'super', 'write_python_file',
      '[]', json.dumps({'filepath': 'hello.txt', 'logic': 'print_hello'}),
      now, now))
db.commit()
print(f'task 1: {task1_id} (write_hello_file, status=pending)')

# 3. Wait for supervisor to dispatch (will go to Linux super profile)
print()
print('waiting 30s for supervisor to dispatch task 1...')
time.sleep(30)

# 4. Check status
r = c.get(f'{base}/api/tasks?project_id={proj_id}', headers=auth())
tasks = r.json()['tasks']
for t in tasks:
    print(f'  {t["id"]} {t["status"]:10s} {t["name"]}')

# 5. Wait for completion (up to 5 min)
print()
print('waiting for task 1 to complete (up to 5 min)...')
deadline = time.time() + 300
while time.time() < deadline:
    r = c.get(f'{base}/api/tasks?project_id={proj_id}', headers=auth())
    tasks = r.json()['tasks']
    t1 = next((t for t in tasks if t['id'] == task1_id), None)
    if t1 and t1['status'] == 'completed':
        print(f'  task 1 done!')
        break
    time.sleep(10)
else:
    print('  TIMEOUT')
    sys.exit(1)

# 6. Manual regen state.md
print()
print('triggering state.md regen...')
r = c.post(f'{base}/api/projects/{proj_id}/memory/state/regenerate', headers=auth())
print(f'  regen: {r.status_code} {r.json()}')

# 7. Verify state.md exists
pdir = rf'C:\Project\minimax code\hermes-project\{proj_id}'
state_path = os.path.join(pdir, 'state.md')
if not os.path.exists(state_path):
    print('FAIL: state.md not created')
    sys.exit(1)
print(f'  state.md: {os.path.getsize(state_path)} bytes')
print()
print('--- state.md ---')
print(open(state_path, encoding='utf-8').read())
print('---')

# 8. Add task 2 (read hello.txt)
print('adding task 2: read_hello_file...')
task2_id = f't-phase2-{int(time.time())}-2'
db.execute("""
    INSERT INTO tasks (id, project_id, name, agent_role, action, status,
                       depends_on, params, output_path,
                       created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, NULL, ?, ?)
""", (task2_id, proj_id, 'read_hello_file', 'super', 'read_and_verify',
      json.dumps([task1_id]),
      json.dumps({'file': 'hello.txt'}),
      now, now))
db.commit()
print(f'task 2: {task2_id} (read_hello_file)')

# 9. Watch Linux daemon log for task 2 prompt
print()
print('waiting for supervisor to dispatch task 2...')
time.sleep(40)

# 10. Read Linux daemon log
print('=' * 60)
print('Linux daemon log (last 60 lines)')
print('=' * 60)
import subprocess
log = subprocess.check_output(
    ['ssh', 'stanley@192.168.2.161', 'tail -60 /tmp/hermes-daemon.log'],
    timeout=15
).decode('utf-8', errors='replace')
print(log)

# 11. Validate
print('=' * 60)
print('inject validation')
print('=' * 60)
if 'PROJECT STATE (L3: state.md)' in log:
    print('PASS: [PROJECT STATE (L3: state.md)] marker found in daemon log')
else:
    print('FAIL: [PROJECT STATE] marker NOT in daemon log')
    sys.exit(1)
if 'PROJECT MEMORY (L2: facts.md)' in log:
    print('PASS: [PROJECT MEMORY (L2: facts.md)] marker found in daemon log')
else:
    print('FAIL: [PROJECT MEMORY] marker NOT in daemon log')
    sys.exit(1)
# Check ordering: STATE should appear BEFORE MEMORY in the prompt
state_idx = log.find('PROJECT STATE')
mem_idx = log.find('PROJECT MEMORY')
if state_idx < mem_idx:
    print(f'PASS: STATE ({state_idx}) appears BEFORE MEMORY ({mem_idx})')
else:
    print(f'FAIL: ordering wrong: STATE={state_idx}, MEMORY={mem_idx}')
    sys.exit(1)

print()
print('*** ALL PASS: L3 state.md inject working correctly ***')
