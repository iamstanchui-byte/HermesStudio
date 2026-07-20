"""Supervisor — background loop that drives project lifecycle.

Runs as an asyncio task started from FastAPI's lifespan. Every N seconds:

  For each project:
    1. If state=planning  -> call LLM Planner -> save tasks -> state=ready
    2. If state=ready|running:
       a) Find pending tasks with all deps satisfied
       b) Assign each to an available agent with matching role
          (state pending -> assigned, set assigned_agent_id + assigned_profile_id)
       c) Move assigned tasks to running (MVP: immediately; real wrapper
          dispatch will replace this later)
       d) Propagate failures: for each failed task, mark downstream
          pending/assigned tasks as skipped
       e) Project state transitions: ready -> running (first task runs);
          running -> completed (all done)

  Errors are caught and sent to the notifier (Telegram if enabled), then
  logged. Supervisor never crashes on per-project errors.

Per REVIEW §3.1: state transitions use fetch+check+update pattern (not
"update WHERE + check new state") — the new state is always the target.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from hermes_orch.core.audit import audit_log
from hermes_orch.core.notifier import Notifier
from hermes_orch.core.planner import Planner
from hermes_orch.utils import now_iso as _now_iso, now_aware

log = logging.getLogger("hermes_orch.supervisor")


async def _load_role_skills(db: Any) -> dict[str, list[str]]:
    """Build a {role_name: [skill_name, ...]} map from the latest applied
    (or pending) profile_configs per profile.

    Skills are `profile_configs.file_path` values starting with 'skills/'
    (e.g. 'skills/mt5.md' → skill name 'mt5'). For each role we look at all
    profiles with that name, find the latest version of each skill, and
    union the result. Skills marked as deleted (applied with empty content)
    are excluded.
    """
    rows = await db.fetchall(
        "SELECT ap.name AS role, pc.file_path, pc.desired_content, pc.status, pc.created_at "
        "FROM profile_configs pc "
        "JOIN agent_profiles ap ON ap.id = pc.profile_id "
        "WHERE pc.file_path LIKE 'skills/%' "
        "ORDER BY ap.name ASC, pc.file_path ASC, pc.created_at DESC"
    )
    # Keep only the newest version per (role, file_path)
    seen: set[tuple[str, str]] = set()
    out: dict[str, list[str]] = {}
    for r in rows:
        key = (r["role"], r["file_path"])
        if key in seen:
            continue
        seen.add(key)
        if r["status"] == "applied" and (r["desired_content"] or "") == "":
            continue  # deleted
        name = r["file_path"].removeprefix("skills/").removesuffix(".md")
        out.setdefault(r["role"], []).append(name)
    return out


async def _load_role_capabilities(db: Any) -> dict[str, dict[str, bool]]:
    """Build a {role_name: {capability: bool}} map from agent_profiles.capabilities.

    Phase 4 (smart dispatch): operators curate a JSON map per profile like
    `{"mt5": true, "xauusd_feed": true, "fred_csv": false}`. The planner
    uses this to decide whether to set `required_capability` on a task.
    The supervisor uses this to fail-fast with `dispatch.mismatch` if a
    task with `required_capability=X` lands on a profile without X.

    Profiles with empty `{}` capabilities default to "can do anything"
    (we return `{}` and the supervisor treats that as permissive). To
    actually enforce, the operator must explicitly set `false` for the
    capabilities the role should NOT have.

    Important: we KEEP false-valued entries in the union. If profile X
    has `{"mt5": false}` and the operator's intent is "X cannot do mt5",
    we must propagate that. If we filtered false (the v-if-trick), then
    `{"mt5": false}` would collapse to `{}` and the supervisor would
    treat the role as permissive — silent failure. The planner also
    needs to see the explicit false to avoid assigning mt5 tasks to X.
    """
    rows = await db.fetchall("SELECT name, capabilities FROM agent_profiles")
    out: dict[str, dict[str, bool]] = {}
    for r in rows:
        caps_raw = r.get("capabilities")
        if not caps_raw:
            continue
        try:
            parsed = json.loads(caps_raw) if isinstance(caps_raw, str) else caps_raw
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        # Union across all profiles of the same role. If ANY profile of
        # a role has capability X=true, the role "has" it (supervisor may
        # dispatch to one of those profiles). If ALL profiles have X=false,
        # the role doesn't have it (dispatch.mismatch).
        role_caps = out.setdefault(r["name"], {})
        for k, v in parsed.items():
            k = str(k)
            v = bool(v)
            if v:
                # Any true wins. Don't overwrite an existing false.
                role_caps[k] = True
            elif k not in role_caps:
                # Only set false if we haven't seen a true from another
                # profile of the same role.
                role_caps[k] = False
    return out


class Supervisor:
    def __init__(self, db: Any, cfg: dict[str, Any], notifier: Notifier, planner: Planner):
        self.db = db
        self.cfg = cfg
        self.notifier = notifier
        self.planner = planner
        self.interval = int((cfg.get("supervisor") or {}).get("poll_interval_seconds", 5))
        self.stuck_minutes = int((cfg.get("supervisor") or {}).get("stuck_planning_warn_minutes", 10))
        # Last time the session-cleanup sweep ran (None = never). Used
        # by _maybe_sweep_sessions to throttle to ~once per hour.
        self._last_sweep_at: "datetime | None" = None
        # Last time the project-cleanup sweep ran (None = never). Used
        # by _maybe_sweep_projects to throttle to once per ~24h.
        self._last_project_sweep_at: "datetime | None" = None
        # CleanupJob reference (set by main.py after construction).
        # supervisor only invokes it; the same instance is also exposed
        # via app.state.cleanup for the API endpoints.
        self._cleanup_job: Any = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def set_cleanup_job(self, job: Any) -> None:
        """Inject the CleanupJob instance (called by main.py after
        both objects are constructed). Lets the supervisor's daily
        tick call job.run() without re-creating the job each time,
        and ensures the API endpoints (which read app.state.cleanup)
        and the supervisor share the same instance (and therefore
        the same _last_run_at state)."""
        self._cleanup_job = job

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="hermes-supervisor")
        # Phase 3: fire-and-forget user-level recent.md regen at startup
        # so the planner has a fresh cross-project summary from the
        # first goal it sees. Non-blocking -- if it fails, the manual
        # POST /memory/recent/regenerate endpoint is the fallback.
        try:
            from hermes_orch.core.memory import get_memory_writer
            from hermes_orch.core.synthesis import get_recent_generator
            recent_gen = get_recent_generator(db=self.db)
            memory = get_memory_writer()
            asyncio.create_task(
                recent_gen.regenerate_recent_async(
                    memory_writer=memory, trigger="startup"
                ),
                name="hermes-recent-regen",
            )
        except Exception as e:
            log.warning(f"startup recent regen trigger failed: {e}")
        log.info(f"supervisor started; interval={self.interval}s")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=self.interval + 2)
            except asyncio.TimeoutError:
                self._task.cancel()
        log.info("supervisor stopped")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as e:
                log.exception(f"supervisor tick crashed: {e}")
                try:
                    await self.notifier.send(
                        "Supervisor tick crashed",
                        f"`{type(e).__name__}: {e}`",
                        level="error",
                    )
                except Exception:
                    pass
            # Hourly session-cleanup sweep. Cheap if nothing to do (one
            # SELECT), runs alongside the main tick loop. We check
            # elapsed wall time against the configured interval.
            try:
                await self._maybe_sweep_sessions()
            except Exception as e:
                log.exception(f"session sweep crashed: {e}")
            # Daily project-cleanup sweep. Hard-deletes projects that
            # have been in 'deleted' state longer than the configured
            # retention. Off by default if cleanup.daily_sweep=false.
            try:
                await self._maybe_sweep_projects()
            except Exception as e:
                log.exception(f"project sweep crashed: {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    # ===== tick (one pass) =====

    async def tick(self) -> None:
        # Stale-agent check: if a 'verified' agent hasn't heartbeated
        # in 90s, mark it 'stale' so the dot color matches the
        # "online" definition (90s heartbeat cutoff). Without this,
        # an agent that died stays 'verified' forever (sticky
        # status) and the dashboard shows contradictory info
        # (green dot + WINDOWS=0).
        try:
            from datetime import timedelta
            cutoff = (now_aware() - timedelta(seconds=90)).isoformat()
            await self.db.execute(
                "UPDATE agents SET status = 'stale' "
                "WHERE status = 'verified' AND last_heartbeat_at < ?",
                (cutoff,),
            )
        except Exception as e:
            log.debug(f"stale-agent scan failed: {e}")

        # Stuck-task check: if a 'running' task's assigned agent is
        # stale (>3 min no heartbeat), the wrapper probably died
        # mid-task. Mark the task as failed so the project can move
        # on (otherwise it sits in 'running' forever and the iter
        # loop's `nonterm` check blocks further progress).
        try:
            from datetime import timedelta
            stuck_cutoff = (now_aware() - timedelta(seconds=180)).isoformat()
            # Find stuck tasks: assigned to a verified agent whose
            # last_heartbeat is older than 3 min. (Note: 'stale' status
            # is the more recent flip; we also catch agents that never
            # got flipped yet because the stale-agent check above runs
            # every tick but the agent's heartbeat has been stale for
            # >90s. The 180s threshold gives the stale-flip a chance
            # to land first.)
            stuck_tasks = await self.db.fetchall(
                "SELECT t.id, t.project_id, t.name, t.assigned_agent_id, "
                "a.last_heartbeat_at "
                "FROM tasks t JOIN agents a ON a.id = t.assigned_agent_id "
                "WHERE t.status = 'running' "
                "AND a.last_heartbeat_at < ?",
                (stuck_cutoff,),
            )
            for st in stuck_tasks:
                await self.db.execute(
                    "UPDATE tasks SET status = 'failed', updated_at = ? "
                    "WHERE id = ? AND status = 'running'",
                    (_now_iso(), st["id"]),
                )
                # Free the profile (it might be stuck claiming this task)
                await self.db.execute(
                    "UPDATE agent_profiles SET status = 'idle', "
                    "current_task_id = NULL, updated_at = ? "
                    "WHERE current_task_id = ?",
                    (_now_iso(), st["id"]),
                )
                await audit_log(
                    self.db, "task.stuck_wrapper",
                    actor="supervisor",
                    project_id=st["project_id"],
                    task_id=st["id"],
                    payload={
                        "name": st["name"],
                        "assigned_agent_id": st["assigned_agent_id"],
                        "last_heartbeat_at": st["last_heartbeat_at"],
                        "reason": "wrapper heartbeat stale > 3min; marking failed",
                    },
                )
                log.warning(
                    f"task {st['id']} ({st['name']}) marked failed: "
                    f"wrapper heartbeat stale"
                )
        except Exception as e:
            log.exception(f"stuck-task scan failed: {e}")

        projects = await self.db.fetchall(
            "SELECT * FROM projects WHERE state IN ('planning','ready','running')"
        )
        for proj in projects:
            try:
                await self._drive_project(proj)
            except Exception as e:
                log.exception(f"drive_project {proj['id']} failed: {e}")
                await self.notifier.send(
                    f"Project {proj['id']} tick error",
                    f"`{type(e).__name__}: {e}`",
                    level="error",
                )

    async def _drive_project(self, proj: dict[str, Any]) -> None:
        pid = proj["id"]
        state = proj["state"]

        if state == "planning":
            await self._handle_planning(proj)
            return

        if state in ("ready", "running"):
            await self._handle_execution(proj)
            return

    # ===== planning -> ready =====

    def _plan_source_label(self) -> str:
        """Return 'llm' / 'llm-fallback' / 'mock' for audit logging.

        - 'llm': the LLM call succeeded and produced the plan
        - 'llm-fallback': the LLM call failed (network, truncation, parse
          error) and we used the deterministic mock plan instead
        - 'mock': the planner is configured in mock-only mode (no api_key)

        Without the 'llm-fallback' state, operators can't tell a working
        LLM plan from a failed one in the audit log.
        """
        if self.planner.mock:
            return "mock"
        if getattr(self.planner, "last_plan_was_fallback", False):
            return "llm-fallback"
        return "llm"

    async def _handle_planning(self, proj: dict[str, Any]) -> None:
        pid = proj["id"]
        goal = proj.get("goal") or proj.get("name") or ""
        # Fetch all available roles across all agents
        profiles = await self.db.fetchall("SELECT DISTINCT name FROM agent_profiles")
        available_roles = [p["name"] for p in profiles]
        if not available_roles:
            await self.notifier.send(
                f"Project {pid} can't plan",
                "No agent profiles registered. Add at least one agent + profile, "
                "or set the project's tasks manually via dashboard.",
                level="warn",
            )
            return
        # Fetch role -> skills map so the planner picks the right role for
        # each step based on what the user has taught each agent.
        role_skills = await _load_role_skills(self.db)
        # Phase 4: fetch role -> capabilities map. The planner injects
        # this into the prompt so it knows which roles can do what, and
        # uses it to set `required_capability` on tasks that need a
        # specific integration (e.g. "mt5", "xauusd_feed").
        role_capabilities = await _load_role_capabilities(self.db)
        # Warn if stuck in planning too long
        await self._maybe_warn_stuck(proj)
        # Call planner
        try:
            plan = await self.planner.plan(goal, available_roles, role_skills, role_capabilities)
        except Exception as e:
            log.warning(f"planner failed for {pid}: {e}")
            # Reset state to 'ready' (manual mode default) so the supervisor
            # doesn't loop forever retrying the same broken plan every
            # tick. The user can fix the goal / retry via /replan.
            await self.db.execute(
                "UPDATE projects SET state = 'ready', updated_at = ? WHERE id = ?",
                (_now_iso(), pid),
            )
            await audit_log(
                self.db, "project.planning_failed",
                actor="supervisor",
                project_id=pid,
                payload={"error": f"{type(e).__name__}: {e}", "goal_preview": goal[:200]},
            )
            await self.notifier.send(
                f"Planner failed for {pid}",
                f"Goal: {goal[:200]}\nError: `{type(e).__name__}: {e}`\nState reset to 'ready'. Use 'Generate plan' on the project page to retry.",
                level="error",
            )
            return
        # Save tasks. First pass: insert all (with name -> id map for deps)
        name_to_id: dict[str, str] = {}
        try:
            for t in plan:
                tid = "t-" + uuid.uuid4().hex[:8]
                name_to_id[t["name"]] = tid
                await self.db.insert("tasks", {
                    "id": tid,
                    "project_id": pid,
                    "name": t["name"],
                    "agent_role": t["agent_role"],
                    "depends_on": json.dumps([]),  # filled in 2nd pass
                    "on_parent_failure": "skip",
                    "status": "pending",
                    "priority": "normal",
                    "action": t["action"],
                    "params": json.dumps(t.get("params") or {}),
                    "retry_count": 0,
                    "max_retries": 2,
                    "timeout_seconds": 1800,
                })
            # Second pass: wire depends_on (map names -> ids)
            for t in plan:
                dep_ids = [name_to_id[d] for d in t.get("depends_on") or []]
                tid = name_to_id[t["name"]]
                await self.db.execute(
                    "UPDATE tasks SET depends_on = ? WHERE id = ?",
                    (json.dumps(dep_ids), tid),
                )
            # Transition project
            now = _now_iso()
            await self.db.execute(
                "UPDATE projects SET state = 'ready', updated_at = ? WHERE id = ?",
                (now, pid),
            )
            await audit_log(
                self.db, "project.plan_generated",
                actor="supervisor",
                project_id=pid,
                payload={
                    "task_count": len(plan),
                    # Report the actual planner used: if the LLM call
                    # failed and we fell back to mock, say so. Previously
                    # this always reported "llm" when self.planner.mock
                    # was False, which hid LLM failures from the audit log.
                    "planner": self._plan_source_label(),
                },
            )
            # Phase 1 of 3-tier memory: append to L2 (facts.md) Plan
            # History section. Best-effort — failure does not affect
            # the audit log or plan save.
            try:
                from hermes_orch.core.memory import get_memory_writer
                action_list = ", ".join(
                    f"{t.get('name','?')}({t.get('action','?')})"
                    for t in plan
                )
                get_memory_writer().append_fact_L2(
                    project_id=pid,
                    section="## Plan History",
                    fact_text=(
                        f"Plan N: {len(plan)} tasks "
                        f"[{action_list[:300]}]"
                        f" — planner={self._plan_source_label()}"
                    ),
                    # Use a timestamp in the cite so multiple plans
                    # (initial + replans) for the same project each get
                    # a distinct, traceable cite_id. The literal "pid"
                    # placeholder was never replaced -- every plan cite
                    # said "@pid" which is uninformative and identical
                    # across plans.
                    cite_id=f"plan_generated@{_now_iso()}",
                )
            except Exception:
                pass
            log.info(
                f"project {pid}: plan generated ({self._plan_source_label()}), "
                f"{len(plan)} tasks -> state=ready"
            )
        except Exception as e:
            log.exception(f"failed to save plan for {pid}: {e}")
            await self.notifier.send(
                f"Failed to save plan for {pid}",
                f"`{type(e).__name__}: {e}`",
                level="error",
            )

    async def _maybe_warn_stuck(self, proj: dict[str, Any]) -> None:
        """Warn (once) if project has been in 'planning' > stuck_minutes."""
        try:
            created_str = (proj.get("created_at") or "").replace("Z", "+00:00")
            created = datetime.fromisoformat(created_str)
            if created.tzinfo is None:
                # SQLite stores naive datetimes — treat as UTC
                created = created.replace(tzinfo=timezone.utc)
        except Exception:
            return
        age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
        if age_min < self.stuck_minutes:
            return
        # Has anyone warned about this project being stuck? Check audit log.
        # Compute the cutoff in Python (local time + offset) for the same
        # reason as the history/tasks days filter: stored created_at is
        # now local with offset, and `datetime('now', '-1 hour')` would
        # return UTC naive — string comparison across the two formats is
        # unreliable. Best-effort: if the SELECT fails for any reason,
        # fall through to the warning (better to warn twice than not at all).
        from datetime import timedelta as _td
        recent_cutoff = (now_aware() - _td(hours=1)).isoformat()
        recent = await self.db.fetchone(
            "SELECT id FROM audit_log WHERE project_id = ? AND event_type = ? "
            "AND created_at > ? LIMIT 1",
            (proj["id"], "project.stuck_in_planning", recent_cutoff),
        )
        if recent:
            return
        await self.notifier.send(
            f"Project {proj['id']} stuck in planning",
            f"Goal: {(proj.get('goal') or '')[:200]}\n"
            f"Age: {age_min:.0f} min\n"
            f"Possible cause: planner failing repeatedly, or no agent profiles.",
            level="warn",
        )
        await audit_log(
            self.db, "project.stuck_in_planning",
            actor="supervisor",
            project_id=proj["id"],
            payload={"age_minutes": age_min},
        )

    # ===== session cleanup sweeper =====

    def _sweep_interval_seconds(self) -> int:
        return int(
            (self.cfg.get("supervisor") or {}).get(
                "session_sweep_interval_seconds", 3600
            )
        )

    def _session_ttl_days(self) -> int:
        # Default 1 day: orchestrator doesn't reuse hermes sessions
        # across tasks, so the long TTL only wastes disk. Operators
        # who need longer history (e.g. for `hermes sessions list`
        # auditing) can set this higher in their config.yaml.
        return int(
            (self.cfg.get("supervisor") or {}).get("session_ttl_days", 1)
        )

    async def _maybe_sweep_sessions(self) -> None:
        """Throttled hourly sweep. Skipped cheaply when not due.

        Configurable via supervisor.session_sweep_interval_seconds.
        The actual sweep logic lives in `sweep_sessions()` so it can
        be invoked synchronously from a CLI without throttling.
        """
        if self._session_ttl_days() <= 0:
            return  # auto-cleanup disabled
        interval = self._sweep_interval_seconds()
        now = now_aware()
        if self._last_sweep_at is not None and (now - self._last_sweep_at).total_seconds() < interval:
            return
        self._last_sweep_at = now
        try:
            await self.sweep_sessions(ttl_days=self._session_ttl_days())
        except Exception as e:
            log.exception(f"session sweep failed: {e}")

    # ===== project cleanup sweeper =====

    def _project_sweep_interval_seconds(self) -> int:
        return int(
            (self.cfg.get("cleanup") or {}).get("sweep_interval_seconds", 86400)
        )

    def _project_retention_days(self) -> int:
        return int((self.cfg.get("cleanup") or {}).get("retention_days", 30))

    def _project_daily_sweep_enabled(self) -> bool:
        return bool((self.cfg.get("cleanup") or {}).get("daily_sweep", True))

    async def _maybe_sweep_projects(self) -> None:
        """Throttled daily project-cleanup sweep.

        Hard-deletes projects in 'deleted' state older than the
        configured retention days. Mirrors _maybe_sweep_sessions.
        Disabled if cleanup.daily_sweep=false. Skipped cheaply when
        not due.
        """
        if not self._project_daily_sweep_enabled():
            return
        if self._project_retention_days() <= 0:
            return  # auto-cleanup disabled (retention_days=0)
        interval = self._project_sweep_interval_seconds()
        now = now_aware()
        if (
            self._last_project_sweep_at is not None
            and (now - self._last_project_sweep_at).total_seconds() < interval
        ):
            return
        self._last_project_sweep_at = now
        job = getattr(self, "_cleanup_job", None)
        if job is None:
            return
        try:
            await job.run(trigger="auto")
        except Exception as e:
            log.exception(f"project sweep failed: {e}")

    async def sweep_sessions(self, *, ttl_days: int, dry_run: bool = False) -> dict:
        """Delete hermes sessions older than `ttl_days` from the
        hermes backend AND mark them as deleted in the orchestrator's
        project_sessions table.

        Two-phase state machine for the row:
          active  -> pending_cleanup  (sweeper picks it up here)
          pending_cleanup -> deleted   (wrapper acks after running
                                        `hermes sessions delete <id>`)

        The wrapper reads `cleanup_session_ids` from each heartbeat
        response and POSTs `/sessions/{id}/cleanup-ack` after the local
        delete succeeds. The heartbeat response is per-agent (filtered
        to profiles owned by that agent), so the right wrapper does the
        right delete.

        Only sessions with `source='orchestrator'` in project_sessions
        are touched. User-created sessions (if we ever support them)
        are not in the table and therefore not affected.

        Returns a small report dict for the CLI / dashboard:
            {"candidates": N, "marked_pending": M, "errors": [...]}
        """
        from datetime import timedelta
        if ttl_days <= 0:
            return {"candidates": 0, "marked_pending": 0, "errors": [], "disabled": True}
        cutoff = (now_aware() - timedelta(days=ttl_days)).isoformat()
        rows = await self.db.fetchall(
            "SELECT id, project_id, session_id, role FROM project_sessions "
            "WHERE status = 'active' AND source = 'orchestrator' "
            "AND COALESCE(last_used_at, created_at) < ? "
            "ORDER BY COALESCE(last_used_at, created_at) ASC "
            "LIMIT 200",
            (cutoff,),
        )
        report = {"candidates": len(rows), "marked_pending": 0, "errors": []}
        if dry_run:
            return report
        for row in rows:
            try:
                await self.db.execute(
                    "UPDATE project_sessions SET status = 'pending_cleanup' "
                    "WHERE id = ?",
                    (row["id"],),
                )
                report["marked_pending"] += 1
            except Exception as e:
                report["errors"].append({"session": row["session_id"], "error": str(e)})
        if report["marked_pending"]:
            log.info(
                f"session cleanup: marked {report['marked_pending']} session(s) as "
                f"pending_cleanup (ttl={ttl_days}d, cutoff={cutoff}). Wrappers "
                f"will delete from local hermes backends on next heartbeat."
            )
        return report

    # ===== execution (ready / running) =====

    async def _handle_execution(self, proj: dict[str, Any]) -> None:
        pid = proj["id"]
        # 1. Propagate failures: any failed task marks downstream pending/assigned as skipped
        await self._propagate_failures(pid)
        # 2. Find pending tasks ready to be assigned (all deps completed)
        ready = await self._find_ready_tasks(pid)
        no_progress = True  # assume no progress until we actually do something
        for t in ready:
            ok = await self._assign_task(t)
            if ok:
                no_progress = False
        # 3. (skip) Assigned tasks stay 'assigned' until wrapper claims them via
        #    POST /api/tasks/{id}/start. Supervisor used to auto-promote to
        #    'running', but that was a stub — real work needs the wrapper.
        # 4. Project state transitions
        await self._maybe_advance_project_state(proj, no_progress)
        # 5. Q2 iteration loop: if the project has a coordinator_role and
        #    max_iterations, AND all current tasks are terminal, advance
        #    current_iteration and dispatch a "review" task to the
        #    coordinator. When the review task's deliverable contains
        #    "DECISION: PASS" (or max iterations hit), the project moves
        #    to completed. Coordinator tasks that create new sub-tasks
        #    will be picked up in the next tick (no manual trigger).
        await self._maybe_iterate(proj)

    # ===== Q2: project iteration loop =====

    def _projects_root(self) -> Path:
        """Path under which per-project folders live. Mirrors api/projects.py
        so the supervisor can read project files directly without going
        through HTTP."""
        from pathlib import Path
        return Path(self.cfg["projects"]["storage_root"]).resolve()

    def _project_dir(self, project_id: str) -> Path:
        return self._projects_root() / project_id

    async def _maybe_iterate(self, proj: dict[str, Any]) -> None:
        """Decide whether to dispatch a new iteration review task.

        Conditions to iterate:
          - project state is 'ready' or 'running'
          - project has a coordinator_role set
          - max_iterations > 0 (0 = single-pass, no iteration)
          - current_iteration < max_iterations
          - all tasks for this project are in a terminal state
            (no pending/assigned/running)
          - and no iteration task is already in flight
            (i.e. no task with action starting "_iteration_review:" exists
             in a non-terminal state)

        If all conditions hold, we increment current_iteration, create a
        single "review" task assigned to coordinator_role, and the
        coordinator agent decides (via its own LLM call against the
        deliverable + accept_criteria) whether to:
          - Write decision.md with "DECISION: PASS" (we read this next tick
            and complete the project)
          - Write decision.md with "DECISION: FAIL" + POST new sub-tasks
            via /api/tasks/ to queue more work
        """
        pid = proj["id"]
        coordinator = (proj.get("coordinator_role") or "").strip()
        max_iter = int(proj.get("max_iterations") or 0)
        if not coordinator or max_iter <= 0:
            return  # not an iterative project
        # Manual-mode projects (no goal) must NOT iterate. The user is
        # still in the design phase (e.g. designing which role to use
        # before writing the goal). Iterating an empty project just
        # makes the coordinator write a confused "DECISION: FAIL: no
        # goal" twice. Wait until the user has a goal.
        if not (proj.get("goal") or "").strip():
            return
        cur_iter = int(proj.get("current_iteration") or 0)
        if cur_iter >= max_iter:
            # Cap reached. If a decision.md is present, read it for the
            # last_iteration_summary. If the project is still 'running'
            # with no in-flight work, complete it.
            await self._maybe_complete_after_iter_cap(proj)
            return

        # Don't iterate while there's work in flight
        nonterm = await self.db.fetchone(
            "SELECT COUNT(*) as n FROM tasks WHERE project_id = ? "
            "AND status NOT IN ('completed', 'skipped', 'cancelled', 'failed', 'interrupted')",
            (pid,),
        )
        any_nonterm = (nonterm or {}).get("n", 0) > 0
        if any_nonterm:
            return  # work still in flight

        # Don't re-iterate if a review task for the current iteration is
        # already pending/assigned/running (avoids duplicate review tasks
        # in the same tick)
        existing = await self.db.fetchone(
            "SELECT id, status FROM tasks WHERE project_id = ? "
            "AND action LIKE '_iteration_review:%' "
            "AND status NOT IN ('completed', 'failed', 'cancelled', 'skipped', 'interrupted') "
            "ORDER BY created_at DESC LIMIT 1",
            (pid,),
        )
        if existing:
            return  # already in flight

        # Consume a just-completed review task's decision (if any).
        # IMPORTANT: we only act on decision.md when the matching review
        # task is in 'completed' state AND has not already been consumed
        # (we mark consumed tasks via result='consumed:<iter>'). This
        # prevents stale-decision auto-completion: after a manual replan,
        # decision.md is unlinked, but a wrapper auto-upload could
        # re-create it from a stale agent cache. The wrapper now skips
        # decision.md (see agent_cli.py auto-upload skip list), so the
        # file stays unlinked until a FRESH review task writes a new one.
        # The status check here is a second line of defense in case a
        # future wrapper regression re-introduces stale uploads.
        # Without the consumed-marker check, a single completed review
        # task would be re-consumed on every subsequent tick (e.g. when
        # the user adds more tasks via replan), which incorrectly bumps
        # current_iteration and (at cap) auto-completes the project.
        latest_review = await self.db.fetchone(
            "SELECT id, status, result FROM tasks WHERE project_id = ? "
            "AND action LIKE '_iteration_review:%' "
            "ORDER BY created_at DESC LIMIT 1",
            (pid,),
        )
        if latest_review and latest_review["status"] == "completed":
            # Skip if already consumed
            if (latest_review.get("result") or "").startswith("consumed"):
                # Already consumed — fall through to dispatch a new review
                pass
            else:
                is_pass = await self._decision_is_pass(proj)
                await self._complete_iterative_project(proj, decision_pass=is_pass)
                # Mark the review task as consumed so the next tick doesn't
                # re-consume it. result is a free-text field; we use a
                # prefix marker that's both human-readable and
                # machine-checkable.
                cur_iter_at_consume = int(proj.get("current_iteration") or 0)
                await self.db.execute(
                    "UPDATE tasks SET result = ?, updated_at = ? WHERE id = ?",
                    (f"consumed:iter={cur_iter_at_consume} pass={is_pass}",
                     _now_iso(), latest_review["id"]),
                )
                return

        # All clear — dispatch a review task
        accept = (proj.get("accept_criteria") or "").strip() or "(no criteria provided)"
        deliverable = (proj.get("deliverable_path") or "").strip() or "(no specific deliverable path)"
        last_summary = (proj.get("last_iteration_summary") or "").strip()
        new_iter = cur_iter + 1
        task_id = "t-" + uuid.uuid4().hex[:8]
        action = (
            f"_iteration_review:{new_iter}: You are the project coordinator. "
            f"This is iteration {new_iter} of {max_iter}. The project goal is: "
            f"\"{(proj.get('goal') or '').strip()}\". The deliverable is at "
            f"`{deliverable}` (under the project folder, accessible via "
            f"`/api/projects/{pid}/files/<rel>`). "
        )
        if last_summary:
            action += f"Previous iteration summary: {last_summary}. "
        action += (
            f"Accept criteria: {accept}. "
            f"Read the deliverable, evaluate against the criteria, then either: "
            f"(a) write `decision.md` to the project root containing exactly the line "
            f"`DECISION: PASS` followed by a 1-2 sentence summary (if the deliverable "
            f"meets the criteria), or "
            f"(b) write `decision.md` with `DECISION: FAIL: <reason>` and POST one or "
            f"more new tasks via POST /api/tasks/ to address the gap "
            f"(set `agent_role` to a role that has the right skills). "
            f"The orchestrator will read decision.md on the next tick."
        )
        await self.db.insert(
            "tasks",
            {
                "id": task_id,
                "project_id": pid,
                "name": f"[coord] review iteration {new_iter}/{max_iter}",
                "agent_role": coordinator,
                "depends_on": json.dumps([]),
                "on_parent_failure": "skip",
                "status": "pending",
                "priority": "high",
                "action": action,
                "params": json.dumps({"yolo": True, "iteration": new_iter}),
                "retry_count": 0,
                "max_retries": 1,
                "timeout_seconds": 1200,
                "output_path": "decision.md",
                # Set created_at explicitly with microsecond precision.
                # SQLite's DEFAULT CURRENT_TIMESTAMP is second-precision,
                # which means tasks created in the same second tie on
                # ORDER BY created_at DESC. That broke the iter loop's
                # "find the latest review task" query (it could return
                # the wrong review and skip the just-dispatched one).
                "created_at": _now_iso(),
            },
        )
        await self.db.execute(
            "UPDATE projects SET current_iteration = ?, updated_at = ? WHERE id = ?",
            (new_iter, _now_iso(), pid),
        )
        # Clear any stale decision.md before the new review task runs.
        # The wrapper's auto-upload now skips decision.md, so this stays
        # unlinked until the coord agent writes a fresh one. Without this,
        # a leftover decision.md from a previous iter could mislead the
        # supervisor on the next tick (this is defense-in-depth; the
        # status==completed check in _maybe_iterate is the primary fix).
        dpath = self._project_dir(pid) / "decision.md"
        try:
            dpath.unlink()
        except FileNotFoundError:
            pass
        await audit_log(
            self.db, "project.iteration_dispatched",
            actor="supervisor",
            project_id=pid,
            payload={
                "iteration": new_iter,
                "max_iterations": max_iter,
                "coordinator_role": coordinator,
                "task_id": task_id,
            },
        )
        log.info(
            "project %s: dispatched iteration %d/%d review task %s to %s",
            pid, new_iter, max_iter, task_id, coordinator,
        )

    async def _decision_is_pass(self, proj: dict[str, Any]) -> bool:
        """Read project_folder/decision.md (latest written) and check for
        a 'DECISION: PASS' line. Returns False if the file is missing or
        the decision isn't a pass."""
        pid = proj["id"]
        dpath = self._project_dir(pid) / "decision.md"
        if not dpath.exists():
            return False
        try:
            text = dpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return False
        # First non-empty line containing "DECISION: PASS" counts as pass.
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("DECISION:") and "PASS" in line.upper():
                return True
        return False

    async def _decision_summary(self, proj: dict[str, Any]) -> str:
        """Read the first 200 chars of decision.md for last_iteration_summary."""
        pid = proj["id"]
        dpath = self._project_dir(pid) / "decision.md"
        if not dpath.exists():
            return ""
        try:
            text = dpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("DECISION:"):
                return line[:200]
        return text[:200]

    async def _complete_iterative_project(
        self, proj: dict[str, Any], *, decision_pass: bool
    ) -> None:
        """Mark the current iteration done (or complete the project at cap).

        Semantics: current_iteration is the number of the iteration that
        has just had its review task consumed. The dispatch code already
        bumped it from N-1 to N when the review task was created, so we
        do NOT increment again here. The cap check is against max_iter
        (the configured limit).

        If cur_iter < max_iter, the project stays in 'ready' state (the
        user can replan to start the next iter, or the supervisor will
        wait for new tasks). If cur_iter >= max_iter, the project is
        marked 'completed'.
        """
        pid = proj["id"]
        now = _now_iso()
        summary = await self._decision_summary(proj)
        cur_iter = int(proj.get("current_iteration") or 0)
        max_iter = int(proj.get("max_iterations") or 0)
        at_cap = cur_iter >= max_iter
        new_state = "completed" if at_cap else "ready"
        await self.db.execute(
            "UPDATE projects SET state = ?, "
            "last_iteration_summary = ?, updated_at = ? WHERE id = ?",
            (new_state,
             summary or ("auto-stopped: hit max_iterations" if not decision_pass else "PASS"),
             now, pid),
        )
        await audit_log(
            self.db, "project.iteration_completed",
            actor="supervisor",
            project_id=pid,
            payload={
                "decision": "PASS" if decision_pass else "CAP_REACHED",
                "current_iteration": cur_iter,
                "max_iterations": max_iter,
                "at_cap": at_cap,
                "summary": summary,
            },
        )
        # Phase 1 of 3-tier memory: append verdict to L2 (facts.md) Coord
        # Verdicts section. Best-effort.
        try:
            from hermes_orch.core.memory import get_memory_writer
            verdict = "PASS" if decision_pass else "FAIL"
            short_summary = (summary or "").replace("\n", " ").replace("\r", " ")
            if len(short_summary) > 200:
                short_summary = short_summary[:200] + "..."
            get_memory_writer().append_fact_L2(
                project_id=pid,
                section="## Coord Verdicts",
                fact_text=(
                    f"Iter {cur_iter}/{max_iter} ({new_state}): "
                    f"DECISION: {verdict}"
                    + (f" -- {short_summary}" if short_summary else "")
                ),
                cite_id=f"iteration_completed@iter={cur_iter}",
            )
        except Exception:
            pass
        # Phase 2 of 3-tier memory: trigger L3 (state.md) regeneration.
        # Best-effort — LLM call failures are logged but don't affect
        # the project's iter state. Triggered here (per spec:
        # "regenerated on every iteration_completed") because that's
        # when the project state is most useful for downstream tasks.
        try:
            from hermes_orch.core.memory import get_memory_writer
            from hermes_orch.core.synthesis import get_state_generator
            memory = get_memory_writer()
            facts_text = memory.read_facts_full(pid) or ""
            state_gen = get_state_generator(db=self.db)
            regen_ok = await state_gen.regenerate_state_async(
                project_id=pid,
                project_meta={
                    "id": pid,
                    "name": proj.get("name", pid),
                    "state": new_state,
                    "current_iteration": cur_iter,
                    "max_iterations": max_iter,
                },
                facts_text=facts_text,
                memory_writer=memory,
                trigger="iteration_completed",
            )
            if regen_ok:
                try:
                    await audit_log(
                        self.db, "project.state_regenerated",
                        actor="synthesis",
                        project_id=pid,
                        payload={
                            "trigger": "iteration_completed",
                            "iter": cur_iter,
                        },
                    )
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"L3 state regen failed for {pid}: {e}")
        # Unlink decision.md after consuming. The wrapper's auto-upload
        # skips decision.md, so this stays unlinked until a future review
        # task writes a fresh verdict. Without this, the file lingers and
        # a future _maybe_iterate call (with the latest review status
        # check bypassed somehow) would read a stale verdict.
        dpath = self._project_dir(pid) / "decision.md"
        try:
            dpath.unlink()
        except FileNotFoundError:
            pass
        if at_cap:
            await self.notifier.send(
                f"Project {pid} completed (iteration {cur_iter}/{max_iter})",
                f"Goal: {(proj.get('goal') or '')[:200]}\nDecision: {summary or '(no summary)'}",
                level="info",
            )
        else:
            log.info(
                "project %s: iteration %d/%d complete (PASS); project stays in 'ready' for next iter",
                pid, cur_iter, max_iter,
            )

    async def _maybe_complete_after_iter_cap(self, proj: dict[str, Any]) -> None:
        """If the project has hit max_iterations and the latest review task
        is in a terminal state (or there's nothing to wait for), complete
        the project. Records the last decision summary."""
        pid = proj["id"]
        # Only act if the project is still in a non-terminal state
        cur = await self.db.fetchone(
            "SELECT state FROM projects WHERE id = ?", (pid,),
        )
        if not cur or cur["state"] not in ("ready", "running"):
            return
        # Has the coordinator written a decision?
        summary = await self._decision_summary(proj)
        # If there's a pending/assigned/running review task, wait for it.
        inflight = await self.db.fetchone(
            "SELECT id FROM tasks WHERE project_id = ? "
            "AND action LIKE '_iteration_review:%' "
            "AND status NOT IN ('completed', 'failed', 'cancelled', 'skipped', 'interrupted') "
            "LIMIT 1",
            (pid,),
        )
        if inflight:
            return
        # No in-flight work and we've hit the cap. Complete.
        decision_pass = "PASS" in (summary or "").upper()
        await self._complete_iterative_project(proj, decision_pass=decision_pass)

    async def _find_ready_tasks(self, project_id: str) -> list[dict[str, Any]]:
        """Pending tasks where all depends_on are completed/skipped.

        Also auto-cancels pending tasks whose deps are no longer
        satisfiable (any dep is failed/cancelled/interrupted) — those
        tasks would never become ready, so leaving them pending just
        blocks the project from being marked complete.
        """
        rows = await self.db.fetchall(
            "SELECT * FROM tasks WHERE project_id = ? AND status = 'pending' "
            "ORDER BY created_at ASC",
            (project_id,),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            deps = json.loads(r.get("depends_on") or "[]")
            if not deps:
                out.append(r)
                continue
            # All deps must be completed (or skipped) to be ready
            placeholders = ",".join("?" for _ in deps)
            dep_rows = await self.db.fetchall(
                f"SELECT id, status FROM tasks WHERE id IN ({placeholders})",
                tuple(deps),
            )
            statuses = {d["status"] for d in dep_rows}
            unsatisfiable = statuses & {"failed", "cancelled", "interrupted"}
            if unsatisfiable:
                # Mark the dependent task cancelled too, with a note
                # explaining which dep caused it. Operator can re-plan
                # or manually re-enable if needed.
                failed_dep = next(
                    d["id"] for d in dep_rows if d["status"] in unsatisfiable
                )
                await self.db.execute(
                    "UPDATE tasks SET status = 'cancelled', updated_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (_now_iso(), r["id"]),
                )
                await audit_log(
                    self.db, "task.auto_cancelled",
                    actor="supervisor",
                    project_id=project_id,
                    task_id=r["id"],
                    payload={
                        "name": r.get("name"),
                        "reason": f"dependency {failed_dep} unsatisfiable",
                        "dep_statuses": {d["id"]: d["status"] for d in dep_rows},
                    },
                )
                log.info(
                    f"task {r['id']} ({r.get('name')}) auto-cancelled: "
                    f"dep {failed_dep} unsatisfiable"
                )
                continue
            if statuses <= {"completed", "skipped"}:
                out.append(r)
        return out

    async def _assign_task(self, task: dict[str, Any]) -> bool:
        """Find an available agent with matching role; mark task assigned.

        Returns True if assignment happened, False if no agent / task already
        taken.
        """
        tid = task["id"]
        role = task["agent_role"]
        # Re-read to avoid races
        cur = await self.db.fetchone(
            "SELECT id, status, project_id, agent_role, required_capability "
            "FROM tasks WHERE id = ?",
            (tid,),
        )
        if not cur or cur["status"] != "pending":
            return False
        # Path A (#22): inject the project's procedure.md into the task row
        # at assignment time. The wrapper reads task.procedure_md and
        # prepends it to the agent's prompt as project context (so the
        # agent sees the workflow steps before doing its work). Reading
        # the file at dispatch time (vs every time the wrapper fetches
        # the project) keeps the wrapper's API surface unchanged and
        # makes the task row self-contained for audit.
        if not cur.get("procedure_md"):
            try:
                proj_root = Path(self.cfg["projects"]["storage_root"]).resolve()
                proc_file = proj_root / cur["project_id"] / "procedure.md"
                if proc_file.exists():
                    proc_text = proc_file.read_text(encoding="utf-8")
                    await self.db.execute(
                        "UPDATE tasks SET procedure_md = ? WHERE id = ?",
                        (proc_text, tid),
                    )
                    cur["procedure_md"] = proc_text
            except Exception as e:
                log.warning("failed to load procedure.md for task %s: %s", tid, e)
        # Find an idle profile for this role (prefer verified agents)
        prof = await self.db.fetchone(
            "SELECT ap.id, ap.agent_id, ap.capabilities FROM agent_profiles ap "
            "JOIN agents a ON a.id = ap.agent_id "
            "WHERE ap.name = ? AND a.status = 'verified' "
            "ORDER BY ap.agent_id LIMIT 1",
            (role,),
        )
        if not prof:
            # Fall back to any profile (even un-verified) so demo still flows
            prof = await self.db.fetchone(
                "SELECT id, agent_id, capabilities FROM agent_profiles WHERE name = ? LIMIT 1",
                (role,),
            )
            if not prof:
                log.info(f"task {tid} (role={role}): no agent has this role")
                return False
        # Phase 4 (smart dispatch): if the task requires a capability the
        # chosen profile doesn't have, fail the task with dispatch.mismatch
        # instead of silently letting the agent fall back to a worse tool
        # (the XAUUSD case: Linux super must use the MT5 bridge, not
        # Yahoo's free feed, or the analysis is built on stale/wrong prices).
        required = task.get("required_capability")
        if required:
            caps_raw = prof.get("capabilities")
            profile_caps: dict[str, bool] = {}
            if caps_raw:
                try:
                    parsed = json.loads(caps_raw) if isinstance(caps_raw, str) else caps_raw
                    if isinstance(parsed, dict):
                        profile_caps = {str(k): bool(v) for k, v in parsed.items()}
                except (json.JSONDecodeError, TypeError):
                    pass
            # If the profile has an explicit capabilities map AND the required
            # capability is not in it (or set to false), fail. We don't fail
            # when capabilities is empty `{}` because the operator hasn't
            # curated it yet — that's the permissive default, intentional
            # so adding a new profile doesn't break old flows.
            if profile_caps and not profile_caps.get(required, False):
                now = _now_iso()
                err_msg = (
                    f"dispatch.mismatch: profile '{role}' (agent {prof['agent_id']}) "
                    f"lacks capability '{required}' "
                    f"(profile has: {sorted(k for k, v in profile_caps.items() if v)})"
                )
                await self.db.execute(
                    "UPDATE tasks SET status = 'failed', error = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (err_msg, now, tid),
                )
                await audit_log(
                    self.db, "dispatch.mismatch",
                    actor="supervisor",
                    project_id=task["project_id"],
                    task_id=tid,
                    agent_id=prof["agent_id"],
                    payload={
                        "role": role,
                        "profile_id": prof["id"],
                        "required_capability": required,
                        "profile_capabilities": profile_caps,
                        "error": err_msg,
                    },
                )
                log.warning(
                    "task %s (%r) FAILED: required_capability=%r but profile %s has %s",
                    tid, task.get("name"), required, prof["id"],
                    sorted(k for k, v in profile_caps.items() if v),
                )
                return False
        now = _now_iso()
        await self.db.execute(
            "UPDATE tasks SET status = 'assigned', assigned_agent_id = ?, "
            "assigned_profile_id = ?, updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (prof["agent_id"], prof["id"], now, tid),
        )
        # Mark the profile as busy so the dashboard reflects activity
        await self.db.execute(
            "UPDATE agent_profiles SET status = 'busy', current_task_id = ?, "
            "updated_at = ? WHERE id = ?",
            (tid, now, prof["id"]),
        )
        await audit_log(
            self.db, "task.assigned",
            actor="supervisor",
            project_id=task["project_id"],
            task_id=tid,
            agent_id=prof["agent_id"],
            payload={
                "profile_id": prof["id"],
                "role": role,
                "required_capability": required,  # Phase 4
            },
        )
        log.info(f"task {tid} ({task.get('name')!r}, role={role}) -> agent {prof['agent_id']}, profile busy")
        return True

    async def _promote_assigned_to_running(self, project_id: str) -> int:
        """Move all assigned tasks to running.

        MVP: do it immediately. Later, this is where the wrapper will actually
        receive the task via heartbeat or push.
        """
        rows = await self.db.fetchall(
            "SELECT id, project_id FROM tasks WHERE project_id = ? AND status = 'assigned'",
            (project_id,),
        )
        n = 0
        for r in rows:
            now = _now_iso()
            await self.db.execute(
                "UPDATE tasks SET status = 'running', last_liveness_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'assigned'",
                (now, now, r["id"]),
            )
            await audit_log(
                self.db, "task.started",
                actor="supervisor",
                project_id=r["project_id"],
                task_id=r["id"],
            )
            n += 1
        return n

    async def _propagate_failures(self, project_id: str) -> None:
        """For each failed task, mark downstream pending/assigned as skipped."""
        failed = await self.db.fetchall(
            "SELECT id FROM tasks WHERE project_id = ? AND status = 'failed'",
            (project_id,),
        )
        if not failed:
            return
        failed_ids = [f["id"] for f in failed]
        # Find any task with depends_on containing a failed id
        placeholders = ",".join("?" for _ in failed_ids)
        dependents = await self.db.fetchall(
            f"SELECT id, name, depends_on FROM tasks WHERE project_id = ? "
            f"AND status IN ('pending', 'assigned') "
            f"AND depends_on LIKE ?",
            (project_id, "%" + failed_ids[0] + "%"),
        )
        # We need to also check tasks that depend on any of the failed ids
        # (SQLite LIKE on JSON is ugly; do a Python pass)
        all_active = await self.db.fetchall(
            "SELECT id, name, depends_on FROM tasks WHERE project_id = ? "
            "AND status IN ('pending', 'assigned')",
            (project_id,),
        )
        failed_set = set(failed_ids)
        for t in all_active:
            try:
                deps = set(json.loads(t.get("depends_on") or "[]"))
            except (json.JSONDecodeError, TypeError):
                deps = set()
            if deps & failed_set:
                now = _now_iso()
                await self.db.execute(
                    "UPDATE tasks SET status = 'skipped', "
                    "error = 'parent task failed', updated_at = ? "
                    "WHERE id = ? AND status IN ('pending', 'assigned')",
                    (now, t["id"]),
                )
                await audit_log(
                    self.db, "task.skipped",
                    actor="supervisor",
                    project_id=project_id,
                    task_id=t["id"],
                    payload={"reason": "parent_failed"},
                )
                log.info(f"task {t['id']} ({t['name']!r}) skipped (parent failed)")

    async def _maybe_advance_project_state(self, proj: dict[str, Any], no_progress: bool) -> None:
        """ready -> running (first task runs); running -> completed (all done)."""
        pid = proj["id"]
        # Re-read current state
        cur = await self.db.fetchone("SELECT state FROM projects WHERE id = ?", (pid,))
        if not cur or cur["state"] not in ("ready", "running"):
            return
        # Empty project guard: don't auto-advance an empty 'ready' project
        # (manual mode: user hasn't added any tasks yet — stay in 'ready' and wait)
        total = await self.db.fetchone(
            "SELECT COUNT(*) as n FROM tasks WHERE project_id = ?",
            (pid,),
        )
        if (total or {}).get("n", 0) == 0:
            return
        # Any non-terminal task?
        nonterm = await self.db.fetchone(
            "SELECT COUNT(*) as n FROM tasks WHERE project_id = ? "
            "AND status NOT IN ('completed', 'skipped', 'cancelled', 'failed')",
            (pid,),
        )
        any_nonterm = (nonterm or {}).get("n", 0) > 0
        any_running = await self.db.fetchone(
            "SELECT COUNT(*) as n FROM tasks WHERE project_id = ? AND status = 'running'",
            (pid,),
        )
        now = _now_iso()
        if cur["state"] == "ready" and (any_running["n"] > 0 or not any_nonterm):
            # First task started OR no tasks left -> move to running
            await self.db.execute(
                "UPDATE projects SET state = 'running', updated_at = ? WHERE id = ?",
                (now, pid),
            )
            await audit_log(self.db, "project.started", actor="supervisor", project_id=pid)
            return
        if cur["state"] == "running" and not any_nonterm:
            # Q2: if this is an iterative project (coordinator + max_iter),
            # do NOT auto-complete here. _maybe_iterate (step 5 of
            # _handle_execution) will either dispatch a review task or
            # complete the project based on decision.md. Without this
            # defer, the supervisor would race past the iteration step
            # and set state=completed before a review task ever runs.
            if (proj.get("coordinator_role") or "").strip() and int(proj.get("max_iterations") or 0) > 0:
                return
            await self.db.execute(
                "UPDATE projects SET state = 'completed', updated_at = ? WHERE id = ?",
                (now, pid),
            )
            await audit_log(
                self.db, "project.completed", actor="supervisor", project_id=pid,
            )
            await self.notifier.send(
                f"Project {pid} completed",
                f"Goal: {(proj.get('goal') or '')[:200]}",
                level="info",
            )
            return
        # If running with no progress and no assignable task -> pause
        if cur["state"] == "running" and no_progress and not any_nonterm:
            # Will be caught by the "all done" branch above; nothing to do
            return
