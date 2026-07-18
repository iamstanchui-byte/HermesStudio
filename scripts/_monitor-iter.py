"""Monitor the iterative project state."""
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


pid = "proj-5ef3a991"
for i in range(40):
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
    if i == 39:
        print("  TIMEOUT")
    time.sleep(5)
