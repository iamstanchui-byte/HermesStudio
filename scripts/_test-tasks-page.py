"""Run tasks_page with a real request and report the exception."""
import asyncio
import traceback
from hermes_orch.api.dashboard import tasks_page
from hermes_orch.db import Database
from starlette.requests import Request

async def main():
    db = Database(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
    await db.connect()
    try:
        # Build a real Request with a real app.state.db
        the_db = db
        class FakeApp:
            class state:
                pass
            state.db = the_db
        class _State:
            db = the_db
        FakeApp.state = _State
        scope = {
            'type': 'http', 'method': 'GET', 'path': '/tasks',
            'query_string': b'',
            'headers': [(b'host', b'test')],
            'app': FakeApp,
        }
        req = Request(scope)
        try:
            r = await tasks_page(req)
            print(f"OK, status={r.status_code}")
        except Exception as e:
            traceback.print_exc()
    finally:
        await db.close()

asyncio.run(main())
