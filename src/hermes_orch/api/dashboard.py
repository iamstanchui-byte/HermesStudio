"""Dashboard pages (per REVIEW.md §7).

Pages:
- GET /                  -> redirect to /agents
- GET /agents            -> Agents page (with expandable profile sub-cards)
- GET /tasks             -> Tasks page (filterable)
- GET /projects          -> Projects list
- GET /projects/{id}     -> Project detail (plan + tasks)
- GET /history           -> History (audit log)

Live updates via 5s polling (vanilla JS setInterval + fetch).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()

# Templates directory (relative to this file: src/hermes_orch/api/dashboard.py)
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _llm_configured(cfg: dict[str, Any] | None) -> bool:
    if not cfg:
        return False
    llm = cfg.get("llm") or {}
    return bool((llm.get("api_key") or "").strip())


def _base_context(request: Request, active_page: str) -> dict[str, Any]:
    """Common context for all dashboard templates (llm_configured + active_page)."""
    return {
        "active_page": active_page,
        "llm_configured": _llm_configured(getattr(request.app.state, "config", None)),
    }


def _project_storage_view(cfg: dict) -> dict:
    """Compact view of the project storage config for templates."""
    from pathlib import Path
    proj = cfg.get("projects") or {}
    root = (proj.get("storage_root") or "").strip()
    is_default = root in ("./projects", "projects", "")
    project_count = -1
    exists = False
    writable = False
    if root:
        p = Path(root)
        exists = p.exists() and p.is_dir()
        if exists:
            try:
                project_count = sum(
                    1 for x in p.iterdir() if x.is_dir() and not x.name.startswith(".")
                )
                # Test write access with a tiny file
                test_file = p / ".orch-write-test"
                try:
                    test_file.write_text("ok\n", encoding="utf-8")
                    test_file.unlink()
                    writable = True
                except Exception:
                    writable = False
            except Exception:
                pass
    return {
        "storage_root": root,
        "exists": exists,
        "writable": writable,
        "project_count": project_count,
        "is_default": is_default,
    }


# ===== Helpers =====


def _parse_json_fields(row: dict[str, Any], *fields: str) -> dict[str, Any]:
    """Parse JSON-encoded string fields into Python objects."""
    for col in fields:
        v = row.get(col)
        if isinstance(v, str):
            try:
                row[col] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                row[col] = {} if col != "depends_on" else []
    return row


def _compute_task_timing(task: dict[str, Any]) -> dict[str, Any]:
    """Derive start/end/duration for a task from existing DB columns.

    - started_at: last_liveness_at (set when wrapper claimed) or null
    - completed_at: updated_at if terminal state, else null
    - duration_seconds: completed_at - started_at, or running duration
    """
    from datetime import datetime
    TERMINAL = {"completed", "failed", "cancelled", "interrupted", "skipped"}
    result: dict[str, Any] = {"started_at": None, "completed_at": None, "duration_seconds": None}

    # Parse timestamps
    def _parse(s: str | None):
        if not s:
            return None
        try:
            # Strip trailing Z, replace with +00:00
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    started = _parse(task.get("last_liveness_at"))
    if not started and task.get("status") in ("running", "assigned", "pending"):
        # Not yet started by a wrapper
        pass
    elif started and task.get("status") in ("running",):
        # Still running — duration so far
        now = datetime.now(started.tzinfo) if started.tzinfo else datetime.now()
        result["started_at"] = started.isoformat()
        result["duration_seconds"] = max(0, (now - started).total_seconds())
    elif started:
        result["started_at"] = started.isoformat()

    completed = None
    if task.get("status") in TERMINAL:
        completed = _parse(task.get("updated_at"))
        if completed:
            result["completed_at"] = completed.isoformat()
            if started:
                delta = (completed - started).total_seconds()
                result["duration_seconds"] = max(0.0, delta)

    return result


def _format_duration(seconds: float | None) -> str:
    """Format a duration in seconds as a human-readable string."""
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 1:
        return "<1s"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


async def _load_agents(db: Any) -> list[dict[str, Any]]:
    rows = await db.fetchall("SELECT * FROM agents ORDER BY created_at DESC")
    agents = []
    for row in rows:
        profiles = await db.fetchall(
            "SELECT * FROM agent_profiles WHERE agent_id = ? ORDER BY name",
            (row["id"],),
        )
        profile_list = [dict(p) for p in profiles]
        # Augment each profile with its current skills (latest version per name).
        for p in profile_list:
            p["skills"] = await _load_profile_skills(db, p["id"])
        agents.append(
            {
                "id": row["id"],
                "ip": row.get("ip"),
                "os_type": row.get("os_type"),
                "status": row["status"],
                "last_heartbeat_at": row.get("last_heartbeat_at"),
                "created_at": row.get("created_at"),
                "profiles": profile_list,
            }
        )
    return agents


async def _load_profile_skills(db: Any, profile_id: str) -> list[dict[str, Any]]:
    """Return latest version of each skill for a profile, ordered by name.

    A skill is `profile_configs.file_path` starting with 'skills/'. We only
    keep the newest row per file_path (created_at DESC), and we treat empty
    applied content as a deletion — those entries are filtered out so the
    dashboard shows what's actually on the host.
    """
    rows = await db.fetchall(
        "SELECT * FROM profile_configs WHERE profile_id = ? "
        "AND file_path LIKE 'skills/%' "
        "ORDER BY file_path ASC, created_at DESC",
        (profile_id,),
    )
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["file_path"] in seen:
            continue
        seen.add(r["file_path"])
        # Treat applied-with-empty-content as deleted; skip from list
        if r["status"] == "applied" and (r["desired_content"] or "") == "":
            continue
        name = r["file_path"].removeprefix("skills/").removesuffix(".md")
        out.append({
            "name": name,
            "file_path": r["file_path"],
            "status": r["status"],
            "size": len(r["desired_content"] or ""),
            "created_at": r.get("created_at"),
            "applied_at": r.get("applied_at"),
            "error": r.get("error"),
            "content": r["desired_content"] or "",
        })
    return out


async def _load_tasks(db: Any) -> list[dict[str, Any]]:
    rows = await db.fetchall("SELECT * FROM tasks ORDER BY created_at DESC")
    return [_parse_json_fields(dict(r), "depends_on", "params", "result") for r in rows]


# ===== Routes =====


@router.get("/", response_class=RedirectResponse)
async def root() -> RedirectResponse:
    """Redirect to /agents."""
    return RedirectResponse(url="/agents")


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request) -> HTMLResponse:
    """Agents page with expandable profile sub-cards."""
    db = request.app.state.db
    agents = await _load_agents(db)
    return templates.TemplateResponse(
        request=request,
        name="agents.html",
        context={**_base_context(request, "agents"), "agents": agents},
    )


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(
    request: Request,
    status: str | None = None,
    days: int | None = 7,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse:
    """Tasks page (filterable by status, date range, limit, paginated)."""
    db = request.app.state.db
    where = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if days:
        # Compute the cutoff in local time with offset, matching the
        # format that db.insert / audit_log now writes. SQLite's
        # datetime('now', '-N days') would return UTC naive, but our
        # stored timestamps are local +offset — string comparison across
        # mixed formats is unreliable (date strings compare OK, but
        # the time portion has T-separator vs space-separator differences
        # plus a +08:00 suffix). Computing the cutoff in Python and
        # passing it as a parameter sidesteps the issue: both sides of
        # the comparison are now in the same ISO-8601-with-offset format.
        from datetime import datetime, timedelta
        cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
        where.append("created_at >= ?")
        params.append(cutoff)
    where_sql = " WHERE " + " AND ".join(where) if where else ""

    # Total count for pagination
    total_row = await db.fetchone(
        f"SELECT COUNT(*) as c FROM tasks{where_sql}", tuple(params)
    )
    total = total_row["c"] if total_row else 0

    # Page rows
    sql = f"SELECT * FROM tasks{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    page_params = tuple(params) + (limit, offset)
    raw_rows = await db.fetchall(sql, page_params)
    tasks = []
    for r in raw_rows:
        for col in ("depends_on", "params", "result"):
            v = r.get(col)
            if isinstance(v, str):
                try:
                    r[col] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    r[col] = {} if col != "depends_on" else []
        tasks.append(r)
    projects = await db.fetchall("SELECT id, name FROM projects ORDER BY created_at DESC")
    project_map = {p["id"]: (p["name"] or p["id"]) for p in projects}
    # Attach project name + timing to each task for the template
    for t in tasks:
        t["project_name"] = project_map.get(t["project_id"], t["project_id"])
        t["timing"] = _compute_task_timing(t)
    profile_rows = await db.fetchall(
        "SELECT agent_id, name FROM agent_profiles ORDER BY agent_id, name"
    )
    all_profiles = [
        {"agent_id": r["agent_id"], "name": r["name"]} for r in profile_rows
    ]
    return templates.TemplateResponse(
        request=request,
        name="tasks.html",
        context={
            **_base_context(request, "tasks"),
            "tasks": tasks,
            "projects": projects,
            "project_map": project_map,
            "all_profiles": all_profiles,
            "filter_status": status,
            "filter_days": days,
            "filter_limit": limit,
            "filter_offset": offset,
            "total_count": total,
        },
    )


@router.get("/projects", response_class=HTMLResponse)
async def projects_list_page(
    request: Request, show_archived: bool = False
) -> HTMLResponse:
    """Projects list. Default: hide archived. Pass show_archived=true to include."""
    db = request.app.state.db
    if show_archived:
        projects = await db.fetchall(
            "SELECT * FROM projects ORDER BY created_at DESC"
        )
    else:
        projects = await db.fetchall(
            "SELECT * FROM projects WHERE state != 'archived' "
            "ORDER BY created_at DESC"
        )
    # Profiles for the "Coordinator role" dropdown in the create form
    profile_rows = await db.fetchall(
        "SELECT agent_id, name FROM agent_profiles ORDER BY agent_id, name"
    )
    all_profiles = [
        {"agent_id": r["agent_id"], "name": r["name"]} for r in profile_rows
    ]
    return templates.TemplateResponse(
        request=request,
        name="projects_list.html",
        context={
            **_base_context(request, "projects"),
            "projects": projects,
            "show_archived": show_archived,
            "all_profiles": all_profiles,
        },
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_page(
    project_id: str,
    request: Request,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse:
    """Single project view: plan + tasks (paginated)."""
    db = request.app.state.db
    project = await db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")

    # Cap limit to prevent abuse
    if limit < 1:
        limit = 50
    if limit > 500:
        limit = 500
    if offset < 0:
        offset = 0

    # Total count for pagination
    total_count_row = await db.fetchone(
        "SELECT COUNT(*) as n FROM tasks WHERE project_id = ?", (project_id,)
    )
    total_count = total_count_row["n"] if total_count_row else 0

    # Paginated task rows (project-scoped SQL, not "load all then filter")
    task_rows = await db.fetchall(
        "SELECT * FROM tasks WHERE project_id = ? "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (project_id, limit, offset),
    )
    project_tasks = [
        _parse_json_fields(dict(r), "depends_on", "params", "result")
        for r in task_rows
    ]
    # Compute execution timing for each task (started_at, completed_at, duration)
    for t in project_tasks:
        t["timing"] = _compute_task_timing(t)

    # All profiles for role dropdown
    profile_rows = await db.fetchall(
        "SELECT agent_id, name FROM agent_profiles ORDER BY agent_id, name"
    )
    all_profiles = [
        {"agent_id": r["agent_id"], "name": r["name"]} for r in profile_rows
    ]
    # SOUL presets for this project (so the page can show them inline + let
    # the user edit / apply each one)
    preset_rows = await db.fetchall(
        "SELECT sp.id, sp.project_id, sp.profile_id, sp.role_name, sp.content, "
        "sp.created_at, sp.updated_at, ap.agent_id, ap.name AS profile_name "
        "FROM project_soul_presets sp "
        "JOIN agent_profiles ap ON ap.id = sp.profile_id "
        "WHERE sp.project_id = ? "
        "ORDER BY ap.agent_id, ap.name",
        (project_id,),
    )
    soul_presets = [dict(r) for r in preset_rows]
    return templates.TemplateResponse(
        request=request,
        name="project.html",
        context={
            **_base_context(request, "projects"),
            "project": project,
            "tasks": project_tasks,
            "total_count": total_count,
            "filter_limit": limit,
            "filter_offset": offset,
            "all_profiles": all_profiles,
            "soul_presets": soul_presets,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Settings page (LLM + Telegram)."""
    from hermes_orch.config import LLM_PROVIDERS
    cfg = request.app.state.config or {}
    llm = cfg.get("llm") or {}
    tg = cfg.get("telegram") or {}
    api_key = (llm.get("api_key") or "").strip()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            **_base_context(request, "settings"),
            "providers": LLM_PROVIDERS,
            "current_provider": llm.get("provider"),
            "current_base_url": llm.get("base_url"),
            "current_model": llm.get("model"),
            "api_key_last4": api_key[-4:] if len(api_key) >= 4 else None,
            "tg_token_last4": ((tg.get("bot_token") or "").strip())[-4:] or None,
            "tg_token_set": bool((tg.get("bot_token") or "").strip()),
            "tg_chat_id": (tg.get("chat_id") or "").strip() or None,
            "tg_enabled": bool(tg.get("enabled", False)),
            "project_storage": _project_storage_view(cfg),
        },
    )


