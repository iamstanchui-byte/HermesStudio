"""E2E test 2 for self-teach: action is realistic, no explicit self-teach
instruction. We rely on the wrapper's injected hint telling the agent
the absolute path to the profile's skills/ dir.

The agent should see the task, do its work, and (because the API it just
used is reusable) self-teach a skill doc at the absolute path the
wrapper injects.
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


# Cleanup any previous test skill
for name in ("hk-weather",):
    stale = os.path.join(SKILLS_DIR, name + ".md")
    if os.path.exists(stale):
        os.unlink(stale)
        print(f"cleaned up stale {stale}")

# 1) Create a project (manual mode)
print("=== creating project ===")
proj = post(
    "/api/projects/",
    {"name": "self-teach-test-2", "mode": "manual", "goal": ""},
)
pid = proj["id"]
print(f"  project {pid}")

# 2) Add a task with a realistic action: "query HK weather". NO mention of
# self-teach or any specific output path. The agent should infer that the
# wttr.in API is reusable and (because of the wrapper's prompt hint) write
# a skill doc to the profile's skills/ dir.
print("\n=== adding task (realistic, no self-teach instruction) ===")
task_action = (
    "Use the terminal tool to fetch the current Hong Kong weather from "
    "https://wttr.in/Hong+Kong?format=j1 (just the current_condition "
    "section is enough). Report back the current temp_C, humidity, and "
    "weatherDesc[0].value."
)
task = post(
    "/api/tasks/",
    {
        "project_id": pid,
        "name": "Query HK weather",
        "agent_role": "win-agent01",
        "action": task_action,
        "params": {"yolo": True},
        "output_path": "output.txt",
    },
)
tid = task["id"]
print(f"  task {tid}")

# 3) Wait for completion
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

# 4) Check if skill file appeared at the right path
target_skill = os.path.join(SKILLS_DIR, "hk-weather.md")
print(f"\n=== checking for skill at: {target_skill} ===")
if os.path.exists(target_skill):
    sz = os.path.getsize(target_skill)
    print(f"  PASS: file exists, {sz} bytes")
else:
    print("  FAIL: file not found")
    print("  the agent may not have decided to self-teach (which is fine — the hint is permissive)")

# 5) If file appeared, wait for auto-sync to push it to orchestrator
if os.path.exists(target_skill):
    print("\n=== waiting 35s for wrapper auto-sync (30s throttle) ===")
    time.sleep(35)
    skills = {s["name"]: s for s in get("/api/agents/win-local-1/profiles/win-agent01/skills")}
    if "hk-weather" in skills:
        print(f"  PASS: dashboard knows hk-weather, status={skills['hk-weather']['status']} size={skills['hk-weather']['size']}")
        got = get("/api/agents/win-local-1/profiles/win-agent01/skills/hk-weather")
        print(f"  content preview: {got['content'][:200]!r}")
    else:
        print(f"  known skills: {list(skills.keys())}")
        print("  FAIL: dashboard doesn't know hk-weather")
