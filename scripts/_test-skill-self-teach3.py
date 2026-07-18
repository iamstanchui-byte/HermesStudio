"""E2E test 3: action asks agent to use the absolute path injected by the
wrapper's self-teach hint. This verifies that the hint's path is reachable
and that the agent CAN self-teach when it has a reason to.

The agent is told to query HK weather, then self-teach a reusable skill doc
about wttr.in. The skill path is intentionally NOT specified in the action
— the agent must extract it from the wrapper's context hint.
"""
import json
import os
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
SKILLS_DIR = r"C:\Users\stanley\AppData\Local\hermes\profiles\win-agent01\skills"


def get(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=5).read())


def post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


# Cleanup
stale = os.path.join(SKILLS_DIR, "hk-weather.md")
if os.path.exists(stale):
    os.unlink(stale)
    print(f"cleaned up stale {stale}")

# Create project
print("=== creating project ===")
proj = post("/api/projects/", {"name": "self-teach-3", "mode": "manual", "goal": ""})
pid = proj["id"]
print(f"  project {pid}")

# Add task: query weather, then self-teach using the absolute path
# provided by the wrapper's hint. The action does NOT specify any path —
# the agent must use the hint's path.
print("\n=== adding task ===")
task_action = (
    "1. Use the terminal tool to fetch the current Hong Kong weather from "
    "https://wttr.in/Hong+Kong?format=j1. Report back the current temp_C, "
    "humidity, and weatherDesc[0].value.\n"
    "2. Since the wttr.in format=j1 API is genuinely reusable for future "
    "weather queries, write a markdown skill doc about it. Use the "
    "absolute path from the SKILL SELF-TEACHING section of the project "
    "context (substitute the skill name 'hk-weather' for <name>). "
    "Document the endpoint, key response fields, and a curl example."
)
task = post(
    "/api/tasks/",
    {
        "project_id": pid,
        "name": "Query HK weather + self-teach skill",
        "agent_role": "win-agent01",
        "action": task_action,
        "params": {"yolo": True},
        "output_path": "output.txt",
    },
)
tid = task["id"]
print(f"  task {tid}")

# Wait for completion
print("\n=== waiting for task to complete (up to 90s) ===")
deadline = time.time() + 90
while time.time() < deadline:
    t = get(f"/api/tasks/{tid}")
    if t["status"] in ("completed", "failed", "cancelled", "interrupted", "skipped"):
        break
    time.sleep(3)
print(f"  final status: {t['status']}")
if t["status"] != "completed":
    print(f"  error: {t.get('error')}")
    sys.exit(1)

# Check skill file
target = os.path.join(SKILLS_DIR, "hk-weather.md")
print(f"\n=== checking for skill at: {target} ===")
if os.path.exists(target):
    sz = os.path.getsize(target)
    print(f"  PASS: file exists, {sz} bytes")
    # Wait for auto-sync
    print("\n=== waiting 35s for wrapper auto-sync ===")
    time.sleep(35)
    skills = {s["name"]: s for s in get("/api/agents/win-local-1/profiles/win-agent01/skills")}
    if "hk-weather" in skills:
        print(f"  PASS: dashboard knows hk-weather, status={skills['hk-weather']['status']} size={skills['hk-weather']['size']}")
        got = get("/api/agents/win-local-1/profiles/win-agent01/skills/hk-weather")
        print(f"  content preview: {got['content'][:200]!r}")
    else:
        print(f"  known skills: {list(skills.keys())}")
        print("  FAIL: dashboard doesn't know hk-weather")
        sys.exit(1)
else:
    print("  FAIL: file not found in profile skills/")
    sys.exit(1)
