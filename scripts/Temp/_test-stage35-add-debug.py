"""Debug version: trace the Add task UI flow."""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import asyncio
from playwright.async_api import async_playwright

BASE = "http://localhost:8765"
PROJ = "proj-d07a152f"
TASKS_A = "t-195181ff"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await ctx.new_page()

        # Capture all network requests
        def on_request(req):
            if "tasks" in req.url or "chat" in req.url:
                print(f"  REQ {req.method} {req.url}  body={req.post_data[:200] if req.post_data else 'none'}")
        page.on("request", on_request)

        # Capture responses
        def on_response(res):
            if "tasks" in res.url or "chat" in res.url:
                print(f"  RES {res.status} {res.url}")
        page.on("response", on_response)

        # Capture console
        page.on("console", lambda m: print(f"  CON {m.type}: {m.text}"))
        page.on("pageerror", lambda e: print(f"  PAGE_ERR: {e}"))

        # Capture alerts
        alerts = []
        async def on_dialog(d):
            print(f"  DIALOG: {d.type} '{d.message}'")
            alerts.append(d.message)
            await d.dismiss()  # dismiss so it doesn't block
        page.on("dialog", lambda d: asyncio.create_task(on_dialog(d)))

        await page.goto(f"{BASE}/projects/{PROJ}/visual", wait_until="networkidle")
        await page.wait_for_timeout(500)
        print("\n>> Click Add task")
        await page.locator("button:has-text('Add task')").click()
        await page.wait_for_timeout(300)
        print(">> Fill form")
        await page.locator("#vp-edit-name").fill("UI-debug-task")
        await page.locator("#vp-edit-action").fill("do_something")
        await page.locator("#vp-edit-params").fill('{"k": "v"}')
        await page.locator(f'.vp-sp-deps[value="{TASKS_A}"]').check()
        await page.wait_for_timeout(200)
        print(">> Click Create")
        # Use exact match to avoid matching other things
        create_btns = await page.locator(".vp-sp-btn-primary").all()
        for i, b in enumerate(create_btns):
            txt = await b.text_content()
            print(f"   vp-sp-btn-primary[{i}]: '{txt}'")
        # Click the one with text "Create"
        for b in create_btns:
            txt = await b.text_content() or ""
            if "Create" in txt:
                await b.click()
                break
        await page.wait_for_timeout(3000)
        print(f"\n>> Cards after: {(await page.locator('.vp-card').all()) and len(await page.locator('.vp-card').all())}")
        print(f">> Alerts: {alerts}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
