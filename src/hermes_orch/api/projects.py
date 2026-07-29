"""Project endpoints + file API (per REVIEW.md §3.6, §4).

All file access goes through HTTP (no SMB/NFS) per §3.6.
Project folder structure (per §3.2):
    ./projects/<project_id>/
    ├── plan.md       (YAML frontmatter + body)
    ├── status.md     (YAML frontmatter + body)
    ├── decisions.md
    ├── agents/<id>/notes.md
    └── ...
"""
from __future__ import annotations

import json
import re

# v1.4 (2026-07-29): strip common ANSI escape codes (CSI sequences
# ending in a letter — covers SGR color/style codes, cursor moves,
# erase, etc.). We strip on the way OUT (GET /output) rather than
# on the way IN (POST /output-chunk) so the audit_log keeps the
# raw chunk for any future debugging needs.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from a string.

    Hermes writes colored output to its stdout (e.g.
    ``[1;38;2;255;215;0m╺─━━━━ Hermes ━━━━╸[0m``). The terminal
    renders those as colors, but in the dashboard's <pre> block
    they show up as raw bytes and make the text hard to read.
    Stripping them server-side is cheaper + simpler than running
    a JS-side terminal emulator.
    """
    return _ANSI_ESCAPE_RE.sub("", text)
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from hermes_orch.auth import require_hmac_auth
from pydantic import BaseModel, Field

from hermes_orch.core.audit import audit_log
from hermes_orch.core.loop_status import compute_loop_status
from hermes_orch.utils import now_iso as _now_iso

# Imported here (not at module top) to avoid circular imports
# (tasks.py imports from api.projects for some types).
from hermes_orch.api.tasks import _do_cancel_task  # noqa: E402

router = APIRouter()


# ===== Pydantic models =====


class ProjectCreate(BaseModel):
    # Phase 4+ repositioning (2026-07-25): create is now SIMPLE. Just a
    # name is required. Goal/iter-loop/etc. are setup INSIDE the
    # project page, not at create time. New projects always start at
    # state='planned' (blank, awaiting tasks or Run click) — there is
    # no "auto-plan on create" anymore. The user adds tasks manually
    # via the project page, or clicks "Generate plan" to have the
    # LLM planner create them. Either way, the project stays at
    # state='planned' until the user explicitly clicks Run.
    name: str  # required: just a name
    goal: str | None = None  # optional: stored, but not used until Generate plan
    coordinator_role: str | None = None  # optional: iter-loop, set later
    accept_criteria: str | None = None
    deliverable_path: str | None = None
    max_iterations: int = 0


class Project(BaseModel):
    id: str
    name: str | None
    goal: str
    state: str
    created_at: str | None
    updated_at: str | None
    # Q3 fields
    coordinator_role: str | None = None
    accept_criteria: str | None = None
    deliverable_path: str | None = None
    max_iterations: int = 0
    current_iteration: int = 0
    last_iteration_summary: str | None = None
    # Stage 2b (2026-07-23): link back to the workflow package this
    # project was spawned from (NULL for projects not from a workflow).
    source_workflow_id: str | None = None


class PlanTask(BaseModel):
    id: str
    name: str | None = None
    agent_role: str | None = None
    status: str = "pending"
    depends_on: list[str] = Field(default_factory=list)


class PlanFrontmatter(BaseModel):
    project_id: str
    state: str = "planning"
    created_at: str
    tasks: list[PlanTask] = Field(default_factory=list)


class PlanUpdate(BaseModel):
    frontmatter: PlanFrontmatter
    body: str = ""


# ===== Helpers =====
# _now_iso is now imported from hermes_orch.utils (consolidated).


def _project_id() -> str:
    """Generate a new project ID like 'proj-1a2b3c4d' (8 hex chars).

    Used by create_project. Kept here (rather than in utils) because
    it's project-API-specific — the wrapper uses a different ID
    scheme for agents, and tasks use 't-' + uuid4().hex.
    """
    return "proj-" + secrets.token_hex(4)


def _projects_root(request: Request) -> Path:
    cfg = request.app.state.config
    root = Path(cfg["projects"]["storage_root"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _project_dir(request: Request, project_id: str) -> Path:
    base = _projects_root(request)
    pdir = base / project_id
    if not pdir.exists():
        raise HTTPException(404, f"Project not found: {project_id}")
    return pdir


def _append_chat_jsonl(
    request: Request,
    project_id: str,
    message_id: int | None,
    role: str,
    content: str,
    suggestions: list | None,
    created_at: str,
) -> None:
    """Append a single chat message to projects/{id}/chat.jsonl.

    Added 2026-07-29 (Phase 2 of docs/chatbox-plan-editor.md). The
    DB table `project_chat_messages` remains the source of truth for
    the UI; this file is a parallel append-only log for operator
    inspection (cat / tail / grep), audit, and easy backup.

    Format: one JSON object per line, with a trailing newline.
    Fields:
      id, project_id, role, content, suggestions, created_at

    Defensive: any I/O error is logged but does NOT fail the chat
    call. The DB row is the canonical record; this file is best-effort.
    """
    try:
        pdir = _project_dir(request, project_id)
        path = pdir / "chat.jsonl"
        record = {
            "id": message_id,
            "project_id": project_id,
            "role": role,
            "content": content,
            "suggestions": suggestions or [],
            "created_at": created_at,
        }
        # Use ensure_ascii=False for friendlier display of
        # multilingual content (Cantonese / Mandarin / mixed).
        # newline-terminate each line so the file is line-oriented.
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            f"failed to append chat.jsonl for {project_id}: {e}"
        )


def _validate_relpath(path: str) -> str:
    """Validate relative path — reject absolute, .., etc."""
    if not path:
        raise HTTPException(400, "Path required")
    if path.startswith("/") or path.startswith("\\"):
        raise HTTPException(400, "Absolute paths not allowed")
    if ".." in Path(path).parts:
        raise HTTPException(400, "Path traversal not allowed")
    return path


def _resolve_inside(base: Path, rel: str) -> Path:
    """Resolve rel inside base, ensuring we don't escape."""
    full = (base / rel).resolve()
    base_resolved = base.resolve()
    # Use os.path.commonpath to be robust
    try:
        full.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(400, "Path traversal not allowed")
    return full


def _parse_plan_md(content: str) -> tuple[dict[str, Any], str]:
    """Parse plan.md → (frontmatter_dict, body_str)."""
    if not content.startswith("---"):
        return {}, content
    m = re.match(r"^---\n(.*?)\n---\n?(.*)", content, re.DOTALL)
    if not m:
        return {}, content
    fm_str, body = m.group(1), m.group(2)
    try:
        fm = yaml.safe_load(fm_str) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, body


def _serialize_plan_md(fm: dict[str, Any], body: str) -> str:
    """Serialize (frontmatter, body) → plan.md text."""
    fm_str = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    # Body should start with newline if non-empty
    if body and not body.startswith("\n"):
        body = "\n" + body
    return f"---\n{fm_str}\n---\n{body}"


# ===== Project CRUD =====


@router.post("/", response_model=Project, status_code=201)
async def create_project(body: ProjectCreate, request: Request) -> Project:
    """Create a new project. Initializes plan.md, status.md, decisions.md.

    Phase 4+ repositioning (2026-07-25): the new flow is "create blank
    project -> setup plan inside -> Run". The project always starts at
    state='planned' (blank), regardless of whether a goal is provided.
    The user then:
      1. Adds tasks manually via the project page, OR
      2. Clicks "Generate plan" (LLM creates tasks from goal), OR
      3. Both — some manual, some planned.
    ...and stays at state='planned' until they explicitly click Run.
    This is the orch-as-coordinator principle: LLM is a planner, not
    a control flow. Tasks don't auto-dispatch.
    """
    db = request.app.state.db
    project_id = _project_id()
    now = _now_iso()
    initial_state = "planned"  # always; user clicks Run to dispatch
    initial_goal = body.goal or ""

    await db.insert(
        "projects",
        {
            "id": project_id,
            "name": body.name,
            "goal": initial_goal,
            "state": initial_state,
            "coordinator_role": body.coordinator_role or "",
            "accept_criteria": body.accept_criteria or "",
            "deliverable_path": body.deliverable_path or "",
            "max_iterations": int(body.max_iterations or 0),
            "current_iteration": 0,
            "last_iteration_summary": "",
        },
    )

    pdir = _projects_root(request) / project_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "agents").mkdir(exist_ok=True)

    # Initial plan.md
    plan_fm = {"project_id": project_id, "state": initial_state, "created_at": now, "tasks": []}
    goal_section = f"## Goal\n\n{initial_goal}\n" if initial_goal else "## Goal\n\n_(no goal set — add one via Edit, or click Generate plan)_\n"
    plan_body = f"\n# Project: {body.name or project_id}\n\n{goal_section}"
    (pdir / "plan.md").write_text(_serialize_plan_md(plan_fm, plan_body), encoding="utf-8")

    # Initial status.md
    status_fm = {"state": initial_state, "last_updated": now}
    status_body = "\n# Status\n\nJust created (blank project — add tasks or click Generate plan, then click Run).\n"
    (pdir / "status.md").write_text(
        _serialize_plan_md(status_fm, status_body), encoding="utf-8"
    )

    # Initial decisions.md
    decisions_fm = {"decisions": []}
    (pdir / "decisions.md").write_text(
        _serialize_plan_md(decisions_fm, "\n# Decisions\n\n"), encoding="utf-8"
    )

    # Phase 1 of 3-tier memory (docs/design/3-tier-memory.md): bootstrap
    # facts.md for L2 (curated facts). The L1 (trace.jsonl) bootstrap
    # happens automatically when audit_log() runs below — it mirrors
    # every event to the per-project trace file.
    try:
        from hermes_orch.core.memory import get_memory_writer
        memory = get_memory_writer()
        memory.init_facts_file(project_id, project_name=body.name or project_id)
        if initial_goal:
            memory.append_fact_L2(
                project_id=project_id,
                section="## Goal",
                fact_text=initial_goal,
                cite_id="project.created",
            )
    except Exception as e:
        # Don't fail project creation on memory init failure.
        import logging
        logging.getLogger("hermes_orch.api.projects").warning(
            f"facts.md bootstrap failed: {e}"
        )

    await audit_log(
        db, "project.created",
        actor="operator",
        project_id=project_id,
        payload={"name": body.name, "goal": initial_goal, "state": initial_state},
    )
    return Project(
        id=project_id,
        name=body.name,
        goal=initial_goal,
        state=initial_state,
        created_at=now,
        updated_at=now,
        coordinator_role=body.coordinator_role,
        accept_criteria=body.accept_criteria,
        deliverable_path=body.deliverable_path,
        max_iterations=body.max_iterations or 0,
        current_iteration=0,
        last_iteration_summary=None,
    )


@router.get("/")
async def list_projects(request: Request) -> dict:
    """List all projects."""
    db = request.app.state.db
    rows = await db.fetchall(
        "SELECT id, name, goal, state, created_at, updated_at, "
        "coordinator_role, accept_criteria, deliverable_path, "
        "max_iterations, current_iteration, last_iteration_summary "
        "FROM projects ORDER BY created_at DESC"
    )
    return {"projects": rows}


@router.get("/{project_id}", response_model=Project)
async def get_project(project_id: str, request: Request) -> Project:
    """Get project metadata."""
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT id, name, goal, state, created_at, updated_at, "
        "coordinator_role, accept_criteria, deliverable_path, "
        "max_iterations, current_iteration, last_iteration_summary, "
        "source_workflow_id "
        "FROM projects WHERE id = ?",
        (project_id,),
    )
    if not row:
        raise HTTPException(404, f"Project not found: {project_id}")
    return Project(**row)


# ===== File API (§3.6 — all access via HTTP) =====


@router.get("/{project_id}/files/{path:path}")
async def read_file(
    project_id: str,
    path: str,
    request: Request,
    _agent_id: str = Depends(require_hmac_auth),
) -> Response:
    """Read a file from the project folder (v1.6: HMAC-authed)."""
    pdir = _project_dir(request, project_id)
    safe = _validate_relpath(path)
    full = _resolve_inside(pdir, safe)
    if not full.exists():
        raise HTTPException(404, f"File not found: {path}")
    if not full.is_file():
        raise HTTPException(400, f"Not a file: {path}")

    content = full.read_text(encoding="utf-8", errors="replace")
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"X-File-Path": path},
    )


@router.put("/{project_id}/files/{path:path}")
async def write_file(
    project_id: str,
    path: str,
    request: Request,
    _agent_id: str = Depends(require_hmac_auth),
) -> dict:
    """Write a file (whole content) to the project folder.

    Per orch-as-coordinator principle: this endpoint is for SMALL
    metadata-bearing files (markdown reports, JSON metadata, text
    summaries). For large data outputs (>15MB), the agent should
    write directly to its share folder (see agent_profiles.storage_refs)
    and only store the reference here. Hard cap below prevents
    accidental DoS or OOM.
    """
    # 15MB per-file cap (matches email attachment UX). Bigger files
    # should go to share folder, not through orch. Constant lives
    # in one place so we can tune later if needed.
    MAX_FILE_BYTES = 15 * 1024 * 1024
    db = request.app.state.db
    pdir = _project_dir(request, project_id)
    safe = _validate_relpath(path)
    full = _resolve_inside(pdir, safe)
    body = await request.body()
    if len(body) > MAX_FILE_BYTES:
        raise HTTPException(
            413,
            f"File too large: {len(body)} bytes (max {MAX_FILE_BYTES // (1024*1024)}MB). "
            f"Large outputs should go to share folder — see agent_profiles.storage_refs. "
            f"Store only metadata/reference in orch.",
        )
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(body)
    await audit_log(
        db, "file.written",
        actor="operator",
        project_id=project_id,
        payload={"path": path, "size": len(body)},
    )
    return {"path": path, "size": len(body), "written_at": _now_iso()}


@router.delete("/{project_id}/files/{path:path}")
async def delete_file(project_id: str, path: str, request: Request) -> dict:
    """Delete a file (or directory) from the project folder."""
    db = request.app.state.db
    pdir = _project_dir(request, project_id)
    safe = _validate_relpath(path)
    full = _resolve_inside(pdir, safe)
    if not full.exists():
        raise HTTPException(404, f"File not found: {path}")
    if full.is_dir():
        shutil.rmtree(full)
    else:
        full.unlink()
    await audit_log(
        db, "file.deleted",
        actor="operator",
        project_id=project_id,
        payload={"path": path},
    )
    return {"path": path, "deleted_at": _now_iso()}


# ===== Auto-generate procedure.md (#22) =====
#
# Path A for recurring templates: the agent needs a "how to do this
# workflow" markdown that's read before each task. Hand-writing it for
# every project is friction, so this endpoint asks the LLM to look at
# the project's tasks + facts + decision and render a n8n-style
# procedure.md.
#
# The endpoint just calls LLM once with a structured prompt, writes
# the result to <project>/procedure.md, and returns it. No DB mutation
# beyond an audit log entry. If the user doesn't like the generated
# procedure, they can click Edit and rewrite it by hand (the existing
# Edit form posts back to the PUT file endpoint).


