"""Check v2 iteration loop details."""
import json
import urllib.request

pid = "proj-48b50520"
p = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8765/api/projects/{pid}", timeout=5).read())
print(f"project state={p['state']} iter={p['current_iteration']}/{p['max_iterations']}")
print(f"last_summary: {p.get('last_iteration_summary', '')!r}")
print()

tasks = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8765/api/tasks/?project_id={pid}", timeout=5).read())
if isinstance(tasks, dict):
    tasks = tasks.get("tasks", [])

# Group by phase
v1_ids = ("t-53645d1e", "t-7e7f055e", "t-36508b93", "t-43477a67", "t-e9f9df5d", "t-9a2feeb2")
v2 = [t for t in tasks if t["id"] not in v1_ids]
review = [t for t in v2 if t.get("action", "").startswith("_iteration_review:")]
data = [t for t in v2 if t not in review]

print(f"v2 data tasks: {len(data)}")
for t in data:
    print(f"  - {t['name']!r} status={t['status']}")
print(f"\nv2 review tasks: {len(review)}")
for t in review:
    print(f"  - {t['name']!r} status={t['status']} iter_meta={t.get('params', {}).get('iteration')}")
