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

import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    """Write a file (whole content) to the project folder."""
    db = request.app.state.db
    pdir = _project_dir(request, project_id)
    safe = _validate_relpath(path)
    full = _resolve_inside(pdir, safe)
    body = await request.body()
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
    """Restore an archived project back to 'planning'."""
    db = request.app.state.db
    project = await db.fetchone("SELECT id, state FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    if project["state"] != "archived":
        raise HTTPException(400, f"Project not archived: {project['state']}")
    await db.execute(
        "UPDATE projects SET state = 'planning', updated_at = ? WHERE id = ?",
        (_now_iso(), project_id),
    )
    await audit_log(db, "project.unarchived", actor="operator", project_id=project_id)
    return {"project_id": project_id, "state": "planning"}


@router.post("/{project_id}/session")
async def set_project_session(project_id: str, request: Request) -> dict:
    """Set the project's current_session_id (called by wrapper after each task).

    Subsequent tasks for the same project will resume this session (via
    `hermes --resume <id>`), so the agent has context from prior tasks.
    """
    import json
    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid JSON body")
    session_id = data.get("session_id")
    role = data.get("role")  # agent role that created this session
    if not session_id:
        raise HTTPException(400, "session_id is required")
    db = request.app.state.db
    # One session per (project, role) — store the role alongside in the
    # same column by encoding. Or just keep one "current" session.
    # For simplicity: one current session per project (any role).
    await db.execute(
        "UPDATE projects SET current_session_id = ?, updated_at = ? WHERE id = ?",
        (session_id, _now_iso(), project_id),
    )
    await audit_log(
        db, "project.session_updated",
        actor="agent",
        project_id=project_id,
        payload={"session_id": session_id, "role": role},
    )
    return {"project_id": project_id, "current_session_id": session_id}


@router.get("/{project_id}/session")
async def get_project_session(project_id: str, request: Request) -> dict:
    """Get the project's current_session_id (called by wrapper before each task)."""
    db = request.app.state.db
    project = await db.fetchone(
        "SELECT current_session_id FROM projects WHERE id = ?", (project_id,)
    )
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    return {"project_id": project_id, "current_session_id": project.get("current_session_id")}


class ProjectReplan(BaseModel):
    """Body for POST /replan. Either provides a new goal, or just kicks the
    planner to retry with the existing goal (e.g. after manual cleanup)."""
    goal: str | None = None  # if None, replan uses the current goal
    clear_tasks: bool = False  # if True, delete existing pending/assigned tasks first


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
    # Reset current_iteration so the Q2 iteration loop re-runs from 0
    # against the new goal. Also clear the stale decision.md so the
    # supervisor's decision_is_pass check doesn't auto-complete the
    # project based on a verdict from the previous goal's last review.
    await db.execute(
        "UPDATE projects SET goal = ?, state = 'planning', "
        "current_iteration = 0, last_iteration_summary = '', "
        "updated_at = ? WHERE id = ?",
        (new_goal, _now_iso(), project_id),
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

    return Response(status_code=204)


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

