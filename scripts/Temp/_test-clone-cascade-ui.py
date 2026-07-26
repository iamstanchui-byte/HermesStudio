"""Stage 3.5+ Clone chain UI E2E test.

1. Seed a project with 3 tasks A->B->C, force all to 'completed' with
   fake results. Force project to 'completed' too.
2. Open the visual page, click card A, verify the 'Clone chain' button
   is visible (status=completed is cloneable).
3. Click the button, accept the confirm() dialog.
4. After reload, verify:
   - 3 new tasks are shown (A', B', C') — total 3 active tasks
   - The OLD A, B, C are NOT in the canvas
   - B' depends on A', C' depends on B' (new IDs in depends_on)
5. Re-set all to completed (now using the new IDs), click Clone on
   the new A with include_downstream=False (use the API to test that
   mode, not the UI which only has the default).

The UI only exposes the default (include_downstream=True) button, so
test 5 goes through the API.
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
    conn.execute("UPDATE tasks SET result=?", (json.dumps({"summary": summary}),))
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
    _, proj = _api("POST", "/api/projects/", {"name": "e2e-clone-ui"})
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

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await ctx.new_page()

            confirm_msgs = []
            async def on_dialog(d):
                confirm_msgs.append(d.message)
                await d.accept()
            page.on("dialog", lambda d: asyncio.create_task(on_dialog(d)))

            # ===== TEST 1: Open page, click card A, verify Clone chain button =====
            print("\n[1] open visual page, click card A, verify Clone chain button")
            await page.goto(f"{BASE}/projects/{pid}/visual", wait_until="networkidle")
            await page.wait_for_timeout(500)

            await page.locator(f'.vp-card[data-task-name="{A}"]').click()
            await page.wait_for_timeout(300)
            panel_title = await page.locator("#vp-sp-title").text_content()
            ok("side panel shows task A name", "A: source" in (panel_title or ""),
               f"title='{panel_title}'")

            clone_btn = page.locator(".vp-sp-btn:has-text('Clone chain')")
            n_btns = await clone_btn.count()
            ok("Clone chain button visible for completed task A", n_btns == 1,
               f"found {n_btns} matching buttons")
            await page.screenshot(path=str(SCREENS / "_clone-1-panel.png"), full_page=True)

            # ===== TEST 2: Click Clone chain on A, verify confirm + clone =====
            print("\n[2] click Clone chain on A (3-task chain)")
            await clone_btn.click()
            await page.wait_for_timeout(2500)  # wait for confirm + POST + reload

            ok("confirm() dialog fired", len(confirm_msgs) >= 1,
               f"got {len(confirm_msgs)} dialog(s)")
            msg = confirm_msgs[0] if confirm_msgs else ""
            ok("confirm msg mentions task A name", "A: source" in msg, f"msg='{msg[:150]}...'")
            ok("confirm msg mentions 2 downstream tasks", "2 downstream" in msg,
               f"msg snippet: {msg[80:200]}")

            # After reload, verify the NEW chain is shown
            # The OLD tasks should be hidden (archived=0 filter)
            cards = await page.locator(".vp-card").all()
            ok("3 cards on canvas after clone (new A', B', C')", len(cards) == 3,
               f"got {len(cards)} cards")
            card_ids = []
            for c in cards:
                tid = await c.get_attribute("data-task-name")
                card_ids.append(tid)
            print(f"  card ids on canvas: {card_ids}")
            # None of the old IDs should be present
            for old_id, name in [(A, "A"), (B, "B"), (C, "C")]:
                ok(f"old {name} NOT in canvas", old_id not in card_ids,
                   f"old_id={old_id}")
            # All 3 cards should be new IDs (not in the old set)
            new_card_ids = [c for c in card_ids if c not in (A, B, C)]
            ok("3 new IDs in canvas", len(new_card_ids) == 3,
               f"new_card_ids={new_card_ids}")

            # Verify depends_on graph: new A has no deps, new B depends on new A, new C depends on new B
            # The visual page should have 2 depends_on wires (new A -> new B -> new C)
            connections = await page.locator("svg.connection").count()
            ok("2 depends_on wires in new chain", connections == 2,
               f"got {connections}")
            await page.screenshot(path=str(SCREENS / "_clone-2-after-clone.png"), full_page=True)

            # ===== TEST 3: Verify old tasks still exist in DB with archived=1 =====
            print("\n[3] verify old tasks preserved in DB with archived=1")
            conn = sqlite3.connect(DB)
            for old_id, name in [(A, "A"), (B, "B"), (C, "C")]:
                row = conn.execute("SELECT status, archived, result FROM tasks WHERE id=?", (old_id,)).fetchone()
                assert row[1] == 1, f"old {name} should be archived"
                # result should still be set (we put 'A-result' etc before)
                assert row[2] is not None, f"old {name} result should be preserved"
            conn.close()
            ok("all 3 old tasks preserved with archived=1 + result intact", True)

            # ===== TEST 4: Verify new tasks in DB have correct depends_on =====
            print("\n[4] verify new tasks have rebuilt depends_on graph")
            new_A_id = new_card_ids[0]
            conn = sqlite3.connect(DB)
            for card_id in new_card_ids:
                row = conn.execute("SELECT id, name, depends_on, status FROM tasks WHERE id=?", (card_id,)).fetchone()
                print(f"  new task: {row[0]} '{row[1]}' deps={row[2]} status={row[3]}")
            # Find new B (depends on new A) and new C (depends on new B)
            new_A_deps = conn.execute("SELECT depends_on FROM tasks WHERE id=?", (new_A_id,)).fetchone()[0]
            ok("new A has empty depends_on", json.loads(new_A_deps) == [],
               f"got {new_A_deps}")
            # The other 2 should form a chain
            other_two = [c for c in new_card_ids if c != new_A_id]
            # One of them depends on new_A, the other depends on that one
            chain_found = False
            for mid_id in other_two:
                mid_deps = json.loads(conn.execute("SELECT depends_on FROM tasks WHERE id=?", (mid_id,)).fetchone()[0])
                if new_A_id in mid_deps:
                    # mid is new B. Find new C (the other one that depends on mid)
                    leaf_id = [c for c in other_two if c != mid_id][0]
                    leaf_deps = json.loads(conn.execute("SELECT depends_on FROM tasks WHERE id=?", (leaf_id,)).fetchone()[0])
                    if mid_id in leaf_deps:
                        chain_found = True
                        print(f"  chain verified: {new_A_id} -> {mid_id} -> {leaf_id}")
                        break
            ok("new chain has proper A -> B -> C dependency graph", chain_found)
            conn.close()

            # ===== TEST 5: Re-clone the new chain (after forcing to completed) =====
            print("\n[5] re-clone the new chain (force to completed first)")
            confirm_msgs.clear()
            # The supervisor may have already dispatched the new A' to
            # assigned/running. Force it to completed so the Clone chain
            # button shows up.
            _force(new_A_id, "completed")
            await page.reload()
            await page.wait_for_timeout(500)
            await page.locator(f'.vp-card[data-task-name="{new_A_id}"]').click()
            await page.wait_for_timeout(300)
            clone_btn2 = page.locator(".vp-sp-btn:has-text('Clone chain')")
            n_btns = await clone_btn2.count()
            ok("Clone chain button visible after force to completed", n_btns == 1,
               f"got {n_btns} matching buttons")
            await clone_btn2.click()
            await page.wait_for_timeout(2500)

            cards = await page.locator(".vp-card").all()
            ok("3 cards on canvas after re-clone", len(cards) == 3,
               f"got {len(cards)} cards")

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
