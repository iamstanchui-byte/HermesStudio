"""Smoke test: list contracts + invoke plan in mock mode."""
import io
import json
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# List
r = urllib.request.urlopen("http://127.0.0.1:8765/api/contracts", timeout=10)
d = json.loads(r.read().decode("utf-8"))
print("=== Contracts ===")
for c in d["contracts"]:
    impl = "✓" if c["implemented"] else "."
    print(f"  [{impl}] {c['name']:10} {c['description'][:60]}")

# Invoke plan in mock mode
print("\n=== POST /api/contracts/plan/draft ===")
body = json.dumps({
    "input": {
        "project_name": "smoke-test",
        "project_goal": "verify contracts API works",
        "available_skills": [
            {"name": "smoke-fetch-url", "deterministic": True},
        ],
    },
}).encode("utf-8")
req = urllib.request.Request(
    "http://127.0.0.1:8765/api/contracts/plan/draft",
    data=body, method="POST",
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
        print(f"  contract: {d.get('contract')}")
        print(f"  implemented: {d.get('implemented')}")
        print(f"  output keys: {sorted(d.get('output', {}).keys())}")
except urllib.error.HTTPError as e:
    raw = e.read().decode("utf-8")
    print(f"  status: {e.code}")
    print(f"  body: {raw[:300]}")
