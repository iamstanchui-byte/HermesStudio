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
import re
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
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
) -> dict:
    """List tasks (filterable).

    By default, tasks whose project is in 'archived' or 'deleted' state
    are hidden (joined filter on projects table). Pass
    `include_archived=true` to see them (e.g. for a "Show archived
    projects" toggle on the task page).
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
async def start_task(task_id: str, request: Request) -> Task:
    """Agent acks the task and starts running (assigned → running)."""
    db = request.app.state.db
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task["status"] != "assigned":
        raise HTTPException(400, f"Task not in assigned state: {task['status']}")

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
    return _row_to_task(row)


# Accept both GET and POST: the wrapper's liveness poller uses POST
# (semantically correct — this endpoint has a side effect, updating
# last_liveness_at), but the route was originally registered as GET.
# Keeping GET works for any future read-only inspectors; POST is the
# primary path the wrapper uses.
@router.get("/{task_id}/poll")
@router.post("/{task_id}/poll")
async def poll_task(task_id: str, request: Request) -> dict:
    """Agent polls for liveness. Updates last_liveness_at."""
    db = request.app.state.db
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task["status"] != "running":
        return {"status": task["status"], "should_stop": True}

    now = _now_iso()
    await db.execute(
        "UPDATE tasks SET last_liveness_at = ?, updated_at = ? WHERE id = ?",
        (now, now, task_id),
    )
    return {"status": "running", "should_stop": False}


@router.post("/{task_id}/result", response_model=Task)
async def submit_result(task_id: str, body: TaskResult, request: Request) -> Task:
    """Agent submits task result (running → completed | failed)."""
    db = request.app.state.db
    if body.status not in ("completed", "failed"):
        raise HTTPException(400, f"Invalid result status: {body.status}")

    # Fetch + check state FIRST
    task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    if task["status"] != "running":
        raise HTTPException(400, f"Task not in running state: {task['status']}")

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
            from hermes_orch.core.token_usage import record_token_usage
            tu = body.token_usage
            task_name = task.get("name") or task.get("agent_role") or "?"
            call_label = f"task:{task_name[:40]}"
            provider = tu.get("billing_provider")
            if provider:
                call_label = f"{call_label} ({provider})"
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
                call_kind="agent_task",
                call_label=call_label,
            )
        except Exception as e:  # noqa: BLE001
            # Token tracking must never break a real task completion.
            import logging
            logging.getLogger(__name__).warning(
                "token_usage record failed for task %s: %s", task_id, e
            )

    return updated


@router.post("/{task_id}/cancel", response_model=Task)
async def cancel_task(task_id: str, request: Request) -> Task:
    """Operator cancels a task (any non-terminal state → cancelled)."""
    db = request.app.state.db
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
        actor="operator",
        project_id=task["project_id"],
        task_id=task_id,
        agent_id=task.get("assigned_agent_id"),
    )
    return _row_to_task(row)


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
