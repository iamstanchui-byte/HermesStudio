"""Apply schema to DB (idempotent)."""
import asyncio
from hermes_orch.db import Database, SCHEMA

async def main():
    db = Database(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
    await db.connect()
    print("applying schema...")
    await db._conn.executescript(SCHEMA)
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='project_soul_presets'"
    )
    rows = await cur.fetchall()
    print(f"project_soul_presets table exists: {len(rows) > 0}")
    await db.close()

asyncio.run(main())
