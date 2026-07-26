"""Single task HTML pages (Commit 3 of Object Layer, 2026-07-27).

Renders the /single-tasks list page + detail page. The data
fetching is via the API endpoints in api/single_tasks.py; this
module is HTML-only.

Why a separate module (vs adding to dashboard.py)?
  - dashboard.py is already 1500+ lines and growing
  - api/single_tasks.py is the API; pages are a thin presentation
    layer that could be regenerated / replaced without touching
    the API
  - Easier to add per-page tests (template renders, links) without
    loading all of dashboard.py's imports
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from hermes_orch.db import SINGLE_TASKS_PROJECT_ID

router = APIRouter()


@router.get("/single-tasks", response_class=HTMLResponse)
async def single_tasks_list_page(request: Request) -> HTMLResponse:
    """List all single tasks (newest first). Shows status, kind,
    goal, result/error. The "+ Create single task" button opens an
    inline form (no separate page needed for the simple case)."""
    from hermes_orch.api.single_tasks import _row_to_single_task_out
    db = request.app.state.db
    rows = await db.fetchall(
        "SELECT * FROM tasks WHERE project_id = ? AND is_single_task = 1 "
        "ORDER BY created_at DESC LIMIT 200",
        (SINGLE_TASKS_PROJECT_ID,),
    )
    tasks = [_row_to_single_task_out(r) for r in rows]
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "single_tasks.html",
        {
            "tasks": tasks,
            "active_page": "single-tasks",
            **_base_context(request, "single-tasks"),
        },
    )


@router.get("/single-tasks/{task_id}", response_class=HTMLResponse)
async def single_task_detail_page(task_id: str, request: Request) -> HTMLResponse:
    """Single task detail page (goal, status, timing, result, error)."""
    from hermes_orch.api.single_tasks import _row_to_single_task_out
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT * FROM tasks WHERE id = ? AND is_single_task = 1",
        (task_id,),
    )
    if not row:
        raise HTTPException(404, f"Single task not found: {task_id}")
    task = _row_to_single_task_out(row)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "single_task_detail.html",
        {
            "task": task,
            "active_page": "single-tasks",
            **_base_context(request, "single-tasks"),
        },
    )


def _base_context(request: Request, active_page: str) -> dict:
    """Standard context dict for every page (LLM status + theme)."""
    llm_cfg = request.app.state.config.get("llm", {}) or {}
    llm_configured = bool(llm_cfg.get("api_key", "").strip())
    return {
        "llm_configured": llm_configured,
        "active_page": active_page,
    }
