"""Seed a project with 3 pending tasks for Stage 3.5 UI E2E."""
import urllib.request
import json

BASE = "http://localhost:8765"


def _post(path, body):
    req = urllib.request.Request(
        BASE + path,
        method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


# 1. Create project
proj = _post("/api/projects/", {"name": "e2e-stage35-ui"})
pid = proj["id"]
print("project:", proj["id"], proj["name"], "state=" + proj["state"])

# 2. Add 3 pending tasks (chained via depends_on)
# task 1 = A (no deps)
tA = _post("/api/tasks/", {
    "project_id": pid, "name": "A: list folders",
    "agent_role": "win-agent01", "action": "list_folders",
    "params": {"root": "project_temp_folder"},
    "depends_on": [],
})
print("  A:", tA["id"], "status=" + tA["status"])

# task 2 = B (depends on A)
tB = _post("/api/tasks/", {
    "project_id": pid, "name": "B: count 2026",
    "agent_role": "win-agent01", "action": "count_matching",
    "params": {"pattern": "2026"},
    "depends_on": [tA["id"]],
})
print("  B:", tB["id"], "status=" + tB["status"], "deps=" + str(tB["depends_on"]))

# task 3 = C (depends on B)
tC = _post("/api/tasks/", {
    "project_id": pid, "name": "C: write report",
    "agent_role": "win-agent02", "action": "write_report",
    "params": {"format": "md"},
    "depends_on": [tB["id"]],
})
print("  C:", tC["id"], "status=" + tC["status"], "deps=" + str(tC["depends_on"]))

print("\nURL: /projects/" + pid + "/visual")
