"""Replan with XAUUSD goal — expect fallback to mock plan."""
import json
import time
import urllib.request

pid = "proj-48b50520"
goal = "Read existing CPI/PPI data and brief from this project folder. Fetch 24 months of XAUUSD monthly closes from MT5 bridge. Compute Pearson correlation between monthly CPI/PPI change rates and XAUUSD returns. Write report_v2.md with new correlation analysis section."
print("=== replan with v2 goal ===")
req = urllib.request.Request(
    f"http://127.0.0.1:8765/api/projects/{pid}/replan",
    data=json.dumps({"goal": goal, "clear_tasks": True}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
r = urllib.request.urlopen(req, timeout=10)
result = json.loads(r.read())
print(f"  state={result['state']} cleared={result['cleared_tasks']}")

# Wait for fallback to fire and tasks to dispatch
for i in range(20):
    time.sleep(3)
    p = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8765/api/projects/{pid}", timeout=5).read())
    tasks_resp = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8765/api/tasks/?project_id={pid}", timeout=5).read())
    if isinstance(tasks_resp, dict):
        tasks_resp = tasks_resp.get("tasks", [])
    new_tasks = [t for t in tasks_resp if t["id"] not in ("t-53645d1e", "t-7e7f055e", "t-36508b93", "t-43477a67", "t-e9f9df5d", "t-9a2feeb2")]
    print(f"  t+{(i+1)*3}s state={p['state']} new_tasks={len(new_tasks)}")
    for t in new_tasks:
        print(f"    - {t['name']!r} status={t['status']} role={t['agent_role']}")
    if p["state"] == "running":
        print("\n  fallback worked, tasks running")
        break
    if p["state"] in ("completed", "cancelled"):
        print(f"\n  unexpectedly terminated: {p['state']}")
        break
