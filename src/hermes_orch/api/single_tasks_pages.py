# coding: utf-8
"""Single task HTML pages (Commit 3 of Object Layer, 2026-07-27).

Renders the /single-tasks/{id} DETAIL page only — the LIST page
has been merged into /tasks (per the unified-tasks decision, 2026-07-27).

The /single-tasks URL now redirects to /tasks (preserves any
existing bookmarks or external links). The detail page stays
because external systems may have linked to a specific single task
URL; the page itself is unchanged from the original commit 3.

The data fetching for the detail page is via the API endpoints in
api/single_tasks.py; this module is HTML-only.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hermes_orch.db import SINGLE_TASKS_PROJECT_ID

router = APIRouter()


@router.get("/single-tasks", response_class=HTMLResponse)
async def single_tasks_list_page(request: Request) -> RedirectResponse:
    """Redirect to the unified /tasks page (the list view lives there now).

    The /tasks page shows single tasks mixed in with project tasks,
    with a "Single" badge in the Type column. This redirect keeps
    the /single-tasks URL alive for any bookmarked links.
    """
    return RedirectResponse(url="/tasks", status_code=307)


@router.get("/single-tasks/{task_id}", response_class=HTMLResponse)
async def single_task_detail_page(task_id: str, request: Request) -> HTMLResponse:
    """Single task detail page (goal, status, timing, result, error).

    Kept as a separate URL even after the merge — external systems
    (chatbox code-gen, audit log links) may have referenced the
    detail URL. The page itself is unchanged from the original
    commit 3.
    """
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
            "active_page": "tasks",
            **_base_context(request, "tasks"),
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
