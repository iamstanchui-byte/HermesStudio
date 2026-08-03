# coding: utf-8
"""Task endpoints (per REVIEW.md §1, §4).

Task lifecycle:
    pending → assigned → running → completed | failed | failed_timeout
                            → failed_dispatch (3 attempts all timed out)
                            → cancelled (operator)
                            → interrupted (operator "interrupt now")

State transitions handled by:
- POST /api/tasks              — create (status: pending)
- POST /api/tasks/{id}/assign  — dispatcher assigns to agent (pending → assigned)
- POST /api/tasks/{id}/start   — agent acks (assigned → running)
- GET  /api/tasks/{id}/poll    — agent liveness check (running → running)
- POST /api/tasks/{id}/poll    — same; POST is the canonical path the wrapper uses
- POST /api/tasks/{id}/result  — agent submits result (running → completed/failed)
- POST /api/tasks/{id}/cancel  — operator (any → cancelled)
- POST /api/tasks/{id}/interrupt — operator (running → interrupted)
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from hermes_orch.core.sse import publish_event
from hermes_orch.auth import require_hmac_auth
from pydantic import BaseModel, Field


# Pollution markers (LLM-fooling pattern #10, 2026-07-25)
# LLM agents sometimes include parts of the prompt (context blocks,
# user recent, workflow procedure, etc.) in their response. The
# wrapper's _strip_prompt_echo catches some but not all. We do a
# final strip on the server side so the result.summary is just
# the LLM's actual work output.
#
# The markers are the 4 horizontal-rule "header" lines that the
# wrapper / project memory injects into the agent's prompt:
#   --- PROJECT CONTEXT ---
#   --- USER RECENT (L3: recent.md, last 7 days) ---
#   --- PROJECT STATE (L3: state.md) ---
#   --- WORKFLOW PROCEDURE ---
#   --- OUTPUT FORMAT ---
#   --- STORAGE ---
#   --- SKILL: <name> ---
# Plus the wrapper's own transcript markers:
#   [...] (single line, often meaning "stripped content")
#   […session metadata stripped…]
#   […transcript continued…]
_POLLUTION_MARKER_PATTERN = re.compile(
    r"^---\s*(?:"
    r"PROJECT\s+CONTEXT|"
    r"USER\s+RECENT[^|]*\|[^|]*\|[^|]*\|"
    r"PROJECT\s+STATE[^|]*\|[^|]*\|"
    r"WORKFLOW\s+PROCEDURE|"
    r"OUTPUT\s+FORMAT|"
    r"STORAGE|"
    r"SKILL:\s*[^\s-][^-]*"
    r")\s*---"
    r"|^[\u2026\.\[\(].*?(stripped|continued|metadata|content omitted|truncated).*?[\u2026\.\]\)]"
    r"|^\s*\[\u2026\]\s*$",
    re.MULTILINE,
)


def _strip_pollution_markers(text: str) -> str:
    """Strip known pollution markers from agent result text.

    LLM agents sometimes include parts of the prompt context
    (project context, user recent, etc.) in their response.
    The wrapper does some stripping via _strip_prompt_echo but
    not all cases are caught. This server-side pass is a
    defense-in-depth: if the line is just a pollution header
    or stripped-content marker (no actual content), drop it.

    Strips:
      - Header lines: '--- PROJECT CONTEXT ---' etc.
      - Wrapper markers: '[…session metadata stripped…]', etc.
      - Single line '[…]' placeholder (truncation marker)

    Does NOT strip:
      - Body content of any block (the LLM might have written
        real text between markers — we keep it)
      - Lines that look like headers but contain content after
        the '---' on the same line (defensive: leave them)
    """
    if not text:
        return text
    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        # Drop single-line pollution markers
        if not stripped:
            out_lines.append(line)
            continue
        # Drop wrapper truncation placeholders
        if stripped in ("[…]", "[…]", "[...]", "[truncated]"):
            continue
        if re.match(r"^\[[\.\u2026].*?(stripped|continued|metadata|truncated|omitted)", stripped):
            continue
        # Drop section header lines (the '--- XXX ---' style)
        # Must be: '--- ' + header content + ' ---?' (end).
        # Allow letters (any case), spaces, parens, dots, colons,
        # digits, underscores, hyphens, slashes, pipes, commas.
        # The key is that the line is JUST a header (no body
        # content). Examples that should match:
        #   --- PROJECT CONTEXT ---
        #   --- USER RECENT (L3: recent.md, last 7 days) ---
        #   --- PROJECT STATE (L3: state.md) ---
        #   --- WORKFLOW PROCEDURE ---
        #   --- OUTPUT FORMAT ---
        #   --- STORAGE ---
        #   --- SKILL: bus ---
        if re.match(
            r"^---\s+[A-Za-z][A-Za-z0-9\s\(\)\.:_/\-|,]+\s+---?\s*$",
            stripped,
        ):
            continue
        out_lines.append(line)
    return "\n".join(out_lines).strip()

from hermes_orch.core.audit import audit_log
from hermes_orch.utils import now_iso as _now_iso

router = APIRouter()


# ===== Pydantic models =====


class TaskCreate(BaseModel):
    project_id: str
    name: str | None = None
    agent_role: str  # Required: which role this task needs
    depends_on: list[str] = Field(default_factory=list)
    on_parent_failure: str = "skip"  # skip | wait | fail
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    priority: str = "normal"  # normal | high
    max_retries: int = 2
    timeout_seconds: int = 1800
    output_path: str | None = None  # NEW: relative path under project folder (e.g. "raw/weather.json")
    # Phase 4 (smart dispatch): if set, the supervisor will only assign
    # this task to a profile whose `capabilities[required_capability] = true`.
    # If no profile with that role has the capability, the task is marked
    # failed with reason "dispatch.mismatch: profile <X> lacks capability <Y>"
    # and a `dispatch.mismatch` audit event is written. This is the fix
    # for the "Linux super picked Yahoo data instead of MT5" case.
    required_capability: str | None = None


class Task(BaseModel):
    id: str
    project_id: str
    name: str | None = None
    agent_role: str
    assigned_agent_id: str | None = None
    assigned_profile_id: str | None = None
    status: str
    depends_on: list[str]
    on_parent_failure: str
    priority: str
    action: str
    params: dict[str, Any]
    retry_count: int
    max_retries: int
    timeout_seconds: int
    output_path: str | None = None
    last_liveness_at: str | None = None
    # Real start/end timestamps. started_at is set on /start
    # (server-side, when the wrapper claims the task and status
    # flips to 'running'). ended_at is set on /result, /cancel, or
    # /interrupt (any terminal transition). Both are authoritative —
    # the dashboard no longer hacks duration from
    # `updated_at - last_liveness_at`.
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    required_capability: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # Path A (#22): per-task denormalized procedure.md. The supervisor
    # copies the project's procedure.md into this column at assignment
    # time; the wrapper reads it and prepends to the agent's prompt.
    procedure_md: str | None = None


class TaskResult(BaseModel):
    status: str  # "completed" | "failed"
    session_id: str | None = None
    summary: str | None = None
    error: str | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    # Files the agent created in cache_dir that exceeded the 15MB per-file
    # cap and were SKIPPED from upload. Stored so the dashboard can show
    # "N files too large — use share folder at <path>". Each entry has
    # `{path, size_bytes, reason}`.
    skipped_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    # Wrapper-reported per-task token usage. Read from the profile's
    # hermes state.db (sessions table) by the wrapper after each hermes
    # subprocess completes. Mapped into the orchestrator's token_usage
    # table with call_kind='agent_task'. Optional for backward
    # compatibility — old wrappers that don't report tokens still work.
    token_usage: dict[str, Any] | None = None
    # Skills the agent actually loaded during the task. Wrapper parses
    # the hermes transcript (line "┊ 📚 skill     <name>  <duration>") and
    # reports the unique list. Used by promote-to-workflow so the
    # synthesized step preserves every skill the source used
    # (Stage 1.5 multi-skill awareness, 2026-07-23).
    skills_used: list[str] | None = None
    # v3.12.1 follow-up #6: hermes session turn count (number of
    # messages in the session) at task completion. The wrapper
    # reads this from `state.db` (sessions.message_count) after
    # the hermes subprocess exits. Stored in
    # `task_dispatch.history_turn_count` so the dashboard /
    # operator can verify the conversation-history growth fix
    # is working (e.g. 4x growth flattens to 1.3x with hermes
    # 0.19.1's micro-compaction enabled).
    history_turn_count: int | None = None


class TaskAssign(BaseModel):
    agent_id: str
    profile_id: str | None = None


# ===== Helpers =====
# _now_iso is now imported from hermes_orch.utils (consolidated).


def _task_id() -> str:
    return "t-" + secrets.token_hex(4)


def _row_to_task(row: dict[str, Any]) -> Task:
    """Parse DB row → Task model, including JSON columns."""
    import json

    for col in ("depends_on", "params", "result"):
        v = row.get(col)
        if isinstance(v, str):
            try:
                row[col] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                row[col] = {} if col != "depends_on" else []
    return Task(**row)


# ===== Endpoints =====


@router.post("/", response_model=Task, status_code=201)
async def create_task(body: TaskCreate, request: Request) -> Task:
    """Create a new task. Status: pending. Dispatcher will pick it up.

    If the project is in a terminal state (completed/paused), this also
    wakes it up back to 'ready' so the supervisor will process the new
    task. Without this, the supervisor would ignore new tasks on a
    completed project.
    """
    db = request.app.state.db

    # Validate project exists
    proj = await db.fetchone("SELECT id, state FROM projects WHERE id = ?", (body.project_id,))
    if not proj:
        raise HTTPException(404, f"Project not found: {body.project_id}")

    # Validate parent tasks (if any)
    if body.depends_on:
        for parent_id in body.depends_on:
            parent = await db.fetchone("SELECT id FROM tasks WHERE id = ?", (parent_id,))
            if not parent:
                raise HTTPException(400, f"Parent task not found: {parent_id}")

    # Wake up a terminal project so the supervisor will pick up the new task
    now = _now_iso()
    if proj["state"] in ("completed", "paused", "cancelled"):
        await db.execute(
            "UPDATE projects SET state = 'ready', updated_at = ? WHERE id = ?",
            (now, body.project_id),
        )
        await audit_log(
            db, "project.woken",
            actor="operator",
            project_id=body.project_id,
            payload={"previous_state": proj["state"], "trigger": "task.created"},
        )

    task_id = _task_id()
    await db.insert(
        "tasks",
        {
            "id": task_id,
            "project_id": body.project_id,
            "name": body.name,
            "agent_role": body.agent_role,
            "depends_on": body.depends_on,
            "on_parent_failure": body.on_parent_failure,
            "status": "pending",
            "priority": body.priority,
            "action": body.action,
            "params": body.params,
            "max_retries": body.max_retries,
            "timeout_seconds": body.timeout_seconds,
            "output_path": body.output_path,
            "required_capability": body.required_capability,
        },
    )

    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    await audit_log(
        db, "task.created",
        actor="operator",
        project_id=body.project_id,
        task_id=task_id,
        payload={"agent_role": body.agent_role, "action": body.action, "name": body.name},
    )
    return _row_to_task(row)


@router.get("/")
async def list_tasks(
    request: Request,
    project_id: str | None = None,
    status: str | None = None,
    agent_id: str | None = None,
    role: str | None = None,
    include_archived: bool = False,
    exclude_archived_tasks: bool = False,
) -> dict:
    """List tasks (filterable).

    Two independent "archived" filters — easy to confuse, so be
    explicit about which is which:

      `include_archived` (project-level): when False (default), tasks
        whose PROJECT is in 'archived'/'deleted' state are hidden
        (joined filter on projects table). Set True to see them (e.g.
        the Tasks page's "show archived projects" toggle).

      `exclude_archived_tasks` (task-level): when True, tasks whose
        individual `archived=1` column is set are hidden. The
        `tasks.archived` flag is set by clone-and-cascade (and
        apply-workflow's archived step, if any) to hide old tasks
        after the operator creates a fresh replacement chain. The
        default view of a project (text + visual) excludes these.
        The visual view's background poller relies on this filter —
        without it, archived "before" tasks re-appear in the canvas
        after the first poll tick.

    Both can be combined: e.g. `?include_archived=true&exclude_archived_tasks=true`
    shows tasks for archived projects but only the active ones.
    """
    db = request.app.state.db
    where = []
    params: list[Any] = []
    if project_id:
        where.append("t.project_id = ?")
        params.append(project_id)
    if status:
        where.append("t.status = ?")
        params.append(status)
    if agent_id:
        where.append("t.assigned_agent_id = ?")
        params.append(agent_id)
    if role:
        where.append("t.agent_role = ?")
        params.append(role)
    # JOIN projects so we can filter by project state. Only when we
    # need to (avoid the JOIN cost when not filtering by project).
    join_projects = not include_archived
    if join_projects:
        where.append("p.state NOT IN ('archived', 'deleted')")
    # Task-level archive filter (independent of project-level above).
    if exclude_archived_tasks:
        where.append("t.archived = 0")
    sql = "SELECT t.* FROM tasks t"
    if join_projects:
        sql += " JOIN projects p ON t.project_id = p.id"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.created_at DESC"
    rows = await db.fetchall(sql, tuple(params))
    return {"tasks": [_row_to_task(r).model_dump() for r in rows]}


@router.get("/{task_id}", response_model=Task)
async def get_task(task_id: str, request: Request) -> Task:
    """Get task details."""
    db = request.app.state.db
    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not row:
        raise HTTPException(404, f"Task not found: {task_id}")
    return _row_to_task(row)


@router.post("/{task_id}/assign", response_model=Task)
async def assign_task(task_id: str, body: TaskAssign, request: Request) -> Task:
    """Dispatcher assigns task to an agent (pending → assigned).

    Validates:
    - Task is in 'pending' status (checked BEFORE update)
    - Agent has a profile matching the task's required role
    """
    db = request.app.state.db

    # Fetch + check current state FIRST
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task["status"] != "pending":
        raise HTTPException(400, f"Task not in pending state: {task['status']}")

    # Verify agent + profile
    agent = await db.fetchone("SELECT * FROM agents WHERE id = ?", (body.agent_id,))
    if not agent:
        raise HTTPException(404, f"Agent not found: {body.agent_id}")
    if body.profile_id:
        profile = await db.fetchone(
            "SELECT * FROM agent_profiles WHERE id = ? AND agent_id = ?",
            (body.profile_id, body.agent_id),
        )
        if not profile:
            raise HTTPException(404, f"Profile not found: {body.profile_id}")
        if profile["name"] != task["agent_role"]:
            raise HTTPException(
                400,
                f"Profile role mismatch: profile={profile['name']} task_role={task['agent_role']}",
            )

    await db.execute(
        "UPDATE tasks SET assigned_agent_id = ?, assigned_profile_id = ?, "
        "status = 'assigned', updated_at = ? WHERE id = ?",
        (body.agent_id, body.profile_id, _now_iso(), task_id),
    )
    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    await audit_log(
        db, "task.assigned",
        actor="dispatcher",
        project_id=task["project_id"],
        task_id=task_id,
        agent_id=body.agent_id,
        payload={"profile_id": body.profile_id},
    )
    return _row_to_task(row)


@router.post("/{task_id}/start", response_model=Task)
async def start_task(
    task_id: str,
    request: Request,
    agent_id: str = Depends(require_hmac_auth),
) -> Task:
    """Agent acks the task and starts running (assigned → running).

    v1.6: HMAC-authed. The X-Agent-Id (verified via require_hmac_auth)
    must match task.assigned_agent_id.
    """
    db = request.app.state.db
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task["status"] != "assigned":
        raise HTTPException(400, f"Task not in assigned state: {task['status']}")
    if task.get("assigned_agent_id") != agent_id:
        raise HTTPException(
            403,
            f"X-Agent-Id ({agent_id}) is not the owner of task {task_id}",
        )

    now = _now_iso()
    # started_at is set the first time the task flips to 'running' and
    # preserved across retries. A retry from 'running' → re-assigned →
    # 'running' would otherwise reset started_at, but in practice retries
    # are handled by the supervisor (which creates a fresh task row), so
    # this column is set once and never overwritten.
    await db.execute(
        "UPDATE tasks SET status = 'running', last_liveness_at = ?, "
        "started_at = COALESCE(started_at, ?), updated_at = ? "
        "WHERE id = ?",
        (now, now, now, task_id),
    )
    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    await audit_log(
        db, "task.started",
        actor=f"agent:{task['assigned_agent_id']}",
        project_id=task["project_id"],
        task_id=task_id,
        agent_id=task["assigned_agent_id"],
    )
    # v1.8: SSE notify. The dashboard's status pill + loop_status
    # badge update instantly when a task flips assigned -> running.
    await publish_event(
        task["project_id"],
        "task.state_changed",
        {
            "task_id": task_id,
            "status": "running",
            "agent_id": task["assigned_agent_id"],
            "started_at": now,
        },
    )
    return _row_to_task(row)


# Accept both GET and POST: the wrapper's liveness poller uses POST
# (semantically correct — this endpoint has a side effect, updating
# last_liveness_at), but the route was originally registered as GET.
# Keeping GET works for any future read-only inspectors; POST is the
# primary path the wrapper uses.
@router.get("/{task_id}/poll")
@router.post("/{task_id}/poll")
async def poll_task(
    task_id: str,
    request: Request,
    agent_id: str = Depends(require_hmac_auth),
) -> dict:
    """Agent polls for liveness. Updates last_liveness_at.

    v1.6: HMAC-authed. The X-Agent-Id must match task.assigned_agent_id.
    """
    db = request.app.state.db
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task.get("assigned_agent_id") != agent_id:
        raise HTTPException(
            403,
            f"X-Agent-Id ({agent_id}) is not the owner of task {task_id}",
        )
    if task["status"] != "running":
        return {"status": task["status"], "should_stop": True}

    now = _now_iso()
    await db.execute(
        "UPDATE tasks SET last_liveness_at = ?, updated_at = ? WHERE id = ?",
        (now, now, task_id),
    )
    return {"status": "running", "should_stop": False}


@router.post("/{task_id}/result", response_model=Task)
async def submit_result(
    task_id: str,
    body: TaskResult,
    request: Request,
    agent_id: str = Depends(require_hmac_auth),
) -> Task:
    """Agent submits task result (running → completed | failed).

    v1.6: HMAC-authed. The X-Agent-Id must match task.assigned_agent_id.
    """
    db = request.app.state.db
    if body.status not in ("completed", "failed"):
        raise HTTPException(400, f"Invalid result status: {body.status}")

    # Fetch + check state FIRST
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task["status"] != "running":
        raise HTTPException(400, f"Task not in running state: {task['status']}")
    if task.get("assigned_agent_id") != agent_id:
        raise HTTPException(
            403,
            f"X-Agent-Id ({agent_id}) is not the owner of task {task_id}",
        )

    # Store result as JSON
    import json

    # LLM-fooling pattern #10 (2026-07-25): the LLM agent
    # sometimes includes parts of the prompt (--- PROJECT
    # CONTEXT ---, --- USER RECENT ---, etc.) in its response.
    # The wrapper's _strip_prompt_echo doesn't catch all
    # cases. Do a final server-side strip on the summary so
    # the user sees just the LLM's actual work, not the
    # prompt context echoed back.
    clean_summary = _strip_pollution_markers(body.summary or "")

    result_json = json.dumps(
        {
            "summary": clean_summary,
            "session_id": body.session_id,
            "error": body.error,
            "artifacts": body.artifacts,
            # Stage 1.5 multi-skill (2026-07-23): which skills the
            # agent actually loaded during the task. Wrapper parses
            # the hermes transcript and reports. Stored in result JSON
            # so promote-to-workflow can read it without scanning
            # agent-side cache (which the server can't access).
            "skills_used": body.skills_used or [],
        }
    )
    now = _now_iso()
    # ended_at is set on every terminal transition (completed or failed)
    # so the dashboard's "took" field reflects the real runtime, not the
    # 1-2s gap between the final liveness poll and this UPDATE.
    await db.execute(
        "UPDATE tasks SET status = ?, result = ?, error = ?, ended_at = ?, "
        "updated_at = ? "
        "WHERE id = ?",
        (body.status, result_json, body.error, now, now, task_id),
    )
    # Free the profile so the dashboard reflects "idle" again
    if task.get("assigned_profile_id"):
        await db.execute(
            "UPDATE agent_profiles SET status = 'idle', current_task_id = NULL, "
            "updated_at = ? WHERE id = ?",
            (now, task["assigned_profile_id"]),
        )
    # Auto-register artifact if the task declared an output_path and the
    # wrapper attached an artifact entry to the result. (The wrapper computes
    # sha256 + size before submitting.)
    #
    # Defense in depth: dedupe the artifacts list by (path, sha256) before
    # iterating. If the wrapper accidentally sends the same file twice
    # (e.g., a merge bug in the auto-upload loop, fixed 2026-07-22), we
    # still produce one artifact row per (path, sha) pair. First occurrence
    # wins (path/sha order in the list is preserved).
    if body.status == "completed" and body.artifacts:
        seen_artifacts: set[tuple[str, str]] = set()
        deduped_artifacts: list[dict[str, Any]] = []
        for art in body.artifacts:
            key = (art.get("path", ""), art.get("sha256", "") or "")
            if key in seen_artifacts:
                continue
            seen_artifacts.add(key)
            deduped_artifacts.append(art)
        artifacts_to_register = deduped_artifacts
        for art in artifacts_to_register:
            try:
                # Wrapper sends either {"path": "rel/path", "size_bytes": N, "sha256": "..."}
                # or the legacy {"path": "rel", "absolute_path": "abs"}.
                ap = art.get("absolute_path")
                rel = art.get("path")
                if not rel:
                    continue
                # Resolve the local path on the orchestrator side
                cfg = request.app.state.config
                projects_root = Path(cfg["projects"]["storage_root"]).resolve()
                apath = (
                    Path(ap)
                    if ap and Path(ap).is_absolute()
                    else projects_root / task["project_id"] / rel
                )
                if not apath.exists() or not apath.is_file():
                    continue
                size = apath.stat().st_size
                sha = (
                    art.get("sha256")
                    or hashlib.sha256(apath.read_bytes()).hexdigest()
                )
                await db.insert(
                    "artifacts",
                    {
                        "id": f"art-{uuid.uuid4().hex[:12]}",
                        "task_id": task_id,
                        "project_id": task["project_id"],
                        "name": rel,
                        "content_type": art.get("content_type"),
                        "size_bytes": size,
                        "checksum": sha,
                        "storage_kind": "local",
                        "storage_path": str(apath),
                        "agent_id": task.get("assigned_agent_id"),
                    },
                )
                await audit_log(
                    db, "artifact.registered",
                    actor=f"agent:{task['assigned_agent_id']}",
                    project_id=task["project_id"],
                    task_id=task_id,
                    agent_id=task.get("assigned_agent_id"),
                    payload={"path": rel, "size": size, "sha256": sha[:12]},
                )
            except Exception as e:
                # Non-fatal: artifact registration failure shouldn't fail the task
                import traceback
                traceback.print_exc()
    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    updated = _row_to_task(row)

    # On failure: propagate to dependents per §4.3
    if body.status == "failed":
        await _propagate_failure(db, task_id)

    # Audit log
    if body.status == "completed":
        await audit_log(
            db, "task.completed",
            actor=f"agent:{task['assigned_agent_id']}",
            project_id=task["project_id"],
            task_id=task_id,
            agent_id=task["assigned_agent_id"],
            payload={"summary": body.summary, "session_id": body.session_id},
        )
        # Phase 1 of 3-tier memory (docs/design/3-tier-memory.md):
        # append a 1-line summary of the task to L2 (facts.md) Task
        # Results section, citing the L1 event. Best-effort.
        try:
            from hermes_orch.core.memory import get_memory_writer
            # Extract a 1-line summary (first 200 chars, single line)
            summary = (body.summary or "").replace("\n", " ").replace("\r", " ")
            if len(summary) > 200:
                summary = summary[:200] + "..."
            artifacts = body.artifacts or []
            artifact_names = [a.get("name") or a.get("path", "?") for a in artifacts]
            fact_text = (
                f"[{task_id}] {task.get('name', '?')} "
                f"({task.get('agent_role', '?')})"
            )
            if summary:
                fact_text += f" -- {summary}"
            if artifact_names:
                fact_text += f" (artifacts: {', '.join(artifact_names[:3])})"
            get_memory_writer().append_fact_L2(
                project_id=task["project_id"],
                section="## Task Results",
                fact_text=fact_text,
                cite_id=f"task.completed@{task_id}",
            )
            # Track artifacts in the Files section. DEDUP: a given
            # (name, size) pair only appears once per project even if
            # multiple tasks read the same artifact (which is common --
            # downstream tasks all auto-report upstream files in their
            # body.artifacts list). Without dedup, "## Files" grows
            # linearly with task count and is hard to read.
            import re as _re
            existing_files = get_memory_writer().read_facts_full(
                project_id=task["project_id"],
                section="## Files (artifacts)",
            )
            seen: set[tuple[str, int]] = set()
            for line in existing_files.splitlines():
                fm = _re.match(r"^- \[cite:[^]]+\] (.+) \((\d+) bytes\)$", line.strip())
                if fm:
                    seen.add((fm.group(1), int(fm.group(2))))
            for a in artifacts:
                aname = a.get("name") or a.get("path", "?")
                asize = a.get("size_bytes", 0)
                if (aname, asize) in seen:
                    continue
                seen.add((aname, asize))
                get_memory_writer().append_fact_L2(
                    project_id=task["project_id"],
                    section="## Files (artifacts)",
                    fact_text=f"{aname} ({asize} bytes)",
                    cite_id=f"task.completed@{task_id}",
                )
        except Exception:
            pass
    else:  # failed
        await audit_log(
            db, "task.failed",
            actor=f"agent:{task['assigned_agent_id']}",
            project_id=task["project_id"],
            task_id=task_id,
            agent_id=task["assigned_agent_id"],
            payload={"error": body.error},
        )

    # Record per-task token usage reported by the wrapper. Best-effort:
    # if the wrapper didn't report (older versions) or the state.db
    # schema is older than hermes v0.17, body.token_usage will be None
    # and we skip silently. Cost of writing a row is ~1ms.
    if body.token_usage:
        try:
            from hermes_orch.core.token_usage import record_token_usage, extract_cache_read_tokens
            tu = body.token_usage
            task_name = task.get("name") or task.get("agent_role") or "?"
            call_label = f"task:{task_name[:40]}"
            provider = tu.get("billing_provider")
            if provider:
                call_label = f"{call_label} ({provider})"
            # v3.1.2: prefer the wrapper-reported cache_read_tokens (the
            # wrapper already parses the LLM's usage block per provider),
            # fall back to the generic extractor if not provided.
            # Either path returns 0 when the provider doesn't report
            # cache (most non-Anthropic) so the column is harmless.
            cache_read = int(tu.get("cache_read_tokens") or 0) or extract_cache_read_tokens(tu)
            await record_token_usage(
                db,
                agent_id=task.get("assigned_agent_id"),
                profile_id=task.get("assigned_profile_id"),
                project_id=task["project_id"],
                task_id=task_id,
                role=task.get("agent_role"),
                model=tu.get("model") or "unknown",
                base_url=tu.get("billing_base_url"),
                prompt_tokens=int(tu.get("prompt_tokens") or 0),
                completion_tokens=int(tu.get("completion_tokens") or 0),
                total_tokens=int(tu.get("total_tokens") or 0),
                cache_read_tokens=cache_read,  # v3.1.2
                call_kind="agent_task",
                call_label=call_label,
            )
        except Exception as e:  # noqa: BLE001
            # Token tracking must never break a real task completion.
            import logging
            logging.getLogger(__name__).warning(
                "token_usage record failed for task %s: %s", task_id, e
            )

    # v3.12.1 follow-up #6: persist the wrapper-reported
    # `history_turn_count` (the hermes session's `message_count`
    # at task completion) into the most-recent task_dispatch row
    # for this task. Lets the dashboard / operator verify the
    # conversation-history growth fix is working — the 4x
    # growth curve we measured (commit 20fb097) should flatten
    # to ~1.3x with hermes 0.19.1's micro-compaction enabled.
    #
    # Best-effort: a missing or None value is silently skipped
    # (e.g. older wrappers that don't report it). We update by
    # `MAX(dispatched_at)` because a task may have multiple
    # task_dispatch rows (apply_workflow + loopback_reset paths
    # can both create one for the same task id). The most recent
    # row is the "final" dispatch for this task.
    if body.history_turn_count is not None:
        try:
            await db.execute(
                "UPDATE task_dispatch SET history_turn_count = ? "
                "WHERE task_id = ? AND dispatched_at = ("
                "  SELECT MAX(dispatched_at) FROM task_dispatch "
                "  WHERE task_id = ?"
                ")",
                (int(body.history_turn_count), task_id, task_id),
            )
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "history_turn_count record failed for task %s: %s",
                task_id, e,
            )

    # v1.8: SSE notify. When a task transitions running → completed
    # or running → failed, the dashboard's status pill + loop_status
    # badge update instantly (no waiting for the next 5s poll tick).
    # The browser re-evaluates the loop_status from audit_log data
    # on its next refresh; for now we just notify the state change.
    #
    # v1.9.3 fix (2026-07-30): `updated` is a pydantic Task model,
    # not a dict — use attribute access, not `.get()`. The v1.8 line
    # used `updated.get("project_id", ...)` which throws AttributeError
    # on every result submission. The 500 was silently being returned
    # to the wrapper; the wrapper logged the 401 (a DIFFERENT bug from
    # v1.9.1) and the task was marked failed via the stuck_wrapper
    # timeout. With v1.9.3's body-signing fix, the wrapper now reaches
    # this code path; the 500 surfaces. Both bugs were chained: the
    # 401 was the loud symptom, the 500 was the silent one.
    await publish_event(
        updated.project_id or task.get("project_id", ""),
        "task.state_changed",
        {
            "task_id": task_id,
            "status": body.status,  # "completed" or "failed"
            "agent_id": task.get("assigned_agent_id"),
            "ended_at": _now_iso(),
        },
    )
    return updated


@router.post("/{task_id}/cancel", response_model=Task)
async def cancel_task(task_id: str, request: Request) -> Task:
    """Operator cancels a task (any non-terminal state → cancelled)."""
    row = await _do_cancel_task(request.app.state.db, task_id, actor="operator")
    # v1.8: SSE notify. Cancel button in the dashboard now updates
    # the status pill instantly instead of waiting for the next
    # 5s poll tick.
    await publish_event(
        row.get("project_id", ""),
        "task.state_changed",
        {
            "task_id": task_id,
            "status": "cancelled",
            "agent_id": row.get("assigned_agent_id"),
        },
    )
    return _row_to_task(row)


async def _do_cancel_task(
    db: Any, task_id: str, *, actor: str = "operator"
) -> dict[str, Any]:
    """Core cancel-task logic, extracted so other endpoints (e.g. the
    project-scoped wrapper at /api/projects/{id}/tasks/{id}/cancel)
    can call it without re-implementing the state-machine checks.

    Raises HTTPException(404/400) on bad input. Returns the updated
    task row dict (not yet mapped to the Task pydantic model)."""
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task["status"] not in ("pending", "assigned", "running"):
        raise HTTPException(400, f"Task not in cancellable state: {task['status']}")

    now = _now_iso()
    # ended_at is the same as the state transition for cancel; if the
    # task never started (was 'pending' or 'assigned'), ended_at will
    # be the cancel time, which is correct (zero or near-zero duration).
    await db.execute(
        "UPDATE tasks SET status = 'cancelled', ended_at = ?, updated_at = ? "
        "WHERE id = ?",
        (now, now, task_id),
    )
    # Free the profile (only if it was actually assigned to this task)
    if task.get("assigned_profile_id"):
        await db.execute(
            "UPDATE agent_profiles SET status = 'idle', current_task_id = NULL, "
            "updated_at = ? WHERE id = ? AND current_task_id = ?",
            (now, task["assigned_profile_id"], task_id),
        )
    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    await audit_log(
        db, "task.cancelled",
        actor=actor,
        project_id=task["project_id"],
        task_id=task_id,
        agent_id=task.get("assigned_agent_id"),
    )
    return row


@router.post("/{task_id}/interrupt", response_model=Task)
async def interrupt_task(task_id: str, request: Request) -> Task:
    """Operator interrupts a running task (打尖). running → interrupted."""
    db = request.app.state.db
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task["status"] != "running":
        raise HTTPException(400, f"Task not in running state: {task['status']}")

    now = _now_iso()
    await db.execute(
        "UPDATE tasks SET status = 'interrupted', ended_at = ?, updated_at = ? "
        "WHERE id = ?",
        (now, now, task_id),
    )
    if task.get("assigned_profile_id"):
        await db.execute(
            "UPDATE agent_profiles SET status = 'idle', current_task_id = NULL, "
            "updated_at = ? WHERE id = ? AND current_task_id = ?",
            (now, task["assigned_profile_id"], task_id),
        )
    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    await audit_log(
        db, "task.interrupted",
        actor="operator",
        project_id=task["project_id"],
        task_id=task_id,
        agent_id=task.get("assigned_agent_id"),
    )
    return _row_to_task(row)


@router.post("/{task_id}/retry", response_model=Task)
async def retry_task(task_id: str, request: Request) -> Task:
    """Operator retries a failed/interrupted/cancelled/skipped task.

    Resets the task to 'pending' so the supervisor's next tick picks it
    up and re-dispatches it. The task ID is preserved (operators can
    re-link logs and audit history to the same task). Increments
    retry_count so the operator can see how many times this task has
    been retried.

    Phase 4 Stage 2 (2026-07-25): added so the visual project page
    can offer a 'Retry' button on failed tasks. Previously operators
    had to use the supervisor's loop-back mechanism (requires the
    project to have feedback_to + max_iterations configured) or
    manually re-run the whole workflow — neither is a good UX for
    "I just want to try this one task again".

    Idempotency: a second retry on the same task (now pending) is
    a 400 — operator should wait for the supervisor to re-dispatch
    it first.
    """
    db = request.app.state.db
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    # Only failed/interrupted/cancelled/skipped can be retried. A
    # pending/assigned/running task is already in flight — no point
    # retrying. A completed task is "done" — operator should clone
    # the workflow and re-run, not retry (avoids the "successful
    # retry overwrites successful result" surprise).
    RETRYABLE = ("failed", "interrupted", "cancelled", "skipped")
    if task["status"] not in RETRYABLE:
        raise HTTPException(
            400,
            f"Task not in retryable state: {task['status']!r}. "
            f"Retry only works for: {sorted(RETRYABLE)}",
        )

    now = _now_iso()
    # Reset terminal-state fields, increment retry_count, free profile
    # (only if it was assigned — defensive; normally interrupted/cancelled
    # already freed the profile but failed might not have).
    await db.execute(
        "UPDATE tasks SET "
        "  status = 'pending', "
        "  result = NULL, "
        "  error = NULL, "
        "  started_at = NULL, "
        "  ended_at = NULL, "
        "  last_liveness_at = NULL, "
        "  retry_count = retry_count + 1, "
        "  assigned_agent_id = NULL, "
        "  assigned_profile_id = NULL, "
        "  updated_at = ? "
        "WHERE id = ?",
        (now, task_id),
    )
    if task.get("assigned_profile_id"):
        await db.execute(
            "UPDATE agent_profiles SET status = 'idle', current_task_id = NULL, "
            "updated_at = ? WHERE id = ? AND current_task_id = ?",
            (now, task["assigned_profile_id"], task_id),
        )
    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    new_retry_count = (row["retry_count"] or 0) if row else 0
    await audit_log(
        db, "task.retried",
        actor="operator",
        project_id=task["project_id"],
        task_id=task_id,
        agent_id=task.get("assigned_agent_id"),
        payload={
            "prev_status": task["status"],
            "retry_count": new_retry_count,
        },
    )
    return _row_to_task(row)


# ===== clone-and-cascade (re-run a chain as new tasks) — Phase 4+ (2026-07-26) =====
#
# Replaces the earlier "reset-and-cascade" endpoint, which destroyed
# history by clearing result/error/timestamps. User feedback
# (2026-07-26): "正常設計 task & task result 是分開的, 但現在是組合
# 一起了, 如果是測試, 最快當然是 clone". The clone semantic:
#   - Source task + every downstream task gets a NEW sibling task
#     (new ID, fresh status=pending, all result/error/started_at
#     /ended_at are NULL by default).
#   - The OLD tasks are NOT touched (preserved with their results,
#     audit log, and on-disk artifacts).
#   - The OLD tasks are marked `archived=1` so the project views
#     filter them out by default. The user can still see them via
#     "show archived" (TODO: add the toggle) or via the audit log.
#   - depends_on is rebuilt in the new graph: each new task's
#     depends_on = [new_id if old_id in cascade_set else old_id for
#     old_id in old.depends_on]. So a NEW child of an OLD parent
#     (parent not in cascade) keeps the OLD parent ID; a NEW child
#     of a NEW parent points to the NEW parent ID.
#   - Project is woken from completed/paused/cancelled to ready
#     (same as /create and the old /reset-and-cascade).
#
# Why this design is better than reset-and-cascade:
#   1. PRESERVES HISTORY: old tasks stay in the DB with their full
#      audit trail. The operator can compare "before vs after" by
#      looking at archived tasks.
#   2. PROPER PLAN/RESULT SEPARATION: the user pointed out that
#      "task (structure) & task result (execution) should be
#      separate". clone-and-cascade effectively makes each new
#      task a fresh execution of an existing plan. The plan row
#      is the new task; the result is whatever the new task
#      produces. Old plans + old results stay linked via
#      audit log + archive.
#   3. POST-CLONE EDIT: the user said "之後我可以再加減 task". The
#      new chain is a normal plan, so they can PATCH/POST/DELETE
#      on the new tasks as if it were a fresh plan.


class CloneCascadeBody(BaseModel):
    """Body for POST /api/tasks/{id}/clone-and-cascade. All fields optional."""
    # If true (default), also clone every task that depends
    # (transitively) on this task. If false, only clone this
    # single task. Downstream tasks are detected by walking the
    # depends_on graph in reverse (every task that has this task
    # in its depends_on, recursively).
    include_downstream: bool = True


# Statuses from which a task can be "cloned" (i.e. a new fresh
# task is created alongside). Same constraints as the old
# _RESETTABLE_STATUSES: not running (would race with the wrapper),
# not pending/assigned (nothing to clone from — operator should
# just edit a pending task or wait).
_CLONEABLE_STATUSES = ("failed", "interrupted", "cancelled", "skipped", "completed")


@router.post("/{task_id}/clone-and-cascade")
async def clone_and_cascade(
    task_id: str, request: Request, body: CloneCascadeBody | None = None
) -> dict[str, Any]:
    """Clone a task (and optionally all downstream tasks) as fresh pending tasks.

    The original tasks are preserved untouched (in DB + audit log +
    on-disk artifacts) but marked `archived=1` so the project views
    filter them out by default.

    Returns: {
        "new_task_ids": [str, ...],     # IDs of the newly created tasks
        "old_task_ids": [str, ...],     # IDs of the archived source tasks
        "id_map": {old_id: new_id, ...},# mapping for the depends_on rebuild
        "project_id": str,
        "include_downstream": bool,
    }
    """
    if body is None:
        body = CloneCascadeBody()
    db = request.app.state.db
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task["status"] not in _CLONEABLE_STATUSES:
        raise HTTPException(
            400,
            f"Task not in cloneable state: {task['status']!r}. "
            f"Cloneable states: {sorted(_CLONEABLE_STATUSES)}. "
            f"If the task is running, interrupt it first. "
            f"If pending/assigned, just edit it (PATCH) or wait.",
        )
    project_id = task["project_id"]

    # ---- BFS: find all downstream tasks ----
    cascade_ids: list[str] = [task_id]
    if body.include_downstream:
        # Load all NON-ARCHIVED tasks in this project (we don't want
        # archived tasks to appear in the BFS — they shouldn't be
        # dependents of anything new).
        all_rows = await db.fetchall(
            "SELECT id, depends_on FROM tasks "
            "WHERE project_id = ? AND archived = 0",
            (project_id,),
        )
        all_tasks: dict[str, list[str]] = {}
        for r in all_rows:
            deps_raw = r.get("depends_on")
            if isinstance(deps_raw, str):
                try:
                    deps = json.loads(deps_raw)
                except Exception:
                    deps = []
            elif isinstance(deps_raw, list):
                deps = deps_raw
            else:
                deps = []
            all_tasks[r["id"]] = deps

        # BFS in reverse (every task that lists us in its depends_on,
        # then their children, etc.)
        visited: set[str] = {task_id}
        frontier: list[str] = [task_id]
        while frontier:
            next_frontier: list[str] = []
            for tid in frontier:
                for other_id, deps in all_tasks.items():
                    if other_id in visited:
                        continue
                    if tid in deps:
                        visited.add(other_id)
                        cascade_ids.append(other_id)
                        next_frontier.append(other_id)
            frontier = next_frontier

    # ---- Load the source task rows we need to clone (full row,
    # including params/depends_on/etc.) ----
    # Build a placeholder string for the IN clause. SQLite supports
    # up to 999 placeholders by default; we typically have <50.
    placeholders = ",".join("?" for _ in cascade_ids)
    source_rows = await db.fetchall(
        f"SELECT * FROM tasks WHERE id IN ({placeholders})",
        tuple(cascade_ids),
    )
    sources_by_id: dict[str, dict[str, Any]] = {r["id"]: dict(r) for r in source_rows}

    # ---- Create the new tasks in dependency order (parents first,
    # so the new depends_on can reference the just-created new IDs
    # for in-cascade parents). cascade_ids is already in BFS order
    # which is parent-first; we can iterate it directly. ----
    now = _now_iso()
    new_id_map: dict[str, str] = {}  # old_id -> new_id
    new_ids_in_order: list[str] = []
    for old_id in cascade_ids:
        src = sources_by_id.get(old_id)
        if src is None:
            # Should not happen, but defensive: skip if missing
            continue
        new_id = _task_id()
        new_id_map[old_id] = new_id
        new_ids_in_order.append(new_id)

        # Rebuild depends_on: any old_id IN the cascade set becomes
        # its new sibling; anything else (an external dep) stays
        # as-is. Empty if src had no deps.
        old_deps_raw = src.get("depends_on")
        if isinstance(old_deps_raw, str):
            try:
                old_deps = json.loads(old_deps_raw)
            except Exception:
                old_deps = []
        elif isinstance(old_deps_raw, list):
            old_deps = old_deps_raw
        else:
            old_deps = []
        new_deps = [new_id_map.get(d, d) for d in old_deps]
        # Dedup (defensive)
        seen: set[str] = set()
        deduped_deps: list[str] = []
        for d in new_deps:
            if d != new_id and d not in seen:
                seen.add(d)
                deduped_deps.append(d)

        # Pull params (JSON) and re-encode. If the source's params
        # are NULL (default), we keep them NULL in the clone.
        params_raw = src.get("params")
        if params_raw is None or params_raw == "":
            clone_params: Any = None
        elif isinstance(params_raw, str):
            try:
                clone_params = json.loads(params_raw)
            except Exception:
                clone_params = None
        else:
            clone_params = params_raw

        # Required capability (also JSON, single string or null)
        clone_req_cap = src.get("required_capability") or None

        await db.insert(
            "tasks",
            {
                "id": new_id,
                "project_id": project_id,
                "name": src.get("name"),
                "agent_role": src.get("agent_role"),
                "depends_on": deduped_deps,
                "on_parent_failure": src.get("on_parent_failure", "skip"),
                "status": "pending",
                "priority": src.get("priority", "normal"),
                "action": src.get("action"),
                "params": clone_params,
                "max_retries": src.get("max_retries", 2),
                "timeout_seconds": src.get("timeout_seconds", 1800),
                "output_path": src.get("output_path"),
                "required_capability": clone_req_cap,
                "feedback_to": "[]",
                "procedure_md": src.get("procedure_md", "") or "",
                "archived": 0,
            },
        )

    # ---- Mark the OLD tasks as archived ----
    if cascade_ids:
        archive_placeholders = ",".join("?" for _ in cascade_ids)
        await db.execute(
            f"UPDATE tasks SET archived = 1, updated_at = ? "
            f"WHERE id IN ({archive_placeholders})",
            (now, *cascade_ids),
        )

    # ---- Wake the project if it was in a terminal state ----
    proj = await db.fetchone(
        "SELECT state FROM projects WHERE id = ?", (project_id,),
    )
    if proj and proj["state"] in ("completed", "paused", "cancelled"):
        await db.execute(
            "UPDATE projects SET state = 'ready', updated_at = ? WHERE id = ?",
            (now, project_id),
        )
        await audit_log(
            db, "project.woken",
            actor="operator",
            project_id=project_id,
            payload={
                "previous_state": proj["state"],
                "trigger": "task.clone_and_cascade",
                "source_task_id": task_id,
                "new_count": len(new_ids_in_order),
            },
        )

    # ---- Audit log: one event on the SOURCE task, with the
    # mapping so the operator can trace old -> new. The new tasks
    # get their own task.created events via the db.insert path
    # (well, they don't, because db.insert doesn't auto-audit;
    # we add a single task.cloned_cascade event that lists them). ----
    await audit_log(
        db, "task.cloned_cascade",
        actor="operator",
        project_id=project_id,
        task_id=task_id,
        agent_id=task.get("assigned_agent_id"),
        payload={
            "include_downstream": body.include_downstream,
            "old_task_ids": cascade_ids,
            "new_task_ids": new_ids_in_order,
            "id_map": new_id_map,
            "archived_count": len(cascade_ids),
            "created_count": len(new_ids_in_order),
            "project_woken": proj is not None and proj["state"] in ("completed", "paused", "cancelled"),
        },
    )

    return {
        "new_task_ids": new_ids_in_order,
        "old_task_ids": cascade_ids,
        "id_map": new_id_map,
        "project_id": project_id,
        "include_downstream": body.include_downstream,
    }


# ===== PATCH (edit task structure) — Phase 4 Stage 3.5 (2026-07-26) =====
#
# The visual project page now lets the operator add/edit/delete
# tasks directly (the "plan builder" UX). These endpoints back
# those UI actions.
#
# Edit policy (intentionally simple for v1):
#   - Only edit tasks in non-terminal state (pending/assigned).
#     Running tasks are owned by the wrapper; the operator
#     should interrupt first. Completed/failed/interrupted/
#     cancelled/skipped are terminal — operator should retry,
#     not edit in place. (Re-running with a different structure
#     is the cleanest path; mid-flight edits would race with
#     the wrapper's polling.)
#   - All editable fields are optional. Pass only the ones you
#     want to change. name/agent_role/action/depends_on/params
#     are supported.
#   - depends_on is a list of task IDs (NOT names) in this
#     endpoint. The frontend resolves names -> IDs before
#     calling (matches the chat-apply pattern).


class TaskPatch(BaseModel):
    """Body for PATCH /api/tasks/{id}. All fields optional."""
    name: str | None = None
    agent_role: str | None = None
    action: str | None = None
    depends_on: list[str] | None = None  # list of task IDs
    params: dict[str, Any] | None = None
    output_path: str | None = None
    priority: str | None = None  # 'low' | 'normal' | 'high'
    on_parent_failure: str | None = None  # 'skip' | 'fail' | 'wait'


# Tasks that can be safely edited (not in flight, not terminal).
_EDITABLE_STATUSES = ("pending", "assigned")


@router.patch("/{task_id}", response_model=Task)
async def patch_task(
    task_id: str, body: TaskPatch, request: Request
) -> Task:
    """Edit a task's structure (name, agent_role, action,
    depends_on, params, etc.). Used by the visual project page
    'plan builder' UX.

    Refuses to edit running or terminal tasks (400). For
    completed/failed/interrupted tasks, use POST /retry to
    re-dispatch, or use the supervisor's loop-back mechanism
    via project.feedback_to.

    depends_on is a list of task IDs; the server validates that
    every referenced ID exists in the same project. Self-refs
    are silently dropped. Forward refs (depends on a later
    task in the same project) are allowed (supervisor's
    _maybe_loop_back can use them for loop-back semantics).
    """
    db = request.app.state.db
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task["status"] not in _EDITABLE_STATUSES:
        raise HTTPException(
            400,
            f"Cannot edit task in state {task['status']!r}. "
            f"Editable states: {list(_EDITABLE_STATUSES)}. For terminal "
            f"tasks use POST /retry (or supervisor loop-back).",
        )
    project_id = task["project_id"]
    set_parts: list[str] = []
    set_params: list[Any] = []
    # name
    if body.name is not None:
        clean_name = (body.name or "").strip()
        if not clean_name:
            raise HTTPException(400, "name cannot be empty")
        # Ensure uniqueness within project (other tasks)
        dup = await db.fetchone(
            "SELECT id FROM tasks WHERE project_id = ? AND name = ? AND id != ?",
            (project_id, clean_name, task_id),
        )
        if dup:
            raise HTTPException(
                400, f"name '{clean_name}' already used by task {dup['id']} in this project"
            )
        set_parts.append("name = ?")
        set_params.append(clean_name)
    # agent_role
    if body.agent_role is not None:
        role = (body.agent_role or "").strip()
        if role:
            prof = await db.fetchone(
                "SELECT id FROM agent_profiles WHERE name = ?", (role,),
            )
            if not prof:
                available = await db.fetchall(
                    "SELECT name FROM agent_profiles ORDER BY name"
                )
                names = [p["name"] for p in available]
                raise HTTPException(
                    400,
                    f"agent_role '{role}' is not a registered profile. "
                    f"Available profiles: {names}.",
                )
        set_parts.append("agent_role = ?")
        set_params.append(role)
    # action
    if body.action is not None:
        clean_action = (body.action or "").strip()
        if not clean_action:
            raise HTTPException(400, "action cannot be empty")
        set_parts.append("action = ?")
        set_params.append(clean_action)
    # depends_on
    if body.depends_on is not None:
        # Validate: every ID must exist in the same project,
        # and must not be self. Dedupe.
        new_deps = []
        seen: set[str] = set()
        for did in body.depends_on:
            if not isinstance(did, str) or did == task_id or did in seen:
                continue
            other = await db.fetchone(
                "SELECT id FROM tasks WHERE id = ? AND project_id = ?",
                (did, project_id),
            )
            if not other:
                raise HTTPException(
                    400, f"depends_on references '{did}' which is not in this project"
                )
            new_deps.append(did)
            seen.add(did)
        set_parts.append("depends_on = ?")
        set_params.append(json.dumps(new_deps))
    # params
    if body.params is not None:
        set_parts.append("params = ?")
        set_params.append(json.dumps(body.params))
    # output_path
    if body.output_path is not None:
        set_parts.append("output_path = ?")
        set_params.append((body.output_path or "").strip() or None)
    # priority
    if body.priority is not None:
        if body.priority not in ("low", "normal", "high"):
            raise HTTPException(
                400, f"priority must be one of low/normal/high, got {body.priority!r}"
            )
        set_parts.append("priority = ?")
        set_params.append(body.priority)
    # on_parent_failure
    if body.on_parent_failure is not None:
        if body.on_parent_failure not in ("skip", "fail", "wait"):
            raise HTTPException(
                400,
                f"on_parent_failure must be one of skip/fail/wait, got {body.on_parent_failure!r}",
            )
        set_parts.append("on_parent_failure = ?")
        set_params.append(body.on_parent_failure)
    if not set_parts:
        # No-op patch; return current row
        return _row_to_task(task)
    set_parts.append("updated_at = ?")
    set_params.append(_now_iso())
    set_params.append(task_id)
    await db.execute(
        f"UPDATE tasks SET {', '.join(set_parts)} WHERE id = ?",
        tuple(set_params),
    )
    # If name or action changed, also clear cached values in
    # dependent tables (artifacts.output_path, etc. — not
    # implemented for v1 but the hooks are here).
    await audit_log(
        db, "task.patched",
        actor="operator",
        project_id=project_id,
        task_id=task_id,
        agent_id=task.get("assigned_agent_id"),
        payload={"fields_changed": [p.split(" =")[0] for p in set_parts]},
    )
    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return _row_to_task(row)


# ===== DELETE (remove task from plan) — Phase 4 Stage 3.5 (2026-07-26) =====


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: str, request: Request):
    """Delete a task from a project (plan builder UX).

    Behavior:
      - Refuses to delete running or terminal tasks. Only
        pending/assigned tasks can be deleted (same as PATCH).
        Operator should interrupt or retry first.
      - Scrubs depends_on references in sibling tasks. If
        task A depends on task X, and X is deleted, A's
        depends_on is updated to remove X (instead of leaving
        a dangling reference that would crash the dispatcher).
      - Hard-deletes the row. Audit log: task.deleted.

    Idempotent: a second DELETE on the same task is 404
    (the row is already gone).
    """
    db = request.app.state.db
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task["status"] not in _EDITABLE_STATUSES:
        raise HTTPException(
            400,
            f"Cannot delete task in state {task['status']!r}. "
            f"Deletable states: {list(_EDITABLE_STATUSES)}. For terminal "
            f"tasks, the result is preserved; just leave them.",
        )
    project_id = task["project_id"]
    # Scrub depends_on references in siblings. Load all
    # tasks in the project, parse their depends_on JSON,
    # remove the deleted id, write back.
    siblings = await db.fetchall(
        "SELECT id, depends_on FROM tasks WHERE project_id = ? AND id != ?",
        (project_id, task_id),
    )
    now = _now_iso()
    scrubbed = []
    for s in siblings:
        deps_raw = s.get("depends_on")
        if not deps_raw:
            continue
        if isinstance(deps_raw, str):
            try:
                deps = json.loads(deps_raw) or []
            except (json.JSONDecodeError, TypeError):
                continue
        else:
            deps = deps_raw or []
        if task_id not in deps:
            continue
        new_deps = [d for d in deps if d != task_id]
        await db.execute(
            "UPDATE tasks SET depends_on = ?, updated_at = ? WHERE id = ?",
            (json.dumps(new_deps), now, s["id"]),
        )
        scrubbed.append(s["id"])
    # Free profile if assigned (defensive — pending tasks
    # shouldn't have one, but assigned might)
    if task.get("assigned_profile_id"):
        await db.execute(
            "UPDATE agent_profiles SET status = 'idle', current_task_id = NULL, "
            "updated_at = ? WHERE id = ? AND current_task_id = ?",
            (now, task["assigned_profile_id"], task_id),
        )
    # Hard-delete the row. CASCADE will clean up artifacts.
    await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    await audit_log(
        db, "task.deleted",
        actor="operator",
        project_id=project_id,
        task_id=task_id,
        agent_id=task.get("assigned_agent_id"),
        payload={
            "name": task.get("name"),
            "scrubbed_dependents": scrubbed,
        },
    )
    return Response(status_code=204)


# ===== Promote task to workflow package (commit 2 of merge, 2026-07-27) =====


class PromoteTaskToWorkflowBody(BaseModel):
    """Body for POST /api/tasks/{id}/promote-to-workflow."""
    name: str = Field(
        ..., min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="kebab-case workflow name (must be unique)",
    )
    description: str = Field("", max_length=500)


@router.post("/{task_id}/promote-to-workflow")
async def promote_task_to_workflow(
    task_id: str, body: PromoteTaskToWorkflowBody, request: Request,
) -> dict:
    """Promote a finished task to a single-step workflow package.

    Closes the ad-hoc → reusable loop: a task that worked well can be
    lifted into the workflow catalog as a 1-step template that other
    users (or schedules) can run with different variables.

    Per the 2026-07-27 unified-tasks decision: this is the inverse of
    "apply workflow to project". Use case is "I just did this ad-hoc
    task and it works well, let me turn it into a reusable template".

    Constraints (so we don't promote half-finished or errored work):
      - Task must exist
      - Task must be in a terminal state (completed / failed /
        cancelled / interrupted / skipped) — pending / running tasks
        don't have settled action+params to lift
      - Workflow name must be unique

    What gets lifted:
      - step_template: one step with the task's action, agent_role,
        required_capability, and params
      - variables: empty (the single task was run with concrete params;
        a workflow consumer can edit the step later if they want vars)
      - description: from request body (operator-provided); falls back
        to "Single task {id} promoted to workflow" if empty

    Returns: {workflow_id, name, redirect_url}
    """
    import secrets
    import json as _json
    db = request.app.state.db
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    # Only promote from terminal states — the action/params of an
    # in-flight task are not yet settled. A "failed" task is allowed
    # because the operator may have learned what NOT to do; they can
    # still wrap it as a template (and edit the step later).
    TERMINAL = ("completed", "failed", "cancelled", "interrupted", "skipped")
    if (task.get("status") or "") not in TERMINAL:
        raise HTTPException(
            400,
            f"Task {task_id} is in state {task.get('status')!r}; "
            f"only terminal tasks can be promoted. Wait for it to "
            f"finish, or retry/cancel it first.",
        )
    # Name uniqueness
    existing = await db.fetchone(
        "SELECT id FROM workflow_packages WHERE name = ?", (body.name,)
    )
    if existing:
        raise HTTPException(
            409,
            f"workflow package name={body.name!r} already exists "
            f"(id={existing['id']}); pick a different name or "
            f"PATCH the existing one.",
        )
    # Build the single step from the task's settled fields.
    # We use the task's `action` (always set, even single tasks get
    # 'do_task') as the step action; `agent_role` is the role
    # requested at create time (or empty if the supervisor picked);
    # `required_capability` and `params` come from the task.
    raw_params = task.get("params")
    step_params: dict = {}
    if isinstance(raw_params, str) and raw_params.strip():
        try:
            p = _json.loads(raw_params)
            # params for a task is {goal, source, single_task_kind, ...}
            # — for a workflow step, we want the operator-relevant
            # knobs only. The operator can edit the step in the
            # workflow editor to add what they need.
            if isinstance(p, dict):
                # Pull out the action-relevant params (not the
                # metadata like source/goal)
                for k in ("symbol", "url", "path", "input", "args"):
                    if k in p:
                        step_params[k] = p[k]
        except (_json.JSONDecodeError, TypeError):
            pass
    elif isinstance(raw_params, dict):
        step_params = raw_params
    # Slugify the step name. The workflow validator requires kebab-case
    # step names (matching the same pattern as the workflow name).
    # If the task name doesn't slugify cleanly (e.g. all CJK chars,
    # empty after slugify), fall back to a generated name.
    raw_name = task.get("name") or ""
    step_name = raw_name.lower()
    step_name = re.sub(r"[^a-z0-9]+", "-", step_name)
    step_name = step_name.strip("-")[:50]
    if not step_name or not re.match(r"^[a-z0-9]", step_name):
        step_name = f"step-from-{task_id}"
    step = {
        "name": step_name,
        "action": task.get("action") or "do_task",
        "agent_role": task.get("agent_role") or "",
        # The workflow step field is `params_template` (not `params`).
        # `required_capability` and `output_path` are not workflow
        # step fields; output_path IS a valid step field — if the
        # task had one, lift it.
        "params_template": step_params,
        "depends_on": [],
    }
    # Lift output_path if the task had one — it's a real workflow
    # step field (where the step writes its result).
    task_output_path = task.get("output_path")
    if task_output_path:
        step["output_path"] = task_output_path
    pkg = {
        "step_template": [step],
        "variables": [],
        "description": body.description or (
            f"Workflow promoted from task {task_id} "
            f"(action={step['action']!r}, role={step['agent_role']!r})"
        ),
    }
    # Validate the package shape (defensive — we built it ourselves
    # so it should always pass, but if a validator gets stricter
    # later we'd rather fail here than write garbage).
    try:
        from hermes_orch.api.workflows import _validate_workflow_package
        ok, err = _validate_workflow_package(pkg)
        if not ok:
            raise HTTPException(422, f"Built workflow failed validation: {err}")
    except ImportError:
        # workflows module not available (shouldn't happen in normal
        # operation) — skip validation rather than crash
        pass
    wid = f"wf-{secrets.token_hex(6)}"
    now = _now_iso()
    try:
        await db.execute(
            "INSERT INTO workflow_packages "
            "(id, name, version, description, step_template, variables, "
            " source_project_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                wid, body.name, "1.0.0", pkg["description"],
                _json.dumps(pkg["step_template"], ensure_ascii=False),
                _json.dumps(pkg["variables"], ensure_ascii=False),
                task.get("project_id") or None,  # source = the task's project
                now, now,
            ),
        )
    except Exception as e:
        raise HTTPException(500, f"workflow insert failed: {type(e).__name__}: {e}")
    await audit_log(
        db, "workflow.created",
        actor="operator",
        project_id=task.get("project_id"),
        task_id=task_id,
        payload={
            "name": body.name,
            "from_task": task_id,
            "from_action": step["action"],
            "from_agent_role": step["agent_role"],
        },
    )
    return {
        "workflow_id": wid,
        "name": body.name,
        "redirect_url": f"/workflows/{wid}",
    }


# ===== Failure propagation (§4.3) =====


async def _propagate_failure(db: Any, failed_task_id: str) -> None:
    """When a task fails, update its dependents per on_parent_failure.

    - 'skip' (default): dependents → 'skipped'
    - 'wait': dependents stay in 'pending' (operator decides)
    - 'fail': dependents → 'failed' (cascade)
    """
    import json

    dependents = await db.fetchall(
        "SELECT id, on_parent_failure, depends_on FROM tasks WHERE status = 'pending'"
    )
    now = _now_iso()
    for dep in dependents:
        parents = dep["depends_on"]
        if isinstance(parents, str):
            parents = json.loads(parents)
        if failed_task_id not in parents:
            continue
        # This task depends on the failed one
        policy = dep["on_parent_failure"]
        if policy == "skip":
            await db.execute(
                "UPDATE tasks SET status = 'skipped', updated_at = ? WHERE id = ?",
                (now, dep["id"]),
            )
        elif policy == "fail":
            await db.execute(
                "UPDATE tasks SET status = 'failed', error = 'parent failed', updated_at = ? "
                "WHERE id = ?",
                (now, dep["id"]),
            )
        # 'wait' → no action, stays in pending
