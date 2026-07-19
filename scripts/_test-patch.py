"""Test PATCH endpoint with various body formats."""
import httpx

base = "http://localhost:8765"
url = f"{base}/api/agents/win-local-1/profiles/win-agent01"

body1 = '{"capabilities": {"mt5": true}}'
print(f"Test 1 body: {body1!r}")
r = httpx.patch(url, json={"capabilities": {"mt5": True}}, timeout=10)
print(f"  httpx.json= body: {r.status_code}  {r.text[:200]}")
print()

r2 = httpx.patch(url, content=body1, headers={"Content-Type": "application/json"}, timeout=10)
print(f"Test 2 raw content: {r2.status_code}  {r2.text[:200]}")
print()

body3 = '{"description": "", "capabilities": {"mt5": true}}'
r3 = httpx.patch(url, content=body3, headers={"Content-Type": "application/json"}, timeout=10)
print(f"Test 3 description+capabilities: {r3.status_code}  {r3.text[:200]}")
