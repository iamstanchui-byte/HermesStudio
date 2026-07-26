"""Playwright e2e for the Optimize button on the project page."""
import time

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

    # Open the chat panel on a project page
    page.goto(
        "http://127.0.0.1:8765/projects/proj-8fece23e",
        wait_until="networkidle",
        timeout=15000,
    )
    print("GET /projects/proj-8fece23e: status ok")
    has_optimize = page.locator("#chat-optimize-btn").count() > 0
    print(f"  has Optimize button: {has_optimize}")

    # Open the chat if not open
    chat_panel = page.locator("#chat-panel")
    if "hidden" in (chat_panel.get_attribute("class") or ""):
        page.evaluate("toggleChatPanel && toggleChatPanel()")
        time.sleep(0.3)
    print(f"  chat panel visible: {not ('hidden' in (chat_panel.get_attribute('class') or ''))}")

    # Click Optimize
    page.locator("#chat-optimize-btn").click()
    print("  clicked Optimize — waiting for LLM response")

    # Wait for either suggestions to appear or an error
    try:
        page.wait_for_selector(
            "text=deterministic candidate",
            timeout=60000,
        )
        print("  LLM returned suggestions card")
    except Exception:
        # Either LLM error or 0 suggestions
        try:
            page.wait_for_selector(
                "text=No deterministic candidates found",
                timeout=5000,
            )
            print("  LLM returned 0 suggestions")
        except Exception:
            try:
                page.wait_for_selector(
                    "text=Optimize failed",
                    timeout=5000,
                )
                err_text = page.locator("text=Optimize failed").first.inner_text()
                print(f"  LLM error: {err_text[:200]}")
            except Exception:
                print(f"  unknown state; errors: {errors[:3]}")

    # Screenshot
    page.screenshot(path="scripts/_e2e-optimize.png", full_page=False)
    print("  screenshot saved: scripts/_e2e-optimize.png")

    real_errors = [e for e in errors if "tailwindcss" not in e[1].lower()]
    print(f"  console errors (non-tailwind): {real_errors}")
    browser.close()
