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

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
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
    cfg = request.app.state.config
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
    # LLM-driven synthesis path (Hermes 4-layer framework, see
    # https://hermes-agent/skills/software-development/project-trace-to-skill-conversion
    # for the canonical reference). The earlier mechanical-render
    # approach produced a "trace dump" SKILL.md that the agent
    # would *reprint* when invoked, because the body contained L2
    # (specific values) and L3 (task IDs / PASS-FAIL / coord
    # scaffolding) instead of an executable procedure. The LLM
    # synthesis path produces a real skill: frontmatter + concrete
    # Hermes tool calls + drop all L2/L3.
    from hermes_orch.api.projects import _project_dir
    pdir = _project_dir(request, project_id)
    evidence = await _gather_skill_evidence(db, pdir, project_id, proj)
    llm_cfg = cfg.get("llm", {})
    try:
        skill_body = await _call_llm_for_skill_synthesis(
            evidence, llm_cfg, body.skill_name
        )
    except HTTPException:
        raise
    except Exception as e:
        # Anything else: surface as 502 with the underlying error.
        import traceback
        raise HTTPException(
            502,
            f"Skill-synthesis LLM call failed: {type(e).__name__}: {e} | "
            f"traceback: {traceback.format_exc()[-500:]}",
        )
    # Validate the LLM output against the 4-layer rules. If it
    # leaked L2/L3 (LLM not strict enough), refuse to push — better
    # to tell the user "LLM produced a non-skill" than ship a dump.
    is_valid, err = _validate_skill_md(skill_body, body.skill_name)
    if not is_valid:
        raise HTTPException(
            500,
            f"LLM produced a SKILL.md that fails 4-layer validation: {err}. "
            "Re-run or hand-fix the LLM prompt.",
        )
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


# ===== SKILL.md rendering helpers (used by promote-to-skill) =====
#
# These existed inline before but the noise (hermes wrapper context
# blocks, \uXXXX escapes from json.dumps(ensure_ascii=True), empty
# facts.md sections, generic "when to use" pointer) made the resulting
# SKILL.md barely useful for future agents. Lifted out so they're
# independently testable.


_PROJECT_CONTEXT_RE = re.compile(
    # Hermes wrapper prepends a block that starts with the literal
    # "--- PROJECT CONTEXT ---" (sometimes after a leading newline) and
    # runs to end-of-result. The "--- USER RECENT (L3: ...)" sentinel
    # and the "--- WORKFLOW PROCEDURE" sentinel are also possible
    # cut-points we want to drop.
    r"\n+---\s*(?:PROJECT CONTEXT|USER RECENT|WORKFLOW PROCEDURE).*$",
    re.DOTALL,
)


# Cap on the action string we print in the SKILL.md. Above this length
# the LLM has clearly concatenated a full task prompt into
# `tasks.action` (a planner-path bug we can't fix at the source yet).
# The short form is what the Workflow list + When-to-use workflow line
# actually want — the long form goes in the citation under "Key facts
# learned" for traceability.
_SHORT_ACTION_MAX = 30


def _short_action(action: str) -> str:
    """Return a SKILL.md-friendly short form of a `tasks.action` string.

    The wrapper normally writes a short action like `fetch_weekly_weather`
    or `investigate`, but on some LLM planner paths the whole task
    prompt (2KB+) ends up in `tasks.action`. That blows up both the
    Workflow list (1 entry eats 30+ lines) and the When-to-use
    "Workflow: N step(s) — a -> b -> c" line (a single long action
    makes the line unreadable).

    Strategy:
      1. Take the first line only (most prompts are multi-line; we
         only want the headline).
      2. If the head fits in _SHORT_ACTION_MAX, return as-is.
         (Real action names like `cross_verify_and_compose_zh` are
         27 chars — fine. Only the LLM-polluted ones blow past 30.)
      3. If too long, try to cut on a separator (em-dash, double-dash,
         single dash, colon, open paren) — these are the punctuation
         marks the LLM uses to separate the action verb from its
         description. The LLM-polluted strings almost always start
         with the verb and then a separator (e.g.
         `investigate — gather the data and context needed for X`).
      4. If no separator found in the first _SHORT_ACTION_MAX chars,
         hard-truncate to _SHORT_ACTION_MAX + "…".
    """
    if not action:
        return ""
    head = action.split("\n", 1)[0].strip()
    if len(head) <= _SHORT_ACTION_MAX:
        return head
    # Try separator cut first — this catches the common
    # "verb — description" pollution pattern
    for sep in (" — ", " -- ", " - ", ": ", "(", " —", " --", " -"):
        idx = head.find(sep)
        if 0 < idx <= _SHORT_ACTION_MAX:
            return head[:idx].rstrip() + "…"
    # No separator: hard truncate
    return head[:_SHORT_ACTION_MAX].rstrip() + "…"





