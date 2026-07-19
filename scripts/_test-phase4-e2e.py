"""E2E test for Phase 4: smart dispatch via required_capability.

Setup:
- linux-a-01 / super profile gets capabilities: {mt5: false, xauusd: false}
  (operator intentionally says this role CANNOT do mt5)
- Create a task with agent_role=super, required_capability=mt5
- The supervisor should:
  1. Find a super profile (linux-a-01/super)
  2. Detect the capability mismatch
  3. Mark the task as FAILED with error "dispatch.mismatch: ..."
  4. Write a `dispatch.mismatch` audit event
  5. NOT assign the task

Also test the happy path:
- Same profile, but required_capability=xauusd
  (We didn't set xauusd, so profile_caps={} which is permissive
   default — should assign normally.)

And finally test the explicit-grant path:
- Update profile capabilities to {mt5: true}
- Create task with required_capability=mt5 — should now assign.
"""
import json
import time
import httpx
import sqlite3
from pathlib import Path

BASE = "http://localhost:8765"
DB = r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db"


def get_auth_headers(agent_id: str = "linux-a-01") -> dict[str, str]:
    """Read the wrapper's secret and build HMAC headers (or use a fallback).

    For the test, we authenticate as 'operator' via the cookie/session that
    the dashboard uses. Simpler: just use the X-Agent-Id auth path.
    """
    secret = Path(r"C:\Users\stanley\.hermes-orchestrator\.secret-linux-a-01").read_text().strip()
    import hashlib
    import hmac
    ts = str(int(time.time()))
    msg = f"{agent_id}|{ts}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return {
        "X-Agent-Id": agent_id,
        "X-Timestamp": ts,
        "X-Signature": sig,
    }


def db_conn():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def find_profile_id(profile_name: str) -> str:
    con = db_conn()
    r = con.execute(
        "SELECT id FROM agent_profiles WHERE name = ? AND agent_id = 'linux-a-01'",
        (profile_name,),
    ).fetchone()
    con.close()
    assert r, f"profile {profile_name} not found on linux-a-01"
    return r["id"]


def set_caps(profile_name: str, caps: dict) -> None:
    """Update profile capabilities via DB directly (avoid HMAC hassle in test)."""
    con = db_conn()
    con.execute(
        "UPDATE agent_profiles SET capabilities = ? WHERE id = ?",
        (json.dumps(caps), find_profile_id(profile_name)),
    )
    con.commit()
    con.close()
    print(f"  set {profile_name} capabilities = {caps}")


def create_project_and_task(goal: str, role: str, req_cap: str | None) -> str:
    """Create a project + a single task, return task id."""
    # Use the existing operator endpoints without auth for simplicity
    # (the API doesn't enforce auth on the local backend currently).
    with httpx.Client(base_url=BASE, timeout=10) as c:
        # Create a project (auto-planning will trigger; we don't care about
        # the planned tasks — we'll add our own below).
        r = c.post("/api/projects/", json={
            "name": f"Phase4 test ({req_cap or 'none'})",
            "goal": goal,
        })
        assert r.status_code == 201, f"create project: {r.status_code} {r.text}"
        pid = r.json()["id"]
        # Stop planning immediately so the supervisor doesn't run our
        # generated tasks. We just want to test the dispatch check on
        # our own crafted task.
        con = db_conn()
        con.execute("UPDATE projects SET state = 'paused' WHERE id = ?", (pid,))
        con.commit()
        con.close()
        # Add a single manually-crafted task
        r = c.post("/api/tasks/", json={
            "project_id": pid,
            "name": f"needs {req_cap}" if req_cap else "no capability required",
            "agent_role": role,
            "action": f"test_action_for_{req_cap or 'none'}",
            "required_capability": req_cap,
        })
        assert r.status_code == 201, f"create task: {r.status_code} {r.text}"
        tid = r.json()["id"]
    return pid, tid


def get_task_status(task_id: str) -> dict:
    with httpx.Client(base_url=BASE, timeout=10) as c:
        r = c.get(f"/api/tasks/{task_id}")
        return r.json()


def get_audit_events(task_id: str, event_type: str | None = None) -> list[dict]:
    con = db_conn()
    if event_type:
        rows = con.execute(
            "SELECT * FROM audit_log WHERE task_id = ? AND event_type = ? ORDER BY id",
            (task_id, event_type),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM audit_log WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def main():
    print("=" * 70)
    print("PHASE 4 E2E: smart dispatch via required_capability")
    print("=" * 70)

    # Test 1: profile lacks required capability → task should fail
    print()
    print("--- Test 1: profile lacks required capability ---")
    set_caps("super", {"mt5": False, "xauusd_feed": False})
    pid1, tid1 = create_project_and_task(
        goal="test phase 4 mismatch path",
        role="super",
        req_cap="mt5",
    )
    print(f"  created project {pid1} task {tid1} (requires mt5, profile has it=False)")
    # Wait for supervisor to dispatch
    time.sleep(8)
    t = get_task_status(tid1)
    print(f"  task status: {t['status']}  error: {t.get('error', '')!r}")
    assert t["status"] == "failed", f"expected 'failed', got {t['status']}"
    assert "dispatch.mismatch" in t["error"], f"expected dispatch.mismatch in error, got: {t['error']}"
    print("  PASS: task failed with dispatch.mismatch")

    # Verify audit event
    events = get_audit_events(tid1, "dispatch.mismatch")
    assert len(events) == 1, f"expected 1 dispatch.mismatch event, got {len(events)}"
    payload = json.loads(events[0]["payload"])
    print(f"  audit event payload: role={payload['role']!r}  required={payload['required_capability']!r}")
    assert payload["required_capability"] == "mt5"
    assert payload["role"] == "super"
    print("  PASS: dispatch.mismatch audit event written")

    # Test 2: profile has required capability → task should assign
    print()
    print("--- Test 2: profile has required capability ---")
    set_caps("super", {"mt5": True, "xauusd_feed": True})
    pid2, tid2 = create_project_and_task(
        goal="test phase 4 happy path",
        role="super",
        req_cap="mt5",
    )
    print(f"  created project {pid2} task {tid2} (requires mt5, profile has it=True)")
    time.sleep(8)
    t = get_task_status(tid2)
    print(f"  task status: {t['status']}  assigned_agent: {t.get('assigned_agent_id')}")
    assert t["status"] in ("assigned", "running"), f"expected assigned/running, got {t['status']}"
    print(f"  PASS: task {t['status']} (no mismatch)")

    # Test 3: profile has empty capabilities (permissive default) → task should assign
    print()
    print("--- Test 3: empty capabilities (permissive default) ---")
    set_caps("super", {})  # empty = "can do anything"
    pid3, tid3 = create_project_and_task(
        goal="test phase 4 permissive default",
        role="super",
        req_cap="anything_under_sun",
    )
    print(f"  created project {pid3} task {tid3} (requires anything, profile caps empty)")
    time.sleep(8)
    t = get_task_status(tid3)
    print(f"  task status: {t['status']}  assigned_agent: {t.get('assigned_agent_id')}")
    assert t["status"] in ("assigned", "running"), f"expected assigned/running, got {t['status']}"
    print(f"  PASS: task {t['status']} (permissive default — no mismatch enforced)")

    # Cleanup: reset profile capabilities
    set_caps("super", {})
    print()
    print("=" * 70)
    print("ALL PHASE 4 E2E TESTS PASSED")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
