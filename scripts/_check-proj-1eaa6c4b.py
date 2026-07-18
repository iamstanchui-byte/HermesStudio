"""Check project 1eaa6c4b state."""
import json
import urllib.request

r = urllib.request.urlopen("http://127.0.0.1:8765/api/projects/proj-1eaa6c4b", timeout=5)
proj = json.loads(r.read())
print(f"project: {proj['name']}")
print(f"  state: {proj['state']}")
print(f"  goal: {proj['goal'][:60]!r}")
print(f"  coordinator: {proj.get('coordinator_role')!r}")
print(f"  max_iter: {proj.get('max_iterations')}")
print(f"  accept_criteria: {proj.get('accept_criteria')!r}")
