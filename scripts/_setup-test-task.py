"""Set up a test task for win-local-1 wrapper daemon.

Creates a project + a task with role 'win-agent01' (matches win-local-1's profile).
This lets us test the wrapper daemon end-to-end.
"""
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx


async def main():
    async with httpx.AsyncClient() as client:
        # Create project in 'running' state (so supervisor doesn't try to plan)
        # First clear any old running projects? No — just create a new one with a unique goal.
        proj_id = "proj-" + uuid.uuid4().hex[:8]
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        proj_body = {
            "name": "Wrapper e2e test",
            "goal": "Test wrapper daemon end-to-end with win-agent01",
            "state": "running",  # skip planning
        }
        r = await client.post("http://localhost:8765/api/projects/", json=proj_body)
        if r.status_code != 201:
            print(f"create project failed: {r.status_code} {r.text}")
            sys.exit(1)
        proj = r.json()
        proj_id = proj["id"]
        print(f"created project: {proj_id}  state={proj['state']}")

        # Create a task with role 'win-agent01' assigned to win-local-1
        task_body = {
            "project_id": proj_id,
            "name": "E2E test task via win-agent01",
            "agent_role": "win-agent01",
            "action": "echo",
            "params": {"message": "Hello from wrapper daemon test"},
            "max_retries": 0,
            "timeout_seconds": 300,
        }
        r = await client.post("http://localhost:8765/api/tasks/", json=task_body)
        if r.status_code != 201:
            print(f"create task failed: {r.status_code} {r.text}")
            sys.exit(1)
        task = r.json()
        task_id = task["id"]
        print(f"created task: {task_id}  status={task['status']}  role={task['agent_role']}")

        # Manually assign to win-local-1 (simulating what supervisor would do)
        # (Avoids waiting for supervisor tick + checks if profile exists)
        r = await client.post(
            f"http://localhost:8765/api/tasks/{task_id}/assign",
            json={"agent_id": "win-local-1"},
        )
        if r.status_code != 200:
            print(f"assign task failed: {r.status_code} {r.text}")
            sys.exit(1)
        task = r.json()
        print(f"assigned to: {task.get('assigned_agent_id')}  status={task['status']}")

        print()
        print(f"READY. Project={proj_id}  Task={task_id}")
        print("Now start the daemon with --once to pick it up:")
        print(f"  python -m hermes_orch.agent_cli start --once")


asyncio.run(main())
