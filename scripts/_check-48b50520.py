"""Check project 48b50520 tasks."""
import json
import urllib.request

pid = "proj-48b50520"
r = urllib.request.urlopen(f"http://127.0.0.1:8765/api/projects/{pid}", timeout=5)
proj = json.loads(r.read())
print(f"project: {proj['name']} ({pid})")
print(f"  state: {proj['state']}")
print(f"  goal: {proj['goal']!r}")
print(f"  coordinator: {proj.get('coordinator_role')!r}")
print(f"  iter: {proj.get('current_iteration')}/{proj.get('max_iterations')}")
print(f"  last_summary: {(proj.get('last_iteration_summary') or '')[:120]!r}")
print()

tasks = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8765/api/tasks/?project_id={pid}", timeout=5).read())
if isinstance(tasks, dict):
    tasks = tasks.get("tasks", [])
print(f"tasks: {len(tasks)}")
for t in tasks:
    print(f"  - {t['id']} {t['name']!r}")
    print(f"      status={t['status']} role={t['agent_role']}")
    print(f"      action={t['action'][:120]!r}")
    print(f"      output_path={t.get('output_path')!r}")
    print(f"      depends_on={t.get('depends_on')!r}")
    print(f"      result_keys={list((t.get('result') or {}).keys())[:5]}")
