"""debug-task.py — find the actual error in POST /api/tasks/.

Runs the endpoint in-process so we get the full Python traceback,
not just "Internal Server Error".
"""
import asyncio
import traceback

from httpx import ASGITransport, AsyncClient

from hermes_orch.main import create_app


async def main() -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            print("[1] Creating project...")
            r = await ac.post("/api/projects/", json={"goal": "Debug", "name": "DebugProj"})
            print(f"  status={r.status_code} body={r.text[:200]}")
            if r.status_code != 201:
                return
            proj_id = r.json()["id"]
            print(f"  proj_id={proj_id}")

            print("\n[2] Creating task (this is where the 500 happens)...")
            try:
                r = await ac.post(
                    "/api/tasks/",
                    json={
                        "project_id": proj_id,
                        "name": "DebugTask",
                        "agent_role": "test",
                        "action": "test_action",
                        "params": {"x": 1},
                    },
                )
                print(f"  status={r.status_code}")
                print(f"  body={r.text}")
            except Exception as e:
                print(f"\n!! EXCEPTION !!")
                print(f"  type: {type(e).__name__}")
                print(f"  msg:  {e}")
                traceback.print_exc()


asyncio.run(main())