def _clean_task_result(raw: str) -> str:
    """Turn a hermes task-result string into a human-readable snippet.

    The wrapper writes results as `{"summary": "..."}` JSON, with
    json.dumps(ensure_ascii=True), so all non-ASCII characters are
    \\uXXXX-escaped. Then it appends a multi-line "--- PROJECT CONTEXT ---"
    block that's purely internal to the wrapper's prompt.

    This function:
      1. json.loads() — auto-decodes \\uXXXX escapes, extracts summary.
      2. Strips the trailing context block.
      3. Returns the cleaned text (may be empty).
    """
    if not raw:
        return ""
    s = raw
    # json.loads handles "string with escapes" -> decoded unicode chars
    # in one shot. If the value is a dict, grab the summary. If it's a
    # bare string, use it. If parsing fails, fall back to the raw text
    # (and let the context-strip step do its best).
    try:
        d = json.loads(s)
    except (ValueError, TypeError):
        d = None
    if isinstance(d, dict):
        s = d.get("summary") or d.get("result") or ""
        if not isinstance(s, str):
            s = str(s)
    elif isinstance(d, str):
        s = d
    # Strip the wrapper context block — everything from the first
    # "--- PROJECT CONTEXT ---" (or similar sentinel) to end-of-string.
    s = _PROJECT_CONTEXT_RE.sub("", s)
    return s.strip()


def _filter_facts_sections(text: str) -> str:
    """Drop empty `## Foo` sections from facts.md.

    facts.md is auto-curated from L1 trace.jsonl and contains many
    sections (Key Findings, Coord Verdicts, Human Notes, etc.) that
    often have just a header and no body. They add noise to SKILL.md
    without telling future agents anything.

    A section is considered "empty" if its body (until the next `## `
    header or EOF) is whitespace-only.
    """
    if not text:
        return ""
    lines = text.splitlines()
    out: list[str] = []
    # State machine: keep track of whether the current section has any
    # non-header content. When we hit the next `## ` header (or EOF),
    # decide whether to keep the buffered section.
    section_buf: list[str] = []  # lines for the current section
    section_has_content = False

    def flush():
        nonlocal section_buf, section_has_content
        if section_has_content:
            out.extend(section_buf)
        section_buf = []
        section_has_content = False

    for line in lines:
        if line.startswith("## "):
            # New section: flush the previous one
            flush()
            section_buf.append(line)
            section_has_content = False
        else:
            section_buf.append(line)
            # Body content = not blank, not a cite-only or bullet-empty
            # line. Conservative: any non-blank line counts.
            if line.strip():
                section_has_content = True
    flush()
    return "\n".join(out)


def _render_when_to_use(proj: dict, tasks: list) -> str:
    """Generate a concrete 'When to use this skill' paragraph.

    The old generic pointer ("Use it when the user asks for a similar
    task") told future agents nothing. This produces a concrete match
    line based on the project's goal + workflow pattern:

        Use when the user asks: <goal>
        Workflow: <N> steps (<step1> -> <step2> -> ...)

    """
    goal = (proj.get("goal") or "").strip()
    parts: list[str] = []
    if goal:
        # Cap the quoted goal at 200 chars so the SKILL.md stays compact.
        quoted = goal if len(goal) <= 200 else goal[:200] + "..."
        parts.append(f"- Use when the user asks: **{quoted}**")
    # Workflow shape: list the action names in order so a future agent
    # can see at a glance what kind of pipeline this is. Use
    # _short_action() to truncate the LLM's verbose action strings
    # (some planner paths concatenate the full task prompt into
    # `tasks.action`, which would blow up the workflow line).
    actions = [
        _short_action(t.get("action") or "")
        for t in tasks
        if t.get("action")
    ]
    if actions:
        arrow = " -> ".join(actions)
        parts.append(f"- Workflow: {len(actions)} step(s) — `{arrow}`")
    if not parts:
        parts.append(
            f"- This skill captures the workflow from project `{proj['id']}`."
        )
    return "\n".join(parts) + "\n"


