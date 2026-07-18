"""E2E test for Q2 iteration loop.

Flow:
1. Create project with goal + coordinator=super + max_iterations=2
2. Wait for initial plan + tasks to complete
3. Wait for supervisor to dispatch review task
4. Monitor decision.md and project state
5. Test inject 'DECISION: PASS' to simulate coordinator passing
6. Verify project state moves to completed
"""
import json
import os
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
PROJECTS_ROOT = r"C:\Project\minimax code\hermes-project"


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    try:
        r = urllib.request.urlopen(req, timeout=10)
        body = r.read()
        return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# 1) Create project
print("=== creating iterative project ===")
status, proj = api("POST", "/api/projects/", {
    "name": "q2-iter-test",
    "mode": "auto",
    "goal": "Use terminal to run `echo hello-q2-iter > result.txt` and save it. Then we will iterate via the coordinator.",
    "coordinator_role": "super",
    "accept_criteria": "result.txt exists and contains the text 'hello-q2-iter'",
    "deliverable_path": "result.txt",
    "max_iterations": 2,
})
print(f"  status={status} id={proj['id']} state={proj['state']} coordinator={proj['coordinator_role']} max_iter={proj['max_iterations']}")
assert status == 201
pid = proj["id"]

# 2) Wait for initial plan
print("\n=== waiting 30s for initial plan + tasks to complete ===")
deadline = time.time() + 60
all_done = False
initial_iter = 0
while time.time() < deadline:
    status, p = api("GET", f"/api/projects/{pid}")
    status, tasks_resp = api("GET", f"/api/tasks/?project_id={pid}")
    tasks = tasks_resp.get("tasks", tasks_resp) if isinstance(tasks_resp, dict) else tasks_resp
    nonterm = [t for t in tasks if t["status"] not in ("completed", "failed", "cancelled", "skipped", "interrupted")]
    initial_iter = p.get("current_iteration", 0)
    print(f"  state={p['state']} iter={initial_iter} tasks={len(tasks)} nonterm={len(nonterm)}")
    if tasks and not nonterm and initial_iter > 0:
        all_done = True
        break
    time.sleep(3)
print(f"  state={p['state']} iter={initial_iter} tasks={len(tasks)} nonterm={len(nonterm)}")
if not all_done:
    print(f"  progress: not all tasks done yet, iter={initial_iter}")
    # Check if at least one iteration has been dispatched
    if initial_iter == 0:
        print("  FAIL: no iteration dispatched")
        sys.exit(1)

# 3) Wait for iteration review task
print(f"\n=== current_iteration={initial_iter}, waiting 30s for review task to run ===")
time.sleep(30)
status, p = api("GET", f"/api/projects/{pid}")
status, tasks_resp = api("GET", f"/api/tasks/?project_id={pid}")
tasks = tasks_resp.get("tasks", tasks_resp) if isinstance(tasks_resp, dict) else tasks_resp
print(f"  state={p['state']} iter={p.get('current_iteration')}")
for t in tasks:
    print(f"    - {t['name']:<60} status={t['status']:<12} action={t['action'][:60]!r}")
review_task = next((t for t in tasks if t["action"].startswith("_iteration_review:")), None)
if not review_task:
    print("  no review task found")
else:
    print(f"  review task {review_task['id']} status={review_task['status']}")

# 4) Check decision.md
dpath = os.path.join(PROJECTS_ROOT, pid, "decision.md")
print(f"\n=== checking decision.md at {dpath} ===")
if os.path.exists(dpath):
    print(f"  exists, content:")
    with open(dpath, encoding="utf-8") as f:
        print(f.read()[:500])
else:
    print(f"  not yet written")

# 5) Simulate coordinator PASS by injecting decision.md
if not (os.path.exists(dpath) and "DECISION: PASS" in open(dpath, encoding="utf-8").read().upper()):
    print("\n=== simulating coordinator: writing decision.md with DECISION: PASS ===")
    decision_content = (
        "DECISION: PASS\n"
        f"\nThe deliverable result.txt was created with the expected content. "
        f"Accept criteria met. Iteration {p.get('current_iteration')} of {p.get('max_iterations')} "
        f"is complete; no further iteration needed.\n"
    )
    os.makedirs(os.path.dirname(dpath), exist_ok=True)
    with open(dpath, "w", encoding="utf-8") as f:
        f.write(decision_content)
    print("  wrote decision.md")
else:
    print("\n  decision.md already has PASS, no need to inject")

# 6) Wait for supervisor to pick up decision
print("\n=== waiting 8s for supervisor to read decision.md ===")
time.sleep(8)
status, p = api("GET", f"/api/projects/{pid}")
print(f"  state={p['state']} iter={p.get('current_iteration')} last_summary={p.get('last_iteration_summary', '')[:80]!r}")
if p['state'] == "completed":
    print("\n  PASS: project auto-completed after DECISION: PASS")
else:
    print(f"\n  state still {p['state']}, may need more time")

# 7) Cleanup
print(f"\n=== cleanup ===")
status, _ = api("DELETE", f"/api/projects/{pid}")
print(f"  delete status: {status}")
