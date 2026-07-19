"""E2E test of 3-tier memory Phase 1: create project, run task, verify facts.md."""
import sys
import json
import time
import hashlib
import httpx
import sqlite3
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

base = "http://127.0.0.1:8765"
secret = open(r"C:\Users\stanley\.hermes-orchestrator\.secret-win-local-1").read().strip()
client = httpx.Client(follow_redirects=True, timeout=30)


def headers():
    return {
        "X-Agent-Id": "win-local-1",
        "X-Timestamp": str(int(time.time())),
        "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
    }


# 1. Create test project
r = client.post(
    f"{base}/api/projects",
    json={
        "name": "Memory Test",
        "mode": "auto",
        "goal": "Test that project memory writes to facts.md",
    },
    headers=headers(),
)
print(f"create: {r.status_code}")
proj = r.json()
proj_id = proj["id"]
print(f"  id: {proj_id}")
print(f"  state: {proj['state']}")

# 2. Wait briefly
time.sleep(2)

# 3. Check that facts.md was created
pdir = rf"C:\Project\minimax code\hermes-project\{proj_id}"
import os
facts_path = os.path.join(pdir, "facts.md")
trace_path = os.path.join(pdir, "trace.jsonl")
print()
print("--- facts.md ---")
if os.path.exists(facts_path):
    with open(facts_path, encoding='utf-8') as f:
        print(f.read())
else:
    print(f"  NOT FOUND: {facts_path}")

print()
print("--- trace.jsonl ---")
if os.path.exists(trace_path):
    with open(trace_path, encoding='utf-8') as f:
        print(f.read())
else:
    print(f"  NOT FOUND: {trace_path}")

# 4. Get via API
print()
print("--- GET /api/projects/{id}/memory/facts ---")
r = client.get(f"{base}/api/projects/{proj_id}/memory/facts", headers=headers())
print(f"status: {r.status_code}")
if r.status_code == 200:
    d = r.json()
    print(f"  size_bytes: {d['size_bytes']}")
    print(f"  content (first 200): {d['content'][:200] if d['content'] else '(empty)'}")

# 5. Get trace
print()
print("--- GET /api/projects/{id}/memory/trace ---")
r = client.get(f"{base}/api/projects/{proj_id}/memory/trace", headers=headers())
print(f"status: {r.status_code}")
if r.status_code == 200:
    d = r.json()
    print(f"  count: {d['count']}")
    for e in d['entries'][:3]:
        print(f"  {e['ts']} {e['event_type']} {e['actor']}")

# 6. Wait for plan to be generated, then check plan_history
print()
print("--- waiting 20s for plan generation ---")
time.sleep(20)

# 7. Re-read facts.md
print()
print("--- facts.md after plan generation ---")
if os.path.exists(facts_path):
    with open(facts_path, encoding='utf-8') as f:
        content = f.read()
    print(content)
    # Check that Plan History section has a fact
    if "## Plan History" in content and "[cite:" in content.split("## Plan History")[1].split("##")[0]:
        print("\n*** PASS: Plan History section has a cited fact ***")
    else:
        print("\n*** FAIL: Plan History section missing cited fact ***")
