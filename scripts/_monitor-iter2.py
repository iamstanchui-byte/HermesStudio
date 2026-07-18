"""Monitor latest project."""
import json
import time
import urllib.request


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(
        "http://127.0.0.1:8765" + path,
        data=data,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    r = urllib.request.urlopen(req, timeout=10)
    body = r.read()
    return json.loads(body) if body else None


# Find latest project
projects = api("GET", "/api/projects/?limit=5")
if isinstance(projects, dict):
    projects = projects.get("projects", [])
pid = projects[0]["id"]
print(f"monitoring {pid} '{projects[0]['name']}'")
print(f"  goal: {projects[0].get('goal', '')[:60]!r}")
print(f"  coord: {projects[0].get('coordinator_role')} max_iter: {projects[0].get('max_iterations')}")

for i in range(60):
    p = api("GET", f"/api/projects/{pid}")
    tasks = api("GET", f"/api/tasks/?project_id={pid}")
    if isinstance(tasks, dict):
        tasks = tasks.get("tasks", [])
    nonterm = [t for t in tasks if t["status"] not in ("completed", "failed", "cancelled", "skipped", "interrupted")]
    review = [t for t in tasks if t.get("action", "").startswith("_iteration_review:")]
    print(f"t+{i*5}s state={p['state']} iter={p['current_iteration']} tasks={len(tasks)} nonterm={len(nonterm)} review={len(review)}")
    for t in tasks:
        print(f"  - {t['name']:<60} status={t['status']:<12} role={t['agent_role']}")
    if p["state"] == "completed":
        print(f"  PASS — last_summary={p.get('last_iteration_summary', '')[:100]!r}")
        break
    time.sleep(5)
