"""Clean up debug tasks from prior runs."""
import urllib.request
import json

with urllib.request.urlopen("http://localhost:8765/api/tasks/?project_id=proj-d07a152f") as r:
    d = json.loads(r.read())
for t in d.get("tasks", []):
    name = t.get("name", "") or ""
    if "UI-debug-task" in name or "UI-created task" in name:
        tid = t["id"]
        req = urllib.request.Request(f"http://localhost:8765/api/tasks/{tid}", method="DELETE")
        try:
            urllib.request.urlopen(req)
            print("deleted", tid, name)
        except Exception as e:
            print("delete err", tid, e)

# Final state
with urllib.request.urlopen("http://localhost:8765/api/tasks/?project_id=proj-d07a152f") as r:
    d = json.loads(r.read())
print("remaining:")
for t in d.get("tasks", []):
    print(" ", t["id"], t["name"])
