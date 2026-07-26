"""Smoke test the /reset-and-cascade endpoint."""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import urllib.request
import json

BASE = "http://localhost:8765"


def _api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, method=method, data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()) if r.length else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _force_status(task_id, status):
    """Manually set task status via SQL (not exposed as API)."""
    import sqlite3
    conn = sqlite3.connect("C:/Users/stanley/.hermes-orchestrator/hermes-orch.db")
    conn.execute("UPDATE tasks SET status=?, updated_at=datetime('now') WHERE id=?", (status, task_id))
    conn.commit()
    conn.close()


def _set_result(task_id, summary):
    """Set a fake result on a task so we can verify it gets cleared."""
    import sqlite3
    conn = sqlite3.connect("C:/Users/stanley/.hermes-orchestrator/hermes-orch.db")
    conn.execute("UPDATE tasks SET result=? WHERE id=?", (json.dumps({"summary": summary}), task_id))
    conn.commit()
    conn.close()


# ---- Setup: create project with 3 tasks A->B->C ----
print("[setup] creating project + 3 tasks")
status, proj = _api("POST", "/api/projects/", {"name": "e2e-reset-cascade"})
pid = proj["id"]
print("  project:", pid)

tA = _api("POST", "/api/tasks/", {
    "project_id": pid, "name": "A: source",
    "agent_role": "win-agent01", "action": "act_a",
    "depends_on": [],
})[1]
tB = _api("POST", "/api/tasks/", {
    "project_id": pid, "name": "B: middle",
    "agent_role": "win-agent01", "action": "act_b",
    "depends_on": [tA["id"]],
})[1]
tC = _api("POST", "/api/tasks/", {
    "project_id": pid, "name": "C: leaf",
    "agent_role": "win-agent01", "action": "act_c",
    "depends_on": [tB["id"]],
})[1]
print("  A:", tA["id"], "B:", tB["id"], "C:", tC["id"])

# Manually mark them all completed (with fake results)
for t, label in [(tA, "A-result"), (tB, "B-result"), (tC, "C-result")]:
    _force_status(t["id"], "completed")
    _set_result(t["id"], label)

# Set project to completed
import sqlite3
conn = sqlite3.connect("C:/Users/stanley/.hermes-orchestrator/hermes-orch.db")
conn.execute("UPDATE projects SET state='completed', updated_at=datetime('now') WHERE id=?", (pid,))
conn.commit()
conn.close()
print("  forced all tasks to 'completed' + project to 'completed'")

# ---- TEST 1: Reset A with cascade (default) ----
print("\n[1] POST /reset-and-cascade A (include_downstream=True default)")
status, body = _api("POST", f"/api/tasks/{tA['id']}/reset-and-cascade", {})
print(f"  status={status}, body keys={list(body.keys()) if body else 'None'}")
print(f"  -> task A status: {body.get('status')}")
assert status == 200, f"expected 200, got {status}"
assert body.get("status") == "pending", f"expected pending, got {body.get('status')}"
assert body.get("result") is None, f"expected None result, got {body.get('result')}"

# Verify B and C were also reset
for t, name in [(tB, "B"), (tC, "C")]:
    status, tb = _api("GET", f"/api/tasks/{t['id']}")
    print(f"  -> task {name} status: {tb.get('status')}, result: {tb.get('result')}")
    assert tb.get("status") == "pending", f"expected {name} pending, got {tb.get('status')}"
    assert tb.get("result") is None, f"expected {name} result None, got {tb.get('result')}"

# Verify project is woken up to ready
status, p = _api("GET", f"/api/projects/{pid}")
print(f"  -> project state: {p.get('state')}")
assert p.get("state") == "ready", f"expected project ready, got {p.get('state')}"

# ---- TEST 2: Reset B with include_downstream=False ----
print("\n[2] set all to completed, then reset B with include_downstream=False")
for t in [tA, tB, tC]:
    _force_status(t["id"], "completed")
    _set_result(t["id"], f"{t['name']}-result-2")
# Re-set project to completed
conn = sqlite3.connect("C:/Users/stanley/.hermes-orchestrator/hermes-orch.db")
conn.execute("UPDATE projects SET state='completed', updated_at=datetime('now') WHERE id=?", (pid,))
conn.commit()
conn.close()

status, body = _api("POST", f"/api/tasks/{tB['id']}/reset-and-cascade", {"include_downstream": False})
print(f"  status={status}, B->{body.get('status')}")
assert status == 200
assert body.get("status") == "pending"

# A should stay completed
status, ta = _api("GET", f"/api/tasks/{tA['id']}")
print(f"  -> task A status: {ta.get('status')}")
assert ta.get("status") == "completed", f"expected A completed, got {ta.get('status')}"

# C should stay completed
status, tc = _api("GET", f"/api/tasks/{tC['id']}")
print(f"  -> task C status: {tc.get('status')}")
assert tc.get("status") == "completed", f"expected C completed, got {tc.get('status')}"

# ---- TEST 3: Reset a running task -> 400 ----
print("\n[3] reset a running task -> expect 400")
_force_status(tA["id"], "running")
status, body = _api("POST", f"/api/tasks/{tA['id']}/reset-and-cascade", {})
print(f"  status={status}, body.detail={body.get('detail') if body else None}")
assert status == 400
assert "running" in body.get("detail", "")
_force_status(tA["id"], "completed")  # restore

# ---- TEST 4: Reset a pending task -> 400 ----
print("\n[4] reset a pending task -> expect 400")
_force_status(tA["id"], "pending")
status, body = _api("POST", f"/api/tasks/{tA['id']}/reset-and-cascade", {})
print(f"  status={status}, body.detail={body.get('detail') if body else None}")
assert status == 400
assert "pending" in body.get("detail", "")
_force_status(tA["id"], "completed")  # restore

# ---- TEST 5: Reset non-existent task -> 404 ----
print("\n[5] reset non-existent task -> expect 404")
status, body = _api("POST", "/api/tasks/t-nonexistent/reset-and-cascade", {})
print(f"  status={status}")
assert status == 404

# ---- TEST 6: Verify audit log ----
print("\n[6] verify audit log has task.reset_cascade events")
conn = sqlite3.connect("C:/Users/stanley/.hermes-orchestrator/hermes-orch.db")
cur = conn.execute("SELECT event_type, COUNT(*) FROM audit_log WHERE project_id=? AND event_type='task.reset_cascade' GROUP BY event_type", (pid,))
rows = cur.fetchall()
conn.close()
print(f"  audit: {rows}")
assert len(rows) == 1 and rows[0][1] == 2, f"expected 2 reset_cascade events, got {rows}"

# ---- Cleanup ----
print("\n[cleanup] deleting project")
import sqlite3
conn = sqlite3.connect("C:/Users/stanley/.hermes-orchestrator/hermes-orch.db")
conn.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
conn.execute("DELETE FROM projects WHERE id=?", (pid,))
conn.commit()
conn.close()
print("  done")

print("\n===== ALL 6 API TESTS PASSED =====")
