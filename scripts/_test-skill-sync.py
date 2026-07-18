"""E2E test: put a skill file directly on the agent filesystem, then trigger
the dashboard's 'Sync from disk' button via API, and verify the orchestrator
registers it.
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


def post(path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


# 1) Write a skill file directly on the agent filesystem (simulating either
#    an operator copying in a file via Explorer, or an agent self-teaching
#    itself by writing a skill doc).
name = "manual-import-test"
file_path = os.path.join(SKILLS_DIR, name + ".md")
content = "# Manual Import Test\n\nThis skill was placed on the agent host manually,\nthen registered with the orchestrator via the 'Sync from disk' button.\n"
print(f"=== writing file to disk: {file_path} ===")
os.makedirs(SKILLS_DIR, exist_ok=True)
with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)
print(f"  wrote {os.path.getsize(file_path)} bytes")

# 2) Check the orchestrator doesn't know about it yet
print("\n=== orchestrator view (should be empty for this skill) ===")
skills = {s["name"]: s for s in get("/api/agents/win-local-1/profiles/win-agent01/skills")}
print(f"  known skills: {list(skills.keys())}")
assert name not in skills, f"FAIL: {name!r} already in DB before sync"

# 3) Click the "Sync from disk" button (POST .../skills/sync)
print(f"\n=== triggering sync ===")
triggered = post("/api/agents/win-local-1/profiles/win-agent01/skills/sync")
print(f"  trigger config id={triggered['id']} file={triggered['file_path']}")

# 4) Wait for the wrapper to apply the marker + scan + push the new file
print("\n=== waiting 12s for wrapper to pick up trigger + sync ===")
time.sleep(12)
skills_after = {s["name"]: s for s in get("/api/agents/win-local-1/profiles/win-agent01/skills")}
print(f"  known skills: {list(skills_after.keys())}")
if name in skills_after:
    print(f"  PASS: {name} is registered, status={skills_after[name]['status']} size={skills_after[name]['size']}")
else:
    print(f"  FAIL: {name} not registered")
    sys.exit(1)

# 5) Get the content back
got = get(f"/api/agents/win-local-1/profiles/win-agent01/skills/{name}")
print(f"  content: {got['content']!r}")
assert "Manual Import Test" in got["content"], "FAIL: content mismatch"
print("  PASS: content matches")

# 6) Delete the test file (cleanup)
print(f"\n=== cleanup ===")
try:
    os.unlink(file_path)
    print(f"  removed {file_path}")
except Exception as e:
    print(f"  WARN: {e}")
print("done")
