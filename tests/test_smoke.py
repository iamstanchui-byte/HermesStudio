"""Smoke test: verify the FastAPI app boots and /api/health responds."""
import pytest
from httpx import ASGITransport, AsyncClient

from hermes_orch.main import create_app


@pytest.mark.asyncio
async def test_health_check() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            r = await ac.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "hermes-orchestrator"}
