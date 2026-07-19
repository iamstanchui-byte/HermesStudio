"""Verify L2 task.completed hook writes to facts.md Task Results section.

Strategy: directly call /api/tasks/{id}/result with running-state task.
Bypasses wrapper / Linux roundtrip. Tests the SERVER-SIDE hook only.
"""
import sys
import time
import json
import hashlib
import sqlite3
import httpx
import os

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

base = "http://127.0.0.1:8765"
secret_path = r"C:\Users\stanley\.hermes-orchestrator\.secret-win-local-1"
secret = open(secret_path, encoding='utf-8').read().strip()
client = httpx.Client(follow_redirects=True, timeout=30)


def auth_headers():
    return {
        "X-Agent-Id": "win-local-1",
        "X-Timestamp": str(int(time.time())),
        "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
    }


# 1. Create a fresh test project so we don't pollute anything
r = client.post(
    f"{base}/api/projects",
    json={
        "name": "L2 TaskHook Verify",
        "mode": "manual",  # manual so we don't dispatch through supervisor
        "goal": "Verify task.completed L2 hook writes to facts.md",
    },
    headers=auth_headers(),
)
print(f"create project: {r.status_code}")
proj = r.json()
proj_id = proj["id"]
print(f"  id: {proj_id} state={proj['state']}")

time.sleep(1)

# 2. Manually insert a task in running state (bypassing plan/dispatcher)
db_path = r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db"
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
task_id = f"t-l2test-{int(time.time())}"
params_json = json.dumps({})
depends_json = json.dumps([])
now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
# Use UTC for DB consistency (DB uses UTC-naive)
now_db = time.strftime("%Y-%m-%d %H:%M:%S")
conn.execute(
    """
    INSERT INTO tasks
      (id, project_id, name, agent_role, action, status,
       depends_on, params,
       assigned_agent_id, assigned_profile_id,
       created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, 'running',
            ?, ?,
            'win-local-1', 'win-agent01',
            ?, ?)
    """,
    (task_id, proj_id, "L2 hook test task", "test", "noop",
     depends_json, params_json,
     now_db, now_db),
)
conn.commit()
print(f"insert task: {task_id} (status=running)")

# 3. POST /api/tasks/{id}/result with completed + summary + 1 artifact
body = {
    "status": "completed",
    "summary": (
        "Wrote a small test artifact and verified that the server-side "
        "L2 task.completed hook appends a 1-line entry to facts.md "
        "Task Results section, plus the artifact name to Files section."
    ),
    "session_id": "test-session-l2-hook-001",
    "artifacts": [
        {
            "path": "l2_hook_test_artifact.txt",
            "size_bytes": 1024,
            "sha256": "deadbeef" + "0" * 56,
        },
    ],
}
r = client.post(f"{base}/api/tasks/{task_id}/result", json=body)
print(f"submit_result: {r.status_code}")
if r.status_code != 200:
    print(f"  body: {r.text}")
    sys.exit(1)
out = r.json()
print(f"  new status: {out['status']}")

time.sleep(1)

# 4. Read facts.md
pdir = rf"C:\Project\minimax code\hermes-project\{proj_id}"
facts_path = os.path.join(pdir, "facts.md")
print(f"\n--- facts.md at {facts_path} ---")
if not os.path.exists(facts_path):
    print("NOT FOUND")
    sys.exit(1)
content = open(facts_path, encoding='utf-8').read()
print(content)
print("---")

# 5. Assertions
ok = True
if "## Task Results" not in content:
    print("FAIL: '## Task Results' section missing")
    ok = False
else:
    # Extract Task Results section
    sec = content.split("## Task Results")[1].split("##")[0]
    if task_id not in sec:
        print(f"FAIL: task_id {task_id} not in Task Results section")
        ok = False
    elif "L2 hook test task" not in sec:
        print("FAIL: task name not in Task Results section")
        ok = False
    elif "[cite:" not in sec:
        print("FAIL: no cite: tag in Task Results")
        ok = False
    else:
        print("PASS: ## Task Results has the task entry with cite")

if "## Files (artifacts)" not in content:
    print("FAIL: '## Files (artifacts)' section missing")
    ok = False
else:
    sec = content.split("## Files (artifacts)")[1].split("##")[0]
    if "l2_hook_test_artifact.txt" not in sec:
        print("FAIL: artifact name not in Files section")
        ok = False
    elif "1024" not in sec:
        print("FAIL: artifact size not in Files section")
        ok = False
    else:
        print("PASS: ## Files (artifacts) has the artifact entry")

# 6. Also check trace.jsonl
trace_path = os.path.join(pdir, "trace.jsonl")
if os.path.exists(trace_path):
    n = sum(1 for _ in open(trace_path, encoding='utf-8'))
    print(f"PASS: trace.jsonl has {n} events")

if ok:
    print("\n*** ALL PASS: L2 task.completed hook writes correctly ***")
    sys.exit(0)
else:
    print("\n*** FAIL ***")
    sys.exit(1)