# ===== LLM-driven promote-to-skill (hermes 4-layer framework) =====
#
# Reference: ~/.hermes/profiles/<role>/skills/software-development/
# project-trace-to-skill-conversion/SKILL.md (bundled by hermes). The
# mechanical-render approach (this file's earlier version) produces
# a "trace dump" SKILL.md: L2 raw data + L3 task IDs / PASS-FAIL /
# coord scaffolding get embedded in the body, so when an agent
# loads the skill it just *reprints* the body instead of executing
# a procedure. The fix is to delegate SKILL.md construction to an
# LLM, with a strict prompt that applies the 4-layer separation:
# keep L0 (structure) + L1 (real-world actions, rewritten as
# Hermes tool calls) and drop L2 (specific values) + L3 (engine
# scaffolding) entirely. Output is then validated against the
# same anti-dump assertions the hermes skill recommends in Phase 5.
_SKILL_MAX_DESCRIPTION_CHARS = 60
_SKILL_MAX_FILE_BYTES = 100_000
_L3_LITERAL_TOKENS = (
    # Full L3 token (not just prefix) — the LLM sometimes references
    # "[cite:" syntactically in its own examples, but never
    # "[cite:task.completed@..." as a real citation.
    "[cite:task.completed@",
    "task.completed@",
    "DECISION: PASS",
    "DECISION: FAIL",
    "coord_pickup",
    "iteration_completed@",
)
_L3_PHRASES = (
    "plan history", "task results", "coord verdicts", "files (artifacts)",
    "key facts", "key insights", "key learnings", "what we learned",
    "run summary", "outcome", "project facts", "auto-curated",
)


_SKILL_SYNTHESIS_PROMPT = """You are converting a project execution trace into a real, reusable skill following the Hermes 4-layer separation framework.

# Goal
Produce a SKILL.md that, when loaded by an agent via `/<skill-name>`, makes the agent EXECUTE the procedure — not reprint the body. Trace dumps make agents echo back the report; this skill must make them DO the work.

# 4-Layer Separation (mandatory)
For every piece of evidence below, classify it:
- **L0 structure** (step count, ordering, dependencies) — KEEP, rewrite as `## Step N: <verb> <thing>`
- **L1 actions** (which API / tool / website / command was used) — KEEP, rewrite as concrete Hermes tool calls (`web_search`, `web_extract`, `terminal`, `read_file`, `patch`, `web_fetch`)
- **L2 data** (specific values fetched: coordinates, forecasts, dates, raw JSON, names) — DROP. Stale on next run.
- **L3 decisions** (`task.completed` IDs, PASS/FAIL verdicts, `coord_pickup` handoffs, `[iteration_completed@iter=N]` markers) — DROP entirely. Internal to the originating engine.

# Cardinal test
Does this line tell the agent WHAT TO DO this turn? If yes (L0/L1) → keep. If it describes what already happened or whether it was approved (L2/L3) → drop.

# Output schema (strict, follow exactly)
- File starts with `---` (byte 0) → YAML frontmatter.
- Required frontmatter fields:
  - `name`: kebab-case, MUST equal the requested skill_name below.
  - `description`: ≤ {_max} chars total, MUST start with "Use when " (or "Use whenever ").
  - `version`: 0.1.0.
  - `author`: a real name + handle, or "Hermes Agent".
  - `license: MIT`.
- Optional: `platforms: [linux, macos, windows]`.
- `metadata.hermes.tags`: 3-6 short tags.
- `metadata.hermes.related_skills`: MUST list `project-trace-to-skill-conversion` FIRST (anti-pattern propagation reference), then any sibling skills in the same domain.
- Body sections, in order: `## Overview`, `## When to Use`, `## Steps` (with `## Step N: <verb> <thing>` sub-headings), `## Pitfalls`, `## Verification`.
- Total file size: target 8-15 KB, hard cap 100 KB.

# Step format (mandatory)
Each `## Step N: <verb> <thing>` must specify:
1. **Action verb** (Fetch, Search, Read, Compose, Cross-verify, etc.) — strong verb, not a noun.
2. **Hermes tool** to use (`web_search`, `web_extract`, `terminal`, `read_file`, `patch`, `web_fetch`). NEVER use internal engine names like `reverify_raw_data`, `cross_verify_typhoon`, `_iteration_review`, `coord_pickup`, `handoff: coord_pickup`.
3. **Target**: an exact URL, exact CLI command, or exact search query.
4. **Completion criterion**: a checkable assertion (e.g. "result contains forecast for next 7 days with high/low °C and precipitation probability"). If you cannot write a checkable criterion, the step is too vague — sharpen it.
5. NO fetched data values, task IDs, or PASS/FAIL markers inside the step.

# Anti-patterns to drop (these WILL fail validation)
- `[cite:task.completed@...]` (L3 trace citation)
- `## Plan History` / `## Task Results` / `## Coord Verdicts` / `## Files (artifacts)` headings (L3)
- Internal engine names: `reverify_raw_data`, `cross_verify_typhoon`, `compose_weather_report_zh`, `_iteration_review:1`, `coord_pickup`, `handoff: coord_pickup`
- Specific values: actual coordinates, specific forecast numbers, raw JSON dumps, today's date
- `DECISION: PASS` / `DECISION: FAIL` markers (L3)
- `_iteration_review:1` style action names (L3 coord scaffolding — omit the step entirely)

# Description rules
- MUST start with "Use when " (or "Use whenever ").
- ≤ {_max} chars total (hard limit; the skill authoring standard enforces this).
- Trigger-class (what kind of user request should invoke this skill), NOT the project goal literally.
- Examples:
  - GOOD: "Use when the user asks for a KMB 296A bus route stops list in Hong Kong."
  - GOOD: "Use when the user asks for a weekly Taipei weather report in Chinese with typhoon cross-verification."
  - BAD: "查香港九巴296a的站表" (Chinese only, no trigger class)
  - BAD: "Use this skill to do stuff" (too generic)
  - BAD: a 100+ char description (over the limit)

# Evidence
{evidence}

# Output
Output ONLY the SKILL.md content, starting with `---` (the frontmatter opening). No preamble, no explanation, no code fence wrappers. End with exactly one trailing newline.
"""