@router.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request,
    event_type: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    days: int | None = 7,
) -> HTMLResponse:
    """History / audit log (filterable)."""
    db = request.app.state.db
    where = []
    params: list[Any] = []
    if event_type:
        where.append("event_type = ?")
        params.append(event_type)
    if project_id:
        where.append("project_id = ?")
        params.append(project_id)
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    if days:
        # Same local-time cutoff as the tasks page above — keeps the
        # comparison format-consistent regardless of which timestamp
        # format the rows were originally written in.
        from datetime import datetime, timedelta
        cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
        where.append("created_at >= ?")
        params.append(cutoff)
    sql = "SELECT * FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT 200"
    rows = await db.fetchall(sql, tuple(params))
    # Pretty-print payload JSON
    import json
    for e in rows:
        p = e.get("payload")
        if p:
            try:
                e["payload_pretty"] = json.dumps(json.loads(p), indent=2)
            except (json.JSONDecodeError, TypeError):
                e["payload_pretty"] = p
    # Filter dropdowns
    event_types = await db.fetchall(
        "SELECT DISTINCT event_type FROM audit_log ORDER BY event_type"
    )
    projects = await db.fetchall(
        "SELECT id, name FROM projects ORDER BY created_at DESC LIMIT 50"
    )
    agents = await db.fetchall(
        "SELECT id FROM agents ORDER BY id"
    )
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            **_base_context(request, "history"),
            "events": rows,
            "event_types": [r["event_type"] for r in event_types],
            "projects": projects,
            "agents": [r["id"] for r in agents],
            "filter_event_type": event_type,
            "filter_project_id": project_id,
            "filter_agent_id": agent_id,
            "filter_days": days,
            "active_page": "history",
        },
    )
