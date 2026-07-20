"""Background scheduler for recurring project runs (#22).

The orchestrator's main process holds a singleton `Scheduler` instance.
On lifespan startup we start a daemon asyncio task that ticks every
TICK_SECONDS, queries all enabled `project_schedules`, and fires any
whose `next_fire_at` has passed.

Three execution modes (per-schedule, set at create time):
- `clone`  — every fire creates a new project (clone of the template's
  plan, fresh ID, new audit trail). The standard "GitHub Actions
  workflow_run" pattern. The supervisor's next tick re-derives the
  task list by calling the LLM planner with the template's goal.
  Each fire may produce a slightly different plan (LLM non-determinism).
- `deterministic` — every fire creates a new project AND copies the
  template's tasks table 1:1 (same name / role / action / params /
  depends_on, only IDs change). No LLM call. Use when the user has
  dialed in a workflow and wants the EXACT same task list every
  cycle (e.g. fetch_X, compose_Y, send_Z). State goes straight to
  `ready`, skipping planning.
- `append` — the first fire creates a new project; subsequent fires
  add a fresh task batch to the most recent non-terminal project for
  this schedule. Use when the user wants a single long-lived project
  that accumulates "daily" task batches (e.g. an ongoing monitoring
  dashboard where each day's data lands in a new task).

Skip rule (#22 Q2): if a previous run from the same schedule is still
in a non-terminal state (`planning` or `running`), the new fire is
logged and skipped. Schedule doesn't queue, doesn't block, doesn't
double-run. A monitor-style "every minute" schedule that takes 3
minutes per run will see 2 of 3 fires skipped — by design.

Concurrency: only ONE scheduler instance should be running per
orchestrator process. We hold a class-level singleton and the
lifespan startup calls `start()` exactly once.

The scheduler does NOT call the LLM planner itself; firing a schedule
delegates to the existing supervisor, which already handles `planning`
→ `ready` → `running` → `completed` for regular projects. The
scheduled clone is just a normal `create_project` call followed by a
state transition to `planning` (so the supervisor picks it up on its
next tick, like a user-created project). The exception is
`deterministic`, which inserts tasks directly and goes straight to
`ready`.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from croniter import croniter

from hermes_orch.api.projects import (
    _project_id, _serialize_plan_md,  # reuse private helpers
)
from hermes_orch.core.audit import audit_log
from hermes_orch.utils import now_iso as _now_iso

logger = logging.getLogger(__name__)

# Tick interval: how often the scheduler wakes up to check for due
# schedules. 30s is a good balance — sub-minute cron precision isn't
# useful for human-scale recurring work, and faster ticks just hammer
# the DB. Adjust via env if needed (see Settings page in the future).
TICK_SECONDS = 30

# Non-terminal project states. Used for the skip rule. Mirrors the
# state names set by `create_project` and the supervisor. If the
# project is in one of these, a previous run is "still active" and
# the new fire is skipped.
_NON_TERMINAL_STATES = ("planning", "running", "ready")


class Scheduler:
    """Background scheduler singleton.

    Lifetime: created in main.py lifespan, started after the
    supervisor, stopped before DB close. The scheduler holds a
    reference to the live Database object (not a global) so it
    reads/writes the same DB the supervisor and the API use.
    """

    def __init__(self, db, cfg: dict[str, Any]) -> None:
        self.db = db
        self.cfg = cfg
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    # ===== lifecycle =====

    def start(self) -> None:
        """Start the background tick loop. Idempotent."""
        if self._task and not self._task.done():
            logger.debug("Scheduler.start: already running")
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="hermes-scheduler")
        logger.info("Scheduler started, tick=%ds", TICK_SECONDS)

    async def stop(self) -> None:
        """Stop the tick loop and wait for the current tick to finish."""
        if not self._task:
            return
        self._stopped.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        except Exception as e:
            logger.warning("Scheduler.stop error: %s", e)
        self._task = None
        logger.info("Scheduler stopped")

    # ===== main loop =====

    async def _run(self) -> None:
        """Tick every TICK_SECONDS, fire any due schedules."""
        # First tick: prime next_fire_at for all enabled schedules that
        # don't have one yet (defensive — usually set at create time).
        try:
            await self._prime_next_fires()
        except Exception as e:
            logger.warning("scheduler: prime_next_fires failed: %s", e)
        while not self._stopped.is_set():
            try:
                await self._tick()
            except Exception as e:
                # Never let a tick error kill the loop. Log and keep going.
                logger.exception("scheduler: tick error: %s", e)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass  # normal — loop again

    async def _tick(self) -> None:
        """One pass: find due schedules, fire each (with skip rule)."""
        now_utc = datetime.now(timezone.utc)
        due = await self.db.fetchall(
            "SELECT * FROM project_schedules "
            "WHERE enabled = 1 AND next_fire_at IS NOT NULL "
            "AND next_fire_at <= ?",
            (_now_iso(),),
        )
        if not due:
            return
        logger.info("scheduler: %d due schedule(s)", len(due))
        for sched in due:
            try:
                fired = await self._fire(sched, now_utc)
                # Always update next_fire_at so we don't re-fire the same
                # slot. If the fire was skipped, next_fire_at moves to
                # the next cron slot (so the schedule is "still alive"
                # but the current window is done).
                await self._advance_next_fire(sched)
                if fired:
                    logger.info(
                        "scheduler: fired schedule %s (%s) -> project %s",
                        sched["id"], sched["name"], fired,
                    )
            except Exception as e:
                logger.exception(
                    "scheduler: fire failed for schedule %s: %s", sched["id"], e
                )

    # ===== fire logic =====

    async def _fire(self, sched: dict[str, Any], now_utc: datetime) -> str | None:
        """Execute one schedule fire. Returns the new project id (or None if skipped)."""
        # Skip rule (#22 Q2): if any project from this schedule is still
        # in a non-terminal state, skip. Cheap pre-check before we even
        # look up the template.
        active = await self.db.fetchone(
            "SELECT id FROM projects "
            "WHERE source_schedule_id = ? AND state IN (?, ?, ?) "
            "LIMIT 1",
            (sched["id"], *_NON_TERMINAL_STATES),
        )
        if active:
            reason = f"previous run {active['id']} still active"
            logger.info(
                "scheduler: skip schedule %s (%s) — %s",
                sched["id"], sched["name"], reason,
            )
            await self.db.execute(
                "UPDATE project_schedules SET last_skip_reason = ?, "
                "updated_at = ? WHERE id = ?",
                (reason, _now_iso(), sched["id"]),
            )
            await audit_log(
                self.db, "schedule.skipped",
                actor="scheduler",
                payload={"schedule_id": sched["id"], "reason": reason},
            )
            return None

        template = await self.db.fetchone(
            "SELECT * FROM projects WHERE id = ?",
            (sched["template_project_id"],),
        )
        if not template:
            logger.warning(
                "scheduler: schedule %s references missing template %s",
                sched["id"], sched["template_project_id"],
            )
            return None
        if not template.get("is_template"):
            logger.warning(
                "scheduler: schedule %s template %s is not marked is_template=1; "
                "auto-marking so the schedule can keep running",
                sched["id"], template["id"],
            )
            await self.db.execute(
                "UPDATE projects SET is_template = 1 WHERE id = ?",
                (template["id"],),
            )

        mode = (sched.get("mode") or "clone").lower()
        if mode == "clone":
            return await self._fire_clone(sched, template)
        elif mode == "deterministic":
            return await self._fire_clone_deterministic(sched, template)
        elif mode == "append":
            return await self._fire_append(sched, template)
        else:
            logger.error("scheduler: schedule %s has unknown mode=%r", sched["id"], mode)
            return None

    async def _fire_clone(
        self, sched: dict[str, Any], template: dict[str, Any]
    ) -> str:
        """Clone mode: create a brand-new project from the template's plan.

        The new project starts in `planning` state so the supervisor
        will pick it up on its next tick and dispatch the LLM planner
        if needed (or, for manual-mode templates, transition straight
        to `ready` if the plan is fully denormalized into tasks).
        """
        new_id = _project_id()
        now = _now_iso()
        # Carry the template's iter-loop fields (coordinator_role, etc.)
        # so the cloned project runs with the same iteration rules.
        # We do NOT carry the task list — the supervisor re-derives it
        # from the cloned plan.md (single source of truth).
        await self.db.insert(
            "projects",
            {
                "id": new_id,
                "name": _next_clone_name(template.get("name") or template["id"], now_utc_str:=_now_iso()),
                "goal": template.get("goal") or "",
                "state": "planning",
                "coordinator_role": template.get("coordinator_role") or "",
                "accept_criteria": template.get("accept_criteria") or "",
                "deliverable_path": template.get("deliverable_path") or "",
                "max_iterations": int(template.get("max_iterations") or 0),
                "current_iteration": 0,
                "last_iteration_summary": "",
                "source_schedule_id": sched["id"],
            },
        )
        # Copy the on-disk artifacts (plan.md, status.md, decisions.md,
        # memory/ folder, etc.) so the new project starts with the
        # template's curated structure. We don't copy `current_session_id`
        # — sessions are per-profile-namespaced, and a new project
        # naturally wants fresh sessions.
        await self._clone_project_files(template["id"], new_id)
        await audit_log(
            self.db, "schedule.fired_clone",
            actor="scheduler",
            project_id=new_id,
            payload={
                "schedule_id": sched["id"],
                "template_project_id": template["id"],
                "mode": "clone",
            },
        )
        return new_id

    async def _fire_clone_deterministic(
        self, sched: dict[str, Any], template: dict[str, Any]
    ) -> str:
        """Deterministic clone: same as `_fire_clone` for project creation,
        but ALSO copies the template's `tasks` table rows 1:1 into the new
        project and goes straight to `ready` (skipping planning).

        The supervisor never sees a `planning` state, so the LLM planner
        is NEVER called. The task list for every fire is the same as
        the template's, only with fresh task IDs and `project_id`.

        Use case: a workflow the user has dialed in and wants repeated
        exactly (fetch_X, compose_Y, send_Z). The `clone` mode's LLM
        re-derive produces similar but not identical plans each cycle —
        this avoids that.

        Fields copied per task:
        - name, agent_role, action, params
        - depends_on (mapped via name → new id)
        - on_parent_failure, priority, max_retries, timeout_seconds
        - required_capability (so smart dispatch still routes correctly)
        - output_path

        Fields NOT copied (always fresh on the new project):
        - id (new t-xxxxxxxx)
        - project_id (the new project's id)
        - assigned_agent_id, assigned_profile_id (dispatch hasn't run yet)
        - status (always 'pending' on the new project)
        - retry_count, last_liveness_at, error, result
        - created_at, updated_at (now)

        If the template has no tasks, fall back to `_fire_clone` (LLM
        regen) so the user isn't stuck with a non-functional schedule.
        We log a warning so the dashboard can show "deterministic
        template is empty — falling back to LLM regen" if it wants.
        """
        new_id = _project_id()
        now = _now_iso()
        # Carry the template's iter-loop fields + goal. Same as
        # _fire_clone: tasks are denormalized separately, but the
        # iter-loop config (coordinator_role, accept_criteria, etc.)
        # still applies at the project level.
        await self.db.insert(
            "projects",
            {
                "id": new_id,
                "name": _next_clone_name(template.get("name") or template["id"], now_utc_str:=_now_iso()),
                "goal": template.get("goal") or "",
                "state": "ready",  # skip planning — tasks already inserted below
                "coordinator_role": template.get("coordinator_role") or "",
                "accept_criteria": template.get("accept_criteria") or "",
                "deliverable_path": template.get("deliverable_path") or "",
                "max_iterations": int(template.get("max_iterations") or 0),
                "current_iteration": 0,
                "last_iteration_summary": "",
                "source_schedule_id": sched["id"],
            },
        )
        # Copy on-disk artifacts (plan.md, status.md, decisions.md,
        # memory/) for historical context. Same as _fire_clone.
        await self._clone_project_files(template["id"], new_id)
        # Read the template's tasks 1:1. Each task gets a new t-xxx id,
        # a fresh project_id, status='pending', retry_count=0. We
        # remap depends_on via task NAME (template's depends_on stores
        # task ids; we replace each with the new id of the same name).
        template_tasks = await self.db.fetchall(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at ASC",
            (template["id"],),
        )
        if not template_tasks:
            logger.warning(
                "scheduler: deterministic mode but template %s has no tasks; "
                "falling back to _fire_clone (LLM regen) for schedule %s",
                template["id"], sched["id"],
            )
            await self.db.execute(
                "DELETE FROM projects WHERE id = ?", (new_id,)
            )
            # Remove the folder we just created
            try:
                import shutil
                shutil.rmtree(self._project_dir(new_id), ignore_errors=True)
            except Exception:
                pass
            # Fall through to regular clone. We need to set state back
            # to planning so the supervisor will call the planner.
            return await self._fire_clone_with_state(sched, template, state="planning")
        # Two-pass insert: first collect name→new_id mapping, then wire
        # depends_on using that mapping.
        import json as _json
        import uuid as _uuid
        name_to_new_id: dict[str, str] = {}
        for t in template_tasks:
            new_tid = "t-" + _uuid.uuid4().hex[:8]
            name_to_new_id[t["name"]] = new_tid
            await self.db.insert(
                "tasks",
                {
                    "id": new_tid,
                    "project_id": new_id,
                    "name": t.get("name") or "",
                    "agent_role": t.get("agent_role") or "",
                    "assigned_agent_id": None,
                    "assigned_profile_id": None,
                    "depends_on": _json.dumps([]),  # wired in 2nd pass
                    "on_parent_failure": t.get("on_parent_failure") or "skip",
                    "status": "pending",
                    "priority": t.get("priority") or "normal",
                    "action": t.get("action") or "",
                    "params": t.get("params") or "{}",
                    "retry_count": 0,
                    "max_retries": int(t.get("max_retries") or 2),
                    "timeout_seconds": int(t.get("timeout_seconds") or 1800),
                    "output_path": t.get("output_path") or "",
                    "last_liveness_at": None,
                    "error": None,
                    "result": None,
                    "required_capability": t.get("required_capability"),
                },
            )
        # Second pass: depends_on. Template depends_on stores task IDs,
        # but if those IDs are stale (template was created before
        # denormalization) or just for robustness, we try ID first
        # then fall back to name match.
        template_id_to_name = {t["id"]: t["name"] for t in template_tasks}
        for t in template_tasks:
            raw_deps = t.get("depends_on") or "[]"
            try:
                old_dep_ids = _json.loads(raw_deps) if isinstance(raw_deps, str) else list(raw_deps)
            except (ValueError, TypeError):
                old_dep_ids = []
            new_dep_ids = []
            for old_id in old_dep_ids:
                # Prefer id-lookup; fall back to name-lookup if the
                # template's task id doesn't exist anymore (e.g. task
                # was deleted after the template was marked).
                if old_id in name_to_new_id:
                    new_dep_ids.append(name_to_new_id[old_id])
                else:
                    old_name = template_id_to_name.get(old_id)
                    if old_name and old_name in name_to_new_id:
                        new_dep_ids.append(name_to_new_id[old_name])
            if new_dep_ids:
                await self.db.execute(
                    "UPDATE tasks SET depends_on = ? WHERE id = ?",
                    (_json.dumps(new_dep_ids), name_to_new_id[t["name"]]),
                )
        await audit_log(
            self.db, "schedule.fired_deterministic",
            actor="scheduler",
            project_id=new_id,
            payload={
                "schedule_id": sched["id"],
                "template_project_id": template["id"],
                "mode": "deterministic",
                "task_count": len(template_tasks),
            },
        )
        return new_id

    async def _fire_clone_with_state(
        self, sched: dict[str, Any], template: dict[str, Any], state: str
    ) -> str:
        """Like `_fire_clone` but allows caller to specify the initial
        state. Used by the deterministic fallback path when the
        template has no tasks to copy — we want the supervisor to call
        the LLM planner, so state='planning'. The other project
        creation fields (goal, iter-loop, files) are identical to
        _fire_clone.
        """
        new_id = _project_id()
        now = _now_iso()
        await self.db.insert(
            "projects",
            {
                "id": new_id,
                "name": _next_clone_name(template.get("name") or template["id"], now_utc_str:=_now_iso()),
                "goal": template.get("goal") or "",
                "state": state,
                "coordinator_role": template.get("coordinator_role") or "",
                "accept_criteria": template.get("accept_criteria") or "",
                "deliverable_path": template.get("deliverable_path") or "",
                "max_iterations": int(template.get("max_iterations") or 0),
                "current_iteration": 0,
                "last_iteration_summary": "",
                "source_schedule_id": sched["id"],
            },
        )
        await self._clone_project_files(template["id"], new_id)
        await audit_log(
            self.db, "schedule.fired_clone",
            actor="scheduler",
            project_id=new_id,
            payload={
                "schedule_id": sched["id"],
                "template_project_id": template["id"],
                "mode": "clone",
            },
        )
        return new_id

    async def _fire_append(
        self, sched: dict[str, Any], template: dict[str, Any]
    ) -> str:
        """Append mode: add a new task batch to the most recent active
        project for this schedule, or create one if none exist.

        'Append' here is at the project level: the next supervisor tick
        sees a project in `ready`/`running` state and re-runs the
        template's plan (re-derived from plan.md). We don't duplicate
        rows in the tasks table — that would corrupt the supervisor's
        per-task dedup. Instead, we nudge the project back to
        `planning` so the supervisor re-derives the task list and runs
        the new batch. Tasks that are still `pending` get a fresh shot
        at being dispatched; tasks that are `completed` are left alone.
        """
        target = await self.db.fetchone(
            "SELECT id, state FROM projects "
            "WHERE source_schedule_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (sched["id"],),
        )
        if not target or target["state"] in ("completed", "archived", "deleted"):
            # No live project, or the last one is in a terminal state —
            # spawn a fresh one (same as clone for the first run).
            return await self._fire_clone(sched, template)
        # Nudge the project back to planning so the supervisor re-derives
        # the task list and dispatches the new batch.
        await self.db.execute(
            "UPDATE projects SET state = 'planning', updated_at = ? "
            "WHERE id = ?",
            (_now_iso(), target["id"]),
        )
        await audit_log(
            self.db, "schedule.fired_append",
            actor="scheduler",
            project_id=target["id"],
            payload={
                "schedule_id": sched["id"],
                "template_project_id": template["id"],
                "mode": "append",
            },
        )
        return target["id"]

    # ===== helpers =====

    async def _clone_project_files(self, src_id: str, dst_id: str) -> None:
        """Copy the template's project folder to the new project.

        We copy plan.md (source of truth for the task list), status.md,
        decisions.md, and the memory/ folder (L1 trace, L2 facts, L3
        state). We do NOT copy `current_session_id` / `current_sessions_json`
        because hermes session IDs are namespaced per profile and a new
        project naturally wants fresh sessions.

        Falls back to writing a fresh plan.md if the template folder
        is missing (defensive — could happen if a user deletes the
        template folder manually).
        """
        from hermes_orch.api.projects import _parse_plan_md  # local import: avoid cycle

        src_dir = self._project_dir(src_id)
        dst_dir = self._project_dir(dst_id, create=True)
        if src_dir.exists():
            for f in ("plan.md", "status.md", "decisions.md", "facts.md", "state.md"):
                sp = src_dir / f
                if sp.exists():
                    (dst_dir / f).write_bytes(sp.read_bytes())
            # Copy memory subdir (L1 trace, etc.) if present
            mem_src = src_dir / "memory"
            mem_dst = dst_dir / "memory"
            if mem_src.exists():
                import shutil
                shutil.copytree(mem_src, mem_dst, dirs_exist_ok=True)
        else:
            logger.warning(
                "scheduler: template folder missing for %s; writing fresh plan.md",
                src_id,
            )
            fm = {"project_id": dst_id, "state": "planning", "created_at": _now_iso(), "tasks": []}
            body = f"\n# Project (cloned from {src_id})\n"
            (dst_dir / "plan.md").write_text(_serialize_plan_md(fm, body), encoding="utf-8")
            (dst_dir / "status.md").write_text(
                _serialize_plan_md({"state": "planning", "last_updated": _now_iso()},
                                   "\n# Status\n\nCloned from schedule.\n"),
                encoding="utf-8",
            )

    def _project_dir(self, project_id: str, create: bool = False) -> Path:
        """Resolve the on-disk project directory.

        Mirrors `api/projects._projects_root`. We don't import that
        helper directly because it's an HTTP-handler function and
        pulling the request scope from a background task is fragile.
        Instead we read the same config key.
        """
        root = Path(self.cfg["projects"]["storage_root"]).resolve()
        if create:
            (root / project_id).mkdir(parents=True, exist_ok=True)
            (root / project_id / "agents").mkdir(exist_ok=True)
        return root / project_id

    async def _prime_next_fires(self) -> None:
        """Set next_fire_at for schedules that don't have one yet (defensive)."""
        rows = await self.db.fetchall(
            "SELECT id, cron_expr, timezone FROM project_schedules "
            "WHERE enabled = 1 AND next_fire_at IS NULL"
        )
        now_utc = datetime.now(timezone.utc)
        for r in rows:
            try:
                nxt = self._compute_next_fire(r["cron_expr"], r["timezone"], now_utc)
                await self.db.execute(
                    "UPDATE project_schedules SET next_fire_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (nxt, _now_iso(), r["id"]),
                )
            except Exception as e:
                logger.warning("scheduler: prime_next_fire failed for %s: %s", r["id"], e)

    async def _advance_next_fire(self, sched: dict[str, Any]) -> None:
        """Move the schedule's next_fire_at to the following cron slot."""
        now_utc = datetime.now(timezone.utc)
        try:
            nxt = self._compute_next_fire(
                sched["cron_expr"], sched.get("timezone") or "UTC", now_utc
            )
        except Exception as e:
            logger.warning("scheduler: compute_next_fire failed for %s: %s", sched["id"], e)
            return
        await self.db.execute(
            "UPDATE project_schedules SET next_fire_at = ?, "
            "last_fired_at = ?, updated_at = ? WHERE id = ?",
            (nxt, _now_iso(), _now_iso(), sched["id"]),
        )

    def _compute_next_fire(
        self, cron_expr: str, tz_name: str, now_utc: datetime
    ) -> str:
        """Compute the next fire time as an ISO local string.

        `croniter` needs a timezone-aware datetime to evaluate cron
        expressions in a specific zone. We convert UTC → local, run
        croniter, then format the result in the local zone.
        """
        try:
            tz = ZoneInfo(tz_name or "UTC")
        except Exception:
            tz = ZoneInfo("UTC")
        # croniter iterates in naive time; pass a naive local datetime.
        now_local = now_utc.astimezone(tz).replace(tzinfo=None)
        itr = croniter(cron_expr, now_local)
        nxt_local = itr.get_next(datetime)
        # Attach tz for proper ISO formatting
        nxt_aware = nxt_local.replace(tzinfo=tz)
        # We store local-with-offset so the dashboard renders in the
        # user's zone. Mirror `_now_iso` format (no Z suffix, +HH:MM).
        return nxt_aware.isoformat()


def _next_clone_name(base: str, now_iso: str) -> str:
    """Generate a clone name like 'Daily Report (2026-07-20 22:00)'.

    Keeps the template's name recognizable, adds a timestamp so
    dashboard rows are easy to tell apart at a glance. If the base
    name is empty (template had no name), use the project id.
    """
    # Trim microseconds, keep seconds; format like 'YYYY-MM-DD HH:MM:SS'
    short = now_iso[:19].replace("T", " ")
    return f"{base} ({short})" if base else f"cloned-{short.replace(' ', '-')}"
