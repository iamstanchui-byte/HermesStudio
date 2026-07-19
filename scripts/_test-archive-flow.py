"""Quick smoke test: archive -> unarchive -> verify state='completed'."""
import sqlite3
import httpx

BASE = "http://localhost:8765"
DB = r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db"

# Use a real project
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
project = con.execute("SELECT id, state FROM projects WHERE state IN ('running', 'completed', 'planning') LIMIT 1").fetchone()
con.close()
if not project:
    print("No active project to test with. Skipping.")
    raise SystemExit(0)

pid = project["id"]
initial_state = project["state"]
print(f"Test project: {pid}  initial state: {initial_state}")

with httpx.Client(base_url=BASE, timeout=10) as c:
    # 1. Archive
    r = c.post(f"/api/projects/{pid}/archive")
    print(f"After archive: {r.status_code}  {r.json()}")

    # 2. Check state in DB
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    state = con.execute("SELECT state FROM projects WHERE id = ?", (pid,)).fetchone()["state"]
    con.close()
    print(f"DB state after archive: {state}")
    assert state == "archived", f"expected archived, got {state}"

    # 3. Unarchive
    r = c.post(f"/api/projects/{pid}/unarchive")
    print(f"After unarchive: {r.status_code}  {r.json()}")

    # 4. Check state in DB
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    state = con.execute("SELECT state FROM projects WHERE id = ?", (pid,)).fetchone()["state"]
    con.close()
    print(f"DB state after unarchive: {state}")
    assert state == "completed", f"expected completed (not running again!), got {state}"
    print("PASS: unarchive goes to completed, not planning/running")
