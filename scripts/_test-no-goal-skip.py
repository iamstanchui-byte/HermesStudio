"""Verify Q2 iterate fix: project without goal should NOT dispatch review tasks."""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8765"


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    r = urllib.request.urlopen(req, timeout=10)
    body = r.read()
    return json.loads(body) if body else None


# 1) Create project, no goal, but max_iter=2 + coordinator=super
print("=== creating no-goal iterative project ===")
proj = api("POST", "/api/projects/", {
    "name": "no-goal-iter-test",
    "mode": "manual",
    "goal": "",
    "coordinator_role": "super",
    "max_iterations": 2,
})
pid = proj["id"]
print(f"  id={pid} state={proj['state']} coord={proj['coordinator_role']} max={proj['max_iterations']}")

# 2) Wait 15s — if bug, supervisor would dispatch review task by now
print("\n=== waiting 15s (should NOT dispatch any review task) ===")
for i in range(3):
    p = api("GET", f"/api/projects/{pid}")
    tasks = api("GET", f"/api/tasks/?project_id={pid}")
    if isinstance(tasks, dict):
        tasks = tasks.get("tasks", [])
    review = [t for t in tasks if t.get("action", "").startswith("_iteration_review:")]
    print(f"  t+{(i+1)*5}s state={p['state']} iter={p['current_iteration']} review_tasks={len(review)}")
    if review:
        print("  FAIL: review task was dispatched despite empty goal")
        break
    time.sleep(5)
else:
    p = api("GET", f"/api/projects/{pid}")
    if p["state"] in ("ready", "running"):
        print("  PASS: project stayed in non-terminal state, no review task dispatched")
    else:
        print(f"  unexpected: state={p['state']}")

# 3) Set goal and verify iteration does kick in
print("\n=== setting goal (should trigger iteration 1) ===")
replan = api("POST", f"/api/projects/{pid}/replan", {"goal": "Test that iteration works after goal is set"})
print(f"  state={replan['state']}")
time.sleep(15)
p = api("GET", f"/api/projects/{pid}")
tasks = api("GET", f"/api/tasks/?project_id={pid}")
if isinstance(tasks, dict):
    tasks = tasks.get("tasks", [])
review = [t for t in tasks if t.get("action", "").startswith("_iteration_review:")]
print(f"  state={p['state']} iter={p['current_iteration']} review={len(review)}")

# Cleanup
print("\n=== cleanup ===")
req = urllib.request.Request(f"{BASE}/api/projects/{pid}", method="DELETE")
urllib.request.urlopen(req, timeout=5)
print(f"  deleted {pid}")
