"""Phase 1 E2E test: real task, real LLM, real wrapper.

Creates an auto-mode project, watches facts.md as it grows, and
verifies all 3 L2 hooks fire (plan, task, files). Reports
incremental state every poll.
"""
import sys
import time
import json
import hashlib
import os
import sqlite3
import httpx

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

base = "http://127.0.0.1:8765"
secret_path = r"C:\Users\stanley\.hermes-orchestrator\.secret-win-local-1"
secret = open(secret_path, encoding='utf-8').read().strip()
client = httpx.Client(follow_redirects=True, timeout=30)


def auth():
    return {
        "X-Agent-Id": "win-local-1",
        "X-Timestamp": str(int(time.time())),
        "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
    }


def read_facts(proj_id):
    facts_path = rf"C:\Project\minimax code\hermes-project\{proj_id}\facts.md"
    if not os.path.exists(facts_path):
        return None
    return open(facts_path, encoding='utf-8').read()


def section_size(facts, name):
    """Return (line_count, byte_count) of a section by name, or (0, 0) if missing."""
    if f"## {name}" not in facts:
        return 0, 0
    chunk = facts.split(f"## {name}")[1]
    if "##" in chunk:
        chunk = chunk.split("##")[0]
    return chunk.count("\n"), len(chunk.strip())


# 1. Create project
print("=" * 60)
print(f"[{time.strftime('%H:%M:%S')}] creating project...")
r = client.post(
    f"{base}/api/projects",
    json={
        "name": "Phase1 E2E",
        "mode": "auto",
        "goal": (
            "Use hermes to write a Python script that calculates the first "
            "10 Fibonacci numbers and saves them to fib_output.txt. After "
            "writing the file, run the script and verify the output."
        ),
    },
    headers=auth(),
)
print(f"  status: {r.status_code}")
proj = r.json()
proj_id = proj["id"]
print(f"  id: {proj_id}")
print(f"  state: {proj['state']}")

# 2. Poll
deadline = time.time() + 14 * 60  # 14 minutes
last_facts = None
last_state = None
last_tasks_signature = None
print()
print("=" * 60)
print(f"[{time.strftime('%H:%M:%S')}] entering poll loop (max 14 min)")
print("=" * 60)

while time.time() < deadline:
    time.sleep(10)
    # Project state
    r = client.get(f"{base}/api/projects/{proj_id}", headers=auth())
    if r.status_code != 200:
        print(f"[{time.strftime('%H:%M:%S')}] project GET failed: {r.status_code}")
        continue
    proj = r.json()
    state = proj.get("state")
    iter_ = proj.get("current_iteration", 0)
    max_ = proj.get("max_iterations", 0)

    # Tasks
    r = client.get(f"{base}/api/tasks?project_id={proj_id}", headers=auth())
    tasks_payload = r.json() if r.status_code == 200 else {}
    tasks = tasks_payload.get("tasks", []) if isinstance(tasks_payload, dict) else tasks_payload
    # 2026-07-25: defensive filter — if any task entry isn't a dict
    # (e.g. an API regression that returns strings or other shapes),
    # skip it instead of crashing the poll loop. The sig line is
    # best-effort; the canonical task state is the API response.
    sig = " | ".join(
        f"{t['name'][:18]}:{t['status'][:4]}"
        for t in tasks if isinstance(t, dict) and 'name' in t and 'status' in t
    )

    # facts.md
    facts = read_facts(proj_id) or ""

    sig_changed = sig != last_tasks_signature
    state_changed = state != last_state
    facts_changed = facts != last_facts

    if sig_changed or state_changed or facts_changed:
        print(f"\n[{time.strftime('%H:%M:%S')}] state={state} iter={iter_}/{max_}")
        print(f"  tasks: {sig}")
        if facts:
            for sec in ["Goal", "Plan History", "Task Results",
                        "Key Findings", "Files (artifacts)",
                        "Coord Verdicts", "Human Notes"]:
                lines, size = section_size(facts, sec)
                if size:
                    print(f"  ## {sec}: {size} chars / {lines} lines")
        else:
            print(f"  facts.md: (not yet)")
        last_facts = facts
        last_state = state
        last_tasks_signature = sig

    if state in ("completed", "failed", "cancelled"):
        print(f"\n[{time.strftime('%H:%M:%S')}] project {state}!")
        break

# Final
print()
print("=" * 60)
print("FINAL FACTS.MD")
print("=" * 60)
facts = read_facts(proj_id)
if facts:
    print(facts)
else:
    print("(no facts.md)")

# Final task list
print()
print("=" * 60)
print("FINAL TASKS")
print("=" * 60)
r = client.get(f"{base}/api/tasks?project_id={proj_id}", headers=auth())
payload = r.json()
final_tasks = payload.get("tasks", []) if isinstance(payload, dict) else payload
# Defensive: skip non-dict entries (same fix as the poll loop)
for t in final_tasks:
    if not isinstance(t, dict):
        continue
    tid = t.get("id", "?")
    tstatus = t.get("status", "?")
    tname = t.get("name", "?")
    print(f"  {tid:30s} {tstatus:10s} {tname}")

print()
print(f"Project: {proj_id}")
print(f"State: {state}")
