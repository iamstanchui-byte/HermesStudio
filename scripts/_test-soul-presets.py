"""E2E test for SOUL presets:
1. Create project A (CPI/PPI) and project B (server monitor)
2. Save distinct SOUL presets to each
3. Apply project A's preset to win-agent01
4. Wait 8s for wrapper to apply
5. Verify the SOUL.md on disk matches preset A
6. Apply project B's preset (different content)
7. Verify SOUL.md on disk now matches preset B
"""
import hashlib
import json
import os
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
PROFILE_ID = "win-local-1"
PROFILE_NAME = "win-agent01"
SOUL_PATH = r"C:\Users\stanley\AppData\Local\hermes\profiles\win-agent01\SOUL.md"


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    try:
        r = urllib.request.urlopen(req, timeout=10)
        body = r.read()
        return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# 1) Two projects
print("=== creating projects ===")
status_a, pa = api("POST", "/api/projects/", {"name": "CPI/PPI XAU", "mode": "manual", "goal": ""})
status_b, pb = api("POST", "/api/projects/", {"name": "Server monitor", "mode": "manual", "goal": ""})
print(f"  A: {pa['id']} (status={status_a})")
print(f"  B: {pb['id']} (status={status_b})")
assert status_a == 201 and status_b == 201
pid_a = pa["id"]
pid_b = pb["id"]

# 2) Save distinct SOUL presets
preset_a = (
    "# CPI/PPI Analyst\n\n"
    "You are a financial analyst focused on US macroeconomic data (CPI, PPI) "
    "and their correlation with XAUUSD. Use the MT5 bridge at "
    "http://localhost:5001 for market data, and the web for news.\n"
)
preset_b = (
    "# Server Monitor\n\n"
    "You are a server monitoring agent. Use curl/SSH to check health of "
    "Linux boxes. Alert on CPU > 80%, disk > 90%, or service failures.\n"
)
print("\n=== saving presets ===")
status, _ = api("PUT", f"/api/projects/{pid_a}/soul-presets", {
    "agent_id": PROFILE_ID, "profile_name": PROFILE_NAME, "content": preset_a,
})
print(f"  A preset saved: status={status}")
assert status == 200
status, _ = api("PUT", f"/api/projects/{pid_b}/soul-presets", {
    "agent_id": PROFILE_ID, "profile_name": PROFILE_NAME, "content": preset_b,
})
print(f"  B preset saved: status={status}")
assert status == 200

# 3) List presets for project A
status, presets = api("GET", f"/api/projects/{pid_a}/soul-presets")
print(f"\n=== project A presets: ===")
for p in presets:
    print(f"  {p['agent_id']}/{p['profile_name']}: {p['content'][:60]!r}...")
assert len(presets) == 1

# 4) Apply preset A
print(f"\n=== applying project A preset to {PROFILE_ID}/{PROFILE_NAME} ===")
status, written = api("POST", f"/api/projects/{pid_a}/soul-presets/apply", {})
print(f"  applied: {written}")
assert status == 200

# 5) Wait for wrapper to apply (5-10s)
print("\n=== waiting 8s for wrapper to apply SOUL.md ===")
time.sleep(8)

# 6) Verify SOUL.md on disk matches preset A
print(f"\n=== checking SOUL.md at: {SOUL_PATH} ===")
if not os.path.exists(SOUL_PATH):
    print(f"  FAIL: SOUL.md does not exist")
    sys.exit(1)
with open(SOUL_PATH, encoding="utf-8") as f:
    actual = f.read()
sha_actual = hashlib.sha256(actual.encode()).hexdigest()
sha_expected_a = hashlib.sha256(preset_a.encode()).hexdigest()
print(f"  size: {len(actual)} bytes")
print(f"  expected sha (preset A): {sha_expected_a[:16]}")
print(f"  actual sha:               {sha_actual[:16]}")
if sha_actual == sha_expected_a:
    print("  PASS: SOUL.md matches preset A")
else:
    print(f"  content: {actual[:200]!r}")
    print("  FAIL: SOUL.md does not match preset A")
    sys.exit(1)

# 7) Apply preset B
print(f"\n=== applying project B preset (overwriting) ===")
status, written_b = api("POST", f"/api/projects/{pid_b}/soul-presets/apply", {})
print(f"  applied: {written_b}")
assert status == 200

# 8) Wait + verify
print("\n=== waiting 8s for wrapper to overwrite SOUL.md ===")
time.sleep(8)
with open(SOUL_PATH, encoding="utf-8") as f:
    actual = f.read()
sha_actual = hashlib.sha256(actual.encode()).hexdigest()
sha_expected_b = hashlib.sha256(preset_b.encode()).hexdigest()
print(f"  size: {len(actual)} bytes")
print(f"  expected sha (preset B): {sha_expected_b[:16]}")
print(f"  actual sha:               {sha_actual[:16]}")
if sha_actual == sha_expected_b:
    print("  PASS: SOUL.md now matches preset B")
else:
    print(f"  content: {actual[:200]!r}")
    print("  FAIL: SOUL.md does not match preset B")
    sys.exit(1)

print("\n=== ALL E2E PASS ===")
