"""E2E test for self-teach flow:
1. Create a task on win-agent01 (manual mode, no goal) with action that
   tells the agent to query HK weather via wttr.in AND write a self-teach
   skill doc to ../skills/hk-weather.md.
2. Wait for task to complete.
3. Trigger dashboard Sync from disk (button simulates operator).
4. Verify the new skill appears in orchestrator dashboard.
"""
import json
import os
import subprocess
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


# 0) Cleanup any leftover test skill
stale = os.path.join(SKILLS_DIR, "hk-weather.md")
if os.path.exists(stale):
    os.unlink(stale)
    print(f"cleaned up stale {stale}")

# 1) Create a project (manual mode)
print("=== creating project (manual mode, win-agent01 task) ===")
proj = post(
    "/api/projects/",
    {
        "name": "self-teach-test",
        "mode": "manual",
        "goal": "",  # manual mode, no goal
    },
)
pid = proj["id"]
print(f"  project {pid} state={proj['state']}")

# 2) Add a task that:
#    - Queries HK weather via wttr.in
#    - Writes a self-teach doc to ../skills/hk-weather.md
# The action string is what the agent sees as `action(...)` in its prompt.
# We embed the self-teach instructions directly in the action so the agent
# has a concrete instruction to follow.
print("\n=== adding task ===")
task_action = (
    "self_teach_demo: "
    "1. Use `terminal` to run: curl -s 'https://wttr.in/Hong+Kong?format=j1' "
    "(just confirm the tool works, don't fail if blocked). "
    "2. Write a reusable skill doc to ../skills/hk-weather.md that documents "
    "the wttr.in format=j1 API (returns 3-day forecast as JSON, key fields: "
    "current_condition, weather, maxtempC, mintempC, hourly). "
    "Use markdown. The file path is relative to your current working dir."
)
task = post(
    f"/api/tasks/",
    {
        "project_id": pid,
        "name": "self-teach: query HK weather and write skill doc",
        "agent_role": "win-agent01",
        "action": task_action,
        "params": {"yolo": True},  # auto-approve tool calls
        "output_path": "output.txt",
    },
)
tid = task["id"]
print(f"  task {tid}")

# 3) Wait for task to complete (poll status)
print("\n=== waiting for task to complete (up to 90s) ===")
deadline = time.time() + 90
final_status = None
while time.time() < deadline:
    t = get(f"/api/tasks/{tid}")
    if t["status"] in ("completed", "failed", "cancelled", "interrupted", "skipped"):
        final_status = t["status"]
        break
    time.sleep(3)
print(f"  final status: {final_status}")
if final_status != "completed":
    print(f"  task summary: {t.get('result', {}).get('summary', '')[:300]}")

# 4) Check if skill file appeared on disk
target_skill = os.path.join(SKILLS_DIR, "hk-weather.md")
print(f"\n=== checking for skill on disk: {target_skill} ===")
print(f"  exists: {os.path.exists(target_skill)}")
if os.path.exists(target_skill):
    sz = os.path.getsize(target_skill)
    print(f"  size: {sz} bytes")

# 5) Trigger Sync from disk (simulates dashboard button)
print("\n=== triggering sync ===")
post(f"/api/agents/win-local-1/profiles/win-agent01/skills/sync")

# 6) Wait for auto-sync / wrapper to push to orchestrator
print("\n=== waiting 35s for wrapper auto-sync (30s throttle) ===")
time.sleep(35)
skills = {s["name"]: s for s in get("/api/agents/win-local-1/profiles/win-agent01/skills")}
print(f"  known skills: {list(skills.keys())}")
if "hk-weather" in skills:
    print(f"\n  PASS: hk-weather is registered, status={skills['hk-weather']['status']} "
          f"size={skills['hk-weather']['size']}")
    got = get(f"/api/agents/win-local-1/profiles/win-agent01/skills/hk-weather")
    print(f"  content preview: {got['content'][:200]!r}")
else:
    print("\n  FAIL: hk-weather not registered")
    print("  check daemon.out.log for self-teach activity")
    sys.exit(1)
