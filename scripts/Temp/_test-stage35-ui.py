"""Stage 3.5 UI E2E test.

Walks through the new visual project page features:
  1. Open /projects/{id}/visual on a project with 3 pending tasks
  2. Verify cards render + depends_on wires drawn
  3. Click a card -> side panel -> Edit button -> edit form
  4. Verify edit form fields populated correctly
  5. Click "Add task" button -> new task form
  6. Verify agent_role <select> has profile options
  7. Hover a card -> X delete button visible
  8. Take screenshots of each major state

Output: screenshots saved to scripts/Temp/_stage35-*.png
Exit 0 on success, 1 on failure.
"""
import sys
# Force UTF-8 stdout (PowerShell default is cp1252 which crashes on emoji)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

BASE = "http://localhost:8765"
PROJ = "proj-d07a152f"
TASKS = ["t-195181ff", "t-58b8c16a", "t-96feff3e"]  # A, B, C
SCREENS = Path("scripts/Temp")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await ctx.new_page()
        page.on("pageerror", lambda e: print(f"PAGE ERROR: {e}"))
        page.on("console", lambda m: print(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)

        checks_pass = 0
        checks_fail = 0

        def ok(name, condition, detail=""):
            nonlocal checks_pass, checks_fail
            if condition:
                print(f"  PASS  {name}{(' — ' + detail) if detail else ''}")
                checks_pass += 1
            else:
                print(f"  FAIL  {name}{(' — ' + detail) if detail else ''}")
                checks_fail += 1

        # ===== 1. Open visual page =====
        print("\n[1] open /visual on", PROJ)
        await page.goto(f"{BASE}/projects/{PROJ}/visual", wait_until="networkidle")
        await page.wait_for_timeout(800)  # let drawflow init + paths recompute

        # ===== 2. Verify cards render + wires =====
        print("\n[2] verify cards + wires")
        cards = await page.locator(".vp-card").all()
        ok("3 cards rendered", len(cards) == 3, f"got {len(cards)} cards")
        connections = await page.locator("svg.connection").count()
        ok("2 depends_on wires drawn", connections == 2, f"got {connections}")
        await page.screenshot(path=str(SCREENS / "_stage35-1-canvas.png"), full_page=True)

        # ===== 3. Click card C -> side panel -> Edit =====
        print("\n[3] click card C, open Edit form")
        card_c = page.locator(f'.vp-card[data-task-name="{TASKS[2]}"]')
        await card_c.click()
        await page.wait_for_timeout(300)
        panel_visible = await page.locator("#vp-side-panel").is_visible()
        ok("side panel opens on card click", panel_visible)
        title_text = await page.locator("#vp-sp-title").text_content()
        ok("title shows task name", "C: write report" in (title_text or ""), f"title='{title_text}'")

        # Click Edit
        await page.locator(".vp-sp-btn:has-text('Edit')").click()
        await page.wait_for_timeout(200)

        # ===== 4. Verify edit form fields populated =====
        print("\n[4] verify edit form fields populated")
        name_val = await page.locator("#vp-edit-name").input_value()
        action_val = await page.locator("#vp-edit-action").input_value()
        params_val = await page.locator("#vp-edit-params").input_value()
        agent_role_val = await page.locator("#vp-edit-agent_role").input_value()
        deps_checked = await page.locator(".vp-sp-deps:checked").count()
        ok("name field populated", name_val == "C: write report", f"name='{name_val}'")
        ok("action field populated", action_val == "write_report", f"action='{action_val}'")
        ok("params field populated", '"format"' in params_val, f"params='{params_val}'")
        ok("agent_role field populated", agent_role_val == "win-agent02", f"agent_role='{agent_role_val}'")
        ok("1 dep checked (B)", deps_checked == 1, f"got {deps_checked} checked")
        await page.screenshot(path=str(SCREENS / "_stage35-2-edit-form.png"), full_page=True)

        # ===== 5. Cancel back to read-only =====
        print("\n[5] cancel edit")
        await page.locator(".vp-sp-btn:has-text('Cancel')").first.click()
        await page.wait_for_timeout(200)
        # Should be back in read-only mode (no edit form)
        edit_visible = await page.locator("#vp-edit-name").count()
        ok("back to read-only (no form inputs)", edit_visible == 0)

        # ===== 6. Close panel, test Add task =====
        print("\n[6] close panel + click Add task")
        await page.locator(".vp-sp-close").click()
        await page.wait_for_timeout(200)
        panel_hidden = not await page.locator("#vp-side-panel").is_visible()
        ok("panel closes on X click", panel_hidden)

        await page.locator("button:has-text('Add task')").click()
        await page.wait_for_timeout(200)
        panel_visible = await page.locator("#vp-side-panel").is_visible()
        ok("Add task opens panel", panel_visible)
        title_text = await page.locator("#vp-sp-title").text_content()
        ok("title shows Add task", "Add task" in (title_text or ""), f"title='{title_text}'")

        # ===== 7. Verify agent_role <select> has profile options =====
        print("\n[7] verify agent_role select has profile options")
        options = await page.locator("#vp-edit-agent_role option").all()
        opt_count = len(options)
        opt_texts = []
        for o in options:
            t = await o.text_content()
            v = await o.get_attribute("value")
            opt_texts.append(f"{v}={t}")
        ok("select has >=4 options (empty + 4 profiles)", opt_count >= 4,
           f"got {opt_count}: {', '.join(opt_texts)}")
        has_win_agent = any("win-agent01" in t for t in opt_texts)
        ok("select contains 'win-agent01'", has_win_agent, f"options: {opt_texts}")
        await page.screenshot(path=str(SCREENS / "_stage35-3-add-task.png"), full_page=True)

        # ===== 8. Close, hover card A -> X delete button visible =====
        print("\n[8] close + hover card A to test X delete")
        await page.locator(".vp-sp-close").click()
        await page.wait_for_timeout(200)
        card_a = page.locator(f'.vp-card[data-task-name="{TASKS[0]}"]')
        await card_a.hover()
        await page.wait_for_timeout(200)
        # The X button is inside the card; verify it's visible
        del_btn = page.locator(f'.vp-card[data-task-name="{TASKS[0]}"] .vp-card-del')
        del_visible = await del_btn.is_visible()
        ok("X delete button visible on hover", del_visible)
        await page.screenshot(path=str(SCREENS / "_stage35-4-hover-delete.png"), full_page=True)

        # ===== 9. Verify X is hidden on a non-pending card (n/a here since all pending) =====
        # (Would need a completed task fixture to test; skip)

        # ===== Summary =====
        print(f"\n===== {checks_pass} passed, {checks_fail} failed =====")
        await browser.close()
        sys.exit(0 if checks_fail == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
