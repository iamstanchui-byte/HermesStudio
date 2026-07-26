"""Smoke test for /api/contracts/optimize-tasks endpoint."""
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
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


# Use proj-8fece23e (the test-archived-toggle project)
# It has 6 tasks (3 pending + 3 completed)
print("=== POST /api/contracts/optimize-tasks ===")
s, d = http("POST", "/api/contracts/optimize-tasks", {"project_id": "proj-8fece23e"})
print(f"  status={s}")
if s == 200:
    print(f"  project_id: {d['project_id']}")
    print(f"  task_count_analyzed: {d['task_count_analyzed']}")
    print(f"  suggested_count: {d['suggested_count']}")
    print(f"  generated_at: {d['generated_at']}")
    print(f"  overall_notes: {d['overall_notes'][:200]}")
    for i, sug in enumerate(d['suggestions'][:5]):
        print(f"  [{i+1}] {sug['task_name']} (confidence {sug['confidence']})")
        print(f"      -> {sug['suggested_skill_name']}")
        print(f"      {sug['rationale'][:150]}")
elif s == 404:
    print(f"  project not found")
elif s == 502:
    print(f"  LLM error: {d.get('detail', '')[:300]}")
else:
    print(f"  body: {d}")
