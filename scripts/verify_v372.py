"""Verify v3.7.2: profile-settings modal is a consistent fixed size across tabs.

Before: modal-card had only `max-height: 86vh` and no `height`, so each tab
caused the card to auto-size to its content (Overview / MCP ~280px, Skills
~600px). User saw the modal "jitter" up and down when switching tabs.

After: modal-card has `height: 600px` (with `max-height: 86vh` for short
viewports) and the inner tab content uses `flex: 1; min-height: 0;
overflow-y: auto` so it scrolls inside the fixed-height card.

This script:
  1. Opens /agents
  2. Logs in as admin (the page requires session)
  3. Clicks the first profile card to open the modal
  4. Switches through all 5 tabs (Overview / MCP / Skills / Storage /
     Capabilities), measuring the modal-card height + width each time
  5. Asserts all 5 heights are within 2px of each other (the lock)
  6. Takes a screenshot per tab to `output/agents/v372/` for visual review
"""
import asyncio
import time
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8765"
OUT_DIR = Path("output/agents/v372")


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1600, "height": 900})
        page = await ctx.new_page()

        # Surface any JS errors / 4xx-5xx responses so we don't miss a
        # template render bug introduced by the inline-style change.
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on(
            "console",
            lambda m: errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" else None,
        )
        page.on(
            "response",
            lambda r: errors.append(f"http {r.status}: {r.url}")
            if r.status >= 400 and "/api/" in r.url else None,
        )

        # Log in (page-side auth cookie). Admin/ADMIN is the seeded dev
        # user — matches the screenshots in the bug report.
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30000)
        await page.fill("input[name=username]", "admin")
        await page.fill("input[name=password]", "verify-v372-temp")
        await page.click("button[type=submit]")
        # Login redirects to /agents (the form's `next` defaults there).
        # Give the redirect + page render a moment — `networkidle` was
        # too strict here (a polling timer kept firing).
        await page.wait_for_timeout(2500)

        # If we got bounced to /login (e.g. stale cookie), retry once.
        if "/login" in page.url:
            await page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30000)
            await page.fill("input[name=username]", "admin")
            await page.fill("input[name=password]", "verify-v372-temp")
            await page.click("button[type=submit]")
            await page.wait_for_timeout(2500)

        # Go to the agents page.
        await page.goto(f"{BASE}/agents", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(500)

        # Click the first profile card (whatever agent it's on). If there
        # are zero profiles, the test fails loudly — that's the desired
        # signal (no profiles = nothing to verify).
        first_profile = await page.query_selector(".profile-selector")
        if not first_profile:
            raise SystemExit("FAIL: no profile cards rendered on /agents — "
                             "need at least one profile to verify the modal.")

        await first_profile.click()

        # Wait for the modal to be visible. The openProfileModal() sets
        # `display: flex`; we use the modal-card's bounding box to be
        # sure it's painted.
        modal = page.locator("#profile-modal .modal-card")
        await modal.wait_for(state="visible", timeout=10000)

        # Measure across all 5 tabs.
        tabs = ["overview", "mcp", "skills", "storage", "capabilities"]
        sizes: dict[str, dict[str, int]] = {}

        for tab in tabs:
            tab_btn = page.locator(f'.profile-tab[data-tab="{tab}"]')
            await tab_btn.click()
            # Give the JS-rendered tab content a moment to settle.
            await page.wait_for_timeout(150)

            box = await modal.bounding_box()
            if box is None:
                raise SystemExit(f"FAIL: modal-card not measurable on {tab}")
            sizes[tab] = {
                "x": int(box["x"]),
                "y": int(box["y"]),
                "w": int(box["width"]),
                "h": int(box["height"]),
            }

            # Screenshot the modal region (clip to bounding box + 20px padding).
            shot_path = OUT_DIR / f"tab_{tab}.png"
            await page.screenshot(
                path=str(shot_path),
                clip={
                    "x": max(0, box["x"] - 20),
                    "y": max(0, box["y"] - 20),
                    "width": min(1600, box["width"] + 40),
                    "height": min(900, box["height"] + 40),
                },
            )

        await browser.close()

    # Report
    print("Modal size per tab (px):")
    print(f"  {'tab':<14} {'w':>5} {'h':>5}  {'x':>5} {'y':>5}")
    print("  " + "-" * 42)
    for tab in tabs:
        s = sizes[tab]
        print(f"  {tab:<14} {s['w']:>5} {s['h']:>5}  {s['x']:>5} {s['y']:>5}")

    heights = [sizes[t]["h"] for t in tabs]
    widths = [sizes[t]["w"] for t in tabs]
    h_min, h_max = min(heights), max(heights)
    w_min, w_max = min(widths), max(widths)
    print(f"\nHeight range: {h_min} - {h_max}  (spread {h_max - h_min}px)")
    print(f"Width  range: {w_min} - {w_max}  (spread {w_max - w_min}px)")

    # The lock should make all 5 tabs the same height. Allow 2px tolerance
    # for sub-pixel rendering.
    TOL = 2
    ok_h = (h_max - h_min) <= TOL
    ok_w = (w_max - w_min) <= TOL
    target_h = 600  # the height we set in the inline style

    print()
    print(f"  height locked within {TOL}px:  {'PASS' if ok_h else 'FAIL'}")
    print(f"  width  locked within {TOL}px:  {'PASS' if ok_w else 'FAIL'}")
    print(f"  height near target {target_h}px: "
          f"{'PASS' if abs(h_min - target_h) <= TOL else f'WARN (got {h_min}px)'}")
    print(f"  screenshots:  {OUT_DIR}/")

    if errors:
        print("\nJS / HTTP errors observed during the run:")
        for e in errors:
            print(f"  {e}")

    if not (ok_h and ok_w):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
