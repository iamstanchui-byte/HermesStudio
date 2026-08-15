# coding: utf-8
"""FastAPI app factory + lifespan.

Routes mounted from src/hermes_orch/api/* submodules.
"""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi import HTTPException as FastAPIHTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

from hermes_orch.config import find_config_path, load_config
from hermes_orch.core.cleanup import CleanupJob
from hermes_orch.core.error_contract import (
    make_error_response,
    new_request_id,
    parse_error_detail,
)
from hermes_orch.core.notifier import Notifier
from hermes_orch.core.planner import Planner
from hermes_orch.core.scheduler import Scheduler
from hermes_orch.core.supervisor import Supervisor
from hermes_orch.db import Database

logger = logging.getLogger(__name__)


# v3.12.1 follow-up #7 (reflection test enabler): _HMAC_PATH_PATTERNS
# was previously a local variable inside _create_app(). The systematic
# allowlist test (tests/test_hmac_middleware_allowlist_systematic.py)
# needs to introspect the list, so it's been hoisted to module level.
# Runtime behavior is identical: the same patterns are evaluated by
# the same dispatch() loop in _RequireUserMiddleware. We intentionally
# do NOT (yet) auto-generate this list from the FastAPI route table —
# that's the v3.12.2 router-driven rebuild tracked separately. For
# now the explicit list + the systematic test is the safety net.
_HMAC_PATH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^/api/agents/[^/]+/heartbeat/?$"),
    # v0.7 §1.4 (2026-08-15): the v0.7 status endpoint for
    # bootstrapper enrollment polling. The endpoint itself is
    # HMAC-authed via Depends(require_hmac_auth_v07); the
    # user-cookie middleware just passes it through. Per
    # the existing BUGFIX convention, no trailing-slash
    # required.
    re.compile(r"^/api/agents/[^/]+/status/?$"),
    # v0.7 §1.4 (2026-08-15): the v0.7 enrollment endpoint
    # (POST /api/enrollment/v07). The body is informational;
    # the verifier proves identity via the hmac_key_id +
    # hmac_secret pair (no token needed).
    re.compile(r"^/api/enrollment/v07/?$"),
    re.compile(r"^/api/agents/[^/]+/profiles/[^/]+/configs/pending/?$"),
    re.compile(r"^/api/agents/[^/]+/sessions/[^/]+/cleanup-ack/?$"),
    re.compile(r"^/api/agents/[^/]+/sessions/[^/]+/terminal-ack/?$"),
    re.compile(r"^/api/agents/[^/]+/sessions/[^/]+/tool-ack/?$"),
    re.compile(r"^/api/agents/[^/]+/sessions/[^/]+/tool-output/?$"),
    re.compile(r"^/api/agents/[^/]+/sessions/[^/]+/(?:start|update|complete|fail|log|abort)/?$"),
    # v3.10.3 (2026-08-02) BUGFIX: `/(?:skills|mcp|llm)/.*$` requires
    # at least a `/` after the resource name. The wrapper calls
    # `/api/agents/{id}/profiles/{name}/skills` (NO trailing slash,
    # query string `?include_deleted=1` is stripped to the path
    # before regex.match), which doesn't match this pattern, so
    # the user-cookie middleware returns 401 "Not authenticated"
    # BEFORE the route handler (or `require_hmac_auth`) ever runs.
    # The endpoint itself is HMAC-authed via Depends — there's no
    # reason the middleware should be blocking it. Add `/?` before
    # `.*$` so the path is allowed with or without a trailing
    # slash (and with arbitrary query string after, since the
    # middleware strips the query before testing).
    re.compile(r"^/api/agents/[^/]+/profiles/[^/]+/(?:skills|mcp|llm)/?.*$"),
    # v3.10.3 (2026-08-02) BUGFIX: bootstrap secret endpoint
    # `/api/agents/{id}/secret` was missing from the allowlist
    # (the endpoint itself is intentionally unauthenticated
    # per the inline docstring in api/agents.py — it's the
    # one-shot HMAC bootstrap that pushes the wrapper's shared
    # secret into the DB on wrapper startup). Without this
    # entry, the user-cookie middleware returned 401 BEFORE
    # the route handler, so every wrapper restart printed
    # `[hmac] WARNING: bootstrap returned 401` and the operator
    # saw a noisy startup log. The endpoint is harmless to
    # allowlist: it accepts a body, validates the agent exists,
    # and only mutates `hmac_secret` if the supplied value
    # matches (idempotent 200) or is NULL (one-shot 201); a
    # mismatched value returns 409 with no DB change. So an
    # allowlist entry here only blocks the middleware's
    # cookie check, not the endpoint's own consistency logic.
    re.compile(r"^/api/agents/[^/]+/secret/?$"),
    # v3.12.1 follow-up #6: max_history_config — the wrapper polls
    # this on every tick to learn the server-side
    # `default_max_history_turns` and apply hermes 0.19.1
    # `compression.protect_last_n: N` to `~/.hermes/config.yaml`.
    # Without this entry, the user-cookie middleware returns 401
    # BEFORE the route handler (or `require_hmac_auth`) ever runs,
    # so the wrapper silently keeps its module-level default
    # (`_max_history_turns_cache = 6`) and never observes a value
    # change made via `config.yaml` or a workflow's
    # `ProjectPlan.max_history_turns` override. Symptom: per-task
    # `history_turn_count` instrumentation populates, but hermes
    # compaction settings stay frozen at wrapper boot values.
    re.compile(r"^/api/agents/[^/]+/max_history_config/?$"),
    # v3.5.2 follow-up: GET single agent (HMAC-authed, used by
    # the wrapper for self-lookup / config sync).
    re.compile(r"^/api/agents/[^/]+/?$"),
    # v3.5.2 follow-up: agent acks the config it just wrote.
    # Without this, the wrapper's "ack" call returns 401 and the
    # config row stays in `pending` forever.
    re.compile(r"^/api/agents/[^/]+/profiles/[^/]+/configs/[^/]+/ack/?$"),
    # v3.5.2 follow-up: agent claim / liveness / result on
    # /api/tasks/{id}/{start,poll,result}. Without these, tasks
    # assigned to an agent sit in `assigned` forever — the
    # wrapper can never flip them to `running`. This was the
    # primary reason the user's proj-56c8e080 plan was stuck
    # at "Run → all 3 research tasks assigned, none started".
    re.compile(r"^/api/tasks/[^/]+/(?:start|poll|result)/?$"),
    # v3.5.2 follow-up: live output streaming + tool-call events
    # for the dashboard loop_status / output viewer.
    re.compile(r"^/api/projects/[^/]+/tasks/[^/]+/(?:output-chunk|tool-call)/?$"),
    # v3.5.2 follow-up: agent reads/writes project files
    # (output of upstream step → input of downstream step).
    re.compile(r"^/api/projects/[^/]+/files/"),
    # v3.5.2 follow-up: agent session get/set (continuity
    # across tasks in a project).
    re.compile(r"^/api/projects/[^/]+/session/?$"),
    # v3.5.2 follow-up: memory endpoints (L1 trace / L2 facts /
    # L3 state / global recent). Two shapes:
    #   - /api/projects/memory/recent          (global, no project_id)
    #   - /api/projects/{id}/memory/{state,facts,trace}  (per-project)
    re.compile(r"^/api/projects/(?:memory/recent/?$|[^/]+/memory/(?:state|facts|trace)/?$)"),
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan: load config, connect DB, start supervisor on startup; stop on shutdown."""
    # R7-C (2026-08-10): DB-path contract — resolve the config path
    # EXACTLY ONCE and pass the same resolved path to BOTH
    # `load_config()` AND the DB-path derivation. The DB always
    # lives next to the config file (`config_path.parent /
    # "hermes-orch.db"`), so the service connects to the same DB
    # whether it runs interactively (dev) or as LocalSystem
    # (production).
    #
    # Before this, the hardcoded
    # `Path.home() / ".hermes-orchestrator" / "hermes-orch.db"`
    # ignored HERMES_ORCH_CONFIG entirely and opened a wrong DB
    # when the service ran as LocalSystem (whose $HOME is
    # `C:\Windows\System32\config\systemprofile`). That created an
    # empty DB there and returned 401 "Unknown agent" for every
    # heartbeat, because the server was reading from a DB that had
    # no agent rows.
    #
    # Now: production service (NSSM AppEnvironmentExtra sets
    # HERMES_ORCH_CONFIG to
    # `C:\ProgramData\HermesOrchestrator\config\config.yaml`)
    # opens `C:\ProgramData\HermesOrchestrator\config\hermes-orch.db`
    # — the production DB.
    #
    # Fallback: when no config path resolves (env unset, no home
    # config, no local config), fall back to the historical user-
    # profile path so the operator gets a readable error
    # (Database.connect will fail) instead of silently writing to
    # systemprofile. This path is unit-tested and is the ONLY
    # legacy-mode db_path; it is NEVER used in production because
    # the NSSM service always sets HERMES_ORCH_CONFIG.
    #
    # 2026-08-11 review fix: previous version called
    # `find_config_path()` twice (once inside `load_config()`,
    # once for DB derivation). If env / cwd / file existence
    # changed between the two calls, the loaded config and the
    # DB path could disagree. Now we resolve once and pass the
    # same object to both. `load_config()` accepts an optional
    # `config_path` param; if it's None, load_config() falls back
    # to its own find_config_path() call (preserving the
    # auto-resolve behavior for non-lifespan callers).
    config_path = find_config_path()
    cfg = load_config(config_path=config_path)
    app.state.config = cfg

    # Security hotfix 2026-08-11 (B12, R13): validate the canonical
    # public origin BEFORE any further startup work. If invalid,
    # refuse to bind. This is fail-closed: the server cannot start
    # without a correct public_origin (CSRF allowlist source).
    # See auth/origin_validation.py for the contract.
    from hermes_orch.auth.origin_validation import validate_public_origin
    public_origin_cfg = (
        (cfg.get("server") or {}).get("public_origin", "")
    )
    try:
        canonical_origin = validate_public_origin(public_origin_cfg)
    except ValueError as e:
        # Re-raise as a clear startup error. uvicorn will surface the
        # traceback to the operator; the server never binds to a port.
        raise SystemExit(
            f"\n[FATAL] server.public_origin / HERMES_ORCH_PUBLIC_ORIGIN "
            f"is invalid:\n  {e}\n"
            f"Fix this in config.yaml under 'server.public_origin' or "
            f"via the env var HERMES_ORCH_PUBLIC_ORIGIN, then restart.\n"
        ) from e
    app.state.public_origin = canonical_origin
    logger.info(
        "CSRF public_origin configured: %s", canonical_origin
    )

    if config_path is not None:
        db_path = config_path.parent / "hermes-orch.db"
    else:
        # No config found anywhere — fall back to the historical
        # user-profile path so the operator gets a readable error
        # (Database.connect will fail) instead of silently writing
        # to systemprofile.
        db_path = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
    # v3.13.0: enforce minimum SQLite version for production DB.
    # The `root_path` migration uses ADD COLUMN (no IF NOT EXISTS in
    # SQLite, so we rely on the existing try/except in the migration
    # runner). The minimum version is mostly a guard against surprising
    # schema-mismatch errors on truly ancient SQLite builds (< 3.10
    # from 2020). We allow the orchestrator to START on old SQLite so
    # existing test setups aren't disrupted, but log a clear warning
    # that production should be on 3.35+. The migration runner handles
    # "duplicate column" gracefully so the add-column works regardless.
    import sqlite3 as _sqlite3
    _sqlite_version = tuple(int(p) for p in _sqlite3.sqlite_version.split("."))
    _SQLITE_MIN = (3, 10, 0)  # 2020 release; very conservative
    if _sqlite_version < _SQLITE_MIN:
        import logging
        logging.getLogger("hermes_orch").warning(
            "SQLite version %s is below minimum %s. Schema migrations "
            "may fail in unexpected ways. Upgrade SQLite for production.",
            _sqlite3.sqlite_version, ".".join(str(p) for p in _SQLITE_MIN),
        )

    db = Database(db_path)
    await db.connect()
    app.state.db = db
    # v0.7 §1.4 (2026-08-15): in-process nonce store for HMAC replay
    # protection. Attached to app.state so the v0.7 verifier
    # (auth/hmac_v07.py::require_hmac_auth_v07) can read it on every
    # request. Per-uvicorn-worker; production with multiple workers
    # would need Redis (out of scope for v0.7; see impl plan §7).
    from hermes_orch.auth.nonce_store import InMemoryNonceStore
    app.state.v07_nonce_store = InMemoryNonceStore(ttl_seconds=300)
    # v3.12.2 #3: supervisor uses its own aiosqlite connection so its
    # tick-loop writes don't compete with the API on the same
    # in-process connection. The 2026-08-04 incident showed that
    # `busy_timeout=5000` (v3.12.2 #2) only fixes SQLITE_BUSY (across
    # connections); aiosqlite's worker-thread state can still hit
    # SQLITE_LOCKED (in-transaction read lock in the SAME connection)
    # when the supervisor's read+write loop holds the connection
    # while the API tries to UPDATE (e.g. heartbeat). Two separate
    # aiosqlite connections = two separate worker threads = no
    # in-process lock contention. WAL mode + cross-connection
    # busy_timeout still handle the cross-process / cross-thread
    # cases.
    supervisor_db = Database(db_path)
    await supervisor_db.connect()
    app.state.supervisor_db = supervisor_db

    # Shared Jinja2 templates. dashboard.py's page routes already
    # import a module-level instance; expose the same one on
    # app.state so other routers (single_tasks_pages, etc.) can
    # render without re-creating it.
    from hermes_orch.api.dashboard import templates as _jinja_templates
    app.state.templates = _jinja_templates

    # Object Layer foundation (2026-07-26): ensure the virtual
    # __single_tasks__ project row exists so single-task creation
    # never races a lookup. Idempotent — no-op after the first run.
    from hermes_orch.db import ensure_single_tasks_project
    await ensure_single_tasks_project(db)

    # Start the brain: notifier + planner + supervisor (background task)
    notifier = Notifier(cfg)
    planner = Planner(cfg, db=db)  # db needed for token-usage recording
    # v3.12.2 #3: supervisor gets its own aiosqlite connection (see
    # `supervisor_db` block above). Decouples the supervisor's tick-
    # loop writes from the API's request-path writes so SQLITE_LOCKED
    # can't fire when the supervisor is busy and the API tries to
    # commit a heartbeat / project-run.
    supervisor = Supervisor(supervisor_db, cfg, notifier, planner)
    # CleanupJob shared by supervisor (daily sweep) + API (manual run).
    # Create BEFORE supervisor.start() so the supervisor can fire its
    # first daily-sweep check on the next tick.
    cleanup_job = CleanupJob(db, cfg)
    supervisor.set_cleanup_job(cleanup_job)
    # Scheduler (#22): turn any project into a template + attach a
    # cron, and the orchestrator will fire it on schedule. Started
    # AFTER the supervisor so its first tick can rely on the same
    # DB state the supervisor sees.
    scheduler = Scheduler(db, cfg)
    app.state.notifier = notifier
    app.state.planner = planner
    app.state.supervisor = supervisor
    app.state.cleanup = cleanup_job
    app.state.scheduler = scheduler
    supervisor.start()
    scheduler.start()

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
        # Stop in reverse order: scheduler first (it queries the DB
        # the supervisor writes to), then supervisor, then close DBs
        # (v3.12.2 #3: supervisor has its own aiosqlite connection,
        # so we close both).
        await scheduler.stop()
        await supervisor.stop()
        await db.close()
        await supervisor_db.close()
        logger.info("Hermes orchestrator stopped")


def create_app() -> FastAPI:
    """Create FastAPI app instance."""
    app = FastAPI(
        title="Hermes Orchestrator",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Hardening Phase 5 (2026-08-15, security/v07-hardening):
    # Unified error JSON contract per spec §1.12. The custom
    # HTTPException handler parses the "ERROR_CODE: human message"
    # detail string and produces the unified shape:
    #   {"error": "CODE", "message": "...", "request_id": "uuid"}
    # The legacy `detail` field is preserved for backward compat
    # with the pre-Phase-5 bootstrapper and dashboard.
    @app.exception_handler(FastAPIHTTPException)
    async def custom_http_exception_handler(
        request: StarletteRequest, exc: FastAPIHTTPException
    ) -> JSONResponse:
        # Resolve the request_id: use the one from the request_id
        # middleware if attached, else generate a new one. The
        # middleware runs before the route handler, so for any
        # HTTPException raised by a route, the request_id is
        # already on request.state.
        request_id = getattr(request.state, "request_id", None) or new_request_id()
        code, message = parse_error_detail(exc.detail)
        return make_error_response(
            code=code,
            message=message,
            request_id=request_id,
            status_code=exc.status_code,
        )

    # Catch-all for unhandled exceptions: return a 500 with the
    # generic INTERNAL_SERVER_ERROR code. The actual exception
    # is logged but NOT surfaced in the response body (don't leak
    # stack traces to clients). The request_id lets operators
    # correlate the response with server logs.
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: StarletteRequest, exc: Exception
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None) or new_request_id()
        logger.exception(
            "unhandled exception in route %s %s (request_id=%s): %s",
            request.method, request.url.path, request_id, exc,
        )
        return make_error_response(
            code="INTERNAL_SERVER_ERROR",
            message="An internal server error occurred; see server logs",
            request_id=request_id,
            status_code=500,
        )

    # Request ID middleware: generates a UUID4 per request,
    # attaches it to request.state.request_id, and sets the
    # X-Request-Id response header. The exception handler above
    # reads request.state.request_id to include in the unified
    # error body. For 200 responses, the X-Request-Id header is
    # the only client-visible artifact.
    class _RequestIdMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: StarletteRequest, call_next):
            # Honor an existing X-Request-Id header (e.g. from
            # the bootstrapper or a load balancer); else generate
            # a new UUID4. This lets clients correlate across
            # multiple hops if needed.
            request_id = (
                request.headers.get("X-Request-Id") or new_request_id()
            )
            request.state.request_id = request_id
            response = await call_next(request)
            response.headers["X-Request-Id"] = request_id
            return response

    app.add_middleware(_RequestIdMiddleware)

    # Static files (Phase 1 of visual workflow builder, 2026-07-24).
    # The visual builder's JS lives at src/hermes_orch/static/.
    # Templates reference /static/visual_workflow.js.
    from fastapi.staticfiles import StaticFiles
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Cache-Control: no-store for the agents router. The dashboard's
    # 10s polling does location.reload() which can keep serving stale
    # page-cached JSON responses (e.g. after we fixed the skill layout
    # bug, users kept seeing the old /SKILL/SKILL.md rendering because
    # their browser had cached the previous API response). Setting
    # no-store on the skills endpoints forces a fresh fetch every time,
    # so the dashboard always shows the current server state.
    # (BaseHTTPMiddleware + StarletteRequest are imported at the
    # top of the module — no function-scope re-import here, to keep
    # Python's name resolution consistent for any middleware class
    # defined before this one in the function body.)
    class _NoStoreOnSkills(BaseHTTPMiddleware):
        async def dispatch(self, request: StarletteRequest, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/api/agents/") and "/skills" in request.url.path:
                response.headers["Cache-Control"] = "no-store"
            return response

    app.add_middleware(_NoStoreOnSkills)

    # v3.4: dashboard user auth gate. Two layers of auth co-exist:
    #   - HMAC for agent → orchestrator (per-route Depends)
    #   - Cookie session for human → dashboard (this middleware)
    # Allowlist contains paths that don't need a user session. Everything
    # else requires a valid `hermes_orch_session` cookie.
    from hermes_orch.auth.cookie import COOKIE_NAME as _COOKIE_NAME
    from hermes_orch.auth.cookie import current_user_id as _current_user_id

    # HMAC-gated agent paths — wrapper signs them, no user session needed.
    # The endpoints themselves still go through require_hmac_auth Depends;
    # this allowlist just keeps the user-cookie middleware out of the way.
    #
    # IMPORTANT: this list must stay in sync with every endpoint that uses
    # `Depends(require_hmac_auth)`. A path missing here is a silent bug —
    # the wrapper signs correctly but the user-cookie middleware returns
    # 401 "Not authenticated" BEFORE the route handler ever runs, so the
    # agent sees a generic 401 with no audit trail. v3.5.2 (2026-07-31)
    # bit us on /api/tasks/{id}/start (and the other 2 task endpoints +
    # 2 project-task endpoints + memory/files/session endpoints) — all
    # HMAC-authed but not in this list. Tasks sat in `assigned` state
    # forever because the agent couldn't claim them. See
    # tests/test_hmac_middleware_allowlist.py for the regression test
    # that keeps this list honest.
    # NOTE: the actual pattern list is defined at module scope above
    # (_HMAC_PATH_PATTERNS) so the systematic reflection test can
    # import it. We just reference the module-level list here.
    _ALLOWLIST_PREFIXES = (
        "/static/",
        "/login",
        "/setup-password",
        "/approval/",  # magic-link approval (future)
        "/api/auth/",
        "/api/health",
        "/docs",  # FastAPI auto-generated
        "/openapi.json",
        "/redoc",
    )

    class _RequireUserMiddleware(BaseHTTPMiddleware):
        """Gate dashboard routes + most /api/* behind a user session cookie.

        - Allowed prefixes → pass through (login, static, auth endpoints, health)
        - HMAC path patterns → pass through (gated by require_hmac_auth inside)
        - Otherwise → require user. If no valid session:
            * HTML page request → 302 redirect to /login?next=...
            * API request → 401 JSON
        """

        async def dispatch(self, request: StarletteRequest, call_next):
            path = request.url.path

            # Always-allow: static, login, auth endpoints, health, docs
            for prefix in _ALLOWLIST_PREFIXES:
                if path == prefix.rstrip("/") or path.startswith(prefix):
                    return await call_next(request)

            # HMAC-gated agent paths (wrapper, not user)
            for pat in _HMAC_PATH_PATTERNS:
                if pat.match(path):
                    return await call_next(request)

            # Everything else requires a user session
            user_id = await _current_user_id(request)
            if not user_id:
                # Distinguish page requests (redirect) from API (JSON 401)
                accept = request.headers.get("accept", "")
                if path.startswith("/api/") or "application/json" in accept:
                    return JSONResponse(
                        {"detail": "Not authenticated"},
                        status_code=401,
                    )
                # Page request → redirect to /login with next= param
                next_url = path
                if request.url.query:
                    next_url = path + "?" + request.url.query
                return RedirectResponse(
                    url=f"/login?next={next_url}", status_code=302
                )

            # Attach user_id for downstream handlers
            request.state.user_id = user_id
            return await call_next(request)

    app.add_middleware(_RequireUserMiddleware)

    # Health check
    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "hermes-orchestrator"}

    # Mount routers (will be filled in as we build)
    from hermes_orch.api.agents import router as agents_router
    from hermes_orch.api.artifacts import router as artifacts_router
    from hermes_orch.api.auth import router as auth_router, page_router as auth_page_router
    from hermes_orch.api.contracts import router as contracts_router
    from hermes_orch.api.dashboard import router as dashboard_router
    from hermes_orch.api.objects import router as objects_router
    from hermes_orch.api.optimize import router as optimize_router
    from hermes_orch.api.plans import router as plans_router
    from hermes_orch.api.projects import router as projects_router
    from hermes_orch.api.schedules import router as schedules_router
    from hermes_orch.api.settings import router as settings_router
    from hermes_orch.api.single_tasks import router as single_tasks_router
    from hermes_orch.api.single_tasks_pages import router as single_tasks_pages_router
    from hermes_orch.api.soul_templates import router as soul_templates_router
    from hermes_orch.api.tasks import router as tasks_router
    from hermes_orch.api.ui_prefs import router as ui_prefs_router
    from hermes_orch.api.users import router as users_router
    from hermes_orch.api.workflows import router as workflows_router

    app.include_router(agents_router, prefix="/api/agents", tags=["agents"])
    app.include_router(artifacts_router, prefix="/api/artifacts", tags=["artifacts"])
    app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
    # v3.4: /login, /setup-password, /logout (HTML pages, no prefix)
    app.include_router(auth_page_router, tags=["auth-pages"])
    app.include_router(contracts_router, prefix="/api/contracts", tags=["contracts"])
    app.include_router(objects_router, prefix="/api/objects", tags=["objects"])
    app.include_router(optimize_router, prefix="/api/contracts/optimize-tasks", tags=["optimize"])
    app.include_router(plans_router, prefix="/api", tags=["plans"])
    app.include_router(single_tasks_router, prefix="/api/single-tasks", tags=["single-tasks"])
    app.include_router(projects_router, prefix="/api/projects", tags=["projects"])
    app.include_router(schedules_router, prefix="/api/schedules", tags=["schedules"])
    app.include_router(settings_router, prefix="/api/settings", tags=["settings"])
    # v1.0.1 (new-user-activation): server lifecycle endpoints
    # (POST /api/server/restart with process-mode-aware handling).
    from hermes_orch.api.server import router as server_router
    app.include_router(server_router, prefix="/api/server", tags=["server"])
    app.include_router(tasks_router, prefix="/api/tasks", tags=["tasks"])
    # v1.0.1 (new-user-activation): onboarding state endpoints
    # (GET /api/me/onboarding, skip, admin reset). Used by the
    # landing page to decide between onboarding.html and /agents.
    from hermes_orch.api.onboarding import router as onboarding_router
    app.include_router(onboarding_router, prefix="/api", tags=["onboarding"])
    # v1.0.1 §3.3: enrollment token endpoints
    # (POST /api/enrollment-tokens, GET, DELETE, POST /api/agents/enroll)
    from hermes_orch.api.enrollment import router as enrollment_router
    app.include_router(enrollment_router, prefix="/api", tags=["enrollment"])
    # v1.0.1 §3.4: starter catalog endpoints
    # (GET /api/starters, GET /api/starters/{name}, POST clone)
    from hermes_orch.api.starters import router as starters_router
    app.include_router(starters_router, prefix="/api", tags=["starters"])
    # Load the bundled starter catalog into app.state.starters
    # (in-process, no DB writes — read-only YAMLs).
    from hermes_orch.core.starters import load_catalog
    app.state.starters = load_catalog()
    # v3.5.0: admin user CRUD (list, create, reset password, disable/enable)
    app.include_router(users_router, prefix="/api/users", tags=["users"])
    # v3.9.0 (Phase 2 UX): per-user UI prefs + plan-presets lookup.
    # Mounted WITHOUT a prefix because the routes already include the
    # full /api/... path (`/api/users/me/ui-prefs` and
    # `/api/projects/{id}/plan/presets`). Adding a prefix would
    # double-mount them.
    app.include_router(ui_prefs_router, tags=["ui-prefs"])
    # v3.9.0 (Phase 3): SOUL template library — admin CRUD on
    # reusable personas + the from-template instantiator. Mounted
    # WITHOUT a prefix because the routes already include the
    # full /api/... path (`/api/soul-templates/...` and the
    # project-scoped `/api/projects/{id}/soul-presets/from-template/{name}`).
    app.include_router(soul_templates_router, tags=["soul-templates"])
    app.include_router(workflows_router, tags=["workflows"])
    # v3.14.0 (Phase 2): human approval endpoints. The routes already
    # include their full /api/... paths (`/api/workflows/{id}/steps/...`,
    # `/api/inbox/approvals`), so we mount WITHOUT a prefix.
    from hermes_orch.api.approvals import router as approvals_router
    app.include_router(approvals_router, tags=["approvals"])
    app.include_router(dashboard_router, tags=["dashboard"])
    app.include_router(single_tasks_pages_router, tags=["pages"])

    return app


app = create_app()
