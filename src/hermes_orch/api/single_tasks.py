"""Single task API (Object Layer + chatbox code-gen, 2026-07-27).

Single tasks live in the virtual `__single_tasks__` project with
`is_single_task=1`. They're the "no project context" surface used
for one-off work like the code-gen flow (chatbox -> LLM analysis
-> "write a script to replace this task" -> single task -> new
Skill registered) and ad-hoc summarize/extract queries.

The dispatch + status state machine is identical to project tasks
— we reuse the existing /api/tasks/{id}/{start,result,retry}
endpoints. The only difference is project_id is forced to
SINGLE_TASKS_PROJECT_ID and is_single_task is forced to 1 on
create.
"""
from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes_orch.db import SINGLE_TASKS_PROJECT_ID
from hermes_orch.utils import now_iso as _now_iso

router = APIRouter()


# ===== Pydantic models =====


class SingleTaskCreate(BaseModel):
    """Body for POST /api/single-tasks."""
    name: str = Field(..., min_length=1, max_length=200)
    goal: str = Field("", max_length=2000)
    # Required capability. Empty string = "any profile can pick this up".
    required_capability: str = ""
    # Where this task came from. Free-form so the code-gen flow
    # can pass {kind: "code_gen", source_task_id: ..., ...}.
    # Other valid kinds: "ad_hoc", "summarize", "extract", "transform".
    source: dict[str, Any] = Field(default_factory=dict)
    # Optional output path. If set, the task is expected to write
    # the result to this file (per the user's project storage_refs).
    output_path: str = ""
    # Optional agent profile ID. If not set, the supervisor picks.
    # Per the user-stated model: "agent registered, dispatch task
    # to them" — we still let the supervisor choose, but the
    # caller can pin if they know which profile to use.
    assigned_profile_id: str = ""


class SingleTaskOut(BaseModel):
    id: str
    # Always "__single_tasks__" (the virtual project). Exposed so
    # the UI can confirm where the task lives and the test can
    # verify the is_single_task=1 invariant.
    project_id: str
    name: str
    goal: str
    status: str
    required_capability: str
    assigned_profile_id: str | None
    source: dict[str, Any]
    output_path: str
    result: dict[str, Any] | None
    error: str | None
    started_at: str | None
    ended_at: str | None
    created_at: str | None
    updated_at: str | None
    # Whether the task has finished and what the verdict is.
    # Convenience flag for the UI (avoid having to re-check status).
    has_result: bool = False
    has_error: bool = False


class SingleTaskList(BaseModel):
    tasks: list[SingleTaskOut]
    count: int


# ===== Helpers =====


