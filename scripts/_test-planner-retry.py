"""Test replan with the v2 goal (XAUUSD correlation analysis)."""
import json
import time
import urllib.request

pid = "proj-48b50520"
goal = (
    "Re-analyze the existing Chinese brief at projects/proj-48b50520/cpi_ppi_gold_中文简报_2026年7月.txt. "
    "Use the existing CPI data file (cpi_data_jun2024_jun2026.txt) and PPI data file (ppi_data_jul2024_jul2026.txt) "
    "to extract monthly CPI/PPI change rates. Then fetch XAUUSD monthly prices for the same 24-month period "
    "from the MT5 bridge at http://localhost:5001/candles/XAUUSD?tf=MN&count=24. Compute Pearson correlation "
    "between monthly CPI YoY, PPI YoY, and XAUUSD monthly returns. Add a lead-lag analysis at -2/-1/0 months. "
    "Output report_v2.md (under the project folder) with the new section 'XAUUSD Correlation Analysis' "
    "and a correlation matrix table."
)
print("=== replan with v2 goal ===")
req = urllib.request.Request(
    f"http://127.0.0.1:8765/api/projects/{pid}/replan",
    data=json.dumps({"goal": goal, "clear_tasks": True}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
r = urllib.request.urlopen(req, timeout=10)
result = json.loads(r.read())
print(f"  state={result['state']} cleared={result['cleared_tasks']}")

# Wait for planner + tasks
print("\n=== waiting 30s for planner ===")
for i in range(10):
    p = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8765/api/projects/{pid}", timeout=5).read())
    tasks_resp = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8765/api/tasks/?project_id={pid}", timeout=5).read())
    if isinstance(tasks_resp, dict):
        tasks_resp = tasks_resp.get("tasks", [])
    nonterm = [t for t in tasks_resp if t["status"] not in ("completed", "failed", "cancelled", "skipped", "interrupted")]
    print(f"  t+{(i+1)*3}s state={p['state']} iter={p.get('current_iteration')} tasks={len(tasks_resp)} nonterm={len(nonterm)}")
    for t in tasks_resp:
        print(f"    - {t['name']:<50} status={t['status']:<12} role={t['agent_role']}")
    if p["state"] == "running" and nonterm:
        print("\n  planner succeeded, tasks running")
        break
    if p["state"] == "ready" and tasks_resp:
        print("\n  planner succeeded, tasks pending")
        break
    time.sleep(3)