async def _gather_skill_evidence(db, pdir: "Path", project_id: str, proj: dict) -> str:
    """Build the evidence block fed to the LLM for skill synthesis.

    Includes project metadata, successful tasks (L0+L1), failed tasks
    (so the LLM can choose to drop them or note them as dead-ends),
    facts.md (truncated), decision.md (truncated), and 1-2 sample
    artifact files (so the LLM can see what the original output shape
    looked like — the LLM should describe the SHAPE, not import
    the values).
    """
    parts: list[str] = ["# Project evidence\n"]
    parts.append("## Project metadata\n")
    parts.append(f"- id: {project_id}\n")
    parts.append(f"- name: {proj.get('name') or project_id}\n")
    parts.append(f"- state: {proj.get('state', '?')}\n")
    parts.append(f"- goal: {proj.get('goal') or '(none)'}\n")

    # Tasks
    tasks = await db.fetchall(
        "SELECT name, agent_role, action, status, result, output_path, depends_on "
        "FROM tasks WHERE project_id = ? "
        "ORDER BY created_at ASC",
        (project_id,),
    )
    successful = [t for t in tasks if (t.get("status") or "") == "completed"]
    failed = [
        t for t in tasks
        if (t.get("status") or "") in ("failed", "skipped", "cancelled")
    ]

    if successful:
        parts.append("\n## Successful tasks (L0 structure + L1 actions)\n")
        for t in successful:
            name = t.get("name") or "task"
            role = t.get("agent_role") or "?"
            short_act = _short_action(t.get("action") or "?")
            output = t.get("output_path") or "(none)"
            deps = t.get("depends_on") or []
            deps_str = f" (after: {', '.join(deps)})" if deps else ""
            parts.append(
                f"- **{name}** [{role}]{deps_str}: action=`{short_act}` -> writes `{output}`\n"
            )
    if failed:
        parts.append(
            "\n## Failed / skipped / cancelled tasks "
            "(DO NOT include these in the skill, but flag if the path was a dead-end)\n"
        )
        for t in failed:
            name = t.get("name") or "task"
            short_act = _short_action(t.get("action") or "?")
            err = (t.get("error") or "unknown")[:200]
            parts.append(f"- {name}: action=`{short_act}` — {err}\n")

    # facts.md
    facts_path = pdir / "facts.md"
    if facts_path.exists():
        facts_text = facts_path.read_text(encoding="utf-8", errors="replace")
        # Drop the L3-flavoured sections so the LLM doesn't see them
        # and re-include them.
        facts_filtered = _filter_facts_sections(facts_text)
        if facts_filtered.strip():
            parts.append(
                f"\n## facts.md (curated memory, {len(facts_text)} bytes -> "
                f"{len(facts_filtered)} after L3 section drop)\n```\n"
                f"{facts_filtered[:2000]}\n```\n"
            )

    # decision.md
    decision_path = pdir / "decision.md"
    if decision_path.exists():
        decision_text = decision_path.read_text(encoding="utf-8", errors="replace")
        parts.append(
            f"\n## decision.md ({len(decision_text)} bytes)\n```\n"
            f"{decision_text[:800]}\n```\n"
        )

    # Sample artifact (1 file, first 20 lines only) — show SHAPE, not
    # content. Large projects hit MiniMax M3 60-120s timeouts when the
    # evidence block is too big; cap aggressively.
    sample_paths: list[str] = []
    for t in successful:
        op = t.get("output_path")
        if op and op not in sample_paths:
            sample_paths.append(op)
    if not sample_paths:
        for cand in ("investigation.md", "report.md", "report.zh.md", "summary.md"):
            if (pdir / cand).exists():
                sample_paths.append(cand)
                break
    for sp in sample_paths[:1]:  # max 1 sample (was 2)
        full = pdir / sp
        if full.exists():
            txt = full.read_text(encoding="utf-8", errors="replace")
            head = "\n".join(txt.splitlines()[:20])  # was 50
            parts.append(
                f"\n## Sample artifact: {sp} "
                f"({len(txt)} bytes, first 20 lines shown)\n```\n{head}\n```\n"
            )

    return "".join(parts)


