"""Smoke test for /api/single-tasks endpoint."""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"


def http(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


# 1. List single tasks (initially empty)
print("=== GET /api/single-tasks ===")
s, d = http("GET", "/api/single-tasks")
print(f"  status={s} count={d.get('count')}")

# 2. Create a single task
print()
print("=== POST /api/single-tasks ===")
s, t = http("POST", "/api/single-tasks", {
    "name": "test single task",
    "goal": "verify the single-tasks endpoint works",
    "source": {"kind": "test", "note": "created by smoke test"},
})
print(f"  status={s} id={t.get('id')}")
print(f"  is_single={t.get('is_single_task') if isinstance(t, dict) else 'n/a'}")

# 3. Get it back
print()
print("=== GET /api/single-tasks/{id} ===")
s, g = http("GET", f"/api/single-tasks/{t['id']}")
print(f"  status={s} name={g.get('name')} status={g.get('status')}")

# 4. List (should now have 1)
print()
print("=== GET /api/single-tasks (after create) ===")
s, d = http("GET", "/api/single-tasks")
print(f"  status={s} count={d['count']} first={d['tasks'][0]['name']}")

# 5. 404
print()
print("=== GET /api/single-tasks/nonexistent ===")
s, _ = http("GET", "/api/single-tasks/nonexistent-id")
print(f"  status={s}")

# 6. Cleanup
print()
print("=== DELETE cleanup ===")
req = urllib.request.Request(f"{BASE}/api/tasks/{t['id']}", method="DELETE")
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        print(f"  delete status={r.status}")
except urllib.error.HTTPError as e:
    print(f"  delete status={e.code}")
