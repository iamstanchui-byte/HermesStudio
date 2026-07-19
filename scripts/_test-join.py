"""Repro the tasks_page 500 by going through the actual async db wrapper."""
import asyncio
import traceback
from hermes_orch.db import Database
from starlette.requests import Request
from starlette.datastructures import URL

async def main():
    db = Database(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
    await db.connect()
    try:
        # Try the actual SQL the page runs
        rows = await db.fetchall("""
            SELECT t.* FROM tasks t
            JOIN projects p ON t.project_id = p.id
            WHERE p.state NOT IN ('archived', 'deleted')
            ORDER BY t.created_at DESC LIMIT 50 OFFSET 0
        """, ())
        print(f"OK, {len(rows)} rows")
    except Exception as e:
        print("FAILED:")
        traceback.print_exc()
    finally:
        await db.close()

asyncio.run(main())
