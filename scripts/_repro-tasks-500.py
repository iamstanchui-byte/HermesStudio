"""Repro the /tasks 500 by running the route through FastAPI TestClient."""
import sys
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
from hermes_orch.main import app  # may not exist; try direct import

# Try the main module
try:
    from hermes_orch.main import app
except ImportError:
    # Direct create
    from fastapi import FastAPI
    from hermes_orch.api.dashboard import router
    from hermes_orch.db import Database

    app = FastAPI()
    app.include_router(router)

    @app.on_event("startup")
    async def startup():
        app.state.db = Database(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
        await app.state.db.connect()

    @app.on_event("shutdown")
    async def shutdown():
        await app.state.db.close()

client = TestClient(app)
r = client.get("/tasks")
print(f"Status: {r.status_code}")
print(f"Body (first 500 chars): {r.text[:500]}")
