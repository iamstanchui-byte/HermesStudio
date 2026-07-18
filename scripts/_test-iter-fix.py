"""Test the iter fix: after a replan (current_iteration reset, decision.md
should be unlinked), the supervisor must dispatch a NEW review task —
NOT auto-complete based on stale decision.md.

Scenario:
  1. Create a project with max_iter=2
  2. Simulate v1: insert a coord review task and complete it
  3. v1's review writes decision.md -> supervisor consumes -> project
     should be marked completed/iter=1
  4. Replan (reset current_iteration, unlink decision.md)
  5. Insert 4 v2 tasks, mark them all completed
  6. Run _maybe_iterate on the project
  7. Assert: a NEW review task was dispatched (not auto-completion)
"""
import asyncio
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "src")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from hermes_orch.db import Database
from hermes_orch.core.supervisor import Supervisor
from hermes_orch.core.audit import audit_log
from hermes_orch.core.notifier import Notifier
from hermes_orch.core.planner import Planner


async def main():
    # Use a temp project storage so we don't pollute the real one
    tmp = Path(tempfile.mkdtemp(prefix="iter-fix-test-"))
    projects_root = tmp / "projects"
    projects_root.mkdir()

    # Use a temp DB so we don't touch the real one
    db_path = tmp / "test.db"
    db = Database(str(db_path))
    await db.connect()

    # Minimal cfg for supervisor
    cfg = {
        "projects": {"storage_root": str(projects_root)},
        "llm": {"mock": True, "timeout_seconds": 5},
    }
    sup = Supervisor(db, cfg, Notifier({}), Planner(cfg))

    # 1. Create project
    pid = "proj-" + uuid.uuid4().hex[:8]
    await db.insert("projects", {
        "id": pid,
        "name": "Iter fix test",
        "goal": "Test the iter fix end-to-end",
        "state": "running",
        "coordinator_role": "super",
        "accept_criteria": "Tests should pass",
        "deliverable_path": "report.md",
        "max_iterations": 3,
        "current_iteration": 1,  # post-dispatch value (review task for iter 1 was just created)
    })
    # Ensure project dir exists
    (projects_root / pid).mkdir(parents=True, exist_ok=True)

    # ---- Step 1: simulate v1's coord review task completing ----
    # Insert a v1 review task and mark it completed (simulating that the
    # coord agent has already run and written decision.md).
    v1_review_id = "t-" + uuid.uuid4().hex[:8]
    await db.insert("tasks", {
        "id": v1_review_id,
        "project_id": pid,
        "name": "[coord] review iteration 1/3",
        "agent_role": "super",
        "status": "completed",
        "action": "_iteration_review:1:3 You are the project coordinator...",
        "output_path": "decision.md",
        "depends_on": "[]",
        "params": "{}",
        "priority": "high",
        "retry_count": 0,
        "max_retries": 1,
        "timeout_seconds": 1200,
    })
    # Write decision.md to disk (v1's verdict)
    decision_path = projects_root / pid / "decision.md"
    decision_path.write_text("DECISION: PASS\n\nv1 looked good.\n", encoding="utf-8")
    print(f"[setup] v1 review task {v1_review_id} inserted as completed; decision.md written")

    # ---- Step 2: call _maybe_iterate, should consume v1's review ----
    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    print(f"[v1] project before _maybe_iterate: state={proj['state']} cur_iter={proj['current_iteration']}")
    await sup._maybe_iterate(proj)
    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    print(f"[v1] project after _maybe_iterate:  state={proj['state']} cur_iter={proj['current_iteration']}")
    # cur_iter=1 (set by dispatch before this call), consume doesn't
    # increment. at_cap=1<3, state=ready, decision.md unlinked.
    assert proj["current_iteration"] == 1, f"Expected cur_iter=1 (consumed v1 review), got {proj['current_iteration']}"
    assert proj["state"] == "ready", f"Project should be ready (not at cap), got {proj['state']}"
    assert not decision_path.exists(), f"decision.md should be unlinked after consume, but still exists"
    print(f"[v1] OK: consumed v1 review, cur_iter=1, state=ready, decision.md unlinked")

    # ---- Step 3: simulate manual replan (reset state, unlink decision.md, delete old reviews) ----
    # In real life: user calls POST /api/projects/{id}/replan
    # which sets state=planning, current_iteration=0, unlinks decision.md,
    # AND deletes old iteration_review tasks (the new fix).
    await db.execute(
        "UPDATE projects SET state = 'ready', current_iteration = 0, "
        "last_iteration_summary = '', updated_at = ? WHERE id = ?",
        (datetime.now().astimezone().isoformat(), pid),
    )
    # Simulate the new replan behavior: delete old review tasks
    await db.execute(
        "DELETE FROM tasks WHERE project_id = ? AND action LIKE '_iteration_review:%'",
        (pid,),
    )
    print(f"[replan] project reset: state=ready, cur_iter=0, old reviews deleted")

    # Insert 4 v2 tasks and mark them all completed (simulating wrapper doing work)
    for i, name in enumerate(["fetch", "analyze", "compute", "write_report"]):
        tid = "t-" + uuid.uuid4().hex[:8]
        await db.insert("tasks", {
            "id": tid,
            "project_id": pid,
            "name": name,
            "agent_role": "super",
            "status": "completed",
            "action": f"do {name}",
            "depends_on": "[]",
            "params": "{}",
            "priority": "normal",
            "retry_count": 0,
            "max_retries": 3,
            "timeout_seconds": 600,
        })

    # Simulate the BUG condition: even though replan unlinks decision.md, a wrapper
    # could re-upload the stale file from its cache. (Fix #1 prevents this in
    # production, but we want the supervisor fix to be defensive too.)
    decision_path.write_text("DECISION: PASS\n\nSTALE v1 verdict.\n", encoding="utf-8")
    print(f"[setup] Stale decision.md re-created to test supervisor's defensive check")

    # ---- Step 4: call _maybe_iterate, should dispatch NEW review, NOT auto-complete ----
    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    print(f"[v2] project before _maybe_iterate: state={proj['state']} cur_iter={proj['current_iteration']}")
    await sup._maybe_iterate(proj)
    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    print(f"[v2] project after _maybe_iterate:  state={proj['state']} cur_iter={proj['current_iteration']}")

    # Check: was a NEW review task dispatched?
    new_reviews = await db.fetchall(
        "SELECT id, status, action FROM tasks WHERE project_id = ? "
        "AND action LIKE '_iteration_review:%' ORDER BY created_at", (pid,)
    )
    print(f"[v2] review tasks for project: {len(new_reviews)}")
    for r in new_reviews:
        print(f"     - {r['id']} status={r['status']} action={r['action'][:60]!r}")

    assert len(new_reviews) == 1, f"Expected 1 review task (v2 only, v1 was deleted by replan), got {len(new_reviews)}"
    v2_review = new_reviews[0]
    assert v2_review["status"] == "pending", f"v2 review should be pending, got {v2_review['status']}"
    assert "_iteration_review:1:" in v2_review["action"], f"v2 review should be _iteration_review:1:M, got {v2_review['action'][:60]}"
    assert proj["state"] != "completed", f"Project should NOT be auto-completed, but state={proj['state']}"
    assert proj["current_iteration"] == 1, f"cur_iter should be 1 (just incremented when review dispatched), got {proj['current_iteration']}"
    # Stale decision.md should be unlinked by the dispatch code
    assert not decision_path.exists(), f"Stale decision.md should be unlinked when new review dispatched"
    print(f"[v2] OK: dispatched new review (NOT auto-completed from stale decision.md)")

    # ---- Step 5: simulate the new v2 review task completing with PASS ----
    await db.execute(
        "UPDATE tasks SET status = 'completed' WHERE id = ?", (v2_review["id"],),
    )
    # Write fresh decision.md (v2's verdict)
    decision_path.write_text("DECISION: PASS\n\nv2 also looks good.\n", encoding="utf-8")
    print(f"[v2-review] marked v2 review completed; fresh decision.md written")

    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    print(f"[v2-consume] project before _maybe_iterate: state={proj['state']} cur_iter={proj['current_iteration']}")
    await sup._maybe_iterate(proj)
    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    print(f"[v2-consume] project after _maybe_iterate:  state={proj['state']} cur_iter={proj['current_iteration']}")
    # After v2's review consumed, cur_iter stays at 1 (set by v2 dispatch
    # from 0 to 1; consume doesn't increment). Project stays ready
    # (max=3 not yet). Wait — v2 was the 1st iter of the new cycle.
    assert proj["current_iteration"] == 1, f"Expected cur_iter=1 (v2's iter, set by dispatch), got {proj['current_iteration']}"
    assert proj["state"] == "ready", f"Project should be ready (waiting for next iter), got {proj['state']}"
    assert not decision_path.exists(), f"decision.md should be unlinked after consume"
    print(f"[v2-consume] OK: consumed v2 review, cur_iter=1, decision.md unlinked")

    # ---- Step 6: simulate the 3rd round — should hit cap and complete ----
    # Insert 4 more v3 tasks and mark them completed
    for i, name in enumerate(["fetch3", "analyze3", "compute3", "write_report3"]):
        tid = "t-" + uuid.uuid4().hex[:8]
        await db.insert("tasks", {
            "id": tid,
            "project_id": pid,
            "name": name,
            "agent_role": "super",
            "status": "completed",
            "action": f"do {name}",
            "depends_on": "[]",
            "params": "{}",
            "priority": "normal",
            "retry_count": 0,
            "max_retries": 3,
            "timeout_seconds": 600,
        })
    # The current_iteration is 2 now, max is 3. We need 1 more iter.
    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    print(f"[v3] project before _maybe_iterate: state={proj['state']} cur_iter={proj['current_iteration']}")
    await sup._maybe_iterate(proj)
    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    print(f"[v3] project after _maybe_iterate:  state={proj['state']} cur_iter={proj['current_iteration']}")

    new_reviews = await db.fetchall(
        "SELECT id, status, action FROM tasks WHERE project_id = ? "
        "AND action LIKE '_iteration_review:%' ORDER BY created_at", (pid,)
    )
    print(f"[v3] review tasks: {len(new_reviews)}")
    v3_review = new_reviews[-1]  # most recent
    print(f"     - {v3_review['id']} status={v3_review['status']} action={v3_review['action'][:60]!r}")
    assert v3_review["status"] == "pending", f"v3 review should be pending, got {v3_review['status']}"
    # v3 is the 2nd iter of the new cycle (post-replan), so its action is
    # _iteration_review:2:3. After consume, cur_iter stays at 2 (not at
    # cap yet). We need one more iter to reach the cap.
    assert "_iteration_review:2:" in v3_review["action"], f"v3 review should be _iteration_review:2:3, got {v3_review['action'][:60]}"
    print(f"[v3] OK: dispatched v3 review (iter 2/3)")

    # Complete v3 review with PASS
    await db.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (v3_review["id"],))
    decision_path.write_text("DECISION: PASS\n\nv3 final verdict.\n", encoding="utf-8")
    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    print(f"[v3-consume] project before _maybe_iterate: state={proj['state']} cur_iter={proj['current_iteration']}")
    await sup._maybe_iterate(proj)
    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    print(f"[v3-consume] project after _maybe_iterate:  state={proj['state']} cur_iter={proj['current_iteration']}")
    # v3 was iter 2 of the new cycle, so cur_iter stays at 2 after consume.
    # at_cap = 2<3, state=ready, not completed yet.
    assert proj["state"] == "ready", f"Project should be ready (not at cap yet), got {proj['state']}"
    assert proj["current_iteration"] == 2, f"Expected cur_iter=2 (v3 was iter 2/3), got {proj['current_iteration']}"
    assert not decision_path.exists(), f"decision.md should be unlinked after consume"
    print(f"[v3-consume] OK: consumed v3 review, cur_iter=2, state=ready (not yet at cap)")

    # ---- Step 7: trigger the FINAL iter to actually hit the cap ----
    # Insert 4 v4 tasks, mark completed, dispatch v4 review
    for i, name in enumerate(["fetch4", "analyze4", "compute4", "write_report4"]):
        tid = "t-" + uuid.uuid4().hex[:8]
        await db.insert("tasks", {
            "id": tid,
            "project_id": pid,
            "name": name,
            "agent_role": "super",
            "status": "completed",
            "action": f"do {name}",
            "depends_on": "[]",
            "params": "{}",
            "priority": "normal",
            "retry_count": 0,
            "max_retries": 3,
            "timeout_seconds": 600,
        })

    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    print(f"[v4] project before _maybe_iterate: state={proj['state']} cur_iter={proj['current_iteration']}")
    await sup._maybe_iterate(proj)
    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    print(f"[v4] project after _maybe_iterate:  state={proj['state']} cur_iter={proj['current_iteration']}")
    new_reviews = await db.fetchall(
        "SELECT id, status, action FROM tasks WHERE project_id = ? "
        "AND action LIKE '_iteration_review:%' ORDER BY created_at", (pid,)
    )
    v4_review = new_reviews[-1]
    print(f"     - {v4_review['id']} status={v4_review['status']} action={v4_review['action'][:60]!r}")
    assert v4_review["status"] == "pending", f"v4 review should be pending, got {v4_review['status']}"
    assert "_iteration_review:3:" in v4_review["action"], f"v4 review should be _iteration_review:3:3, got {v4_review['action'][:60]}"
    print(f"[v4] OK: dispatched v4 review (iter 3/3, the final iter)")

    # Complete v4 review with PASS
    await db.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (v4_review["id"],))
    decision_path.write_text("DECISION: PASS\n\nv4 final verdict — at cap.\n", encoding="utf-8")
    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    await sup._maybe_iterate(proj)
    proj = (await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    print(f"[v4-consume] state={proj['state']} cur_iter={proj['current_iteration']}")
    assert proj["state"] == "completed", f"Project should be COMPLETED (at cap), got {proj['state']}"
    assert proj["current_iteration"] == 3, f"Expected cur_iter=3 (at cap), got {proj['current_iteration']}"
    assert not decision_path.exists(), f"decision.md should be unlinked after consume"
    print(f"[v4-consume] OK: project completed at iter 3/3, decision.md unlinked")

    # Cleanup
    await db.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n[ALL OK] Iter loop fix verified end-to-end")


if __name__ == "__main__":
    import faulthandler
    faulthandler.enable()
    print("[start] running iter fix test")
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=30))
    except asyncio.TimeoutError:
        print("[TIMEOUT] test hung after 30s")
        sys.exit(1)
