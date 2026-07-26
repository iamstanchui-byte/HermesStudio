"""Test: create a single task, wait for supervisor to dispatch it."""
import json
import time
import urllib.request

# Create a single task
body = json.dumps({
    "name": "supervisor dispatch test",
    "goal": "verify the supervisor dispatches single tasks",
    "source": {"kind": "test", "tag": "supervisor-fix"},
}).encode("utf-8")
req = urllib.request.Request(
    "http://127.0.0.1:8765/api/single-tasks",
    data=body, method="POST",
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=10) as r:
    t = json.loads(r.read())
    print(f"created: {t['id']} status={t['status']}")

# Wait for supervisor to dispatch (polls every 5s)
for i in range(20):
    time.sleep(3)
    r = urllib.request.urlopen(
        f"http://127.0.0.1:8765/api/single-tasks/{t['id']}", timeout=5
    )
    cur = json.loads(r.read())
    print(
        f"t+{(i+1)*3}s: status={cur['status']} agent={cur.get('assigned_profile_id', '—')}"
    )
    if cur["status"] in ("assigned", "running", "completed", "failed"):
        if cur["status"] != "pending":
            print(f"  >>> DISPATCHED: {cur['status']}")
            break

# Cleanup
req = urllib.request.Request(
    f"http://127.0.0.1:8765/api/tasks/{t['id']}", method="DELETE"
)
try:
    urllib.request.urlopen(req, timeout=5)
    print("cleaned up")
except Exception as e:
    print(f"cleanup error: {e}")