_PROCEDURE_PROMPT = """You are a workflow-procedure author for a multi-agent orchestrator.

Given a project's tasks (the steps that were actually run) plus its
curated facts (what the user cared about) and final decision (the
verdict), produce a clear, n8n-style procedure.md that the next agent
can read BEFORE starting the workflow.

# Output rules (strict)
- Output ONLY the markdown. No preamble, no "Here is your procedure",
  no code fence wrappers.
- Plain markdown — no JSON frontmatter, no HTML.
- Length: 30-80 lines, ~1-2 KB. Be concise; this is a reading primer,
  not a tutorial.
- Use second-person imperative ("Fetch the data", "Compose the report")
  so an agent reading it knows exactly what to do at each step.
- Each numbered step should reference the corresponding task (name +
  action) so the agent can match it against the plan it's been given.

# Required sections (in this order)
1. # <project name> — Procedure
2. ## Goal — one or two sentences, copy from project.goal
3. ## Steps — numbered list, one step per task, in execution order
4. ## Pitfalls — 1-3 things that went wrong or that the next agent
   should be careful about (derive from facts.md and decision.md)
5. ## Definition of done — what "good output" looks like for this
   workflow (1-2 sentences)

If facts.md or decision.md are empty, skip the Pitfalls section.

# Project context (input)
"""


@router.post("/{project_id}/procedure/auto-generate")
async def auto_generate_procedure(project_id: str, request: Request) -> dict:
    """Use the LLM to render procedure.md from this project's tasks + facts.

    Writes the generated markdown to <project>/procedure.md (overwriting
    any existing hand-written version — the user can re-Edit it
    afterwards). Returns the rendered text so the UI can show it.

    Failure modes:
      - LLM unreachable: 502 with the error
      - LLM returns empty / unparseable: 502
      - Project not found: 404
      - No tasks yet: 400 (nothing to render)
    """
    import logging as _logging
    log = _logging.getLogger(__name__)

    db = request.app.state.db
    cfg = request.app.state.config

    proj = await db.fetchone(
        "SELECT id, name, goal, state FROM projects WHERE id = ?",
        (project_id,),
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")

    tasks = await db.fetchall(
        "SELECT name, agent_role, action, status, depends_on, params, "
        "       output_path, priority, max_retries, timeout_seconds, "
        "       on_parent_failure "
        "FROM tasks WHERE project_id = ? "
        "ORDER BY created_at ASC",
        (project_id,),
    )
    if not tasks:
        raise HTTPException(
            400,
            f"Project {project_id} has no tasks yet — add tasks first so "
            "the LLM has something to render.",
        )

    # Pull facts.md and decision.md (optional, for pitfalls + done)
    pdir = _project_dir(request, project_id)
    facts_text = ""
    facts_path = pdir / "facts.md"
    if facts_path.exists():
        facts_text = facts_path.read_text(encoding="utf-8", errors="replace")
    decision_text = ""
    decision_path = pdir / "decision.md"
    if decision_path.exists():
        decision_text = decision_path.read_text(encoding="utf-8", errors="replace")

    # Compose the input the LLM sees. Strip wrapper context blocks from
    # task rows so the LLM doesn't get confused by hermes internals
    # (same regex as the SKILL.md renderer, kept in sync).
    from hermes_orch.api.schedules import _PROJECT_CONTEXT_RE

    def _clean(s: str) -> str:
        if not s:
            return ""
        # Try JSON first — many task results are {"summary": "..."}
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                s = d.get("summary") or d.get("result") or ""
            elif isinstance(d, str):
                s = d
        except (ValueError, TypeError):
            pass
        s = _PROJECT_CONTEXT_RE.sub("", s)
        return s.strip()

    task_lines: list[str] = []
    for i, t in enumerate(tasks, 1):
        deps = t.get("depends_on") or []
        deps_str = f" (depends on: {', '.join(deps)})" if deps else ""
        role = t.get("agent_role") or "?"
        action = t.get("action") or "?"
        name = t.get("name") or action
        status = t.get("status") or "?"
        params = t.get("params") or {}
        params_str = ""
        if params:
            params_str = f" params={json.dumps(params, ensure_ascii=False)}"
        out_path = t.get("output_path")
        out_str = f" -> writes to {out_path}" if out_path else ""
        task_lines.append(
            f"{i}. task name='{name}', action='{action}', role='{role}', "
            f"status='{status}'{deps_str}{params_str}{out_str}"
        )
        # Snippet from result (cleaned) if any
        result_raw = t.get("result") or ""
        # NB: tasks table has `result` as JSON dict (not raw string),
        # so the result-snippet side is already structured; we just
        # stringify its summary if present.
        cleaned = _clean(result_raw) if isinstance(result_raw, str) else ""
        if cleaned:
            task_lines.append(f"   result snippet: {cleaned[:200]}")

    # Truncate facts.md so the prompt stays under control
    facts_for_prompt = facts_text[:2500]
    decision_for_prompt = decision_text[:1000]

    prompt = (
        _PROCEDURE_PROMPT
        + "\n## Project name\n"
        + (proj.get("name") or project_id)
        + "\n\n## Project goal\n"
        + (proj.get("goal") or "(no goal set)")
        + "\n\n## Tasks executed (in order)\n"
        + "\n".join(task_lines)
        + "\n\n## facts.md (curated user-side memory)\n"
        + (facts_for_prompt or "(empty)")
        + "\n\n## decision.md (final verdict)\n"
        + (decision_for_prompt or "(no decision yet)")
    )

    # LLM call. Reuse the same httpx pattern as core/synthesis.py
    # to keep the credentials / endpoint format consistent.
    llm_cfg = cfg.get("llm", {})
    base_url = (llm_cfg.get("base_url") or "https://api.minimax.io/v1").rstrip("/")
    api_key = llm_cfg.get("api_key") or ""
    model = llm_cfg.get("model") or "MiniMax-M3"
    timeout = float(llm_cfg.get("timeout_seconds") or 60)

    if not api_key:
        raise HTTPException(
            503,
            "LLM api_key not configured — set llm.api_key in config.yaml "
            "before auto-generating procedures.",
        )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You write clear, terse procedure markdown for AI agents "
                    "to read before starting a workflow. Output ONLY the "
                    "markdown. No preamble."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,  # low — we want deterministic structure
        "max_tokens": 1500,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base_url}/chat/completions", json=payload, headers=headers
            )
        if r.status_code != 200:
            raise HTTPException(
                502,
                f"LLM returned HTTP {r.status_code}: {r.text[:300]}",
            )
        data = r.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise HTTPException(502, f"LLM response shape unexpected: {e}")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(502, "LLM returned empty content")
    except httpx.HTTPError as e:
        log.warning(f"procedure auto-gen LLM call failed for {project_id}: {e}")
        raise HTTPException(502, f"LLM unreachable: {e}")

    # Strip any leading "Sure, here is..." / code-fence wrappers the
    # LLM sometimes adds despite the prompt. Most modern models comply;
    # this is defense for the ones that don't.
    text = text.strip()
    # Strip reasoning traces — MiniMax M3 emits <think>...</think>
    # blocks before the actual answer. These are useful to the model
    # but useless (and noisy) in the saved procedure.md. Match across
    # newlines; non-greedy so we don't eat multiple blocks at once if
    # the model emits more than one.
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    if text.startswith("```"):
        # Strip outer code fence
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Write to procedure.md
    proc_path = pdir / "procedure.md"
    proc_path.write_text(text, encoding="utf-8")
    await audit_log(
        db, "procedure.auto_generated",
        actor="operator",
        project_id=project_id,
        payload={"size": len(text), "task_count": len(tasks)},
    )
    return {
        "path": "procedure.md",
        "size": len(text),
        "content": text,
        "generated_at": _now_iso(),
    }


# ===== Plan API (§4.1 — plan.md frontmatter) =====


@router.get("/{project_id}/plan")
async def get_plan(project_id: str, request: Request) -> dict:
    """Get parsed plan.md (frontmatter as JSON, body as text)."""
    pdir = _project_dir(request, project_id)
    plan_file = pdir / "plan.md"
    if not plan_file.exists():
        raise HTTPException(404, f"plan.md not found for project {project_id}")
    content = plan_file.read_text(encoding="utf-8")
    fm, body = _parse_plan_md(content)
    return {"frontmatter": fm, "body": body}


@router.put("/{project_id}/plan")
async def update_plan(project_id: str, update: PlanUpdate, request: Request) -> dict:
    """Update plan.md (write frontmatter + body)."""
    pdir = _project_dir(request, project_id)
    plan_file = pdir / "plan.md"
    fm = update.frontmatter.model_dump()
    # Sync project state in DB
    db = request.app.state.db
    await db.execute(
        "UPDATE projects SET state = ?, updated_at = ? WHERE id = ?",
        (fm.get("state", "planning"), _now_iso(), project_id),
    )
    plan_file.write_text(_serialize_plan_md(fm, update.body), encoding="utf-8")
    await audit_log(
        db, "plan.updated",
        actor="operator",
        project_id=project_id,
        payload={"state": fm.get("state"), "task_count": len(fm.get("tasks", []))},
    )
    return {"updated_at": _now_iso(), "frontmatter": fm}


@router.post("/{project_id}/open")
async def open_project_folder(project_id: str, request: Request) -> dict[str, Any]:
    """Open the project folder in the OS file manager.

    Browser can't open local paths directly, so we shell out from the
    server. The user's browser must be running on the same host as the
    orchestrator (this won't work for remote browser → local server).
    """
    import platform
    import subprocess
    pdir = _project_dir(request, project_id)
    sysname = platform.system().lower()
    try:
        if sysname.startswith("win"):
            win_path = str(pdir).replace("/", "\\")
            subprocess.Popen(["explorer", win_path])
        elif sysname == "darwin":
            subprocess.Popen(["open", str(pdir)])
        else:
            subprocess.Popen(["xdg-open", str(pdir)])
        return {"ok": True, "path": str(pdir), "platform": sysname}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"file manager not found: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.post("/{project_id}/archive")
async def archive_project(project_id: str, request: Request) -> dict:
    """Soft-archive a project. State set to 'archived', folder + DB records kept."""
    db = request.app.state.db
    project = await db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    await db.execute(
        "UPDATE projects SET state = 'archived', updated_at = ? WHERE id = ?",
        (_now_iso(), project_id),
    )
    await audit_log(db, "project.archived", actor="operator", project_id=project_id)
    return {"project_id": project_id, "state": "archived"}


