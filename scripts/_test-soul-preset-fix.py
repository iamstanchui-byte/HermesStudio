"""Test the SOUL preset form fix on project proj-48b50520."""
import json
import urllib.request

# Get latest project
r = urllib.request.urlopen("http://127.0.0.1:8765/api/projects/?limit=1", timeout=5)
projects = json.loads(r.read())
if isinstance(projects, dict):
    projects = projects.get("projects", [])
pid = projects[0]["id"]
print(f"testing {pid} '{projects[0]['name']}'")

# PUT a preset
req = urllib.request.Request(
    f"http://127.0.0.1:8765/api/projects/{pid}/soul-presets",
    data=json.dumps({"agent_id": "linux-a-01", "profile_name": "super", "content": "You are a CPI/PPI analyst specialist."}).encode(),
    headers={"Content-Type": "application/json"},
    method="PUT",
)
r = urllib.request.urlopen(req, timeout=5)
print(f"PUT status: {r.status}")
preset = json.loads(r.read())
print(f"  id={preset['id']} profile_name={preset['profile_name']} size={len(preset['content'])}")

# List presets
r = urllib.request.urlopen(f"http://127.0.0.1:8765/api/projects/{pid}/soul-presets", timeout=5)
presets = json.loads(r.read())
print(f"presets: {len(presets)}")
for p in presets:
    print(f"  - {p['agent_id']}/{p['profile_name']} status={p['status']}")

# Delete the test preset
req = urllib.request.Request(
    f"http://127.0.0.1:8765/api/projects/{pid}/soul-presets/linux-a-01/super",
    method="DELETE",
)
r = urllib.request.urlopen(req, timeout=5)
print(f"DELETE status: {r.status}")
