"""Recurring project schedules API (#22).

Endpoints:
    GET    /api/schedules                  — list all schedules
    POST   /api/schedules                  — create a schedule
    GET    /api/schedules/{id}             — get one schedule (with computed next fires)
    PATCH  /api/schedules/{id}             — update (enable/disable, edit cron, change template)
    DELETE /api/schedules/{id}             — remove (FK cascades template link)
    POST   /api/schedules/{id}/run-now     — manual fire (skips skip-rule; for testing)
    GET    /api/schedules/{id}/next-fires  — preview next 5 fire times (debug aid)

A schedule references a project that has is_template=1. The
orchestrator's background scheduler (core/scheduler.py) ticks every
30s and fires any schedule whose next_fire_at has passed, honoring
the per-schedule `mode` (clone vs append) and the skip rule.

The `templates` endpoint family (for the dashboard's "new schedule"
form to populate the template dropdown) lives here too:
    GET    /api/schedules/templates/list   — list projects with is_template=1
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes_orch.core.audit import audit_log
from hermes_orch.utils import now_iso as _now_iso

# Reuse the croniter-backed helper from the scheduler module. Lives in
# the core package because both API and scheduler need it; we don't
# want to import the scheduler itself from the API (would create a
# startup-time circularity in tests).
from hermes_orch.core.scheduler import Scheduler as _Scheduler

router = APIRouter()


# ===== Pydantic models =====


class ScheduleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    template_project_id: str = Field(..., min_length=1)
    cron_expr: str = Field(..., min_length=1, max_length=200)
    timezone: str = "Asia/Hong_Kong"
    mode: str = "clone"  # 'clone' | 'deterministic' | 'append'
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    """All fields optional — PATCH semantics. Empty string in
    `cron_expr` is treated as 'leave unchanged' (we validate
    non-empty before applying)."""
    name: str | None = None
    cron_expr: str | None = None
    timezone: str | None = None
    mode: str | None = None
    enabled: bool | None = None
    template_project_id: str | None = None


class Schedule(BaseModel):
    id: str
    name: str
    template_project_id: str
    cron_expr: str
    timezone: str
    mode: str
    enabled: int
    last_fired_at: str | None = None
    next_fire_at: str | None = None
    last_skip_reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # Optional joined fields (populated by GET endpoints, not stored)
    template_name: str | None = None
    last_run_project_id: str | None = None
    last_run_state: str | None = None
    last_run_at: str | None = None


# ===== Helpers =====


def _schedule_id() -> str:
    return "sched-" + secrets.token_hex(6)


def _validate_cron(expr: str) -> None:
    """Validate a cron expression by trying to iterate one step.

    croniter throws if the expression is malformed or has out-of-range
    fields. We don't catch the error here — let the HTTPException 400
    surface it with the library's actual error message.
    """
    from croniter import croniter as _croniter
    _ = _croniter(expr, datetime.now())


def _validate_mode(mode: str) -> str:
    """Normalize + validate mode. Returns the canonical lowercase form.

    Accepted values:
    - 'clone'         — fresh project per fire, LLM re-derives tasks
                        (similar but not identical plan each cycle)
    - 'deterministic' — fresh project per fire, template's task list
                        copied 1:1 (exact same tasks, fresh IDs)
    - 'append'        — reuse most recent non-terminal project,
                        supervisor re-derives tasks
    """
    m = (mode or "clone").lower()
    if m not in ("clone", "deterministic", "append"):
        raise HTTPException(
            400,
            f"mode must be 'clone' | 'deterministic' | 'append', got: {mode}",
        )
    return m


def _validate_timezone(tz_name: str) -> str:
    """Validate the IANA timezone name. We accept empty string as
    'default to Asia/Hong_Kong' (the DB default)."""
    if not tz_name:
        return "Asia/Hong_Kong"
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz_name)
    except Exception:
        raise HTTPException(400, f"unknown timezone: {tz_name}")
    return tz_name


# ===== Endpoints =====


@router.get("/")
async def list_schedules(request: Request) -> dict:
    """List all schedules, joined with template name + last run state.

    Sorted by next_fire_at ASC NULLS LAST (so due-soonest shows first),
    then by created_at DESC. Disabled schedules are pushed to the end
    so the dashboard's "active" tab is naturally the due-soonest ones.
    """
    db = request.app.state.db
    rows = await db.fetchall(
        "SELECT s.*, p.name AS template_name "
        "FROM project_schedules s "
        "LEFT JOIN projects p ON p.id = s.template_project_id "
        "ORDER BY s.enabled DESC, s.next_fire_at ASC, s.created_at DESC"
    )
    # For each schedule, also look up the most recent run's state +
    # timestamp so the dashboard can show "last fire: 5min ago, completed"
    for r in rows:
        last_run = await db.fetchone(
            "SELECT id, state, created_at FROM projects "
            "WHERE source_schedule_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (r["id"],),
        )
        if last_run:
            r["last_run_project_id"] = last_run["id"]
            r["last_run_state"] = last_run["state"]
            r["last_run_at"] = last_run.get("created_at")
    return {"schedules": rows}


@router.post("/", response_model=Schedule, status_code=201)
async def create_schedule(body: ScheduleCreate, request: Request) -> Schedule:
    """Create a new schedule.

    Validates:
    - The template project exists
    - The template project is marked is_template=1 (auto-marks if not,
      to keep a tight UX loop — the user can revert via the API)
    - The cron expression parses
    - The mode is 'clone' or 'append'
    - The timezone is a valid IANA name

    Computes next_fire_at immediately so the schedule is "armed" the
    moment it's created (rather than waiting for the next 30s scheduler
    tick to discover it).
    """
    db = request.app.state.db
    template = await db.fetchone(
        "SELECT id, name, is_template FROM projects WHERE id = ?",
        (body.template_project_id,),
    )
    if not template:
        raise HTTPException(404, f"Template project not found: {body.template_project_id}")
    if not template.get("is_template"):
        # Auto-mark so the user can iterate fast. If they really don't
        # want it, they can PATCH the project to clear is_template (or
        # delete + recreate). Auto-mark keeps the create flow short.
        await db.execute(
            "UPDATE projects SET is_template = 1 WHERE id = ?",
            (template["id"],),
        )
    _validate_cron(body.cron_expr)
    mode = _validate_mode(body.mode)
    tz = _validate_timezone(body.timezone)
    # Compute next_fire_at. We use a fresh Scheduler instance (no
    # start() call) so the next_fire helper is reusable without
    # touching the running singleton.
    cfg = request.app.state.config
    tmp_sched = _Scheduler(db, cfg)
    nxt = tmp_sched._compute_next_fire(body.cron_expr, tz, datetime.now(timezone.utc))
    sched_id = _schedule_id()
    await db.insert(
        "project_schedules",
        {
            "id": sched_id,
            "name": body.name,
            "template_project_id": body.template_project_id,
            "cron_expr": body.cron_expr,
            "timezone": tz,
            "mode": mode,
            "enabled": 1 if body.enabled else 0,
            "next_fire_at": nxt,
        },
    )
    await audit_log(
        db, "schedule.created",
        actor="operator",
        payload={
            "schedule_id": sched_id,
            "name": body.name,
            "template_project_id": body.template_project_id,
            "cron_expr": body.cron_expr,
            "timezone": tz,
            "mode": mode,
            "next_fire_at": nxt,
        },
    )
    return await _fetch_one(sched_id, db)


@router.get("/{sched_id}", response_model=Schedule)
async def get_schedule(sched_id: str, request: Request) -> Schedule:
    """Get one schedule, joined with template name + last run state.

    Use the `_fetch_one` helper (NOT `_fetch_one_raw`) — the raw variant
    only returns the schedule row, while the enriched helper also queries
    for the most recent run and folds `last_run_*` fields into the
    response. The `last_run_*` fields are how the dashboard shows
    "last fire 5min ago, completed" without an extra round-trip.
    """
    return await _fetch_one(sched_id, request.app.state.db)


@router.patch("/{sched_id}", response_model=Schedule)
async def update_schedule(
    sched_id: str, body: ScheduleUpdate, request: Request
) -> Schedule:
    """Update one or more fields. Re-validates cron + mode if changed.

    If `enabled` is toggled from 0→1, recompute next_fire_at so the
    schedule doesn't fire 100 times to "catch up" — it just resumes
    from the next cron slot after now.
    """
    db = request.app.state.db
    row = await _fetch_one_raw(sched_id, db)
    if not row:
        raise HTTPException(404, f"Schedule not found: {sched_id}")
    updates: dict[str, Any] = {}
    if body.name is not None and body.name.strip():
        updates["name"] = body.name.strip()
    if body.cron_expr is not None and body.cron_expr.strip():
        _validate_cron(body.cron_expr)
        updates["cron_expr"] = body.cron_expr.strip()
    if body.timezone is not None:
        updates["timezone"] = _validate_timezone(body.timezone)
    if body.mode is not None:
        updates["mode"] = _validate_mode(body.mode)
    if body.template_project_id is not None:
        t = await db.fetchone(
            "SELECT id FROM projects WHERE id = ?", (body.template_project_id,)
        )
        if not t:
            raise HTTPException(404, f"Template project not found: {body.template_project_id}")
        updates["template_project_id"] = body.template_project_id
    if body.enabled is not None:
        updates["enabled"] = 1 if body.enabled else 0
        # If we just re-enabled, recompute next_fire_at
        if body.enabled:
            new_cron = updates.get("cron_expr") or row["cron_expr"]
            new_tz = updates.get("timezone") or row["timezone"]
            cfg = request.app.state.config
            tmp = _Scheduler(db, cfg)
            updates["next_fire_at"] = tmp._compute_next_fire(
                new_cron, new_tz, datetime.now(timezone.utc)
            )
    if not updates:
        # Nothing to do — return as-is
        return row
    # Build SET clause
    set_parts = [f"{k} = ?" for k in updates]
    set_params = list(updates.values()) + [_now_iso(), sched_id]
    await db.execute(
        f"UPDATE project_schedules SET {', '.join(set_parts)}, updated_at = ? WHERE id = ?",
        tuple(set_params),
    )
    await audit_log(
        db, "schedule.updated",
        actor="operator",
        payload={"schedule_id": sched_id, "changes": list(updates.keys())},
    )
    return await _fetch_one(sched_id, db)


@router.delete("/{sched_id}", status_code=204)
async def delete_schedule(sched_id: str, request: Request):
    db = request.app.state.db
    row = await db.fetchone("SELECT id FROM project_schedules WHERE id = ?", (sched_id,))
    if not row:
        raise HTTPException(404, f"Schedule not found: {sched_id}")
    await db.execute("DELETE FROM project_schedules WHERE id = ?", (sched_id,))
    # Note: projects with source_schedule_id pointing here are NOT
    # deleted — they keep their history. We just NULL out the link
    # so the projects-list badge doesn't say "🔁 schedule-gone" forever.
    await db.execute(
        "UPDATE projects SET source_schedule_id = '' "
        "WHERE source_schedule_id = ?",
        (sched_id,),
    )
    await audit_log(
        db, "schedule.deleted", actor="operator",
        payload={"schedule_id": sched_id},
    )
    return None


@router.post("/{sched_id}/run-now")
async def run_schedule_now(sched_id: str, request: Request) -> dict:
    """Manually trigger a fire (bypasses the skip rule).

    Useful for testing ("does this template actually run?") and for
    catching up after a long downtime. Unlike the scheduler tick,
    this endpoint doesn't skip on previous-run-active — it just fires.

    Returns the resulting project id (clone mode) or the existing
    project id (append mode).
    """
    db = request.app.state.db
    cfg = request.app.state.config
    sched = await db.fetchone(
        "SELECT * FROM project_schedules WHERE id = ?", (sched_id,)
    )
    if not sched:
        raise HTTPException(404, f"Schedule not found: {sched_id}")
    template = await db.fetchone(
        "SELECT * FROM projects WHERE id = ?", (sched["template_project_id"],)
    )
    if not template:
        raise HTTPException(404, f"Template project not found: {sched['template_project_id']}")
    sched_obj = _Scheduler(db, cfg)
    # Dispatch by mode. The scheduler's `_fire` does the same dispatch
    # for the background tick; we replicate it here so manual "run now"
    # honors the schedule's mode (otherwise deterministic schedules would
    # silently fall back to LLM regen when triggered manually).
    mode = (sched.get("mode") or "clone").lower()
    if mode == "clone":
        new_id = await sched_obj._fire_clone(sched, template)
    elif mode == "deterministic":
        new_id = await sched_obj._fire_clone_deterministic(sched, template)
    else:
        new_id = await sched_obj._fire_append(sched, template)
    # Bump last_fired_at + next_fire_at (manual fire still advances the schedule)
    await db.execute(
        "UPDATE project_schedules SET last_fired_at = ?, "
        "next_fire_at = ?, updated_at = ? WHERE id = ?",
        (_now_iso(),
         sched_obj._compute_next_fire(sched["cron_expr"], sched.get("timezone") or "UTC",
                                       datetime.now(timezone.utc)),
         _now_iso(), sched_id),
    )
    await audit_log(
        db, "schedule.run_now",
        actor="operator",
        project_id=new_id,
        payload={"schedule_id": sched_id, "mode": sched.get("mode")},
    )
    return {"project_id": new_id, "schedule_id": sched_id, "mode": sched.get("mode")}


@router.get("/{sched_id}/next-fires")
async def next_fires(sched_id: str, count: int = 5, request: Request = None) -> dict:
    """Preview the next N fire times for this schedule (debug aid).

    `count` is capped at 50 to keep responses small. The output is a
    list of ISO timestamps in the schedule's local timezone.
    """
    count = max(1, min(50, int(count or 5)))
    db = request.app.state.db
    row = await _fetch_one_raw(sched_id, db)
    if not row:
        raise HTTPException(404, f"Schedule not found: {sched_id}")
    cfg = request.app.state.config
    sched_obj = _Scheduler(db, cfg)
    out = []
    base = datetime.now(timezone.utc)
    for _ in range(count):
        nxt = sched_obj._compute_next_fire(row["cron_expr"], row.get("timezone") or "UTC", base)
        out.append(nxt)
        # advance base to nxt for the next iteration
        from datetime import datetime as _dt
        base = _dt.fromisoformat(nxt).astimezone(timezone.utc)
    return {"schedule_id": sched_id, "next_fires": out}


# ===== Templates list (for the new-schedule form's template dropdown) =====


@router.get("/templates/list")
async def list_templates(request: Request) -> dict:
    """List projects with is_template=1, for the dashboard's
    'new schedule' form template dropdown.

    Sorted by updated_at DESC so the most-recently-touched templates
    bubble to the top.
    """
    db = request.app.state.db
    rows = await db.fetchall(
        "SELECT id, name, goal, state, template_description, updated_at, created_at "
        "FROM projects WHERE is_template = 1 "
        "ORDER BY updated_at DESC, created_at DESC"
    )
    return {"templates": rows}


# ===== Mark/unmark project as template (used by the project page button) =====


class MarkTemplate(BaseModel):
    description: str = ""  # optional human-readable description for the template


@router.post("/project/{project_id}/mark-template")
async def mark_project_as_template(
    project_id: str, body: MarkTemplate, request: Request
) -> dict:
    """Mark a project as a reusable template (is_template=1).

    Setting this flag does NOT change the project's state — a running
    or completed project can also be a template (so users can take a
    project that worked well, mark it as a template, then attach a
    schedule to it).
    """
    db = request.app.state.db
    proj = await db.fetchone("SELECT id, state FROM projects WHERE id = ?", (project_id,))
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    await db.execute(
        "UPDATE projects SET is_template = 1, template_description = ?, "
        "updated_at = ? WHERE id = ?",
        (body.description, _now_iso(), project_id),
    )
    await audit_log(
        db, "project.marked_as_template", actor="operator", project_id=project_id,
        payload={"description": body.description[:200]},
    )
    return {"project_id": project_id, "is_template": True, "description": body.description}


@router.delete("/project/{project_id}/mark-template", status_code=204)
async def unmark_project_as_template(project_id: str, request: Request):
    """Unmark a project as a template. Existing schedules that
    reference it will start emitting 'template missing' warnings
    on their next fire; better to delete those schedules explicitly."""
    db = request.app.state.db
    proj = await db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    await db.execute(
        "UPDATE projects SET is_template = 0, template_description = '', "
        "updated_at = ? WHERE id = ?",
        (_now_iso(), project_id),
    )
    await audit_log(
        db, "project.unmarked_as_template", actor="operator", project_id=project_id,
    )
    return None


# ===== Promote to skill (Path B, #22) =====
#
# Render the project's plan + tasks + facts as a SKILL.md and push it to
# the target profile's `skills/<name>/SKILL.md` file (via the same
# profile_configs flow the dashboard's skill upload uses). The wrapper
# picks it up on the next 30s config-sync tick and writes the file to
# the agent host.
#
# Use case: "I just did this thing once, now let agents in any project
# know how to do it." Best done on completed projects, but allowed on
# any state — a half-finished project can still extract a useful lesson.

class PromoteToSkillIn(BaseModel):
    agent_id: str
    profile_name: str
    skill_name: str = Field(..., min_length=2, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")


@router.post("/project/{project_id}/promote-to-skill")
async def promote_project_to_skill(
    project_id: str, body: PromoteToSkillIn, request: Request
) -> dict:
    """Render this project as a SKILL.md and push it to the target profile.

    The skill lands on the agent host within ~30s (the wrapper's
    config-sync tick). The agent's available-skills list will then
    include it.
    """
    import secrets as _secrets
    db = request.app.state.db
    proj = await db.fetchone(
        "SELECT id, name, goal, state FROM projects WHERE id = ?",
        (project_id,),
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    # Look up target profile. Must exist, must be verified (otherwise
    # the wrapper won't accept our config push — no auth secret means
    # the agent hasn't completed bootstrap).
    target = await db.fetchone(
        "SELECT ap.id, ap.name, a.id AS agent_id, a.status AS agent_status, a.secret_hash "
        "FROM agent_profiles ap JOIN agents a ON a.id = ap.agent_id "
        "WHERE ap.name = ? AND a.id = ?",
        (body.profile_name, body.agent_id),
    )
    if not target:
        raise HTTPException(
            404, f"Profile not found: {body.agent_id}/{body.profile_name}"
        )
    if not target.get("secret_hash"):
        raise HTTPException(
            400,
            f"Target agent '{body.agent_id}' has no auth secret — "
            "register it first (or complete the bootstrap flow).",
        )
    # Render the SKILL.md body. Human-readable, no JSON frontmatter —
    # the agent just reads it as a knowledge asset. Sections:
    #   - Goal (1-2 lines from project.goal)
    #   - Workflow steps (task list, action+role per step, status, result snippet)
    #   - Key facts learned (from facts.md, trimmed)
    #   - Outcome (from decision.md, trimmed)
    #   - When to use (pointer)
    parts: list[str] = []
    parts.append(f"# {proj.get('name') or proj['id']}\n")
    if proj.get("goal"):
        parts.append("## Goal\n\n" + proj["goal"] + "\n")
    # Pull task list from DB (cleaner than parsing plan.md, and handles
    # manual-mode projects that never got an LLM-generated plan).
    tasks = await db.fetchall(
        "SELECT name, agent_role, action, status, result "
        "FROM tasks WHERE project_id = ? "
        "ORDER BY created_at ASC",
        (project_id,),
    )
    if tasks:
        parts.append("## Workflow steps\n")
        for i, t in enumerate(tasks, 1):
            tname = t.get("name") or t.get("action") or "task"
            parts.append(
                f"{i}. **{tname}** (`{t.get('action') or '?'}`, "
                f"role=`{t.get('agent_role') or '?'}`) — {t.get('status') or ''}"
            )
            if t.get("result"):
                r = (t.get("result") or "")[:400]
                if len(t.get("result") or "") > 400:
                    r += "..."
                parts.append(f"   - Result: {r}")
        parts.append("")
    # facts.md (curated L2 memory) — what the user cared about
    from hermes_orch.api.projects import _project_dir
    pdir = _project_dir(request, project_id)
    facts_path = pdir / "facts.md"
    if facts_path.exists():
        facts_text = facts_path.read_text(encoding="utf-8", errors="replace")
        if facts_text.strip():
            parts.append("## Key facts learned\n")
            for line in facts_text.splitlines()[:80]:
                parts.append(line)
            parts.append("")
    decision_path = pdir / "decision.md"
    if decision_path.exists():
        decision_text = decision_path.read_text(encoding="utf-8", errors="replace")
        if decision_text.strip():
            parts.append("## Outcome\n")
            parts.append(decision_text[:1500])
            parts.append("")
    parts.append("## When to use this skill\n")
    parts.append(
        f"This skill captures the workflow from project `{proj['id']}`. "
        "Use it when the user asks for a similar task and the same workflow steps apply.\n"
    )
    skill_body = "\n".join(parts).strip() + "\n"
    # Push via profile_configs — same flow as the agents router uses
    # for skills. The wrapper's apply-config loop writes it to
    # <profile>/skills/<name>/SKILL.md.
    skill_id = "skill-" + _secrets.token_hex(6)
    await db.insert(
        "profile_configs",
        {
            "id": skill_id,
            "profile_id": target["id"],
            "file_path": f"skills/{body.skill_name}/SKILL.md",
            "desired_sha256": "",  # wrapper computes on apply
            "desired_content": skill_body,
            "status": "pending",
        },
    )
    await audit_log(
        db, "project.promoted_to_skill",
        actor="operator",
        project_id=project_id,
        payload={
            "skill_id": skill_id,
            "skill_name": body.skill_name,
            "agent_id": body.agent_id,
            "profile_name": body.profile_name,
            "size": len(skill_body),
        },
    )
    return {
        "skill_id": skill_id,
        "skill_name": body.skill_name,
        "size": len(skill_body),
        "agent_id": body.agent_id,
        "profile_name": body.profile_name,
    }


# ===== Internal helpers =====


async def _fetch_one_raw(sched_id: str, db) -> dict | None:
    return await db.fetchone(
        "SELECT s.*, p.name AS template_name "
        "FROM project_schedules s "
        "LEFT JOIN projects p ON p.id = s.template_project_id "
        "WHERE s.id = ?",
        (sched_id,),
    )


async def _fetch_one(sched_id: str, db) -> Schedule:
    row = await _fetch_one_raw(sched_id, db)
    if not row:
        raise HTTPException(404, f"Schedule not found: {sched_id}")
    # last run state
    last = await db.fetchone(
        "SELECT id, state, created_at FROM projects "
        "WHERE source_schedule_id = ? ORDER BY created_at DESC LIMIT 1",
        (sched_id,),
    )
    if last:
        row["last_run_project_id"] = last["id"]
        row["last_run_state"] = last["state"]
        row["last_run_at"] = last.get("created_at")
    return Schedule(**row)
