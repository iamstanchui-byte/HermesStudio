"""Check project cfd63f3a."""
import json
import urllib.request

pid = "proj-cfd63f3a"
r = urllib.request.urlopen(f"http://127.0.0.1:8765/api/projects/{pid}", timeout=5)
proj = json.loads(r.read())
print(f"project: {proj['name']} ({pid})")
print(f"  state: {proj['state']}")
print(f"  goal: {proj['goal'][:80]!r}")
print(f"  coordinator: {proj.get('coordinator_role')!r}")
print(f"  max_iter: {proj.get('max_iterations')}")
print(f"  accept_criteria: {proj.get('accept_criteria')!r}")
print(f"  current_iteration: {proj.get('current_iteration')}")
print(f"  last_summary: {proj.get('last_iteration_summary')!r}")
print()

tasks_resp = urllib.request.urlopen(f"http://127.0.0.1:8765/api/tasks/?project_id={pid}", timeout=5)
tasks = json.loads(tasks_resp.read())
if isinstance(tasks, dict):
    tasks = tasks.get("tasks", [])
print(f"tasks: {len(tasks)}")
for t in tasks:
    print(f"  - {t['id']} {t['name']!r}")
    print(f"      status={t['status']} role={t['agent_role']}")
    print(f"      action={t['action']!r}")
    print(f"      output_path={t.get('output_path')!r}")
    print(f"      params={t.get('params')}")