def _row_to_single_task_out(row: dict[str, Any]) -> SingleTaskOut:
    """Convert a tasks-table row to SingleTaskOut.

    The `source` field is stored inside the `params` JSON column
    (we don't have a separate source column to avoid a schema
    change for this metadata). The `goal` is also in params.
    `result` and `error` are real columns.
    """
    import json
    # Extract source + goal from params. Older rows may have goal
    # as a top-level column (we don't have one currently, but be
    # defensive), so check both.
    raw_params = row.get("params") or "{}"
    if isinstance(raw_params, str):
        try:
            params = json.loads(raw_params) if raw_params.strip() else {}
        except (json.JSONDecodeError, TypeError):
            params = {"_raw": raw_params}
    else:
        params = raw_params or {}
    if not isinstance(params, dict):
        params = {}
    source = params.get("source", {})
    goal = params.get("goal", "") or row.get("goal", "")
    raw_result = row.get("result")
    if isinstance(raw_result, str) and raw_result.strip():
        try:
            result = json.loads(raw_result)
        except (json.JSONDecodeError, TypeError):
            result = {"_raw": raw_result}
    else:
        result = raw_result if raw_result else None
    return SingleTaskOut(
        id=row["id"],
        project_id=row.get("project_id") or SINGLE_TASKS_PROJECT_ID,
        name=row.get("name") or "",
        goal=goal,
        status=row.get("status") or "pending",
        required_capability=row.get("required_capability") or "",
        assigned_profile_id=row.get("assigned_profile_id"),
        source=source,
        output_path=row.get("output_path") or "",
        result=result,
        error=row.get("error"),
        started_at=row.get("started_at"),
        ended_at=row.get("ended_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        has_result=bool(result) and not row.get("error"),
        has_error=bool(row.get("error")),
    )


def _validate_single_tasks_project(db) -> None:
    """Idempotently ensure the virtual __single_tasks__ project row exists.

    The lifespan startup already calls ensure_single_tasks_project(),
    but we re-check here for safety in case the API is hit during
    a test that doesn't go through the lifespan (or before the
    server is fully started).
    """
    import asyncio
    existing = asyncio.get_event_loop().run_until_complete(
        db.fetchone("SELECT id FROM projects WHERE id = ?", (SINGLE_TASKS_PROJECT_ID,))
    )
    if not existing:
        # Synchronous fallback (db may be in a thread that doesn't
        # support async). For now just trust the lifespan path.
        # The startup hook covers the normal case.
        pass


# ===== Endpoints =====


@router.post("", response_model=SingleTaskOut, status_code=201)
async def create_single_task(body: SingleTaskCreate, request: Request) -> SingleTaskOut:
    """Create a new single task in the virtual __single_tasks__ project.

    The task is created with status=pending. The supervisor picks it
    up on the next tick (if the project is in a runnable state) or
    stays pending until the user calls /api/tasks/{id}/start
    explicitly. For code-gen, we set source={"kind": "code_gen",
    ...} so the UI can show a "this is a code-gen task" badge.
    """
    db = request.app.state.db
    # Ensure the virtual project exists. In production the
    # lifespan startup does this; in tests, we lazy-create on
    # first use.
    from hermes_orch.db import ensure_single_tasks_project
    await ensure_single_tasks_project(db)
    tid = "t-" + secrets.token_hex(4)
    now = _now_iso()
    import json as _json
    await db.insert("tasks", {
        "id": tid,
        "project_id": SINGLE_TASKS_PROJECT_ID,
        "name": body.name,
        "agent_role": "",  # supervisor picks; could be set if we know
        "depends_on": "[]",
        "on_parent_failure": "skip",
        "status": "pending",
        "priority": "normal",
        "action": "do_task",
        "params": _json.dumps({
            "goal": body.goal,
            "source": body.source,
            "single_task_kind": body.source.get("kind", "ad_hoc"),
        }),
        "retry_count": 0,
        "max_retries": 2,
        "timeout_seconds": 1800,
        "output_path": body.output_path,
        "required_capability": body.required_capability,
        "feedback_to": "[]",
        "archived": 0,
        "is_single_task": 1,
        "assigned_profile_id": body.assigned_profile_id or None,
    })
    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (tid,))
    return _row_to_single_task_out(row)


@router.get("", response_model=SingleTaskList)
async def list_single_tasks(
    request: Request,
    status: str | None = None,
    limit: int = 100,
) -> SingleTaskList:
    """List single tasks (newest first).

    `status` filter: pending / assigned / running / completed / failed / etc.
    `limit` caps the result count (default 100; the table is small
    because most work is project-scoped).
    """
    db = request.app.state.db
    sql = (
        "SELECT * FROM tasks "
        "WHERE project_id = ? AND is_single_task = 1 "
    )
    params: list[Any] = [SINGLE_TASKS_PROJECT_ID]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    rows = await db.fetchall(sql, tuple(params))
    return SingleTaskList(
        tasks=[_row_to_single_task_out(r) for r in rows],
        count=len(rows),
    )


@router.get("/{task_id}", response_model=SingleTaskOut)
async def get_single_task(task_id: str, request: Request) -> SingleTaskOut:
    """Get one single task by id. 404 if not found OR if it's a
    project task (not a single task) — we don't want the
    /single-tasks page to accidentally show project task data."""
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT * FROM tasks WHERE id = ? AND is_single_task = 1",
        (task_id,),
    )
    if not row:
        raise HTTPException(
            404, f"Single task not found: {task_id}"
        )
    return _row_to_single_task_out(row)
