"""Smoke test the /clone-and-cascade endpoint."""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import urllib.request
import json
import sqlite3

BASE = "http://localhost:8765"
DB = "C:/Users/stanley/.hermes-orchestrator/hermes-orch.db"


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


def _force(task_id, status, project_state=None):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE tasks SET status=?, updated_at=datetime('now') WHERE id=?", (status, task_id))
    if project_state is not None:
        conn.execute("UPDATE projects SET state=?, updated_at=datetime('now') WHERE id=(SELECT project_id FROM tasks WHERE id=?)", (project_state, task_id))
    conn.commit()
    conn.close()


def _set_result(task_id, summary):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE tasks SET result=? WHERE id=?", (json.dumps({"summary": summary}), task_id))
    conn.commit()
    conn.close()


def _is_archived(task_id):
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT archived FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return bool(row[0]) if row else None


print("[setup] create project + 3 chained tasks A->B->C")
_, proj = _api("POST", "/api/projects/", {"name": "e2e-clone-cascade"})
pid = proj["id"]
_, tA = _api("POST", "/api/tasks/", {
    "project_id": pid, "name": "A: source", "agent_role": "win-agent01",
    "action": "act_a", "params": {"k": "v"}, "depends_on": [],
})
_, tB = _api("POST", "/api/tasks/", {
    "project_id": pid, "name": "B: middle", "agent_role": "win-agent01",
    "action": "act_b", "params": {}, "depends_on": [tA["id"]],
})
_, tC = _api("POST", "/api/tasks/", {
    "project_id": pid, "name": "C: leaf", "agent_role": "win-agent01",
    "action": "act_c", "params": {}, "depends_on": [tB["id"]],
})
A, B, C = tA["id"], tB["id"], tC["id"]
for tid, lbl in [(A, "A-result"), (B, "B-result"), (C, "C-result")]:
    _force(tid, "completed")
    _set_result(tid, lbl)
_force(A, "completed", "completed")
print(f"  A={A} B={B} C={C}")
print("  forced all to completed, results set, project to completed")

# ---- TEST 1: Clone A with cascade (default) ----
print("\n[1] POST /clone-and-cascade A (include_downstream=True default)")
status, body = _api("POST", f"/api/tasks/{A}/clone-and-cascade", {})
print(f"  status={status}")
print(f"  new_task_ids: {body.get('new_task_ids')}")
print(f"  old_task_ids: {body.get('old_task_ids')}")
print(f"  id_map: {body.get('id_map')}")
assert status == 200
assert len(body["new_task_ids"]) == 3, f"expected 3 new tasks, got {len(body['new_task_ids'])}"
assert set(body["old_task_ids"]) == {A, B, C}, f"old_task_ids mismatch: {body['old_task_ids']}"
assert body["id_map"][A] in body["new_task_ids"]

# Verify old tasks are archived
for old_id, name in [(A, "A"), (B, "B"), (C, "C")]:
    assert _is_archived(old_id) is True, f"expected {name} archived"
print("  all old tasks marked archived=1")

# Verify new tasks exist and are pending/assigned
# (the supervisor may have already dispatched them by the time
# we check, so we accept 'pending' or 'assigned').
for new_id in body["new_task_ids"]:
    s, t = _api("GET", f"/api/tasks/{new_id}")
    assert t["status"] in ("pending", "assigned"), f"new task {new_id} should be pending/assigned, got {t['status']}"
    assert t["result"] is None, f"new task {new_id} should have None result, got {t.get('result')}"
print("  all new tasks pending/assigned with None result")

# Verify depends_on was rebuilt: new A has no deps, new B depends on new A, new C depends on new B
new_A = body["id_map"][A]
new_B = body["id_map"][B]
new_C = body["id_map"][C]
_, na = _api("GET", f"/api/tasks/{new_A}")
_, nb = _api("GET", f"/api/tasks/{new_B}")
_, nc = _api("GET", f"/api/tasks/{new_C}")
print(f"  new A depends_on: {na['depends_on']}")
print(f"  new B depends_on: {nb['depends_on']}")
print(f"  new C depends_on: {nc['depends_on']}")
assert na["depends_on"] == [], f"new A should have no deps, got {na['depends_on']}"
assert nb["depends_on"] == [new_A], f"new B should depend on new A, got {nb['depends_on']}"
assert nc["depends_on"] == [new_B], f"new C should depend on new B, got {nc['depends_on']}"

# Verify the new tasks preserved other fields
assert na["name"] == "A: source", f"name not preserved"
assert na["action"] == "act_a", f"action not preserved"
assert na["params"] == {"k": "v"}, f"params not preserved: {na['params']}"
print("  fields preserved (name, action, params)")

# Verify project woken to 'planned' (user feedback 2026-07-26:
# must NOT auto-dispatch — user clicks Run button to start)
_, p = _api("GET", f"/api/projects/{pid}")
assert p["state"] == "planned", f"project should be planned, got {p['state']}"
print(f"  project woken to planned: state={p['state']}")

