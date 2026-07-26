"""Debug Reset & cascade UI flow."""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import asyncio
import sqlite3
import urllib.request
import json
from playwright.async_api import async_playwright

BASE = "http://localhost:8765"
DB = "C:/Users/stanley/.hermes-orchestrator/hermes-orch.db"


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


async def main():
    # Setup
    _, proj = _api("POST", "/api/projects/", {"name": "e2e-reset-debug"})
    pid = proj["id"]
    _, tA = _api("POST", "/api/tasks/", {
        "project_id": pid, "name": "A: source",
        "agent_role": "win-agent01", "action": "act_a", "depends_on": []
    })
    _, tB = _api("POST", "/api/tasks/", {
        "project_id": pid, "name": "B: middle",
        "agent_role": "win-agent01", "action": "act_b", "depends_on": [tA["id"]]
    })
    A, B = tA["id"], tB["id"]
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE tasks SET status='completed' WHERE id IN (?, ?)", (A, B))
    conn.execute("UPDATE tasks SET result=?", (json.dumps({"summary": "x"}),))
    conn.execute("UPDATE projects SET state='completed' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    print(f"setup: A={A} B={B}")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await ctx.new_page()

            page.on("console", lambda m: print(f"  CONSOLE.{m.type}: {m.text}"))
            page.on("pageerror", lambda e: print(f"  PAGE_ERR: {e}"))
            page.on("request", lambda req: print(f"  REQ {req.method} {req.url}") if "reset-and-cascade" in req.url or "tasks/" in req.url else None)
            page.on("response", lambda res: print(f"  RES {res.status} {res.url}") if "reset-and-cascade" in res.url else None)

            all_dialogs = []
            async def on_dialog(d):
                print(f"  DIALOG ({d.type}): {d.message[:200]}")
                all_dialogs.append((d.type, d.message))
                await d.accept()
            page.on("dialog", lambda d: asyncio.create_task(on_dialog(d)))

            await page.goto(f"{BASE}/projects/{pid}/visual", wait_until="networkidle")
            await page.wait_for_timeout(800)
            print("\n>> click card A")
            await page.locator(f'.vp-card[data-task-name="{A}"]').click()
            await page.wait_for_timeout(400)
            print("\n>> click Reset & cascade button")
            reset_btns = page.locator(".vp-sp-btn-primary")
            for i in range(await reset_btns.count()):
                txt = await reset_btns.nth(i).text_content() or ""
                print(f"  primary btn[{i}]: '{txt.strip()}'")
            for i in range(await reset_btns.count()):
                txt = await reset_btns.nth(i).text_content() or ""
                if "Reset" in txt:
                    print(f"  -> clicking btn[{i}]: '{txt.strip()}'")
                    await reset_btns.nth(i).click()
                    break
            await page.wait_for_timeout(3000)
            print(f"\n>> dialogs fired: {len(all_dialogs)}")
            for d_type, d_msg in all_dialogs:
                print(f"   {d_type}: {d_msg[:200]}")

            _, body_a = _api("GET", f"/api/tasks/{A}")
            _, body_b = _api("GET", f"/api/tasks/{B}")
            print(f"\n>> A: status={body_a.get('status')} result={body_a.get('result')}")
            print(f">> B: status={body_b.get('status')} result={body_b.get('result')}")

            await browser.close()
    finally:
        conn = sqlite3.connect(DB)
        conn.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.commit()
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
