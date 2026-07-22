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
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from hermes_orch.core.audit import audit_log
from hermes_orch.utils import now_iso as _now_iso

router = APIRouter()


# ===== Pydantic models =====


class ProjectCreate(BaseModel):
    goal: str | None = None  # Optional: omit for manual mode (you add tasks yourself)
    name: str | None = None
    mode: str = "auto"  # auto = planner generates tasks from goal; manual = no goal, you add tasks
    # Q3: system-level project handle. Defaults are project-driven (not yet
    # used by the supervisor loop; populated when the user opts into
    # iterative project mode).
    coordinator_role: str | None = None  # e.g. "super" or "auto" (LLM picks)
    accept_criteria: str | None = None  # plain-text "definition of done"
    deliverable_path: str | None = None  # final artifact path (e.g. "report_v2.md")
    max_iterations: int = 0  # 0 = no cap; otherwise max replan rounds


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

    Two modes:
    - auto (default): requires goal; supervisor calls LLM planner to generate
      tasks, then transitions to 'ready'.
    - manual: no goal needed; project starts in 'ready' state. You add tasks
      one at a time via POST /api/tasks/ {project_id, agent_role, action, ...}.
      Useful for: interactive workflows, testing, exploratory tinkering.
    """
    db = request.app.state.db
    project_id = _project_id()
    now = _now_iso()

    # Determine initial state
    is_manual = body.mode == "manual" or not (body.goal or "").strip()
    initial_state = "ready" if is_manual else "planning"
    initial_goal = body.goal or ""

    await db.insert(
        "projects",
        {
            "id": project_id,
            "name": body.name,
            "goal": initial_goal,
            "state": initial_state,
            # Q3 iteration tracking (all optional / empty for ad-hoc projects)
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
    goal_section = f"## Goal\n\n{initial_goal}\n" if initial_goal else "## Goal\n\n_(manual mode — no goal; add tasks via the API or dashboard)_\n"
    plan_body = f"\n# Project: {body.name or project_id}\n\n{goal_section}"
    (pdir / "plan.md").write_text(_serialize_plan_md(plan_fm, plan_body), encoding="utf-8")

    # Initial status.md
    status_fm = {"state": initial_state, "last_updated": now}
    status_body = "\n# Status\n\nJust created (manual mode — waiting for tasks).\n" if is_manual else "\n# Status\n\nJust created. Planning in progress.\n"
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
        payload={"name": body.name, "goal": initial_goal, "mode": body.mode, "state": initial_state},
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
        "max_iterations, current_iteration, last_iteration_summary "
        "FROM projects WHERE id = ?",
        (project_id,),
    )
    if not row:
        raise HTTPException(404, f"Project not found: {project_id}")
    return Project(**row)


# ===== File API (§3.6 — all access via HTTP) =====


@router.get("/{project_id}/files/{path:path}")
async def read_file(project_id: str, path: str, request: Request) -> Response:
    """Read a file from the project folder."""
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
async def write_file(project_id: str, path: str, request: Request) -> dict:
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
async def set_project_session(project_id: str, request: Request) -> dict:
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
    agent_id = data.get("agent_id") or request.headers.get("X-Agent-Id")
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
    project_id: str, request: Request, role: str | None = None
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

    Use cases:
    - Project was created in manual mode (no goal). Now the user wants the
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
        "message": "replan queued. The supervisor's next tick will call the LLM planner.",
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
async def get_recent(request: Request) -> dict:
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
async def get_project_state(project_id: str, request: Request) -> dict:
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
async def get_project_facts(project_id: str, request: Request) -> dict:
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
) -> dict:
    """Return filtered L1 (trace.jsonl) entries for audit / debug."""
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

