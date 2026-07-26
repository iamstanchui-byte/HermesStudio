"""Stage 3.5+ Reset & cascade UI E2E test.

1. Seed a project with 3 tasks A->B->C, force all to 'completed' with fake
   results. Force project to 'completed' too.
2. Open the visual page, click card A, verify the 'Reset & cascade'
   button is visible (status=completed is resettable).
3. Click the button, accept the confirm() dialog.
4. After reload, verify all 3 cards are pending and results are cleared.
5. Re-set all to completed, open card B, click Reset & cascade.
6. Verify: only B+C reset; A stays completed.
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import asyncio
import sqlite3
import urllib.request
import json
from pathlib import Path
from playwright.async_api import async_playwright

BASE = "http://localhost:8765"
DB = "C:/Users/stanley/.hermes-orchestrator/hermes-orch.db"
SCREENS = Path("scripts/Temp")


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


async def main():
    checks_pass = 0
    checks_fail = 0

    def ok(name, cond, detail=""):
        nonlocal checks_pass, checks_fail
        if cond:
            print(f"  PASS  {name}{(' -- ' + detail) if detail else ''}")
            checks_pass += 1
        else:
            print(f"  FAIL  {name}{(' -- ' + detail) if detail else ''}")
            checks_fail += 1

    # ---- Setup ----
    print("\n[setup] create project + 3 chained tasks A->B->C")
    _, proj = _api("POST", "/api/projects/", {"name": "e2e-reset-cascade-ui"})
    pid = proj["id"]
    _, tA = _api("POST", "/api/tasks/", {
        "project_id": pid, "name": "A: source", "agent_role": "win-agent01",
        "action": "act_a", "depends_on": [],
    })
    _, tB = _api("POST", "/api/tasks/", {
        "project_id": pid, "name": "B: middle", "agent_role": "win-agent01",
        "action": "act_b", "depends_on": [tA["id"]],
    })
    _, tC = _api("POST", "/api/tasks/", {
        "project_id": pid, "name": "C: leaf", "agent_role": "win-agent01",
        "action": "act_c", "depends_on": [tB["id"]],
    })
    A, B, C = tA["id"], tB["id"], tC["id"]
    print(f"  A={A} B={B} C={C}")
    for tid, lbl in [(A, "A-result"), (B, "B-result"), (C, "C-result")]:
        _force(tid, "completed")
        _set_result(tid, lbl)
    _force(A, "completed", "completed")  # also forces project to completed
    print("  forced all to completed + project to completed")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await ctx.new_page()

            # Capture the confirm() dialog and capture the message
            # (we want to verify the scope is shown to the operator).
            confirm_msgs = []
            async def on_dialog(d):
                confirm_msgs.append(d.message)
                await d.accept()
            page.on("dialog", lambda d: asyncio.create_task(on_dialog(d)))

            # ===== TEST 1: Open page, click card A, verify Reset & cascade visible =====
            print("\n[1] open visual page, click card A, verify button")
            await page.goto(f"{BASE}/projects/{pid}/visual", wait_until="networkidle")
            await page.wait_for_timeout(500)

            await page.locator(f'.vp-card[data-task-name="{A}"]').click()
            await page.wait_for_timeout(300)

            panel_title = await page.locator("#vp-sp-title").text_content()
            ok("side panel shows task A name", "A: source" in (panel_title or ""),
               f"title='{panel_title}'")

            reset_btn = page.locator(".vp-sp-btn:has-text('Reset & cascade')")
            n_btns = await reset_btn.count()
            ok("Reset & cascade button visible for completed task A", n_btns == 1,
               f"found {n_btns} matching buttons")
            await page.screenshot(path=str(SCREENS / "_reset-1-panel.png"), full_page=True)

            # ===== TEST 2: Click Reset & cascade on A, verify confirm dialog + cascade =====
            print("\n[2] click Reset & cascade on A (3-task chain)")
            await reset_btn.click()
            await page.wait_for_timeout(2500)  # wait for confirm + POST + reload

            ok("confirm() dialog fired", len(confirm_msgs) >= 1,
               f"got {len(confirm_msgs)} dialog(s)")
            msg = confirm_msgs[0] if confirm_msgs else ""
            ok("confirm msg mentions task A name", "A: source" in msg, f"msg='{msg[:120]}...'")
            ok("confirm msg mentions 2 downstream tasks", "2 downstream" in msg,
               f"msg='{msg[:200]}'")

            # After reload, verify all 3 tasks are reset and results cleared.
            # Note: by the time the test checks, the supervisor may have
            # already re-dispatched the source task (pending -> assigned).
            # We accept either state as proof the reset worked.
            status_a, body_a = _api("GET", f"/api/tasks/{A}")
            status_b, body_b = _api("GET", f"/api/tasks/{B}")
            status_c, body_c = _api("GET", f"/api/tasks/{C}")
            ok("A is pending/assigned (reset, possibly re-dispatched)",
               body_a.get("status") in ("pending", "assigned"),
               f"got {body_a.get('status')}")
            ok("B is pending (downstream, not yet re-dispatched)",
               body_b.get("status") == "pending",
               f"got {body_b.get('status')}")
            ok("C is pending (downstream, not yet re-dispatched)",
               body_c.get("status") == "pending",
               f"got {body_c.get('status')}")
            ok("A result cleared", body_a.get("result") is None, f"got {body_a.get('result')}")
            ok("B result cleared", body_b.get("result") is None, f"got {body_b.get('result')}")
            ok("C result cleared", body_c.get("result") is None, f"got {body_c.get('result')}")
            # Project should be woken to ready
            _, body_p = _api("GET", f"/api/projects/{pid}")
            ok("project woken to ready", body_p.get("state") == "ready",
               f"got state={body_p.get('state')}")
            await page.screenshot(path=str(SCREENS / "_reset-2-after-cascade.png"), full_page=True)

            # ===== TEST 3: B-only reset =====
            print("\n[3] re-set all to completed, click Reset on B (verify only B+C reset)")
            for tid, lbl in [(A, "A-result-2"), (B, "B-result-2"), (C, "C-result-2")]:
                _force(tid, "completed")
                _set_result(tid, lbl)
            _force(A, "completed", "completed")
            confirm_msgs.clear()

            await page.goto(f"{BASE}/projects/{pid}/visual", wait_until="networkidle")
            await page.wait_for_timeout(500)
            await page.locator(f'.vp-card[data-task-name="{B}"]').click()
            await page.wait_for_timeout(300)
            reset_btn2 = page.locator(".vp-sp-btn:has-text('Reset & cascade')")
            await reset_btn2.click()
            await page.wait_for_timeout(2500)

            # A should stay completed, B+C should reset
            status_a, body_a = _api("GET", f"/api/tasks/{A}")
            status_b, body_b = _api("GET", f"/api/tasks/{B}")
            status_c, body_c = _api("GET", f"/api/tasks/{C}")
            ok("A still completed (not in cascade)", body_a.get("status") == "completed",
               f"got {body_a.get('status')}")
            ok("A result NOT cleared", body_a.get("result") is not None,
               f"got {body_a.get('result')}")
            # B is the cascade source — accept pending/assigned/running
            ok("B is reset (pending/assigned/running)",
               body_b.get("status") in ("pending", "assigned", "running"),
               f"got {body_b.get('status')}")
            ok("C is pending (downstream, not yet re-dispatched)",
               body_c.get("status") == "pending",
               f"got {body_c.get('status')}")
            ok("confirm msg mentions 1 downstream", "1 downstream" in (confirm_msgs[0] if confirm_msgs else ""),
               f"msg='{confirm_msgs[0] if confirm_msgs else ''}'")

            # ===== TEST 4: Pending task should NOT show the button =====
            print("\n[4] pending task should NOT show Reset & cascade button")
            # B is now pending; visit visual page, click B, verify button NOT visible
            await page.goto(f"{BASE}/projects/{pid}/visual", wait_until="networkidle")
            await page.wait_for_timeout(500)
            await page.locator(f'.vp-card[data-task-name="{B}"]').click()
            await page.wait_for_timeout(300)
            n_reset = await page.locator(".vp-sp-btn:has-text('Reset & cascade')").count()
            ok("Reset & cascade NOT shown for pending task", n_reset == 0,
               f"found {n_reset} matching buttons")

            await browser.close()

    finally:
        # Cleanup
        conn = sqlite3.connect(DB)
        conn.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.commit()
        conn.close()
        print("\n[cleanup] deleted project")

    print(f"\n===== {checks_pass} passed, {checks_fail} failed =====")
    sys.exit(0 if checks_fail == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
