"""Stage 3.5 PATCH/POST/DELETE round-trip test.

Actually exercises the edit form's save flow:
  1. PATCH task B (rename + change action)
  2. POST a new task with 2 deps
  3. DELETE the new task (verify scrub of deps on the deleted task's refs - n/a here since new task has none, but verifies DELETE works)
  4. Cleanup: restore task B's name + delete the project

Also verifies via API (not UI) that the changes persisted.
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import asyncio
import urllib.request
import json
from pathlib import Path
from playwright.async_api import async_playwright

BASE = "http://localhost:8765"
PROJ = "proj-d07a152f"
TASKS = {"A": "t-195181ff", "B": "t-58b8c16a", "C": "t-96feff3e"}
SCREENS = Path("scripts/Temp")


def _api(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        BASE + path, method=method, data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()) if r.length else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


async def main():
    checks_pass = 0
    checks_fail = 0

    def ok(name, condition, detail=""):
        nonlocal checks_pass, checks_fail
        if condition:
            print(f"  PASS  {name}{(' -- ' + detail) if detail else ''}")
            checks_pass += 1
        else:
            print(f"  FAIL  {name}{(' -- ' + detail) if detail else ''}")
            checks_fail += 1

    # ===== API-level: PATCH task B =====
    print("\n[API] PATCH task B (rename + action)")
    status, body = _api("PATCH", f"/api/tasks/{TASKS['B']}", {
        "name": "B: count 2026 (renamed)",
        "action": "count_matching_v2",
    })
    ok("PATCH returns 200", status == 200, f"status={status}")
    ok("name updated", body and body.get("name") == "B: count 2026 (renamed)")
    ok("action updated", body and body.get("action") == "count_matching_v2")
    # Restore for subsequent tests
    _api("PATCH", f"/api/tasks/{TASKS['B']}", {
        "name": "B: count 2026", "action": "count_matching",
    })

    # ===== API-level: POST new task =====
    print("\n[API] POST new task with 2 deps")
    status, body = _api("POST", "/api/tasks/", {
        "project_id": PROJ, "name": "D: combined report",
        "agent_role": "win-agent01", "action": "combine_results",
        "params": {"a": 1, "b": 2}, "depends_on": [TASKS["A"], TASKS["C"]],
    })
    ok("POST returns 201", status == 201, f"status={status}")
    new_id = body.get("id") if body else None
    ok("new task has id", new_id and new_id.startswith("t-"))
    ok("new task has 2 deps", body and len(body.get("depends_on", [])) == 2)

    # ===== API-level: verify deps on new task =====
    if new_id:
        status, body = _api("GET", f"/api/tasks/{new_id}")
        ok("GET new task returns 200", status == 200)
        ok("GET new task has 2 deps", body and len(body.get("depends_on", [])) == 2)

    # ===== API-level: DELETE the new task + verify scrub on siblings =====
    print("\n[API] DELETE new task (no siblings reference it, so scrub is no-op)")
    if new_id:
        status, body = _api("DELETE", f"/api/tasks/{new_id}")
        ok("DELETE returns 204", status == 204, f"status={status}")
        # verify it's gone
        status, _ = _api("GET", f"/api/tasks/{new_id}")
        ok("GET after DELETE returns 404", status == 404, f"status={status}")

    # ===== UI-level: open the visual page, click Edit, change name, click Save, verify reload =====
    print("\n[UI] PATCH via the form (rename task C)")
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await ctx.new_page()
        await page.goto(f"{BASE}/projects/{PROJ}/visual", wait_until="networkidle")
        await page.wait_for_timeout(500)
        # Click card C
        await page.locator(f'.vp-card[data-task-name="{TASKS["C"]}"]').click()
        await page.wait_for_timeout(200)
        # Click Edit
        await page.locator(".vp-sp-btn:has-text('Edit')").click()
        await page.wait_for_timeout(200)
        # Change name
        name_input = page.locator("#vp-edit-name")
        await name_input.fill("C: write report (UI renamed)")
        # Click Save (triggers reload)
        await page.locator(".vp-sp-btn:has-text('Save')").click()
        # wait for reload + check
        await page.wait_for_timeout(2000)
        # Verify the new name is persisted in the embedded data
        page_text = await page.locator("body").text_content()
        ok("renamed task visible in canvas after save",
           "C: write report (UI renamed)" in (page_text or ""),
           f"text contains target? {'yes' if 'C: write report (UI renamed)' in (page_text or '') else 'no'}")
        # Restore
        _api("PATCH", f"/api/tasks/{TASKS['C']}", {"name": "C: write report"})
        await browser.close()

    # ===== UI-level: Add task via the form =====
    print("\n[UI] Add task via the form (POST via UI)")
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await ctx.new_page()
        await page.goto(f"{BASE}/projects/{PROJ}/visual", wait_until="networkidle")
        await page.wait_for_timeout(500)
        # Click Add task
        await page.locator("button:has-text('Add task')").click()
        await page.wait_for_timeout(200)
        # Fill in form
        await page.locator("#vp-edit-name").fill("UI-created task")
        await page.locator("#vp-edit-action").fill("do_something")
        # agent_role left empty (will default to first profile)
        await page.locator("#vp-edit-params").fill('{"k": "v"}')
        # Tick A as a dep
        await page.locator(f'.vp-sp-deps[value="{TASKS["A"]}"]').check()
        # Click Create
        await page.locator(".vp-sp-btn:has-text('Create')").click()
        await page.wait_for_timeout(2500)  # wait for reload
        # Verify new task is in the canvas
        cards = await page.locator(".vp-card").all()
        ok("4 cards now on canvas (was 3, added 1)", len(cards) == 4, f"got {len(cards)}")
        body_text = await page.locator("body").text_content()
        ok("UI-created task visible in canvas",
           "UI-created task" in (body_text or ""))
        await browser.close()

    # ===== UI-level: Delete via X button =====
    print("\n[UI] Delete task via X button")
    # Find the UI-created task ID
    status, lst = _api("GET", f"/api/tasks/?project_id={PROJ}")
    ui_id = None
    for t in lst.get("tasks", []):
        if t.get("name") == "UI-created task":
            ui_id = t["id"]
            break
    ok("UI-created task found via API", ui_id is not None, f"id={ui_id}")
    if ui_id:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await ctx.new_page()
            # Bypass confirm() dialog
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))
            await page.goto(f"{BASE}/projects/{PROJ}/visual", wait_until="networkidle")
            await page.wait_for_timeout(500)
            # Hover the UI-created card to reveal the X button
            card = page.locator(f'.vp-card[data-task-name="{ui_id}"]')
            await card.hover()
            await page.wait_for_timeout(200)
            # Click the X
            await page.locator(f'.vp-card[data-task-name="{ui_id}"] .vp-card-del').click()
            await page.wait_for_timeout(2000)  # wait for reload
            # Verify it's gone
            cards = await page.locator(".vp-card").all()
            ok("3 cards back on canvas after X delete", len(cards) == 3, f"got {len(cards)}")
            await browser.close()

    # ===== Final API check =====
    print("\n[API] final state check")
    status, lst = _api("GET", f"/api/tasks/?project_id={PROJ}")
    task_names = [t.get("name") for t in lst.get("tasks", [])]
    print("  remaining tasks:", task_names)
    ok("A still exists", "A: list folders" in task_names)
    ok("B still exists", "B: count 2026" in task_names)
    ok("C still exists", "C: write report" in task_names)
    ok("UI-created task gone", "UI-created task" not in task_names)

    print(f"\n===== {checks_pass} passed, {checks_fail} failed =====")
    sys.exit(0 if checks_fail == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
