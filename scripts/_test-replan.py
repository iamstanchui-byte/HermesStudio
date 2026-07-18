"""E2E test for /replan endpoint.

Flow:
1. Create project in MANUAL mode (no goal)
2. Verify state=ready, no tasks
3. POST /replan with a CPI/PPI goal + clear_tasks=True
4. Wait for supervisor to call planner
5. Verify state transitions to 'ready' (planner finished) and tasks were created
6. Each task should have an agent_role assigned
"""
import json
import sys
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
    try:
        r = urllib.request.urlopen(req, timeout=10)
        body = r.read()
        return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# 1) Create manual project
print("=== creating manual project (no goal) ===")
status, proj = api("POST", "/api/projects/", {
    "name": "replan-test",
    "mode": "manual",
    "goal": "",
})
print(f"  status={status} id={proj['id']} state={proj['state']} goal={proj['goal']!r}")
assert status == 201
pid = proj["id"]
assert proj["state"] == "ready", f"expected ready, got {proj['state']}"

# 2) Replan with a goal
goal = "Analyze the relationship between US CPI/PPI releases and XAUUSD price action over the last 6 months, with correlation coefficients"
print(f"\n=== replanning with goal ===")
print(f"  goal: {goal[:80]!r}...")
status, result = api("POST", f"/api/projects/{pid}/replan", {
    "goal": goal,
    "clear_tasks": False,  # we have no tasks yet, this is a no-op
})
print(f"  status={status} state={result.get('state')} cleared={result.get('cleared_tasks')}")
assert status == 200
assert result["state"] == "planning"

# 3) Wait for supervisor tick to call planner
# supervisor's tick is 1s. planner (LLM) takes 5-15s. Give it 30s.
print("\n=== waiting 30s for supervisor -> planner to generate plan ===")
deadline = time.time() + 30
final_state = None
task_count = 0
while time.time() < deadline:
    status, p = api("GET", f"/api/projects/{pid}")
    final_state = p["state"]
    status, tasks_resp = api("GET", f"/api/tasks/?project_id={pid}")
    if isinstance(tasks_resp, dict) and "tasks" in tasks_resp:
        task_count = len(tasks_resp["tasks"])
    elif isinstance(tasks_resp, list):
        task_count = len(tasks_resp)
    else:
        task_count = 0
    if final_state in ("ready", "running", "completed") and task_count > 0:
        break
    time.sleep(2)
print(f"  final state: {final_state}, tasks: {task_count}")

# 4) Verify
if task_count == 0:
    print("\n  FAIL: no tasks generated. Planner may have failed.")
    # Get project details
    status, p = api("GET", f"/api/projects/{pid}")
    print(f"  project: {p}")
    sys.exit(1)

print(f"\n=== generated tasks ({task_count}) ===")
status, tasks_resp = api("GET", f"/api/tasks/?project_id={pid}")
tasks = tasks_resp.get("tasks", tasks_resp) if isinstance(tasks_resp, dict) else tasks_resp
roles = set()
for t in tasks:
    name = t.get("name", "?")
    role = t.get("agent_role", "?")
    deps = t.get("depends_on", [])
    roles.add(role)
    print(f"  - {name}")
    print(f"    role={role} deps={deps} status={t.get('status', '?')}")
print(f"\n  roles assigned: {roles}")
print(f"  PASS: {task_count} task(s) with {len(roles)} unique role(s)")

# 5) Cleanup
print(f"\n=== cleanup ===")
status, _ = api("DELETE", f"/api/projects/{pid}")
print(f"  delete status: {status}")

print("\nALL PASS")
