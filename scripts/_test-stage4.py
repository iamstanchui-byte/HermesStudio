"""Verify the Stage 4 polling on visual_project.html works end-to-end."""
from playwright.sync_api import sync_playwright
import time

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    errors = []
    page.on(
        "console",
        lambda msg: errors.append(("console", msg.type, msg.text))
        if msg.type in ("error", "warning")
        else None,
    )
    page.on("pageerror", lambda exc: errors.append(("pageerror", "", str(exc))))
    page.goto(
        "http://127.0.0.1:8765/projects/proj-8a41f88b/visual",
        wait_until="networkidle",
        timeout=15000,
    )
    # Wait 7s to give the poller a chance to fire twice
    time.sleep(7)
    # Check the banner is hidden by default
    banner_display = page.evaluate(
        '() => document.getElementById("vp-stale-banner").style.display'
    )
    print("banner display (should be 'none'):", repr(banner_display))
    # Check the hash was computed
    has_hash = page.evaluate(
        '() => typeof _vpLastDataHash === "string" && _vpLastDataHash.length > 0'
    )
    print("hash computed:", has_hash)
    # Check the canvas has drawflow nodes
    nodes = page.evaluate("() => document.querySelectorAll('.drawflow-node').length")
    print("drawflow nodes:", nodes)
    print("console errors:", errors)

    # Now open a side panel and verify the banner is hidden (until
    # the hash would change — which it won't, since data is static).
    # Click the first card to open the panel.
    page.evaluate("() => vpOpenPanel(Object.keys(_VP_DATA.tasks)[0])")
    time.sleep(0.5)
    panel_hidden = page.evaluate(
        '() => document.getElementById("vp-side-panel").classList.contains("hidden")'
    )
    print("panel open after click:", not panel_hidden)
    time.sleep(4)  # wait for 1+ poll tick
    # Banner should still be hidden (no data change → no stale)
    banner_display = page.evaluate(
        '() => document.getElementById("vp-stale-banner").style.display'
    )
    print("banner display with panel open, no data change (should be 'none'):",
          repr(banner_display))
    browser.close()
