"""Monitor the v2 plan execution."""
import json
import time
import urllib.request

pid = "proj-48b50520"
v1_task_ids = ("t-53645d1e", "t-7e7f055e", "t-36508b93", "t-43477a67", "t-e9f9df5d", "t-9a2feeb2")

for i in range(40):
    time.sleep(5)
    p = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8765/api/projects/{pid}", timeout=5).read())
    tasks_resp = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8765/api/tasks/?project_id={pid}", timeout=5).read())
    if isinstance(tasks_resp, dict):
        tasks_resp = tasks_resp.get("tasks", [])
    new_tasks = [t for t in tasks_resp if t["id"] not in v1_task_ids]
    nonterm = [t for t in new_tasks if t["status"] not in ("completed", "failed", "cancelled", "skipped", "interrupted")]
    print(f"t+{(i+1)*5}s state={p['state']} iter={p.get('current_iteration')} tasks={len(new_tasks)} nonterm={len(nonterm)}")
    for t in new_tasks:
        print(f"  - {t['name']:<40} status={t['status']:<12} role={t['agent_role']}")
    if p["state"] == "completed":
        break
    if i == 39:
        print("TIMEOUT")