async def _call_llm_for_skill_synthesis(
    evidence: str, llm_cfg: dict, skill_name: str
) -> str:
    """Call the LLM to synthesize a proper SKILL.md from the evidence.

    Same httpx pattern as the auto-generate-procedure endpoint.
    Strips `` reasoning traces and outer code-fence wrappers
    defensively (the model sometimes adds them despite the prompt).
    """
    import httpx

    base_url = (llm_cfg.get("base_url") or "https://api.minimax.io/v1").rstrip("/")
    api_key = llm_cfg.get("api_key") or ""
    model = llm_cfg.get("model") or "MiniMax-M3"
    # Skill synthesis: need more headroom than the default 60s — the
    # model may spend budget on its <think> trace before any real
    # output, especially with longer evidence blocks. Use 120s.
    timeout = float(llm_cfg.get("timeout_seconds") or 120)
    if not api_key:
        raise HTTPException(
            503,
            "LLM api_key not configured — set llm.api_key in config.yaml "
            "before promoting projects to skills.",
        )

    prompt = _SKILL_SYNTHESIS_PROMPT.format(
        _max=_SKILL_MAX_DESCRIPTION_CHARS,
        evidence=evidence,
    )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You write production-quality SKILL.md files for Hermes "
                    "Agent. Apply the 4-layer separation strictly. "
                    "Output only the SKILL.md, no preamble, no code-fence "
                    "wrappers. description MUST start with 'Use when' and be "
                    f"≤ {_SKILL_MAX_DESCRIPTION_CHARS} chars. "
                    "Do not emit any thinking/reasoning trace — output only "
                    "the final SKILL.md content."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        # MiniMax M3 emits <think>...</think> reasoning trace that
        # can eat 2-3K tokens before any actual content. Bump the
        # max_tokens so there's room for both the thinking and a
        # real SKILL.md (8-15KB target). 8000 leaves comfortable
        # headroom; 12000 timed out at 120s in our test runs.
        "max_tokens": 8000,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            f"{base_url}/chat/completions", json=payload, headers=headers
        )
    if r.status_code != 200:
        raise HTTPException(
            502, f"LLM returned HTTP {r.status_code}: {r.text[:300]}"
        )
    data = r.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise HTTPException(502, f"LLM response shape unexpected: {e}")
    # Debug: log to server stderr so we can see what LLM returns
    import sys as _sys
    print(
        f"[promote-to-skill] LLM response: text_len={len(text) if isinstance(text, str) else 'N/A'}, "
        f"finish_reason={data['choices'][0].get('finish_reason')}, "
        f"first_100={(text[:100] if isinstance(text, str) else 'N/A')!r}",
        file=_sys.stderr,
    )
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(502, f"LLM returned empty content (text type={type(text).__name__})")
    # Strip reasoning traces
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    # Strip outer code fence (defensive — most models comply, some don't)
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text.startswith("---"):
        # Last-ditch fix: if the LLM wrapped the frontmatter with text
        # before it, find the first `---` line and trim.
        idx = text.find("\n---\n")
        if idx == -1:
            idx = text.find("---")
        if idx > 0:
            text = text[idx:].lstrip()
    return text