@router.post("/{project_id}/unarchive")
async def unarchive_project(project_id: str, request: Request) -> dict:
    """Restore an archived project.

    Sets state to 'completed' (NOT 'planning') so the supervisor
    doesn't re-run the task pipeline. If you want to re-run,
    manually transition the project (e.g. via a "re-run" button
    on the project page) — restoring from archive is meant to
    bring the project back to a viewable, run-once-finished
    state, not to restart it.
    """
    db = request.app.state.db
    project = await db.fetchone("SELECT id, state FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    if project["state"] != "archived":
        raise HTTPException(400, f"Project not archived: {project['state']}")
    await db.execute(
        "UPDATE projects SET state = 'completed', updated_at = ? WHERE id = ?",
        (_now_iso(), project_id),
    )
    await audit_log(db, "project.unarchived", actor="operator", project_id=project_id)
    return {"project_id": project_id, "state": "completed"}


@router.post("/{project_id}/delete")
async def soft_delete_project(project_id: str, request: Request) -> dict:
    """Soft-delete a project (state='deleted').

    Same as archive in mechanism (just a state change, DB rows preserved)
    but semantically stronger: archive = "park for later", delete =
    "going to cleanup". A future settings-page cleanup job can hard-delete
    projects in archived+deleted state older than 30 days.

    Tasks belonging to this project are NOT auto-archived; the task list
    just filters them out by default (see list_tasks include_archived flag).
    """
    db = request.app.state.db
    project = await db.fetchone("SELECT id, state FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    if project["state"] == "deleted":
        return {"project_id": project_id, "state": "deleted", "noop": True}
    await db.execute(
        "UPDATE projects SET state = 'deleted', updated_at = ? WHERE id = ?",
        (_now_iso(), project_id),
    )
    await audit_log(db, "project.deleted", actor="operator", project_id=project_id)
    return {"project_id": project_id, "state": "deleted"}


@router.post("/{project_id}/undelete")
async def undelete_project(project_id: str, request: Request) -> dict:
    """Restore a soft-deleted project.

    Sets state to 'completed' (NOT 'planning') so the supervisor
    doesn't re-run the task pipeline — see unarchive_project for
    the same rationale.
    """
    db = request.app.state.db
    project = await db.fetchone("SELECT id, state FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    if project["state"] != "deleted":
        raise HTTPException(400, f"Project not deleted: {project['state']}")
    await db.execute(
        "UPDATE projects SET state = 'completed', updated_at = ? WHERE id = ?",
        (_now_iso(), project_id),
    )
    await audit_log(db, "project.undeleted", actor="operator", project_id=project_id)
    return {"project_id": project_id, "state": "completed"}


# ===== Bulk actions (multi-select from projects list page) =====


async def _bulk_state_change(db, project_ids: list[str], new_state: str, audit_event: str) -> dict:
    """Apply the same state change to multiple projects.

    Each project gets its own audit_log entry (so the history shows per-
    project cause). All updates are batched in one execute() for speed.
    Returns per-project results (ok / not_found / already_in_state).
    """
    if not project_ids:
        raise HTTPException(400, "project_ids is required")
    if len(project_ids) > 100:
        raise HTTPException(400, f"too many project_ids ({len(project_ids)}); max 100")
    # Look up current state to skip no-ops and to report not_found
    placeholders = ",".join("?" for _ in project_ids)
    rows = await db.fetchall(
        f"SELECT id, state FROM projects WHERE id IN ({placeholders})",
        tuple(project_ids),
    )
    by_id = {r["id"]: r["state"] for r in rows}
    not_found = [pid for pid in project_ids if pid not in by_id]
    to_change = [
        pid for pid in project_ids
        if pid in by_id and by_id[pid] != new_state
    ]
    if to_change:
        now = _now_iso()
        ph = ",".join("?" for _ in to_change)
        await db.execute(
            f"UPDATE projects SET state = ?, updated_at = ? WHERE id IN ({ph})",
            tuple([new_state, now] + to_change),
        )
        for pid in to_change:
            await audit_log(db, audit_event, actor="operator", project_id=pid)
    return {
        "changed": to_change,
        "noop": [pid for pid in project_ids if pid in by_id and by_id[pid] == new_state],
        "not_found": not_found,
    }


@router.post("/bulk-archive")
async def bulk_archive_projects(request: Request) -> dict:
    """Archive multiple projects in one call.

    Body: {project_ids: ['proj-1', 'proj-2', ...]}
    """
    import json
    db = request.app.state.db
    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON body")
    return await _bulk_state_change(
        db, data.get("project_ids", []), "archived", "project.archived"
    )


@router.post("/bulk-delete")
async def bulk_delete_projects(request: Request) -> dict:
    """Soft-delete multiple projects in one call.

    Body: {project_ids: ['proj-1', 'proj-2', ...]}
    """
    import json
    db = request.app.state.db
    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON body")
    return await _bulk_state_change(
        db, data.get("project_ids", []), "deleted", "project.deleted"
    )


@router.post("/{project_id}/session")
async def set_project_session(
    project_id: str,
    request: Request,
    _agent_id: str = Depends(require_hmac_auth),
) -> dict:
    """Set the project's session for the calling role (wrapper after each task).

    Sessions are stored PER ROLE (not per project) so the wrapper can resume
    only sessions that belong to its own profile. Hermes session namespaces
    are per-profile, so reusing a session from profile Y when running on
    profile X causes "Session not found" and the agent echos back the
    action without doing real work.

    `current_sessions_json` is a JSON dict: {role: session_id, ...}. We
    ALSO keep `current_session_id` (latest wins) for backward compat with
    any external consumer; new code should read the role-specific one.

    Also records the session in project_sessions for the auto-cleanup
    sweeper. Every hermes session the orchestrator wrapper creates
    is logged here so a TTL-based sweeper can reap them. Sessions
    that pre-existed in the hermes backend (i.e. user-created) are
    NOT in this table and therefore not touched.
    """
    import json
    import secrets
    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid JSON body")
    session_id = data.get("session_id")
    role = data.get("role")
    # Wrapper previously sent only {session_id, role} in the body, which
    # left agent_id / profile_id NULL in the project_sessions row. The
    # cleanup-ack endpoint JOINS agent_profiles on profile_id, so NULL
    # rows can never be acked — they sit in pending_cleanup forever.
    # Fall back to deriving both from the request's X-Agent-Id header
    # (HMAC-authenticated) + (agent_id, role) lookup. The wrapper
    # doesn't even need to send them in the body anymore.
    agent_id = data.get("agent_id") or _agent_id
    profile_id = data.get("profile_id")
    if not session_id:
        raise HTTPException(400, "session_id is required")
    if not role:
        # role is now required so the per-role map is correctly populated.
        # Without it, we'd write to current_session_id only (and a future
        # task on a different profile would try to resume it).
        raise HTTPException(400, "role is required (sessions are per-role)")
    if not agent_id:
        raise HTTPException(
            400,
            "agent_id required (X-Agent-Id header or body.agent_id)"
        )
    db = request.app.state.db
    # Resolve profile_id from (agent_id, role) if the wrapper didn't
    # send it. This is the standard case — the wrapper knows the role
    # but doesn't know the UUID of its own profile row.
    if not profile_id:
        prof_row = await db.fetchone(
            "SELECT id FROM agent_profiles "
            "WHERE agent_id = ? AND name = ?",
            (agent_id, role),
        )
        if prof_row:
            profile_id = prof_row["id"]
    # Read existing per-role map and update under this role's key.
    row = await db.fetchone(
        "SELECT current_sessions_json FROM projects WHERE id = ?", (project_id,)
    )
    if not row:
        raise HTTPException(404, f"Project not found: {project_id}")
    try:
        sess_map = json.loads(row["current_sessions_json"] or "{}")
        if not isinstance(sess_map, dict):
            sess_map = {}
    except (json.JSONDecodeError, TypeError):
        sess_map = {}
    sess_map[role] = session_id
    # Update the project's current_session_id (used by --resume).
    # Per-role map (current_sessions_json) is the source of truth; the
    # legacy current_session_id is kept for backward compat with any
    # external consumer and points to this role's session (latest write).
    await db.execute(
        "UPDATE projects SET current_session_id = ?, "
        "current_sessions_json = ?, updated_at = ? WHERE id = ?",
        (session_id, json.dumps(sess_map), _now_iso(), project_id),
    )
    # Record in project_sessions for auto-cleanup. We use a stable
    # row id derived from (project_id, session_id) so re-saves
    # for the same session just update the existing row (bump
    # last_used_at) instead of creating duplicates.
    row_id = f"ps-{secrets.token_hex(8)}"
    # Idempotent insert: if a row already exists for (project_id, session_id)
    # with status='active', bump last_used_at; otherwise insert new.
    existing = await db.fetchone(
        "SELECT id FROM project_sessions "
        "WHERE project_id = ? AND session_id = ? AND status = 'active'",
        (project_id, session_id),
    )
    if existing:
        await db.execute(
            "UPDATE project_sessions SET last_used_at = ? WHERE id = ?",
            (_now_iso(), existing["id"]),
        )
    else:
        await db.insert("project_sessions", {
            "id": row_id,
            "project_id": project_id,
            "session_id": session_id,
            "role": role or "",
            "agent_id": agent_id,
            "profile_id": profile_id,
            "source": "orchestrator",
            "status": "active",
        })
    await audit_log(
        db, "project.session_updated",
        actor="agent",
        project_id=project_id,
        payload={"session_id": session_id, "role": role, "tracked_for_cleanup": True},
    )
    return {
        "project_id": project_id,
        "current_session_id": session_id,
        "session_id": sess_map.get(role),
        "role": role,
    }


@router.get("/{project_id}/session")
async def get_project_session(
    project_id: str,
    request: Request,
    role: str | None = None,
    _agent_id: str = Depends(require_hmac_auth),
) -> dict:
    """Get the project's current session (called by wrapper before each task).

    With `?role=<name>` (recommended): returns the session for that
    specific role. The wrapper passes its own role so it never resumes
    a session that belongs to a different profile (cross-profile session
    reuse is broken at the hermes level — session namespaces are
    per-profile).

    Without `?role`: returns the legacy `current_session_id` (latest
    write wins) for backward compat. The wrapper MUST pass role.
    """
    import json
    db = request.app.state.db
    project = await db.fetchone(
        "SELECT current_session_id, current_sessions_json "
        "FROM projects WHERE id = ?",
        (project_id,),
    )
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    if role:
        try:
            sess_map = json.loads(project["current_sessions_json"] or "{}")
            if not isinstance(sess_map, dict):
                sess_map = {}
        except (json.JSONDecodeError, TypeError):
            sess_map = {}
        sid = sess_map.get(role)
        return {
            "project_id": project_id,
            "current_session_id": sid,  # role-specific
            "role": role,
        }
    # No role filter: legacy behavior (latest write).
    return {
        "project_id": project_id,
        "current_session_id": project.get("current_session_id"),
    }


class ProjectReplan(BaseModel):
    """Body for POST /replan. Either provides a new goal, or just kicks the
    planner to retry with the existing goal (e.g. after manual cleanup).

    The iter-loop fields (coordinator_role, max_iterations, accept_criteria,
    deliverable_path) are optional. If provided, they overwrite the project's
    existing values. The primary use case is a manual-mode project (created
    with no goal) where the operator is now adding a plan: they fill in
    goal AND configure the iter-loop at the same time.
    """
    goal: str | None = None  # if None, replan uses the current goal
    clear_tasks: bool = False  # if True, delete existing pending/assigned tasks first
    coordinator_role: str | None = None  # 'auto' or a profile name; '' to clear
    max_iterations: int | None = None  # 0 = no cap; None = leave unchanged
    accept_criteria: str | None = None  # '' to clear
    deliverable_path: str | None = None  # '' to clear


@router.post("/{project_id}/replan")
async def replan_project(
    project_id: str, body: ProjectReplan, request: Request
) -> dict:
    """Re-trigger the LLM planner for a project.

    Phase 4+ behavior (2026-07-25): planner CREATES tasks but does NOT
    auto-dispatch. After this call + supervisor's next tick, the project
    is in state='planned' (not 'ready'). The user reviews the plan
    (e.g. via /projects/{id}/visual) and clicks Run to actually start.

    Use cases:
    - Project was just created (no tasks yet). Now the user wants the
      planner to generate a plan: set goal + replan.
    - User wants to regenerate the plan (e.g. after editing the goal, or
      because the previous plan was poor).
    - Operator wants to retry planning after a planner failure.

    Behavior:
    - If body.goal is set: update project.goal
    - If body.clear_tasks: delete existing pending/assigned tasks for this
      project (running/terminal tasks are left alone).
    - Set state='planning' so the supervisor's next tick calls
      _handle_planning, which calls the planner.
    - After planning, supervisor sets state='planned' (NOT 'ready' anymore).
    - Audit log: project.replan_requested
    """
    db = request.app.state.db
    project = await db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    new_goal = body.goal if body.goal is not None else project.get("goal") or ""
    if not new_goal.strip():
        raise HTTPException(
            400,
            "cannot replan without a goal. Provide body.goal or set the "
            "project's goal first.",
        )
    # Clear tasks if requested (running ones are left alone to avoid losing work)
    cleared = 0
    if body.clear_tasks:
        cur = await db.execute(
            "DELETE FROM tasks WHERE project_id = ? "
            "AND status IN ('pending', 'assigned', 'running', 'failed', 'cancelled', 'skipped', 'interrupted')",
            (project_id,),
        )
        cleared = cur.rowcount if hasattr(cur, "rowcount") else 0
    # Always delete old iteration_review tasks. Without this, the supervisor's
    # _maybe_iterate would see the previous cycle's completed review task
    # (status=completed) and "consume" its decision.md — auto-completing the
    # fresh project based on a stale verdict. The replan must leave the
    # project in a state where the supervisor can dispatch a NEW review.
    old_reviews = await db.execute(
        "DELETE FROM tasks WHERE project_id = ? "
        "AND action LIKE '_iteration_review:%'",
        (project_id,),
    )
    cleared_reviews = old_reviews.rowcount if hasattr(old_reviews, "rowcount") else 0
    # Build the SET clause incrementally. Always update goal/state/iter
    # state. Only update the iter-loop fields if the caller actually
    # provided them (so a bare replan with no settings doesn't wipe
    # the existing config).
    set_parts = [
        "goal = ?", "state = 'planning'",
        "current_iteration = 0", "last_iteration_summary = ''",
        "updated_at = ?",
    ]
    set_params: list[Any] = [new_goal, _now_iso()]
    if body.coordinator_role is not None:
        set_parts.append("coordinator_role = ?")
        set_params.append(body.coordinator_role)
    if body.max_iterations is not None:
        set_parts.append("max_iterations = ?")
        set_params.append(int(body.max_iterations))
    if body.accept_criteria is not None:
        set_parts.append("accept_criteria = ?")
        set_params.append(body.accept_criteria)
    if body.deliverable_path is not None:
        set_parts.append("deliverable_path = ?")
        set_params.append(body.deliverable_path)
    set_params.append(project_id)
    await db.execute(
        f"UPDATE projects SET {', '.join(set_parts)} WHERE id = ?",
        tuple(set_params),
    )
    try:
        dpath = _project_dir(request, project_id) / "decision.md"
        if dpath.exists():
            dpath.unlink()
    except Exception:
        pass  # best-effort; non-fatal if the file isn't there
    await audit_log(
        db, "project.replan_requested",
        actor="operator",
        project_id=project_id,
        payload={
            "new_goal_preview": new_goal[:200],
            "cleared_tasks": cleared,
            "cleared_reviews": cleared_reviews,
            "previous_state": project["state"],
            "iter_fields_updated": {
                k: v for k, v in {
                    "coordinator_role": body.coordinator_role,
                    "max_iterations": body.max_iterations,
                    "accept_criteria": body.accept_criteria,
                    "deliverable_path": body.deliverable_path,
                }.items() if v is not None
            },
        },
    )
    return {
        "project_id": project_id,
        "state": "planning",
        "goal": new_goal,
        "cleared_tasks": cleared,
        "cleared_reviews": cleared_reviews,
        "message": (
            "replan queued. The supervisor's next tick will call the LLM planner. "
            "After planning, the project will be in state='planned' (tasks created, "
            "but NOT yet dispatched). Click Run on the project page to start dispatch."
        ),
    }


@router.post("/{project_id}/run")
async def run_project(project_id: str, request: Request) -> dict:
    """Trigger the supervisor to dispatch tasks for a project.

    Phase 4+ flow (2026-07-25): project starts at state='planned' (blank
    or LLM-plan-generated). The user reviews the plan (e.g. via the
    visual project page) and clicks Run. This endpoint sets state='ready'
    so the supervisor's next tick picks it up and starts dispatching
    pending tasks.

    Pre-conditions:
    - Project must exist and be in state='planned' (or 'ready' for idempotency).
    - Project must have at least one task (otherwise there's nothing to run).

    Idempotent: calling on state='ready' returns 200 with noop=true.
    Errors: 404 if project not found; 400 if state is 'completed'/'cancelled'/
    'archived'/'deleted' (terminal states); 400 if no tasks yet.
    """
    db = request.app.state.db
    project = await db.fetchone(
        "SELECT id, state, name, goal FROM projects WHERE id = ?", (project_id,),
    )
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    cur_state = project["state"]
    # Terminal states — refuse
    if cur_state in ("completed", "cancelled", "archived", "deleted"):
        raise HTTPException(
            400,
            f"Cannot Run project in state='{cur_state}' (terminal state). "
            f"Archive/delete are one-way. Unarchive first if you want to re-run.",
        )
    # Idempotent: already running
    if cur_state == "ready":
        return {
            "project_id": project_id,
            "state": "ready",
            "noop": True,
            "message": "Project already in 'ready' — supervisor will dispatch on next tick.",
        }
    if cur_state == "running":
        return {
            "project_id": project_id,
            "state": "running",
            "noop": True,
            "message": "Project is already running.",
        }
    if cur_state != "planned":
        # 'planning' is in-flight (planner running) — refuse, user should
        # wait or retry the plan first. 'interrupted' is a supervisor
        # signal, also refuse.
        raise HTTPException(
            400,
            f"Cannot Run project in state='{cur_state}'. "
            f"Wait for planning to complete, or re-trigger via Generate plan.",
        )
    # Check there's at least one task
    task_count = await db.fetchone(
        "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ?",
        (project_id,),
    )
    if not task_count or task_count.get("n", 0) == 0:
        raise HTTPException(
            400,
            f"Project {project_id} has no tasks yet. Add tasks manually "
            f"or click Generate plan before clicking Run.",
        )
    # All checks pass — flip to ready
    await db.execute(
        "UPDATE projects SET state = 'ready', updated_at = ? WHERE id = ?",
        (_now_iso(), project_id),
    )
    await audit_log(
        db, "project.run_requested",
        actor="operator",
        project_id=project_id,
        payload={"previous_state": cur_state, "task_count": task_count["n"]},
    )
    return {
        "project_id": project_id,
        "state": "ready",
        "noop": False,
        "task_count": task_count["n"],
        "message": "Run requested. Supervisor's next tick (within ~5s) will dispatch pending tasks.",
    }


# ===== Apply workflow to existing project (2026-07-26) =====
#
# Reverse of "Promote to workflow": instead of extracting a workflow
# from a project, push a workflow INTO a project as its task list.
# The user has a workflow package (e.g. "monthly-claim-report") and
# wants to use it as the plan for THIS project. Fills variables
# (e.g. report_month=May2026) and the project's task list becomes
# the workflow's steps with placeholders substituted.
#
# Semantics (additive import — import the workflow's OBJECT into
# the project, do NOT replace its task list):
#   - All current non-archived tasks are LEFT ALONE. The workflow's
#     steps JOIN them. Both old and new tasks coexist in the live
#     Tasks list. If you want a destructive replace, use Clone chain
#     (POST /api/tasks/{id}/clone-and-cascade) — that one is surgical.
#   - New tasks are inserted as pending with depends_on wired from
#     the workflow's step names (with 2-pass resolution: workflow-
#     internal first, then existing project task names by name).
#   - Project state is set to 'planned' regardless of prior state
#     (so the supervisor does NOT auto-dispatch — user clicks Run).
#
# Why not just call /api/workflows/{id}/run? That endpoint creates
# a NEW project. Apply-to-existing is the operator's edit-the-plan
# flow: same project, fresh task list.
#
# Body: { workflow_id: str, variables: { name: value, ... } }
class WorkflowApplyBody(BaseModel):
    """Body for POST /api/projects/{id}/apply-workflow.

    `variables` is a dict mapping workflow variable names to values.
    Required variables (per the workflow's declared variables) MUST
    be present. Type coercion (string → int/bool) is handled by
    the same _validate_run_variables helper that /api/workflows/{id}/run
    uses, so the client can send values in their natural form.
    """
    workflow_id: str
    variables: dict[str, Any] = Field(default_factory=dict)


@router.post("/{project_id}/apply-workflow")
async def apply_workflow_to_project(
    project_id: str, body: WorkflowApplyBody, request: Request
) -> dict:
    """Import a workflow's step_template into this project as NEW tasks.

    Mental model: "apply workflow" = import a workflow OBJECT into
    the project. The workflow's steps are added to the project's
    existing task list. Existing tasks are NOT touched — they stay
    in the live Tasks list, and the workflow's steps join them.
    If you want to replace an existing task chain, use Clone chain
    (POST /api/tasks/{id}/clone-and-cascade) — that one is surgical.

    User feedback (2026-07-26): "我一直都說是import workflow 的
    object 入project". The previous version archived all current
    tasks then inserted the workflow's steps (a destructive replace).
    This version is additive — both old and new tasks coexist, and
    the user can manually clean up duplicates via the Tasks section
    (e.g. with Delete on a row, or Clone chain to supersede).

    Pipeline:
      1. Load project (404 if missing/deleted).
      2. Load workflow (by id or name) + parse step_template +
         variables declaration.
      3. Validate user-provided variables + substitute {{var}} in
         the step_template (reuses helpers from api/workflows.py so
         substitution semantics stay identical to /run).
      4. Count current non-archived tasks (so the response can tell
         the user "you had N, now you have N+M"). DON'T archive.
      5. Set the project to state='planned' (pause dispatch). User
         feedback (2026-07-26): "apply workflow 後會自動run, 應該
         要按run 才start". The supervisor's _drive_project only
         handles 'planning' + 'ready'/'running', so from 'planned'
         the new pending tasks wait. User reviews + clicks Run.
      6. Insert the substituted steps as new pending tasks. depends_on
         is resolved in two passes:
         a. First try the step_name → step_name map (the workflow
            references its own steps).
         b. Then try matching against EXISTING tasks in the project
            (by name). This lets a workflow integrate with the
            project's current tasks (e.g. "verify-and-summarize"
            depends on a project task with the same name).
         If both miss, log task.depends_on_unresolved and treat as
         no dependency (loud, not silent).
      7. Audit: project.applied_workflow (top-level) + task.created
         per task (same shape as /run endpoint).
    """
    # Late import: avoid circular import (workflows.py imports from
    # projects.py for _project_id/_projects_root/_serialize_plan_md).
    from hermes_orch.api.workflows import (
        _validate_run_variables,
        _substitute_variables,
    )

    db = request.app.state.db

    # 1. Load project
    proj = await db.fetchone(
        "SELECT id, state, name FROM projects WHERE id = ?", (project_id,)
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    if proj["state"] == "deleted":
        raise HTTPException(400, "Cannot apply workflow to a deleted project. Restore it first.")

    # 2. Load workflow
    wf = await db.fetchone(
        "SELECT * FROM workflow_packages WHERE id = ?", (body.workflow_id,)
    )
    if not wf:
        wf = await db.fetchone(
            "SELECT * FROM workflow_packages WHERE name = ?", (body.workflow_id,)
        )
    if not wf:
        raise HTTPException(404, f"workflow {body.workflow_id!r} not found")

    try:
        step_template = json.loads(wf["step_template"] or "[]")
    except Exception:
        step_template = []
    try:
        variables_declared = json.loads(wf["variables"] or "[]")
    except Exception:
        variables_declared = []

    # 3. Validate + substitute (re-using the run endpoint's helpers)
    ok, err, vars_typed = _validate_run_variables(
        variables_declared, body.variables
    )
    if not ok:
        raise HTTPException(400, f"variable validation failed: {err}")

    substituted = _substitute_variables(step_template, vars_typed)

    if not substituted:
        raise HTTPException(
            400,
            f"workflow {wf['name']!r} has no steps in step_template — "
            "nothing to apply",
        )

    now = _now_iso()

    # 4. Count current non-archived tasks (so the response + UI can
    # show "you had N, now you have N+M" — additive semantics). We
    # don't archive, so the user's existing tasks stay live.
    pre_count_row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? AND archived = 0",
        (project_id,),
    )
    pre_count = pre_count_row["n"] if pre_count_row else 0

    # 5. Set state to 'planned' (NOT 'ready') regardless of prior state.
    # User feedback (2026-07-26): "apply workflow 後會自動run, 應該要按
    # run 才start". Same model as /replan (planning → planned, user
    # clicks Run). For all prior states, we transition: terminal
    # states (completed/failed/cancelled/interrupted/archived) need
    # to flip because the supervisor was ignoring them; non-terminal
    # states (ready/running/planning) need to flip to PAUSE dispatch
    # so the new pending tasks don't auto-run. 'planned' state is
    # the supervisor's "do nothing" state — user reviews + clicks Run.
    PLANNED = "planned"
    woken = False
    new_state = proj["state"]
    if proj["state"] != PLANNED:
        await db.execute(
            "UPDATE projects SET state = ?, updated_at = ? WHERE id = ?",
            (PLANNED, now, project_id),
        )
        woken = True
        new_state = PLANNED
        await audit_log(
            db, "project.woken", actor="operator", project_id=project_id,
            payload={
                "previous_state": proj["state"],
                "trigger": "apply_workflow",
                "workflow_id": wf["id"],
                "workflow_name": wf["name"],
            },
        )

    # 6. Resolve depends_on in two passes. The workflow's step
    # template references other steps by NAME. We map:
    #   a. Workflow-internal: name -> new task id (if both the
    #      dep and the step being inserted are in THIS apply)
    #   b. Project-external: name -> existing project task id
    #      (so a workflow step can depend on a project task with
    #      the same name — e.g. "verify-and-summarize" depends on
    #      an existing "fetch-data" task)
    # Anything not in either map is logged as unresolved (loud,
    # not silent — the audit log will show the gap).
    # Load existing project tasks once, by name.
    existing_task_rows = await db.fetchall(
        "SELECT id, name FROM tasks WHERE project_id = ? AND archived = 0",
        (project_id,),
    )
    existing_name_to_tid: dict[str, str] = {r["name"]: r["id"] for r in existing_task_rows}

    # First pass: collect step names + their pending tids (in order
    # so earlier steps' tids are available to later steps).
    name_to_tid: dict[str, str] = {}
    task_rows: list[dict] = []
    for i, step in enumerate(substituted):
        sname = step.get("name") or f"step-{i+1}"
        tid = "t-" + secrets.token_hex(4)
        name_to_tid[sname] = tid

    # Second pass: build task rows with resolved depends_on.
    for i, step in enumerate(substituted):
        sname = step.get("name") or f"step-{i+1}"
        tid = name_to_tid[sname]
        dep_step_names = step.get("depends_on") or []
        dep_tids: list[str] = []
        unresolved: list[str] = []
        for d in dep_step_names:
            if d in name_to_tid:
                # Same-workflow dependency
                dep_tids.append(name_to_tid[d])
            elif d in existing_name_to_tid:
                # Project-external dependency (workflow integrates
                # with an existing project task)
                dep_tids.append(existing_name_to_tid[d])
            else:
                unresolved.append(d)
        if unresolved:
            await audit_log(
                db, "task.depends_on_unresolved",
                actor="workflow-applier", project_id=project_id, task_id=tid,
                payload={"step_name": sname,
                         "unresolved_deps": unresolved},
            )
        # Carry skill name reference (Stage 1.5) and feedback_to
        # (Phase 0 of visual workflow builder). See run_workflow
        # for the same pattern.
        params = step.get("params_template") or {}
        skill_name = step.get("skill")
        if skill_name:
            params = dict(params)
            params["_workflow_skill"] = skill_name
        raw_fb = step.get("feedback_to") or []
        if isinstance(raw_fb, list):
            feedback_to = [f for f in raw_fb if f != sname]
        else:
            feedback_to = []
        task_rows.append({
            "id": tid,
            "project_id": project_id,
            "name": sname,
            "agent_role": step.get("agent_role") or "",
            "depends_on": json.dumps(dep_tids),
            "on_parent_failure": "skip",
            "status": "pending",
            "priority": "normal",
            "action": step.get("action") or "do_task",
            "params": json.dumps(params),
            "retry_count": 0,
            "max_retries": 2,
            "timeout_seconds": 1800,
            "output_path": step.get("output_path") or "",
            "required_capability": None,
            "feedback_to": json.dumps(feedback_to),
        })
    for t in task_rows:
        try:
            await db.insert("tasks", t)
        except Exception as e:
            raise HTTPException(
                500, f"failed to insert task {t['name']!r}: {e}"
            )
    # task.created audit per task
    for t in task_rows:
        await audit_log(
            db, "task.created",
            actor="workflow-applier", project_id=project_id, task_id=t["id"],
            payload={"agent_role": t["agent_role"],
                     "action": t["action"],
                     "name": t["name"],
                     "source": "apply_workflow"},
        )

    # 7. Top-level audit
    await audit_log(
        db, "project.applied_workflow", actor="operator", project_id=project_id,
        payload={
            "workflow_id": wf["id"],
            "workflow_name": wf["name"],
            "variables_provided": list(vars_typed.keys()),
            "task_count": len(task_rows),
            "pre_count": pre_count,
            "previous_state": proj["state"],
            "new_state": new_state,
            "woken": woken,
        },
    )

    return {
        "project_id": project_id,
        "workflow_id": wf["id"],
        "workflow_name": wf["name"],
        "task_count": len(task_rows),
        "pre_count": pre_count,
        "variables_applied": vars_typed,
        "previous_state": proj["state"],
        "new_state": new_state,
        "woken": woken,
        "tasks": [{"id": t["id"], "name": t["name"]} for t in task_rows],
        "message": (
            f"Applied workflow '{wf['name']}' ({len(task_rows)} new tasks added to {pre_count} existing). "
            + (f"State: {proj['state']} → planned. " if woken else "State already planned. ")
            + "Click ▶️ Run on the project page to dispatch."
        ),
    }


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: str, request: Request):
    """Hard-delete a project: removes folder + cascades DB records (tasks, artifacts)."""
    import shutil

    db = request.app.state.db
    project = await db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    # Remove folder (catches all files)
    pdir = _project_dir(request, project_id)
    if pdir.exists():
        shutil.rmtree(pdir)
    # FK ON DELETE CASCADE handles tasks / artifacts in DB
    await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    await audit_log(
        db, "project.deleted",
        actor="operator",
        project_id=project_id,
        payload={"name": project.get("name") if project else None},
    )
    from fastapi import Response

    # Phase 3 hook: when a project is deleted, kick off a recent.md
    # rebuild so the deleted project doesn't linger in the user's
    # 7-day summary. Fire-and-forget (don't block the DELETE
    # response on a 2-5s LLM call).
    try:
        from hermes_orch.core.memory import get_memory_writer
        from hermes_orch.core.synthesis import get_recent_generator
        db = request.app.state.db
        recent_gen = get_recent_generator(db=db)
        memory = get_memory_writer()
        import asyncio
        asyncio.create_task(
            recent_gen.regenerate_recent_async(
                memory_writer=memory, trigger=f"project.deleted:{project_id}"
            ),
            name=f"hermes-recent-rebuild-{project_id}",
        )
    except Exception as e:
        log.warning(f"delete-triggered recent regen failed: {e}")
    return Response(status_code=204)


# ===== Memory endpoints (Phase 1 of 3-tier memory) =====
# See docs/design/3-tier-memory.md. Phase 1 implements L1 (trace.jsonl)
# and L2 (facts.md); both are auto-written by the system. L3 (state.md)
# is Phase 2.


# ===== User-level memory endpoints (Phase 3) =====
#
# recent.md is the user-level L3 synthesis (7-day cross-project summary).
# Lives at ~/.hermes-orchestrator/memory/recent.md (not per-project).
# Auto-regenerated at supervisor startup and on project deletion;
# manually triggerable here for the dashboard "Refresh" button.

@router.get("/memory/recent")
async def get_recent(
    request: Request,
    _agent_id: str = Depends(require_hmac_auth),
) -> dict:
    """Return the user-level L3 (recent.md) content + size + archive count."""
    from hermes_orch.core.memory import get_memory_writer
    writer = get_memory_writer()
    content = writer.read_recent()
    archive_dir = writer.memory_root / "recent_archive"
    archive_size = 0
    archive_count = 0
    if archive_dir.exists():
        try:
            files = [f for f in archive_dir.iterdir() if f.is_file()]
            archive_count = len(files)
            archive_size = sum(f.stat().st_size for f in files)
        except Exception:
            pass
    rpath = writer.memory_root / "recent.md"
    return {
        "content": content,
        "exists": content is not None,
        "size_bytes": rpath.stat().st_size if rpath.exists() else 0,
        "archive_count": archive_count,
        "archive_size_bytes": archive_size,
    }


@router.post("/memory/recent/regenerate")
async def regenerate_recent(request: Request) -> dict:
    """Manually trigger user-level L3 (recent.md) regeneration.

    Returns immediately with {ok: true, regenerating: true} since
    the LLM call takes 2-5s. The endpoint is fire-and-forget; poll
    GET /memory/recent to see when the new content lands.
    """
    from hermes_orch.core.memory import get_memory_writer
    from hermes_orch.core.synthesis import get_recent_generator
    db = request.app.state.db
    writer = get_memory_writer()
    recent_gen = get_recent_generator(db=db)
    import asyncio
    asyncio.create_task(
        recent_gen.regenerate_recent_async(
            memory_writer=writer, trigger="manual"
        ),
        name="hermes-recent-regen-manual",
    )
    return {"ok": True, "regenerating": True}


@router.get("/{project_id}/memory/state")
async def get_project_state(
    project_id: str,
    request: Request,
    _agent_id: str = Depends(require_hmac_auth),
) -> dict:
    """Return the project's L3 (state.md) synthesized state.

    L3 is regenerated by the supervisor after each
    iteration_completed, or manually via
    POST /api/projects/{id}/memory/state/regenerate.
    """
    pdir = _project_dir(request, project_id)
    if not pdir.exists():
        raise HTTPException(404, f"Project not found: {project_id}")
    from hermes_orch.core.memory import get_memory_writer
    writer = get_memory_writer()
    content = writer.read_state(project_id)
    spath = pdir / "state.md"
    archive_dir = pdir / "state_archive"
    archive_size = 0
    if archive_dir.exists():
        try:
            archive_size = sum(
                f.stat().st_size for f in archive_dir.iterdir() if f.is_file()
            )
        except Exception:
            pass
    return {
        "project_id": project_id,
        "content": content,
        "exists": content is not None,
        "size_bytes": spath.stat().st_size if spath.exists() else 0,
        "archive_size_bytes": archive_size,
    }


@router.post("/{project_id}/memory/state/regenerate")
async def regenerate_project_state(project_id: str, request: Request) -> dict:
    """Manually trigger L3 (state.md) synthesis via LLM.

    Useful when:
    - The project is in manual mode (no iter loop, no auto trigger)
    - Operator wants a fresh state snapshot between iterations
    - Debugging the synthesis prompt

    Cost: ~500 tokens per regen. Failures are reported back as
    {ok: false, error: "..."}.
    """
    db = request.app.state.db
    proj = await db.fetchone(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    from hermes_orch.core.memory import get_memory_writer
    from hermes_orch.core.synthesis import get_state_generator
    memory = get_memory_writer()
    facts_text = memory.read_facts_full(project_id) or ""
    state_gen = get_state_generator(db=request.app.state.db)
    ok = await state_gen.regenerate_state_async(
        project_id=project_id,
        project_meta={
            "id": project_id,
            "name": proj["name"],
            "state": proj["state"],
            "current_iteration": proj["current_iteration"],
            "max_iterations": proj["max_iterations"],
        },
        facts_text=facts_text,
        memory_writer=memory,
        trigger="manual",
    )
    if ok:
        try:
            await audit_log(
                db, "project.state_regenerated",
                actor="operator",
                project_id=project_id,
                payload={"trigger": "manual"},
            )
        except Exception:
            pass
        return {"ok": True, "project_id": project_id}
    return {"ok": False, "project_id": project_id,
            "error": "synthesis failed (check server logs)"}


@router.get("/{project_id}/memory/facts")
async def get_project_facts(
    project_id: str,
    request: Request,
    _agent_id: str = Depends(require_hmac_auth),
) -> dict:
    """Return the project's L2 (facts.md) content for dashboard / replan modal."""
    from hermes_orch.core.memory import get_memory_writer
    pdir = _project_dir(request, project_id)
    if not pdir.exists():
        raise HTTPException(404, f"Project not found: {project_id}")
    writer = get_memory_writer()
    content = writer.read_facts_full(project_id)
    fpath = pdir / "facts.md"
    archive_path = pdir / "facts_archive.md"
    return {
        "project_id": project_id,
        "content": content,
        "size_bytes": fpath.stat().st_size if fpath.exists() else 0,
        "archive_size_bytes": archive_path.stat().st_size if archive_path.exists() else 0,
    }


@router.get("/{project_id}/memory/trace")
async def get_project_trace(
    project_id: str,
    request: Request,
    since: str | None = None,
    event_type: str | None = None,
    limit: int = 200,
    _agent_id: str = Depends(require_hmac_auth),
) -> dict:
    """Return filtered L1 (trace.jsonl) entries for audit / debug (v1.6: HMAC)."""
    pdir = _project_dir(request, project_id)
    if not pdir.exists():
        raise HTTPException(404, f"Project not found: {project_id}")
    from hermes_orch.core.memory import get_memory_writer
    writer = get_memory_writer()
    entries = writer.read_trace(
        project_id=project_id, since=since, event_type=event_type, limit=limit
    )
    return {
        "project_id": project_id,
        "entries": entries,
        "count": len(entries),
    }


@router.patch("/{project_id}/memory/facts")
async def append_project_fact(
    project_id: str, body: dict, request: Request
) -> dict:
    """Append a human-edited fact to L2 (facts.md).

    Body shape:
        {
            "section": "## Notes",     # any of FACTS_SECTIONS
            "fact": "free-form text",
            "cite_id": "optional L1 event_id",
        }
    """
    pdir = _project_dir(request, project_id)
    if not pdir.exists():
        raise HTTPException(404, f"Project not found: {project_id}")
    section = body.get("section", "## Human Notes")
    fact_text = body.get("fact", "").strip()
    cite_id = body.get("cite_id", "human_edit@now")
    if not fact_text:
        raise HTTPException(400, "fact is required")
    from hermes_orch.core.memory import get_memory_writer
    get_memory_writer().append_fact_L2(
        project_id=project_id, section=section, fact_text=fact_text, cite_id=cite_id
    )
    await audit_log(
        request.app.state.db, "project.fact_appended",
        actor="operator",
        project_id=project_id,
        payload={"section": section, "fact": fact_text[:200]},
    )
    return {"ok": True, "project_id": project_id, "section": section}


# ===== SOUL presets (§ — per-project agent identity) =====
#
# A SOUL preset is a per-project snapshot of what SOUL.md should look like
# for a given agent profile when this project is "active". The user designs
# one preset per project per role (e.g. project A's win-agent01 SOUL =
# "XAUUSD correlation specialist"; project B's win-agent01 SOUL = "server
# monitor operator"). When the user wants project A to start work, they
# "apply" its preset, which writes a profile_configs entry (status=pending)
# that the wrapper picks up and applies as a regular SOUL.md update.
#
# Multiple projects can run concurrently as long as they target DIFFERENT
# agent profiles — adding more agents unlocks more parallel projects. There
# is no "wait for all agents idle" requirement.


class SoulPresetUpsert(BaseModel):
    agent_id: str
    profile_name: str
    content: str


class SoulPresetApply(BaseModel):
    """Body for /soul-presets/apply — apply one or all presets for this project."""
    agent_id: str | None = None  # if set with profile_name, apply just that one
    profile_name: str | None = None
    confirm_overwrite: bool = False  # required true if preset != current SOUL


class SoulPreset(BaseModel):
    id: str
    project_id: str
    profile_id: str
    role_name: str
    content: str
    agent_id: str | None = None  # joined from agent_profiles
    profile_name: str | None = None  # joined from agent_profiles
    created_at: str | None = None
    updated_at: str | None = None


@router.get(
    "/{project_id}/soul-presets",
    response_model=list[SoulPreset],
)
async def list_soul_presets(project_id: str, request: Request) -> list[SoulPreset]:
    """List all SOUL presets saved for this project.

    Returns one entry per (project, profile). Useful for the dashboard to
    show "this project has these identity snapshots ready to apply".
    """
    db = request.app.state.db
    project = await db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    rows = await db.fetchall(
        "SELECT sp.id, sp.project_id, sp.profile_id, sp.role_name, sp.content, "
        "sp.created_at, sp.updated_at, ap.agent_id, ap.name AS profile_name "
        "FROM project_soul_presets sp "
        "JOIN agent_profiles ap ON ap.id = sp.profile_id "
        "WHERE sp.project_id = ? "
        "ORDER BY ap.agent_id, ap.name",
        (project_id,),
    )
    return [
        SoulPreset(
            id=r["id"],
            project_id=r["project_id"],
            profile_id=r["profile_id"],
            role_name=r["role_name"],
            content=r["content"],
            agent_id=r.get("agent_id"),
            profile_name=r.get("profile_name"),
            created_at=r.get("created_at"),
            updated_at=r.get("updated_at"),
        )
        for r in rows
    ]


@router.put(
    "/{project_id}/soul-presets",
    response_model=SoulPreset,
)
async def upsert_soul_preset(
    project_id: str, body: SoulPresetUpsert, request: Request
) -> SoulPreset:
    """Save or update a SOUL preset for one (project, profile) pair.

    Idempotent — re-PUTting replaces the existing preset for that pair.
    The preset is just a snapshot in the DB; applying it later writes the
    content to the profile's actual SOUL.md via the profile_configs flow.
    """
    import uuid as _uuid

    db = request.app.state.db
    project = await db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    profile = await db.fetchone(
        "SELECT * FROM agent_profiles WHERE agent_id = ? AND name = ?",
        (body.agent_id, body.profile_name),
    )
    if not profile:
        raise HTTPException(
            404,
            f"Profile not found: {body.agent_id}/{body.profile_name}",
        )
    # Idempotent upsert: update if exists, insert if not
    now = _now_iso()
    existing = await db.fetchone(
        "SELECT id FROM project_soul_presets "
        "WHERE project_id = ? AND profile_id = ?",
        (project_id, profile["id"]),
    )
    if existing:
        await db.execute(
            "UPDATE project_soul_presets "
            "SET content = ?, role_name = ?, updated_at = ? "
            "WHERE id = ?",
            (body.content, profile["name"], now, existing["id"]),
        )
        preset_id = existing["id"]
    else:
        preset_id = str(_uuid.uuid4())
        await db.insert(
            "project_soul_presets",
            {
                "id": preset_id,
                "project_id": project_id,
                "profile_id": profile["id"],
                "role_name": profile["name"],
                "content": body.content,
            },
        )
    row = await db.fetchone(
        "SELECT * FROM project_soul_presets WHERE id = ?", (preset_id,)
    )
    await audit_log(
        db, "project.soul_preset_saved",
        actor="operator",
        project_id=project_id,
        payload={
            "preset_id": preset_id,
            "agent_id": body.agent_id,
            "profile_name": body.profile_name,
            "size": len(body.content),
        },
    )
    return SoulPreset(
        id=row["id"],
        project_id=row["project_id"],
        profile_id=row["profile_id"],
        role_name=row["role_name"],
        content=row["content"],
        agent_id=body.agent_id,
        profile_name=body.profile_name,
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


@router.delete(
    "/{project_id}/soul-presets/{agent_id}/{profile_name}",
    status_code=204,
)
async def delete_soul_preset(
    project_id: str, agent_id: str, profile_name: str, request: Request
) -> Response:
    """Remove a SOUL preset (snapshot in DB only — does not touch the
    live SOUL.md on the agent host)."""
    db = request.app.state.db
    profile = await db.fetchone(
        "SELECT id FROM agent_profiles WHERE agent_id = ? AND name = ?",
        (agent_id, profile_name),
    )
    if not profile:
        raise HTTPException(404, f"Profile not found: {agent_id}/{profile_name}")
    await db.execute(
        "DELETE FROM project_soul_presets "
        "WHERE project_id = ? AND profile_id = ?",
        (project_id, profile["id"]),
    )
    await audit_log(
        db, "project.soul_preset_deleted",
        actor="operator",
        project_id=project_id,
        payload={"agent_id": agent_id, "profile_name": profile_name},
    )
    return Response(status_code=204)


@router.post(
    "/{project_id}/soul-presets/apply",
    response_model=list[dict],
)
async def apply_soul_presets(
    project_id: str, body: SoulPresetApply, request: Request
) -> list[dict]:
    """Apply this project's SOUL preset(s) to the target agent profile(s).

    Implementation: write a new profile_configs row (file_path="SOUL.md")
    with the preset's content and status=pending. The wrapper's existing
    apply-config loop picks it up on the next tick (5s) and writes the file
    to `<profile>/SOUL.md`. Audit log: actor=operator:project-activation.

    If `body.agent_id` and `body.profile_name` are set, only that one preset
    is applied. Otherwise all presets for the project are applied.
    """
    import uuid as _uuid

    db = request.app.state.db
    project = await db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    if body.agent_id and body.profile_name:
        profile = await db.fetchone(
            "SELECT * FROM agent_profiles WHERE agent_id = ? AND name = ?",
            (body.agent_id, body.profile_name),
        )
        if not profile:
            raise HTTPException(
                404, f"Profile not found: {body.agent_id}/{body.profile_name}"
            )
        presets = await db.fetchall(
            "SELECT * FROM project_soul_presets "
            "WHERE project_id = ? AND profile_id = ?",
            (project_id, profile["id"]),
        )
    else:
        presets = await db.fetchall(
            "SELECT * FROM project_soul_presets WHERE project_id = ?",
            (project_id,),
        )
    if not presets:
        raise HTTPException(404, "No presets to apply (save some first)")

    written: list[dict] = []
    for p in presets:
        sha = __import__("hashlib").sha256(p["content"].encode()).hexdigest()
        cfg_id = str(_uuid.uuid4())
        await db.insert(
            "profile_configs",
            {
                "id": cfg_id,
                "profile_id": p["profile_id"],
                "file_path": "SOUL.md",
                "desired_sha256": sha,
                "desired_content": p["content"],
                "status": "pending",
            },
        )
        written.append({
            "config_id": cfg_id,
            "profile_id": p["profile_id"],
            "agent_id": body.agent_id,
            "profile_name": body.profile_name,
            "size": len(p["content"]),
        })
    await audit_log(
        db, "project.soul_preset_applied",
        actor="operator",
        project_id=project_id,
        payload={
            "preset_count": len(presets),
            "agent_filter": body.agent_id,
            "profile_filter": body.profile_name,
            "config_ids": [w["config_id"] for w in written],
        },
    )
    return written


# ===== Project chat box (Phase 4+ #25, 2026-07-25) =====
#
# The project page now has an LLM chat assistant that can:
#   1. Analyze the project's current state (tasks + audit summary)
#   2. Generate a list of workflow steps (LLM-suggested tasks for
#      the user to add manually)
#   3. Identify which tasks are "script-able" (don't need an LLM
#      agent; can be a small Python script the operator runs
#      directly, saving tokens)
#   4. Look at recent task results and suggest next steps
#
# Architecture: the chat is a per-project running conversation.
# Each turn is stored in `project_chat_messages` (role, content,
# optional structured suggestions). The endpoint calls the same
# MiniMax M3 endpoint as everything else (cfg.llm). The
# suggestions are extracted from a fenced JSON block the LLM
# emits at the end of its response (if it has concrete actions
# to propose). The user clicks "Apply" on a suggestion and the
# frontend POSTs /api/projects/{id}/chat/apply with the
# suggestion's type + params. This keeps the LLM's tool-calls
# server-side: the assistant can suggest a structure, the user
# confirms, and the action runs through the normal API.

# System prompt for the chat assistant. Rewritten 2026-07-28 for
# chatbox-as-plan-editor (docs/chatbox-plan-editor.md §7.3). The
# LLM's job is now narrow: edit the project's plan workflow
# object, never create tasks or trigger dispatch.
_CHAT_SYSTEM_PROMPT = """\
You are the chatbox plan editor for a single project in
hermes-orchestrator. The operator sees you as a panel in the
project page. Your ONLY job is to help the user design the
project's `plan` — the structured workflow object that gets
materialized into tasks when they click Run on the dashboard.

# What you can do
  - Read the current plan (snapshot below)
  - Suggest edits to plan.steps (add / remove / modify / re-order)
  - Explain what a plan does in plain language
  - Suggest an initial plan from a goal description

# What you MUST NEVER do
  - NEVER create tasks directly (no `create_task` suggestion)
  - NEVER trigger dispatch (no `run` / `replan` / `materialize`)
  - NEVER invent agent_role / skill / tool names that aren't in
    the `agents_info` block below — the Pydantic validator on
    PUT /api/projects/{id}/plan rejects unknown names with 422
  - NEVER call /api/tasks/ or /api/projects/{id}/run directly
  - Run is human-only (user clicks the Run button on the dashboard)

# Snapshot you receive (per turn, see below)
  - `project`: id, name, state, plan_updated_at (echo this in
    every `update_plan` suggestion's `if_match` field for the
    optimistic lock; null if no plan yet)
  - `plan`: current ProjectPlan or null. If null, you're starting
    from scratch — build one
  - `agents_info.agent_roles` / `.skills` / `.tools`: valid names
    you can put in plan.steps[*]. Use ONLY these, otherwise
    PUT /api/projects/{id}/plan will 422
  - `audit_tail`: last 5 audit events for context

# Step fields — what each one means (REQUIRED vs optional)
  Per 2026-07-29: the previous chat version left `action` empty
  and the user had no way to know what each step actually does.
  Every step MUST have a non-empty `action`. Use the canonical
  verb-phrase form, matching workflow_packages.step_template:
    - `name` (REQUIRED, kebab-case, unique): identifier. e.g.
      "fetch-bus-93k-info", "send-telegram-message"
    - `action` (REQUIRED, 2-200 chars, non-whitespace): short
      verb phrase describing what the agent does. Canonical
      examples: "fetch_url", "fetch_data", "navigate_to_folder",
      "summarize", "send_telegram_message", "create_file",
      "read_file", "search_web", "generate_report",
      "extract_data", "transform_json". Either kebab-case OR
      snake_case verbs work. The agent uses this as the
      primary instruction; without it, the step is unrunnable.
    - `agent_role` (REQUIRED if your `agents_info.agent_roles`
      is non-empty): pick one. Or "" to let supervisor pick.
    - `depends_on` (optional, list of step names): upstream
      steps. Empty list = no upstream.
    - `skill` (optional, "" if N/A): canonical skill name from
      `agents_info.skills`. Leave "" if the step uses a generic
      action, not a specific skill.
    - `tool` (optional, "" if N/A): canonical tool name from
      `agents_info.tools`. Leave "" if no specific tool.
    - `required_capability` (optional, "" if N/A): e.g.
      "summarize", "search_web". The supervisor dispatches
      based on this.
    - `params_template` (optional dict): variables the agent
      should fill in. Use {{var}} placeholders for plan vars.
    - `output_path` (optional, "" if N/A): where the agent
      writes its result.

  **CRITICAL**: do NOT leave `action` empty. Even a simple step
  like "fetch bus 93K info and post to Slack" needs a
  non-empty action like "fetch_bus_info_and_post". The action
  is the agent's primary instruction.

# Workflow per user turn
  1. Read the snapshot (provided below)
  2. Apply the user's edit to your in-memory draft of the plan
  3. Validate your draft (see Validation rules below)
  4. Render the CURRENT plan as a DAG (see DAG format below)
  5. Respond in markdown, then end with EXACTLY ONE fenced JSON
     block (the Apply chip) containing the full new plan:
     ```json
     {{"suggestions": [{{"type": "update_plan", "plan": <full ProjectPlan>, "if_match": "<plan_updated_at>"}}]}}
     ```
     - if_match is the plan_updated_at from the snapshot. If
       plan was null, set if_match to null (server treats null
       as "no prior state, just write").
     - The plan field must be the FULL new plan, not a diff.
       The apply endpoint replaces the whole plan.

# Validation rules (your draft must pass)
  - Every step has a non-empty `name` (kebab-case, lowercase
    letters, digits, hyphens; no spaces, no underscores, no
    uppercase)
  - Every step has a non-empty `action` (≥2 chars, not
    whitespace, ≤200 chars) — see "Step fields" above for
    what to put there. Put a short verb phrase.
  - Step names are unique within the plan
  - `depends_on` is a list of OTHER STEP NAMES (never IDs)
  - All `depends_on` names resolve to steps in the plan (no
    dangling references)
  - No cycles (A→B→A)
  - `agent_role` is in `agents_info.agent_roles` (or empty string)
  - `skill` is in `agents_info.skills` (or empty string)
  - `tool` is in `agents_info.tools` (or empty string)
  - The plan's overall `name` is kebab-case (or empty for "no
    plan yet")
  - `version` is the string "1.0"

# DAG render format (plain text, box-drawing)
  Always end your markdown response with the current plan as a
  DAG so the user can see the shape at a glance. Use exactly
  these box-drawing characters: └─, ├─, │.

  Linear chain:
      step-1
      └─ step-2
          └─ step-3

  Branching (fan-out + fan-in via duplicate rendering):
      step-1
      ├─ step-2
      │     └─ step-4
      └─ step-3
            └─ step-4

  Multiple roots:
      step-1
      └─ step-2
      step-3

  If you include agent_role for clarity (optional, use only when
  user asks "who runs each step?"):
      step-1  (super)
      └─ step-2  (win-agent01)

# Drift detection (per turn)
  Each turn you receive a fresh snapshot. The `plan_updated_at`
  field tells you when the plan last changed. If the user has
  been editing the visual editor or another chat, the snapshot
  may differ from the plan in your in-memory draft. If your
  draft's plan_updated_at is older than the snapshot's, your
  draft is stale. Warn the user ("⚠ plan was edited externally,
  reload?") and use the snapshot's plan_updated_at in your
  if_match.

# Conflict (409) on Apply
  The server returns 409 if your if_match is stale. The chat
  UI handles this automatically and offers a 3-way merge. You
  don't need to do anything special — the UI re-fetches the
  current plan and the user re-applies.

# Response format
  - Plain markdown, terse. 1-15 lines.
  - If user asks a question (no edit), no JSON block.
  - If user wants an edit, end with EXACTLY ONE fenced JSON
    block. Never multiple blocks. Never inline JSON.
  - Never include text after the JSON block.

# Available profiles for plan.steps[*].agent_role
{available_profiles_inline}
"""


class ChatRequest(BaseModel):
    """Body for POST /chat."""
    message: str  # user's current turn
    # History is OPTIONAL. If omitted, server reads the last N
    # messages from DB. If provided, server appends to the DB
    # row + uses for the LLM call. Max 30 turns (defensive cap).
    history: list[dict] | None = None  # [{role, content}, ...]


class ChatApplyRequest(BaseModel):
    """Body for POST /chat/apply. The frontend takes a suggestion
    from a chat message and submits it here for execution."""
    suggestion: dict  # the exact suggestion dict from the chat
    # Optional: which chat message this suggestion came from
    # (for audit). If None, we just log the action with the
    # raw suggestion dict.
    message_id: int | None = None


@router.get("/{project_id}/chat")
async def list_chat_messages(
    project_id: str, request: Request, limit: int = 50
) -> dict:
    """List chat messages for a project (newest first).

    Limit defaults to 50; cap at 200 to prevent OOM on a project
    with thousands of turns. Returns the message list + a
    `next_offset` for pagination (we use offset-based not
    cursor-based for simplicity — the chat is small).
    """
    if limit > 200:
        limit = 200
    db = request.app.state.db
    # Confirm project exists (404 vs empty list)
    proj = await db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    rows = await db.fetchall(
        "SELECT id, role, content, suggestions_json, created_at "
        "FROM project_chat_messages WHERE project_id = ? "
        "ORDER BY id ASC LIMIT ?",
        (project_id, limit),
    )
    messages = []
    for r in rows:
        m = {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "created_at": r["created_at"],
        }
        if r["suggestions_json"]:
            try:
                m["suggestions"] = json.loads(r["suggestions_json"])
            except (json.JSONDecodeError, TypeError):
                m["suggestions"] = None
        else:
            m["suggestions"] = None
        messages.append(m)
    return {"messages": messages, "count": len(messages)}


async def _build_chat_context(project_id: str, db) -> dict:
    """Build a JSON snapshot of the project for the LLM.

    Rewritten 2026-07-28 for chatbox-as-plan-editor
    (docs/chatbox-plan-editor.md §7.3). The chat is now plan-focused:
    it edits the project plan, not individual tasks. The snapshot
    includes the current plan (if any), the valid agent_role /
    skill / tool names (so the LLM doesn't invent any), the
    plan_updated_at (for the optimistic lock), and a short audit
    trail for context.

    Capped sizes so the prompt doesn't blow up on a 200-step plan.
    """
    # Lazy import: projects.py is loaded before plans.py in main.py's
    # router mount order, and we want either order to be safe.
    from hermes_orch.api.plans import ProjectPlan, _compute_plan_agents
    proj = await db.fetchone(
        "SELECT id, name, goal, state, max_iterations, current_iteration, "
        "       plan_json, updated_at "
        "FROM projects WHERE id = ?", (project_id,),
    )
    if not proj:
        return None
    # Parse the current plan (may be null or empty string)
    plan_obj = None
    raw_plan = proj.get("plan_json")
    if raw_plan:
        try:
            data = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
            plan_obj = ProjectPlan.model_validate(data).model_dump(mode="json")
        except Exception:
            # Malformed plan — treat as null in the snapshot so the
            # LLM can rewrite it from scratch.
            plan_obj = None
    # Get the valid agent_role / skill / tool names (shared helper).
    agents_info = await _compute_plan_agents(db, project_id)
    # Recent audit: cap at 5 (chat is plan-focused, not audit-focused)
    audit = await db.fetchall(
        "SELECT event_type, payload, created_at FROM audit_log "
        "WHERE project_id = ? ORDER BY id DESC LIMIT 5",
        (project_id,),
    )
    audit_list = []
    for a in audit:
        try:
            payload = json.loads(a["payload"]) if a["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            payload = {"raw": (a["payload"] or "")[:200]}
        audit_list.append({
            "event_type": a["event_type"],
            "summary": {k: str(v)[:200] for k, v in payload.items()},
            "created_at": a["created_at"][:19] if a["created_at"] else None,
        })
    return {
        "project": {
            "id": proj["id"],
            "name": proj["name"],
            "goal": proj["goal"],
            "state": proj["state"],
            "max_iterations": proj["max_iterations"],
            "current_iteration": proj["current_iteration"],
        },
        "plan": plan_obj,
        # plan_updated_at is the value the LLM must echo in
        # update_plan.if_match for the optimistic lock. None if
        # the project has no plan yet (LLM passes null and the
        # server treats it as "no prior state, just write").
        "plan_updated_at": proj.get("updated_at"),
        "agents_info": agents_info,
        "audit_tail": audit_list,
    }


_SUGGESTION_RE = re.compile(
    r"```json\s*\n?(.*?)\n?\s*```",
    re.DOTALL,
)


def _extract_suggestions(llm_text: str) -> tuple[str, list[dict] | None]:
    """Split LLM response into (display_text, suggestions).

    Suggestions are extracted in this order:
      1. LAST fenced ```json``` block in the response
         (the standard case per the system prompt)
      2. LAST inline JSON object matching {"suggestions": [...]}
         (LLM-fooling pattern #9: LLM sometimes writes inline
          JSON like '"type": "create_task", "name": "..."'
          without the fence)
      3. NOTHING: return (text, None). The UI shows a friendly
         "Reformat" button so the user can ask the LLM to
         try again with proper formatting.

    Returns (text_without_json_block, suggestions_list_or_None).
    """
    if not llm_text:
        return llm_text, None
    # 1) Try fenced JSON block first
    matches = list(_SUGGESTION_RE.finditer(llm_text))
    if matches:
        last = matches[-1]
        raw_json = last.group(1).strip()
        try:
            parsed = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            suggestions = parsed.get("suggestions")
            if isinstance(suggestions, list):
                valid = [s for s in suggestions
                         if isinstance(s, dict) and isinstance(s.get("type"), str)]
                if valid:
                    display = (llm_text[: last.start()] + llm_text[last.end():]).strip()
                    return display, valid
    # 2) Fallback: inline JSON object with "suggestions" key.
    # Use a regex to find a {"suggestions": [...]} pattern.
    # The LLM might write inline like:
    #   type: "create_task", name: "foo", ...
    # OR
    #   {"type": "create_task", "name": "foo"}
    # The regex looks for the brace-delimited object form.
    inline_re = re.compile(
        r'(\{\s*"suggestions"\s*:\s*\[.*?\]\s*\})',
        re.DOTALL,
    )
    inline_matches = list(inline_re.finditer(llm_text))
    if inline_matches:
        last = inline_matches[-1]
        raw_json = last.group(1)
        try:
            parsed = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            suggestions = parsed.get("suggestions")
            if isinstance(suggestions, list):
                valid = [s for s in suggestions
                         if isinstance(s, dict) and isinstance(s.get("type"), str)]
                if valid:
                    # Don't strip from display (the inline form is
                    # part of the prose; stripping it would leave
                    # an awkward gap). Just return the text as-is.
                    return llm_text.strip(), valid
    return llm_text.strip(), None


def _render_dag_section(suggestions: list | None) -> str:
    """Render a DAG code block from any `update_plan` suggestions.

    Added 2026-07-28 for chatbox-as-plan-editor. If any suggestion
    in the list is of type `update_plan` and has a non-empty
    `steps` array, render the plan as a plain-text DAG (via
    `hermes_orch.dag_render.render_plan_dag`) and return a
    markdown-fenced code block to append to the assistant's
    response. If no update_plan suggestion is present, or all
    such suggestions are empty, return "".

    The output is wrapped in a ```text fence so the frontend can
    render it in monospace <pre>. Box-drawing characters
    (└─ ├─ │) are not interpreted as markdown.

    Defensive: any exception (e.g. import error) returns "" so
    the chat endpoint still works.
    """
    if not suggestions:
        return ""
    try:
        from hermes_orch.dag_render import render_plan_dag
    except ImportError:
        return ""
    dag_blocks: list[str] = []
    for s in suggestions:
        if not isinstance(s, dict):
            continue
        if s.get("type") != "update_plan":
            continue
        plan_obj = s.get("plan")
        if not isinstance(plan_obj, dict):
            continue
        steps = plan_obj.get("steps")
        if not isinstance(steps, list) or not steps:
            continue
        dag_text = render_plan_dag(steps)
        dag_blocks.append(dag_text)
    if not dag_blocks:
        return ""
    return "\n\nCurrent plan:\n```text\n" + "\n\n".join(dag_blocks) + "\n```"


@router.post("/{project_id}/chat")
async def chat_with_project(
    project_id: str, body: ChatRequest, request: Request
) -> dict:
    """Send a message to the LLM chat assistant.

    Pipeline:
      1. Build the project context snapshot (capped sizes)
      2. Load recent chat history from DB (or use body.history)
      3. Call LLM with system prompt + context + history + new msg
      4. Extract suggestions (last ```json``` fenced block)
      5. Persist user + assistant messages with suggestions
      6. Return {message, suggestions, message_id, history_count}
    """
    if not body.message or not body.message.strip():
        raise HTTPException(400, "message is required")
    db = request.app.state.db
    cfg = request.app.state.config
    proj = await db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    # Build context
    ctx = await _build_chat_context(project_id, db)
    if ctx is None:
        raise HTTPException(404, f"Project not found: {project_id}")
    # Load recent history (last 30 turns). If body.history is
    # provided, we trust the frontend to have the right list
    # (e.g. after the operator cleared the conversation client-
    # side). Otherwise we read from DB.
    history = body.history
    if history is None:
        rows = await db.fetchall(
            "SELECT role, content FROM project_chat_messages "
            "WHERE project_id = ? ORDER BY id DESC LIMIT 30",
            (project_id,),
        )
        history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    if len(history) > 30:
        history = history[-30:]
    # Persist user message
    now = _now_iso()
    user_msg_id = await db.insert("project_chat_messages", {
        "project_id": project_id,
        "role": "user",
        "content": body.message,
        "suggestions_json": None,
        "created_at": now,
    })
    # Phase 2 (2026-07-29): also append to chat.jsonl for inspection
    _append_chat_jsonl(
        request, project_id, user_msg_id, "user", body.message, None, now,
    )
    # Build LLM messages
    # Fill the {available_profiles_inline} placeholder in the
    # system prompt with the comma-separated profile names from
    # the project context. This stops the LLM from inventing
    # names like 'google-drive-uploader' or 'script-runner' in
    # plan.steps[*].agent_role (the Pydantic validator on PUT
    # /plan 422s on unknown names, so the LLM MUST use a real one).
    # Snapshot uses 'agents_info' (2026-07-28). Note: agents_info
    # has the same shape as the /plan/agents endpoint response —
    # agent_roles is a flat list of strings, not a list of dicts.
    profile_names = list(ctx.get("agents_info", {}).get("agent_roles", []))
    if profile_names:
        inline = ", ".join(f"`{n}`" for n in profile_names)
    else:
        inline = "(no profiles registered — leave agent_role empty)"
    system_prompt = _CHAT_SYSTEM_PROMPT.replace(
        "{available_profiles_inline}", inline
    )
    llm_messages = [
        {"role": "system", "content": system_prompt + "\n\nProject snapshot:\n" + json.dumps(ctx, ensure_ascii=False, indent=2)},
    ]
    for h in history:
        role = h.get("role")
        content = h.get("content", "")
        if role in ("user", "assistant") and content:
            llm_messages.append({"role": role, "content": content})
    llm_messages.append({"role": "user", "content": body.message})
    # LLM call
    llm_cfg = cfg.get("llm", {})
    base_url = (llm_cfg.get("base_url") or "https://api.minimax.io/v1").rstrip("/")
    api_key = llm_cfg.get("api_key") or ""
    model = llm_cfg.get("model") or "MiniMax-M3"
    timeout = float(llm_cfg.get("timeout_seconds") or 90)
    if not api_key:
        raise HTTPException(
            503, "LLM api_key not configured — set llm.api_key in config.yaml"
        )
    payload = {
        "model": model,
        "messages": llm_messages,
        "temperature": 0.4,
        "max_tokens": 1500,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    import logging as _logging
    log = _logging.getLogger(__name__)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base_url}/chat/completions",
                json=payload, headers=headers,
            )
        if r.status_code != 200:
            raise HTTPException(502, f"LLM returned HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise HTTPException(502, f"LLM response shape unexpected: {e}")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(502, "LLM returned empty content")
    except httpx.HTTPError as e:
        log.warning(f"chat LLM call failed for {project_id}: {e}")
        raise HTTPException(502, f"LLM unreachable: {e}")
    # Strip <think> traces (MiniMax M3 emits them before the answer)
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    # Extract suggestions
    display_text, suggestions = _extract_suggestions(text)
    # Render the DAG from any update_plan suggestions and append it
    # to the assistant message so the user can see the current shape
    # without expanding the JSON suggestion chip. (Added 2026-07-28
    # for chatbox-as-plan-editor.)
    try:
        dag_section = _render_dag_section(suggestions)
        if dag_section:
            display_text = display_text + dag_section
    except Exception as e:
        log.warning(f"failed to render DAG for chat: {e}")
    # Persist assistant message
    sugg_json = json.dumps(suggestions) if suggestions else None
    assistant_now = _now_iso()
    cursor = await db.execute(
        "INSERT INTO project_chat_messages "
        "(project_id, role, content, suggestions_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, "assistant", display_text, sugg_json, assistant_now),
    )
    msg_id = cursor.lastrowid if hasattr(cursor, "lastrowid") else None
    # Phase 2 (2026-07-29): also append to chat.jsonl for inspection
    _append_chat_jsonl(
        request, project_id, msg_id, "assistant", display_text, suggestions, assistant_now,
    )
    await audit_log(
        db, "project.chat_message",
        actor="operator",
        project_id=project_id,
        payload={"message_id": msg_id, "has_suggestions": bool(suggestions)},
    )
    return {
        "message": display_text,
        "suggestions": suggestions or [],
        "message_id": msg_id,
        "history_count": len(history) + 2,  # user + assistant just added
    }


@router.post("/{project_id}/chat/clear")
async def clear_chat(project_id: str, request: Request) -> dict:
    """Clear all chat history for a project. Audit logs the
    clear with the count of messages removed."""
    db = request.app.state.db
    proj = await db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    cur = await db.execute(
        "DELETE FROM project_chat_messages WHERE project_id = ?",
        (project_id,),
    )
    removed = cur.rowcount if hasattr(cur, "rowcount") else 0
    await audit_log(
        db, "project.chat_cleared",
        actor="operator",
        project_id=project_id,
        payload={"removed_count": removed},
    )
    return {"project_id": project_id, "removed": removed}


@router.get("/{project_id}/chat.jsonl")
async def get_chat_jsonl(project_id: str, request: Request):
    """Return the project's chat.jsonl file as plain text.

    Added 2026-07-29 (Phase 2). The chat history is also written
    to `projects/{id}/chat.jsonl` for operator inspection
    (cat / tail / grep) and easy backup. This endpoint exposes
    the same content over HTTP so the dashboard's "View chat log"
    button can open it in a new tab.

    Returns 404 if the file does not exist (no chat messages yet
    for this project). Streams the file as plain text.
    """
    from fastapi.responses import PlainTextResponse
    pdir = _project_dir(request, project_id)
    path = pdir / "chat.jsonl"
    if not path.exists():
        raise HTTPException(404, f"No chat log yet for {project_id}")
    # Read up to a sane cap (1MB) to avoid OOM on huge logs.
    MAX_BYTES = 1 * 1024 * 1024
    size = path.stat().st_size
    if size > MAX_BYTES:
        # Return only the last 1MB; tell the client via header.
        # After seeking back 1MB from the end, the position is
        # likely in the middle of a JSONL record — advance to the
        # next newline so the first returned line is complete.
        with open(path, "rb") as f:
            f.seek(-MAX_BYTES, 2)
            data = f.read()
        nl = data.find(b"\n")
        if nl >= 0:
            data = data[nl + 1:]
        return PlainTextResponse(
            data.decode("utf-8", errors="replace"),
            media_type="text/plain; charset=utf-8",
            headers={"X-Chat-Log-Truncated": "1", "X-Chat-Log-Original-Size": str(size)},
        )
    data = path.read_bytes()
    return PlainTextResponse(
        data.decode("utf-8", errors="replace"),
        media_type="text/plain; charset=utf-8",
    )


@router.post("/{project_id}/chat/reformat")
async def reformat_chat_message(
    project_id: str, body: ChatRequest, request: Request
) -> dict:
    """Dedicated endpoint to reformat the LLM's plain-text action
    description into a structured suggestion. LLM-fooling pattern
    #9: the LLM sometimes describes actions in text without a
    JSON block, so the UI can't show Apply buttons. This
    endpoint makes a FRESH LLM call with a focused, single-purpose
    system prompt: 'return ONLY a JSON object with a suggestions
    array, no other text'. This is far more reliable than
    appending a reformat suffix to the chat (the chat has too
    much other context for the LLM to consistently follow
    format rules).

    The result is persisted to project_chat_messages as a new
    assistant turn so the history stays complete.

    Body: {message: str} — the last user message (or the action
    they want reformatted). We re-ask the LLM to format THIS
    request as a structured suggestion.
    """
    if not body.message or not body.message.strip():
        raise HTTPException(400, "message is required")
    db = request.app.state.db
    cfg = request.app.state.config
    proj = await db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    # Build a focused, single-purpose system prompt. The LLM's
    # job is JUST to convert the user's request into structured
    # suggestions. No analysis, no explanation, no other text.
    reformat_system = """\
You are a JSON formatter for a workflow assistant. The user
just asked the assistant to do something, but the assistant
described it in plain text instead of returning a JSON block.

YOUR JOB: convert the user's request into a structured JSON
object with a "suggestions" array. Return ONLY the JSON object.
No other text. No preamble. No explanation. No markdown fence.

Allowed suggestion types:
  - update_plan: {type, plan: <ProjectPlan>, if_match: "<updated_at>"}
    where <ProjectPlan> matches the project's plan schema
    (version, name, description, trigger, variables, steps[]).
    if_match is the updated_at value from the most recent
    GET /api/projects/{id}/plan (optimistic lock).

If the user's request is just a question (no concrete action),
return {"suggestions": []}.

Pick agent_role from this list (verbatim, no other names):
{available_profiles_inline}

Return ONLY valid JSON. Start with { and end with }. No other
characters before or after the JSON.
"""
    profile_names = await db.fetchall(
        "SELECT name FROM agent_profiles ORDER BY name"
    )
    names = [p["name"] for p in profile_names]
    inline = ", ".join(f"`{n}`" for n in names) if names else "(no profiles registered)"
    reformat_system = reformat_system.replace(
        "{available_profiles_inline}", inline
    )
    # LLM call
    llm_cfg = cfg.get("llm", {})
    base_url = (llm_cfg.get("base_url") or "https://api.minimax.io/v1").rstrip("/")
    api_key = llm_cfg.get("api_key") or ""
    model = llm_cfg.get("model") or "MiniMax-M3"
    timeout = float(llm_cfg.get("timeout_seconds") or 60)
    if not api_key:
        raise HTTPException(503, "LLM api_key not configured")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": reformat_system},
            {"role": "user", "content": body.message},
        ],
        "temperature": 0.2,  # very low — we want deterministic JSON
        "max_tokens": 800,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    import logging as _logging
    log = _logging.getLogger(__name__)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base_url}/chat/completions",
                json=payload, headers=headers,
            )
        if r.status_code != 200:
            raise HTTPException(502, f"LLM returned HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise HTTPException(502, f"LLM response shape unexpected: {e}")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(502, "LLM returned empty content")
    except httpx.HTTPError as e:
        log.warning(f"chat reformat LLM call failed for {project_id}: {e}")
        raise HTTPException(502, f"LLM unreachable: {e}")
    # Strip <think> blocks (MiniMax M3 emits them before the answer)
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    # Strip markdown fences in case the LLM still added them
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # Parse the JSON. The system prompt is very emphatic, but
    # the LLM may still wrap in fences or add preamble. Try
    # to find the JSON object directly.
    parsed = None
    # First try direct parse
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # Then try to find { ... } in the text
    if parsed is None:
        m = re.search(r"(\{.*\})", text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
            except (json.JSONDecodeError, TypeError):
                pass
    if not isinstance(parsed, dict):
        raise HTTPException(502, f"LLM did not return valid JSON: {text[:200]}")
    suggestions = parsed.get("suggestions")
    if not isinstance(suggestions, list):
        suggestions = []
    # Validate each suggestion
    valid = [s for s in suggestions
             if isinstance(s, dict) and isinstance(s.get("type"), str)]
    # Build a brief user-facing message describing what we did
    if valid:
        message = f"Reformatted as {len(valid)} action(s). Click Apply on any to execute."
    else:
        message = "No structured actions found in the request. The user may have asked a question rather than an action."
    # Persist as a new assistant turn
    sugg_json = json.dumps(valid) if valid else None
    now = _now_iso()
    cursor = await db.execute(
        "INSERT INTO project_chat_messages "
        "(project_id, role, content, suggestions_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, "assistant", message, sugg_json, now),
    )
    msg_id = cursor.lastrowid if hasattr(cursor, "lastrowid") else None
    # Phase 2 (2026-07-29): also append to chat.jsonl
    _append_chat_jsonl(
        request, project_id, msg_id, "assistant", message, valid, now,
    )
    await audit_log(
        db, "project.chat_reformatted",
        actor="operator",
        project_id=project_id,
        payload={"message_id": msg_id, "suggestion_count": len(valid)},
    )
    return {
        "message": message,
        "suggestions": valid,
        "message_id": msg_id,
    }


@router.post("/{project_id}/chat/apply")
async def apply_chat_suggestion(
    project_id: str, body: ChatApplyRequest, request: Request
) -> dict:
    """Apply a structured suggestion from a chat message.

    Added 2026-07-28 (chatbox-as-plan-editor, Phase 0):
    The only supported suggestion type is now `update_plan`. This
    replaces the entire plan via PUT /api/projects/{id}/plan with
    the suggestion's plan object. Optimistic lock (If-Match) is
    taken from suggestion.if_match (the LLM should echo the
    updated_at it last read). Lock failures (409) propagate to
    the client so the chatbox can show a 3-way merge.

    Removed (2026-07-28):
      - create_task: chatbox no longer creates tasks directly. The
        LLM edits the plan, and the user clicks Run on the dashboard
        to materialize plan → tasks.
      - run: dispatch is human-only (Run button on dashboard).
      - replan: superseded by update_plan (the LLM produces a fresh
        plan object, not just a new goal string).

    Allowed types: ["update_plan"].
    """
    # Local import: avoid module-level cycle (projects.py is loaded
    # before plans.py in main.py's router mount order; keeping this
    # lazy means either order is safe).
    from hermes_orch.api.plans import (
        ProjectPlan,
        ProjectPlanUpdate,
        put_project_plan,
    )
    s = body.suggestion
    if not isinstance(s, dict) or not isinstance(s.get("type"), str):
        raise HTTPException(400, "suggestion must be a dict with a 'type' field")
    stype = s["type"]
    if stype != "update_plan":
        raise HTTPException(
            400,
            f"unknown suggestion type: {stype!r}. Allowed: update_plan.",
        )
    plan_data = s.get("plan")
    if not isinstance(plan_data, dict):
        raise HTTPException(400, "update_plan suggestion missing 'plan' object")
    # Validate the plan shape early (Pydantic) so we fail fast with a
    # clear 422 instead of letting put_project_plan do it.
    try:
        plan = ProjectPlan.model_validate(plan_data)
    except Exception as e:
        raise HTTPException(422, f"update_plan: invalid plan: {e}")
    # If-Match: optional, but if present must be a string
    if_match = s.get("if_match")
    if if_match is not None and not isinstance(if_match, str):
        raise HTTPException(
            400, "update_plan 'if_match' must be a string when provided"
        )
    # Delegate to put_project_plan. It handles:
    #   - 404 for unknown project
    #   - optimistic lock (409 with current_plan in body)
    #   - audit log with actor=operator:chat
    # HTTPException from put_project_plan (e.g. 409) propagates to
    # the chatbox client so it can show a 3-way merge UI.
    result = await put_project_plan(
        project_id=project_id,
        body=ProjectPlanUpdate(plan=plan),
        request=request,
        if_match=if_match,
        audit_actor="operator:chat",
    )
    return {
        "applied": True,
        "type": "update_plan",
        "project_id": result.project_id,
        "updated_at": result.updated_at,
        "step_count": len(result.plan.steps) if result.plan else 0,
    }


# ===== Task Progress Monitor (T2, 2026-07-29) =====
#
# Powers the dashboard's real-time status badges + side panel.
# Polled by the frontend every 5s (per design doc §4).
#
# Endpoints:
#   GET /api/projects/{id}/tasks/{task_id}/status
#     → single task's loop_status + liveness info
#   GET /api/projects/{id}/tasks/running
#     → all running tasks' loop_status (for initial load + polling)
#
# `loop_status` is one of ok / slow / stuck / unknown (see
# src/hermes_orch/core/loop_status.py for semantics). 404 is
# returned if the project or task does not exist, or if the
# task does not belong to the project (multi-tenant guard).


def _task_status_to_dict(task_row: dict, db_path: Path) -> dict:
    """Compute loop_status for a task row and return a JSON-friendly
    dict. The DB row comes from a SELECT * FROM tasks query."""
    ls = compute_loop_status(task_row, db_path)
    return {
        "task_id": task_row["id"],
        "project_id": task_row["project_id"],
        "name": task_row.get("name", ""),
        "agent_role": task_row.get("agent_role", ""),
        "status": task_row.get("status", ""),
        "loop_status": ls.status,
        "loop_reason": ls.reason,
        "duration_s": ls.duration_s,
        "last_event_age_s": ls.last_event_age_s,
        "started_at": task_row.get("started_at"),
        "last_liveness_at": task_row.get("last_liveness_at"),
    }


@router.get("/{project_id}/tasks/{task_id}/status")
async def get_task_status(
    project_id: str, task_id: str, request: Request
) -> dict:
    """Live progress status for a single task.

    Returns 404 if the project or task does not exist, or if the
    task is not part of the project. Used by the dashboard's
    inline-expand detail + side panel (T4)."""
    db = request.app.state.db
    # Verify project exists (404 vs 200-with-empty is ambiguous;
    # we want a hard 404 so the frontend can show a clear error)
    proj = await db.fetchone(
        "SELECT id FROM projects WHERE id = ?", (project_id,)
    )
    if not proj:
        raise HTTPException(404, f"project {project_id} not found")
    # Fetch the task and scope-check it
    task = await db.fetchone(
        "SELECT * FROM tasks WHERE id = ? AND project_id = ?",
        (task_id, project_id),
    )
    if not task:
        raise HTTPException(
            404,
            f"task {task_id} not found in project {project_id}",
        )
    return _task_status_to_dict(task, db.db_path)


@router.get("/{project_id}/tasks/running")
async def list_running_tasks(project_id: str, request: Request) -> dict:
    """Live progress status for all running tasks in a project.

    Returns a list of status dicts (same shape as the single-task
    endpoint). Used by the dashboard on initial load and for 5s
    polling refresh."""
    db = request.app.state.db
    proj = await db.fetchone(
        "SELECT id FROM projects WHERE id = ?", (project_id,)
    )
    if not proj:
        raise HTTPException(404, f"project {project_id} not found")
    rows = await db.fetchall(
        "SELECT * FROM tasks "
        "WHERE project_id = ? AND status = 'running' "
        "ORDER BY started_at ASC NULLS LAST, id ASC",
        (project_id,),
    )
    return {
        "project_id": project_id,
        "tasks": [_task_status_to_dict(r, db.db_path) for r in rows],
        "count": len(rows),
    }


@router.post("/{project_id}/tasks/{task_id}/cancel")
async def cancel_project_task(
    project_id: str, task_id: str, request: Request
) -> dict:
    """Cancel a task with project-scope guard (IDOR-safe).

    The wrapper around /api/tasks/{id}/cancel that also verifies
    the task belongs to the given project. Returns the full
    updated task (same shape as the original endpoint) plus a
    `was_running` flag so the UI can show a clear "cancelled"
    confirmation without re-fetching.

    Used by the Task Progress Monitor side panel (T4) — the
    "Cancel" button calls this endpoint instead of the unscoped
    one so a misclick on the wrong project can never cancel
    someone else's task."""
    db = request.app.state.db
    # Verify project + task ownership in one query (IDOR guard)
    task = await db.fetchone(
        "SELECT * FROM tasks WHERE id = ? AND project_id = ?",
        (task_id, project_id),
    )
    if not task:
        raise HTTPException(
            404,
            f"task {task_id} not found in project {project_id}",
        )
    was_running = task["status"] == "running"
    # Delegate to the core cancel helper. It re-checks state,
    # updates the row, frees the profile, and writes audit log.
    # actor="operator:ui" distinguishes UI-driven cancels from
    # CLI / API ones for later analytics.
    row = await _do_cancel_task(
        db, task_id, actor="operator:ui"
    )
    return {
        "task": _row_to_task_dict(row),
        "was_running": was_running,
        "cancelled_at": row.get("ended_at"),
    }


def _row_to_task_dict(row: dict) -> dict:
    """Convert a tasks-table row to a JSON-friendly dict.

    Local reimplementation (vs importing the original
    _row_to_task from tasks.py) because that helper returns a
    pydantic Task model with strict field validation; for the
    cancel response we want a permissive dict that includes
    ended_at and any other cancel-relevant fields even if the
    schema evolves."""
    return {
        "id": row.get("id"),
        "project_id": row.get("project_id"),
        "name": row.get("name", ""),
        "status": row.get("status", ""),
        "agent_role": row.get("agent_role", ""),
        "started_at": row.get("started_at"),
        "last_liveness_at": row.get("last_liveness_at"),
        "ended_at": row.get("ended_at"),
        "error": row.get("error"),
    }


# ===== Live Output Streaming (v1.1, 2026-07-29) =====
#
# Powers the "see the agent's live output" feature in the Task
# Progress Monitor side panel. The wrapper (hermes-agent) tails
# hermes's stdout/stderr file in a background thread and POSTs
# chunks here as it appears. The frontend (task_progress.js)
# polls the GET endpoint every 2s and appends new chunks to
# the per-task streaming view.
#
# Two endpoints, both project-scoped (IDOR-safe) and
# task-scoped (a wrapper can only push for a task it owns):
#
#   POST /api/projects/{id}/tasks/{task_id}/output-chunk
#     Body: {seq: int, text: str, stream: "stdout"|"stderr"}
#     Headers: X-Agent-Id (must match task.assigned_agent_id)
#     Action: write audit_log row (event_type="agent.output_chunk")
#     Returns: {ok, id} where id is audit_log.id (wrapper uses it
#              to detect gaps on retry)
#
#   GET /api/projects/{id}/tasks/{task_id}/output?since=N
#     Returns: list of chunks with id > N, ordered by id ASC
#     Capped at 500 chunks per request (defensive — a misbehaving
#     wrapper could spam millions of rows; pagination via since=).


from fastapi import Header


@router.post("/{project_id}/tasks/{task_id}/output-chunk")
async def post_output_chunk(
    project_id: str,
    task_id: str,
    request: Request,
    agent_id: str = Depends(require_hmac_auth),
) -> dict:
    """Wrapper → server: push a live output chunk for a running task.

    The wrapper's tailing thread (in hermes-orch-agent) watches
    hermes's stdout/stderr file and POSTs each chunk here. We
    write an audit_log row (event_type="agent.output_chunk") and
    return the new row's id so the wrapper can detect dropped
    writes on retry (e.g. if the POST times out, the wrapper
    can re-send with the same seq; the frontend de-dupes by seq).

    Auth: minimal — the agent must be the one currently assigned
    to the task. Real HMAC verification is TODO (matches the
    /api/agents/{id}/heartbeat MVP per the auth design doc §6.1).
    """
    db = request.app.state.db
    task = await db.fetchone(
        "SELECT id, project_id, assigned_agent_id FROM tasks "
        "WHERE id = ? AND project_id = ?",
        (task_id, project_id),
    )
    if not task:
        raise HTTPException(
            404, f"task {task_id} not found in project {project_id}"
        )
    if task.get("assigned_agent_id") != agent_id:
        raise HTTPException(
            403,
            f"X-Agent-Id ({agent_id}) is not the owner of task {task_id}",
        )
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    seq = body.get("seq")
    text = body.get("text", "")
    stream = body.get("stream", "stdout")
    if seq is None or not isinstance(seq, int):
        raise HTTPException(400, "seq is required and must be int")
    if not isinstance(text, str):
        raise HTTPException(400, "text must be a string")
    if stream not in ("stdout", "stderr"):
        raise HTTPException(400, "stream must be 'stdout' or 'stderr'")
    # Defensive cap: 64KB per chunk. A misbehaving wrapper trying
    # to push 100MB in one go shouldn't OOM the server.
    if len(text) > 65536:
        text = text[:65536]
    await audit_log(
        db,
        "agent.output_chunk",
        actor=f"agent:{agent_id}",
        project_id=project_id,
        task_id=task_id,
        agent_id=agent_id,
        payload={"seq": seq, "text": text, "stream": stream},
    )
    # Return the new row's id so the wrapper can confirm the write
    last = await db.fetchone("SELECT last_insert_rowid() AS id")
    return {"ok": True, "id": last["id"], "seq": seq}


@router.get("/{project_id}/tasks/{task_id}/output")
async def get_task_output(
    project_id: str,
    task_id: str,
    request: Request,
    since: int = 0,
) -> dict:
    """Frontend → server: pull live output chunks for a task.

    Returns all audit_log rows of type agent.output_chunk for this
    task with id > `since` (caller passes the last id they saw;
    default 0 returns everything). Capped at 500 rows per request
    — if there are more, the client should retry with the
    returned `next_since` until empty.

    No agent-auth: the dashboard is already inside the trusted
    network; the project-scope guard (WHERE project_id=?) prevents
    IDOR. We do enforce the task exists (404 otherwise) so callers
    get a clear error instead of an empty list for a typo.
    """
    db = request.app.state.db
    task = await db.fetchone(
        "SELECT id FROM tasks WHERE id = ? AND project_id = ?",
        (task_id, project_id),
    )
    if not task:
        raise HTTPException(
            404, f"task {task_id} not found in project {project_id}"
        )
    rows = await db.fetchall(
        "SELECT id, agent_id, payload, created_at "
        "FROM audit_log "
        "WHERE task_id = ? AND event_type = 'agent.output_chunk' "
        "AND id > ? "
        "ORDER BY id ASC LIMIT 500",
        (task_id, int(since)),
    )
    chunks = []
    for r in rows:
        try:
            p = json.loads(r["payload"]) if r["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            p = {}
        # Strip ANSI escape codes (color, cursor moves) from the text
        # before returning. The audit_log keeps the raw chunk for
        # debugging; the dashboard only needs the human-readable
        # text. Without this, every line starts with sequences like
        # [1;38;2;255;215;0m that hermes emits for terminal colors.
        chunks.append({
            "id": r["id"],
            "seq": p.get("seq"),
            "text": _strip_ansi(p.get("text", "")),
            "stream": p.get("stream", "stdout"),
            "created_at": r["created_at"],
        })
    return {
        "project_id": project_id,
        "task_id": task_id,
        "chunks": chunks,
        "count": len(chunks),
        "next_since": chunks[-1]["id"] if chunks else int(since),
    }


# ===== Bulk task state endpoint (v1.3 hot-fix, 2026-07-29) =====
#
# Powers the dashboard's 5s polling so the UI can update not just
# the loop_status badge (v1) but ALSO the status pill text/class
# (running → done / failed / cancelled). Without this, the row
# stays visually "running" forever after the task finishes,
# because v1 only polled /tasks/running (which excludes non-
# running tasks by definition).
#
# Returns a light shape — only the fields the UI needs to update
# one row. Capped at the project_id-scoped visible tasks (no
# archive=1, no pagination — projects in this orchestrator are
# typically tens to low-hundreds of tasks).


@router.get("/{project_id}/tasks/state")
async def get_project_task_states(project_id: str, request: Request) -> dict:
    """Light shape of all visible tasks in a project, for the
    dashboard's 5s polling loop.

    Returns each task's current status (running / done / failed /
    cancelled / pending / assigned), loop_status (ok / slow /
    stuck / looping / unknown), and the liveness info needed for
    the inline expand panel. Computed per-task (status-aware:
    non-running tasks get loop_status="ok" with reason="task is X"
    per compute_loop_status semantics).
    """
    db = request.app.state.db
    proj = await db.fetchone(
        "SELECT id FROM projects WHERE id = ?", (project_id,)
    )
    if not proj:
        raise HTTPException(404, f"project {project_id} not found")
    rows = await db.fetchall(
        "SELECT * FROM tasks WHERE project_id = ? AND archived = 0 "
        "ORDER BY created_at DESC",
        (project_id,),
    )
    states = [_task_status_to_dict(r, db.db_path) for r in rows]
    return {
        "project_id": project_id,
        "tasks": states,
        "count": len(states),
    }


# ===== Tool-call events for looping detection (v1.2, 2026-07-29) =====
#
# The wrapper emits one event per tool call (e.g. each row of the
# form `┊ 💻 $ <command>` that hermes writes). compute_loop_status
# queries these to detect when the agent is making the same call
# over and over — a real "loop" that v1 couldn't see (it only had
# heartbeat liveness, not content-level repetition).


@router.post("/{project_id}/tasks/{task_id}/tool-call")
async def post_tool_call(
    project_id: str,
    task_id: str,
    request: Request,
    agent_id: str = Depends(require_hmac_auth),
) -> dict:
    """Wrapper → server: emit one tool-call event for looping analysis.

    Body: {tool: str, signature: str}
      - tool:      short human-readable name (e.g. "shell", "read_file")
      - signature: stable hash of the call's args. We use this (not the
                   raw args) to keep audit_log small + avoid leaking
                   potentially-sensitive data into the DB. The same
                   call with the same args produces the same signature.

    The server writes an audit_log row (event_type="agent.tool_call")
    and returns the new row's id. No GET endpoint — compute_loop_status
    queries audit_log directly when it runs.
    """
    db = request.app.state.db
    task = await db.fetchone(
        "SELECT id, project_id, assigned_agent_id FROM tasks "
        "WHERE id = ? AND project_id = ?",
        (task_id, project_id),
    )
    if not task:
        raise HTTPException(
            404, f"task {task_id} not found in project {project_id}"
        )
    if task.get("assigned_agent_id") != agent_id:
        raise HTTPException(
            403, f"X-Agent-Id ({agent_id}) is not the owner of task {task_id}"
        )
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    tool = body.get("tool", "")
    signature = body.get("signature", "")
    if not isinstance(tool, str) or not tool:
        raise HTTPException(400, "tool is required and must be a non-empty string")
    if not isinstance(signature, str) or not signature:
        raise HTTPException(
            400, "signature is required and must be a non-empty string"
        )
    # Defensive caps: tool name (256 chars), signature (64 chars — usually
    # 8-16 hex of a SHA256 prefix). Bigger means a misbehaving wrapper.
    tool = tool[:256]
    signature = signature[:64]
    await audit_log(
        db,
        "agent.tool_call",
        actor=f"agent:{agent_id}",
        project_id=project_id,
        task_id=task_id,
        agent_id=agent_id,
        payload={"tool": tool, "signature": signature},
    )
    last = await db.fetchone("SELECT last_insert_rowid() AS id")
    return {"ok": True, "id": last["id"]}

