"""Playwright e2e for the Single Tasks UI."""
import json
import time
import urllib.request

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 900})
    errors = []
    page.on(
        "console",
        lambda msg: errors.append((msg.type, msg.text))
        if msg.type in ("error", "warning")
        else None,
    )
    page.on("pageerror", lambda exc: errors.append(("pageerror", str(exc))))

    # 1. Single tasks list page
    page.goto(
        "http://127.0.0.1:8765/single-tasks",
        wait_until="networkidle",
        timeout=15000,
    )
    print("GET /single-tasks: status ok")
    has_create_btn = page.locator("text=+ Create single task").count() > 0
    has_heading = page.locator('h1:has-text("Single tasks")').count() > 0
    has_nav = page.locator('nav a[href="/single-tasks"]').count() > 0
    print(f"  has '+ Create single task' button: {has_create_btn}")
    print(f"  has 'Single tasks' heading: {has_heading}")
    print(f"  has nav link to /single-tasks: {has_nav}")

    # 2. Click create button
    page.locator("text=+ Create single task").click()
    time.sleep(0.3)
    form_visible = page.locator("#create-form:not(.hidden)").count() > 0
    print(f"  create form visible: {form_visible}")

    # 3. Fill in the form
    page.fill("#ct-name", "e2e test single task")
    page.fill("#ct-goal", "verify the UI works")
    page.fill("#ct-source", '{"kind": "e2e_test", "tag": "playwright"}')
    print("  form fields filled")

    # 4. Submit
    page.locator("#ct-submit").click()
    page.wait_for_url("**/single-tasks/t-*", timeout=15000)
    print(f"  navigated to: {page.url}")

    # 5. Verify the detail page
    has_goal = page.locator('h2:has-text("Goal")').count() > 0
    has_name = page.locator('h1:has-text("e2e test single task")').count() > 0
    has_kind = page.locator('text=e2e_test').count() > 0
    print(f"  detail page has 'Goal': {has_goal}")
    print(f"  detail page shows 'e2e test single task': {has_name}")
    print(f"  detail page shows 'e2e_test' kind: {has_kind}")

    # 6. Go back to list
    page.goto("http://127.0.0.1:8765/single-tasks", wait_until="networkidle", timeout=15000)
    has_row = page.locator("text=e2e test single task").count() > 0
    print(f"  list page has 'e2e test single task' row: {has_row}")

    # 7. Screenshot
    page.screenshot(path="scripts/_e2e-single-tasks.png", full_page=False)
    print("  screenshot saved: scripts/_e2e-single-tasks.png")

    # 8. Console errors (filter out the harmless tailwind warning)
    real_errors = [e for e in errors if "tailwindcss" not in e[1].lower()]
    print(f"  console errors (non-tailwind): {real_errors}")

    # 9. Cleanup via API
    r = urllib.request.urlopen("http://127.0.0.1:8765/api/single-tasks", timeout=5)
    d = json.loads(r.read())
    for t in d.get("tasks", []):
        if t.get("name") == "e2e test single task":
            req = urllib.request.Request(
                f"http://127.0.0.1:8765/api/tasks/{t['id']}", method="DELETE"
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                print(f"  cleaned up: {t['id']}")
            except Exception as e:
                print(f"  cleanup error: {e}")
    browser.close()
