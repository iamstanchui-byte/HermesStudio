"""FastAPI app factory + lifespan.

Routes mounted from src/hermes_orch/api/* submodules.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI

from hermes_orch.config import load_config
from hermes_orch.core.cleanup import CleanupJob
from hermes_orch.core.notifier import Notifier
from hermes_orch.core.planner import Planner
from hermes_orch.core.supervisor import Supervisor
from hermes_orch.db import Database

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan: load config, connect DB, start supervisor on startup; stop on shutdown."""
    cfg = load_config()
    app.state.config = cfg

    db_path = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
    db = Database(db_path)
    await db.connect()
    app.state.db = db

    # Start the brain: notifier + planner + supervisor (background task)
    notifier = Notifier(cfg)
    planner = Planner(cfg)
    supervisor = Supervisor(db, cfg, notifier, planner)
    # CleanupJob shared by supervisor (daily sweep) + API (manual run).
    # Create BEFORE supervisor.start() so the supervisor can fire its
    # first daily-sweep check on the next tick.
    cleanup_job = CleanupJob(db, cfg)
    supervisor.set_cleanup_job(cleanup_job)
    app.state.notifier = notifier
    app.state.planner = planner
    app.state.supervisor = supervisor
    app.state.cleanup = cleanup_job
    supervisor.start()

    # Fire-and-forget startup cleanup. Skip if retention is 0
    # (disabled). Errors are logged by the job itself; we don't
    # block server startup on cleanup.
    import asyncio
    if cleanup_job.enabled:
        async def _startup_cleanup():
            try:
                await cleanup_job.run(trigger="auto")
            except Exception as e:
                logger.warning("startup cleanup crashed: %s", e)
        asyncio.create_task(_startup_cleanup())

    logger.info("Hermes orchestrator started, db=%s", db_path)
    try:
        yield
    finally:
        await supervisor.stop()
        await db.close()
        logger.info("Hermes orchestrator stopped")


def create_app() -> FastAPI:
    """Create FastAPI app instance."""
    app = FastAPI(
        title="Hermes Orchestrator",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Cache-Control: no-store for the agents router. The dashboard's
    # 10s polling does location.reload() which can keep serving stale
    # page-cached JSON responses (e.g. after we fixed the skill layout
    # bug, users kept seeing the old /SKILL/SKILL.md rendering because
    # their browser had cached the previous API response). Setting
    # no-store on the skills endpoints forces a fresh fetch every time,
    # so the dashboard always shows the current server state.
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request as StarletteRequest

    class _NoStoreOnSkills(BaseHTTPMiddleware):
        async def dispatch(self, request: StarletteRequest, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/api/agents/") and "/skills" in request.url.path:
                response.headers["Cache-Control"] = "no-store"
            return response

    app.add_middleware(_NoStoreOnSkills)

    # Health check
    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "hermes-orchestrator"}

    # Mount routers (will be filled in as we build)
    from hermes_orch.api.agents import router as agents_router
    from hermes_orch.api.artifacts import router as artifacts_router
    from hermes_orch.api.auth import router as auth_router
    from hermes_orch.api.dashboard import router as dashboard_router
    from hermes_orch.api.projects import router as projects_router
    from hermes_orch.api.settings import router as settings_router
    from hermes_orch.api.tasks import router as tasks_router

    app.include_router(agents_router, prefix="/api/agents", tags=["agents"])
    app.include_router(artifacts_router, prefix="/api/artifacts", tags=["artifacts"])
    app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
    app.include_router(projects_router, prefix="/api/projects", tags=["projects"])
    app.include_router(settings_router, prefix="/api/settings", tags=["settings"])
    app.include_router(tasks_router, prefix="/api/tasks", tags=["tasks"])
    app.include_router(dashboard_router, tags=["dashboard"])

    return app


app = create_app()
