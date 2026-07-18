"""Monitor iter loop after replan fix."""
import json
import time
import urllib.request

pid = "proj-48b50520"
for i in range(20):
    time.sleep(5)
    p = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8765/api/projects/{pid}", timeout=5).read())
    tasks = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8765/api/tasks/?project_id={pid}", timeout=5).read())
    if isinstance(tasks, dict):
        tasks = tasks.get("tasks", [])
    v1_ids = ("t-53645d1e", "t-7e7f055e", "t-36508b93", "t-43477a67", "t-e9f9df5d", "t-9a2feeb2")
    v2_ids = ("t-7e7f055e-2",)  # we don't actually know v2 IDs
    new_tasks = [t for t in tasks if t["id"] not in v1_ids and not t.get("action", "").startswith("_iteration_review:")]
    review = [t for t in tasks if t.get("action", "").startswith("_iteration_review:")]
    print(f"t+{(i+1)*5}s state={p['state']} iter={p['current_iteration']} new={len(new_tasks)} review={len(review)}")
    for t in new_tasks[:6]:
        print(f"  data - {t['name']!r} status={t['status']} role={t['agent_role']}")
    for t in review:
        print(f"  REVIEW - {t['name']!r} status={t['status']} iter_meta={t.get('params', {}).get('iteration')}")
    if p["state"] == "completed":
        break