def _validate_skill_md(content: str, skill_name: str) -> tuple[bool, str]:
    """Validate the LLM-produced SKILL.md against the 4-layer rules.

    Returns (ok, error_message). On failure, the caller should refuse
    to push the file and tell the user to re-run / hand-fix.

    Checks (per the hermes Phase 5 anti-dump assertions, adapted):
      1. Non-empty.
      2. Starts with `---` (YAML frontmatter at byte 0).
      3. Frontmatter parses as YAML mapping.
      4. `name` == skill_name.
      5. `description` present.
      6. `description` starts with "Use when" (case-insensitive).
      7. `description` ≤ 60 chars.
      8. `metadata.hermes.related_skills` includes
         `project-trace-to-skill-conversion`.
      9. Body has no `[cite:`, no `task.completed@`, no
         `DECISION: PASS/FAIL`, no `coord_pickup`,
         no `iteration_completed@`.
     10. Body has no L3-flavoured heading (e.g. `## Plan History`,
         `## Coord Verdicts`).
     11. File size ≤ 100 KB.
    """
    if not content or not content.strip():
        return False, "empty content"
    if not content.startswith("---\n"):
        return False, "missing YAML frontmatter (must start with --- at byte 0)"
    m = re.search(r"\n---\s*\n", content[3:])
    if not m:
        return False, "frontmatter not closed (no second --- line found)"
    fm_text = content[3 : m.start() + 3]
    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        return False, f"YAML parse error in frontmatter: {e}"
    if not isinstance(fm, dict):
        return False, "frontmatter is not a YAML mapping"
    for fld in ("name", "description"):
        if fld not in fm or not str(fm.get(fld, "")).strip():
            return False, f"frontmatter missing required field: {fld}"
    if str(fm["name"]).strip() != skill_name:
        # Soft check: the LLM may propose a more descriptive name
        # (e.g. user asked for `bus` but LLM suggests `kmb-route-stops`).
        # The actual file_path is fixed by the user (skills/<skill_name>/
        # SKILL.md), so a frontmatter name mismatch is just a labelling
        # nit — log a warning but accept.
        import sys as _sys
        print(
            f"[promote-to-skill] NOTE: frontmatter name {fm['name']!r} "
            f"differs from requested skill_name {skill_name!r} "
            f"(file_path will use the requested one)",
            file=_sys.stderr,
        )
    desc = str(fm["description"]).strip()
    if not desc.lower().startswith("use when"):
        return False, (
            f"description must start with 'Use when' (got: {desc[:60]!r})"
        )
    if len(desc) > _SKILL_MAX_DESCRIPTION_CHARS:
        return False, (
            f"description too long: {len(desc)} chars > "
            f"{_SKILL_MAX_DESCRIPTION_CHARS} max"
        )
    rh = (
        fm.get("metadata", {})
        .get("hermes", {})
        .get("related_skills", [])
    )
    if not isinstance(rh, list) or "project-trace-to-skill-conversion" not in rh:
        return False, (
            "metadata.hermes.related_skills must include "
            "'project-trace-to-skill-conversion' (the anti-pattern "
            "propagation reference)"
        )

    body = content[m.end() + 3 :].strip()
    body_lower = body.lower()
    for token in _L3_LITERAL_TOKENS:
        if token.lower() in body_lower:
            return False, f"body still contains L3 token {token!r} (not dropped)"
    heading_re = re.compile(r"^##\s+.*$", re.MULTILINE)
    for heading in heading_re.findall(body):
        h_lower = heading.lower()
        for phrase in _L3_PHRASES:
            if phrase in h_lower:
                return False, f"body has L3-flavoured heading {heading!r}"

    if len(content) > _SKILL_MAX_FILE_BYTES:
        return False, (
            f"file too large: {len(content)} bytes > "
            f"{_SKILL_MAX_FILE_BYTES} cap"
        )
    return True, ""

