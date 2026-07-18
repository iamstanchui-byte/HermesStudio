"""Test replan resets iter + clears decision.md."""
import json
import os
import time
import urllib.request

pid = "proj-48b50520"

# Check decision.md exists
dpath = rf"C:\Project\minimax code\hermes-project\{pid}\decision.md"
print(f"before replan: decision.md exists = {os.path.exists(dpath)}")

# Check current state
p = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8765/api/projects/{pid}", timeout=5).read())
print(f"before: state={p['state']} iter={p['current_iteration']} last_summary={p.get('last_iteration_summary', '')[:50]!r}")

# Replan
goal = (
    "Read the v1 Chinese brief at projects/proj-48b50520/cpi_ppi_gold_中文简报_2026年7月.txt. "
    "Now extend the analysis with XAUUSD correlation: fetch 24 months of XAUUSD monthly closes "
    "via MT5 bridge (http://localhost:5001/candles/XAUUSD?tf=MN&count=24), compute Pearson correlation "
    "and lead-lag analysis between CPI/PPI and XAUUSD returns. Add the new 'XAUUSD Correlation' "
    "section to the existing report and save as report_v3.md."
)
req = urllib.request.Request(
    f"http://127.0.0.1:8765/api/projects/{pid}/replan",
    data=json.dumps({"goal": goal, "clear_tasks": True}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
r = urllib.request.urlopen(req, timeout=10)
print(f"replan: {r.status} {json.loads(r.read())['state']}")

# After replan, check iter was reset and decision.md was unlinked
p = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8765/api/projects/{pid}", timeout=5).read())
print(f"after replan: state={p['state']} iter={p['current_iteration']} last_summary={p.get('last_iteration_summary', '')[:50]!r}")
print(f"after replan: decision.md exists = {os.path.exists(dpath)}")