# Verify Run endpoint transitions planned -> ready
status, body = _api("POST", f"/api/projects/{pid}/run", {})
assert status == 200, f"run should succeed, got {status}: {body}"
_, p = _api("GET", f"/api/projects/{pid}")
assert p["state"] == "ready", f"after Run, state should be ready, got {p['state']}"
print(f"  Run button: planned -> ready")

# ---- TEST 2: Clone from middle with include_downstream=False ----
print("\n[2] clone B with include_downstream=False (just B, not C)")
# Set B and C back to completed (the new ones we just made are pending; re-set them for the test)
# Wait — we can't reset the old B/C because they're archived. Use the new ones.
new_B_row = nb
_force(new_B_row["id"], "completed")
_set_result(new_B_row["id"], "B-result-2")
# new C stays pending (not in cascade set)
status, body = _api("POST", f"/api/tasks/{new_B_row['id']}/clone-and-cascade", {"include_downstream": False})
print(f"  status={status}, new_task_ids={body.get('new_task_ids')}")
assert status == 200
assert len(body["new_task_ids"]) == 1, f"expected 1 new task, got {len(body['new_task_ids'])}"
# new B is now archived, new B' is the only new task
newest_B = body["new_task_ids"][0]
assert _is_archived(new_B_row["id"]) is True
_, n_b = _api("GET", f"/api/tasks/{newest_B}")
assert n_b["name"] == "B: middle"
assert n_b["status"] == "pending"
print(f"  new B': {newest_B}, status={n_b['status']}")

# ---- TEST 3: Refuse running ----
print("\n[3] refuse clone on running task")
# Use a brand new task so the supervisor doesn't race us.
_, t3 = _api("POST", "/api/tasks/", {
    "project_id": pid, "name": "T3: running-test", "agent_role": "win-agent01",
    "action": "act_t3", "depends_on": [],
})
t3_id = t3["id"]
# Force the project to 'planned' FIRST so the supervisor
# ignores the new task (supervisor's tick query is
# state IN 'planning','ready','running'). Then force the
# task to 'running'. The /clone-and-cascade endpoint
# checks task.status which is now 'running'.
conn = sqlite3.connect(DB)
conn.execute("UPDATE projects SET state='planned' WHERE id=?", (pid,))
conn.execute("UPDATE tasks SET status='running' WHERE id=?", (t3_id,))
conn.commit()
status_in_db = conn.execute("SELECT status FROM tasks WHERE id=?", (t3_id,)).fetchone()[0]
conn.close()
assert status_in_db == "running", f"expected running, got {status_in_db} (supervisor raced us?)"
status, body = _api("POST", f"/api/tasks/{t3_id}/clone-and-cascade", {})
print(f"  status={status}, detail={body.get('detail')}")
assert status == 400
assert "running" in body["detail"]
_force(t3_id, "pending")

# ---- TEST 4: Refuse pending ----
print("\n[4] refuse clone on pending task")
# Use the same T3 task (just forced to pending)
status, body = _api("POST", f"/api/tasks/{t3_id}/clone-and-cascade", {})
print(f"  status={status}, detail={body.get('detail')}")
assert status == 400
assert "pending" in body["detail"]

# ---- TEST 5: 404 ----
print("\n[5] 404 on non-existent task")
status, body = _api("POST", "/api/tasks/t-nonexistent/clone-and-cascade", {})
print(f"  status={status}")
assert status == 404

# ---- TEST 6: Audit log ----
print("\n[6] audit log has task.cloned_cascade events")
conn = sqlite3.connect(DB)
cur = conn.execute("SELECT event_type, COUNT(*) FROM audit_log WHERE project_id=? AND event_type='task.cloned_cascade' GROUP BY event_type", (pid,))
rows = cur.fetchall()
conn.close()
print(f"  audit: {rows}")
assert len(rows) == 1 and rows[0][1] == 2, f"expected 2 cloned_cascade events, got {rows}"

# ---- TEST 7: Visual page only shows non-archived ----
print("\n[7] visual page shows only the 4 active tasks (A,B,C,newest_B), not 7")
import urllib.request
page = urllib.request.urlopen(f"{BASE}/projects/{pid}/visual").read().decode()
# count task entries in the embedded JSON
import re
m = re.search(r'"tasks":\s*\{(.+?)\}\s*\}', page, re.DOTALL)
if not m:
    # find individual task IDs in the data block
    tasks_in_page = re.findall(r'"(t-[a-f0-9]+)":\s*\{', page)
    print(f"  tasks in page: {len(tasks_in_page)}: {tasks_in_page}")
    # After 2 clone calls: A,B,C archived + 4 new = 4 active
    assert len(tasks_in_page) == 4, f"expected 4 active tasks in page, got {len(tasks_in_page)}"
    # None of the old IDs should be in the active list
    for old in [A, B, C]:
        assert old not in tasks_in_page, f"old task {old} should not be in active view"
print("  visual page correctly filters archived=0")

# ---- Cleanup ----
print("\n[cleanup] deleting project")
conn = sqlite3.connect(DB)
conn.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
conn.execute("DELETE FROM projects WHERE id=?", (pid,))
conn.commit()
conn.close()
print("  done")

print("\n===== ALL 7 API TESTS PASSED =====")
