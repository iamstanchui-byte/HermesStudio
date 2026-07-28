"""Project Plan layer (2026-07-27).

The plan-first architectural shift: a project can carry a "plan"
(structured intent) separate from its "tasks" (actual execution).
The plan is the WHAT, the tasks are the HOW. Per the Perplexity /
user-stated direction, the goal is to remove the archive/complexity
tax by making plans immutable per-run — every click of "Run plan"
materializes a fresh set of tasks from the current plan state.

Phase A (shipped): schema + Pydantic + GET/PUT/DELETE /api/projects/{id}/plan
Phase B (shipped): POST /api/projects/{id}/plan/run — materialize the
  plan into tasks (the actual "Run" button).
Phase C (shipped): visual editor at /api/projects/{id}/plan/visual
  (drawflow canvas, side panel for step details, Plan/Text mode
  toggle, Validate / Save / Generate tasks buttons). See the
  `plan_visual_page` endpoint below.

Per the design contract:
  - A plan is project-scoped (1:1 with project)
  - A plan is a JSON document, validated by ProjectPlan
  - Plan steps reference agent roles (string) and skills/tools
    by canonical name (NOT by row id) — same convention as
    workflow_packages.step_template so plans are portable
  - The plan can be empty ({}) which means "no plan yet, use
    legacy direct-task mode" — this is the default for projects
    that haven't opted into the plan layer
  - The plan is a design-time artifact; "Run" materializes it
    into tasks (Phase B), and the supervisor then dispatches
"""
from __future__ import annotations

import json
import re
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from hermes_orch.utils import now_iso as _now_iso

router = APIRouter()


# ===== Plan JSON structure =====

PLAN_VERSION = "1.0"

# A plan name is kebab-case, matching the same pattern as workflow
# names. Keeps the convention consistent across the catalog.
_KEBAB = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class PlanStep(BaseModel):
    """One step in a project plan.

    Mirrors workflow_packages.step_template schema so plans are
    portable (a plan can be promoted to a workflow package by
    copying the steps array). Per the object-layer / agent-contracts
    foundation, the step references Skills / Tools / Resources by
    canonical name, NOT by row id — keeps plans portable across
    agents (same skill name = same skill on any agent that has it).
    """
    name: str = Field(..., min_length=1, max_length=50)
    agent_role: str = ""
    action: str = ""
    skill: str = ""  # canonical skill name (not row id)
    tool: str = ""   # canonical tool name (not row id)
    required_capability: str = ""
    depends_on: list[str] = Field(default_factory=list)
    params_template: dict[str, Any] = Field(default_factory=dict)
    output_path: str = ""

    @field_validator("name")
    @classmethod
    def _name_kebab(cls, v: str) -> str:
        # Plan step names must be kebab-case (validator contract
        # shared with workflow_packages.step_template).
        if not _KEBAB.match(v):
            raise ValueError(
                f"step name {v!r} must be kebab-case (lowercase letters, "
                f"digits, and hyphens; start with letter or digit)"
            )
        return v


class PlanVariable(BaseModel):
    """One {{var}} placeholder in a plan step's params / output_path.

    Optional here — Phase A just stores the plan JSON. Phase B's
    "Run" endpoint will substitute {{var}} references with values
    (similar to workflow_packages.variables).
    """
    name: str = Field(..., min_length=1, max_length=40)
    type: str = "string"  # string | number | boolean | date
    default: str = ""
    description: str = ""


class ProjectPlan(BaseModel):
    """The plan JSON document stored in projects.plan_json.

    Versioned so we can migrate plan schemas later without breaking
    old plans. Phase A only handles v1.0; future versions (v2.0+)
    can add new fields or change the step shape.

    Empty plan = {'version': '1.0', 'name': '', 'steps': []} which
    means "no plan yet". A project with NULL plan_json is in
    legacy mode (no plan at all).
    """
    version: str = PLAN_VERSION
    name: str = ""
    description: str = ""
    trigger: str = "manual"  # manual | schedule:<schedule_id>
    variables: list[PlanVariable] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_kebab_or_empty(cls, v: str) -> str:
        # Empty name is OK (means "no plan yet"). Non-empty must be
        # kebab-case to match workflow name convention.
        if v and not _KEBAB.match(v):
            raise ValueError(
                f"plan name {v!r} must be kebab-case (lowercase letters, "
                f"digits, and hyphens; start with letter or digit) or empty"
            )
        return v

    @field_validator("variables")
    @classmethod
    def _unique_var_names(cls, v: list[PlanVariable]) -> list[PlanVariable]:
        names = [x.name for x in v]
        if len(names) != len(set(names)):
            raise ValueError("variable names must be unique")
        return v

    @field_validator("steps")
    @classmethod
    def _unique_step_names(cls, v: list[PlanStep]) -> list[PlanStep]:
        names = [x.name for x in v]
        if len(names) != len(set(names)):
            raise ValueError("step names must be unique within a plan")
        return v


# ===== API request/response models =====


class ProjectPlanResponse(BaseModel):
    """Response shape for GET /api/projects/{id}/plan."""
    project_id: str
    has_plan: bool  # True if plan_json is non-NULL in DB
    plan: ProjectPlan | None  # None when has_plan is False
    updated_at: str | None


class ProjectPlanUpdate(BaseModel):
    """Body for PUT /api/projects/{id}/plan."""
    plan: ProjectPlan


# ===== Helpers =====


def _load_plan_from_row(row: dict[str, Any]) -> tuple[bool, ProjectPlan | None, str | None]:
    """Parse the raw plan_json column into a ProjectPlan.

    Returns (has_plan, plan, updated_at). When the column is NULL,
    has_plan=False and plan=None. Empty string "" is treated as
    "empty plan" (valid, has_plan=True). Malformed JSON returns
    HTTPException so the caller can fix the corruption.
    """
    raw = row.get("plan_json")
    updated_at = row.get("updated_at")
    if raw is None:
        return False, None, updated_at
    if not isinstance(raw, str):
        # Defensive: should always be a string from SQLite TEXT
        raw = str(raw)
    raw = raw.strip()
    if not raw:
        # Empty string is treated the same as NULL — no plan yet.
        # The user can PUT a real plan to opt in.
        return False, None, updated_at
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise HTTPException(
            500, f"plan_json is malformed JSON: {e}; "
            f"raw={raw[:200]!r}"
        )
    try:
        plan = ProjectPlan.model_validate(data)
    except Exception as e:
        raise HTTPException(
            500, f"plan_json failed validation: {e}; "
            f"raw={raw[:200]!r}"
        )
    return True, plan, updated_at


# ===== API endpoints =====


@router.get("/projects/{project_id}/plan", response_model=ProjectPlanResponse)
async def get_project_plan(project_id: str, request: Request) -> ProjectPlanResponse:
    """Read a project's plan (Phase A).

    Returns the plan JSON if the project has opted into plan mode
    (plan_json is non-NULL). If the column is NULL, returns
    has_plan=False, plan=None — caller can render "no plan yet" UX
    and offer a button to create one (PUT).

    The /api/projects/{id} endpoint also exposes plan_json directly,
    but this dedicated endpoint is for the plan editor (Phase C will
    have a visual editor at /projects/{id}/plan).
    """
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT id, plan_json, updated_at FROM projects WHERE id = ?",
        (project_id,),
    )
    if not row:
        raise HTTPException(404, f"Project not found: {project_id}")
    has_plan, plan, updated_at = _load_plan_from_row(row)
    return ProjectPlanResponse(
        project_id=project_id,
        has_plan=has_plan,
        plan=plan,
        updated_at=updated_at,
    )


@router.put("/projects/{project_id}/plan", response_model=ProjectPlanResponse)
async def put_project_plan(
    project_id: str, body: ProjectPlanUpdate, request: Request,
) -> ProjectPlanResponse:
    """Write a project's plan (Phase A).

    The plan is stored as JSON in projects.plan_json. Any PUT
    replaces the previous plan (no merge / partial update) — this
    is intentional, the plan is a versioned document and merging
    parts of it gets messy. The previous plan is not preserved as
    history (Phase D may add plan_history; for now, last-write-wins).

    Audit: project.plan.updated.

    Edge cases:
      - Project not found: 404
      - Invalid plan (Pydantic validation): 422 with the field error
      - Empty plan (steps=[]): valid — represents "no plan yet"
      - Same plan PUT twice: idempotent (just overwrites the row)
    """
    db = request.app.state.db
    proj = await db.fetchone(
        "SELECT id, plan_json, updated_at FROM projects WHERE id = ?",
        (project_id,),
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    plan = body.plan
    # Serialize via Pydantic to ensure we write a clean JSON shape
    # (round-trip removes None, sorts fields, etc.). Then dump with
    # ensure_ascii=False for friendlier display in the JSON view.
    plan_json_str = plan.model_dump_json(ensure_ascii=False)
    now = _now_iso()
    await db.execute(
        "UPDATE projects SET plan_json = ?, updated_at = ? WHERE id = ?",
        (plan_json_str, now, project_id),
    )
    # Audit
    try:
        from hermes_orch.core.audit import audit_log
        await audit_log(
            db, "project.plan.updated", actor="operator",
            project_id=project_id,
            payload={
                "name": plan.name,
                "step_count": len(plan.steps),
                "variable_count": len(plan.variables),
                "version": plan.version,
            },
        )
    except Exception:
        # Audit is best-effort in Phase A; don't fail the PUT.
        pass
    return ProjectPlanResponse(
        project_id=project_id,
        has_plan=True,
        plan=plan,
        updated_at=now,
    )


@router.delete("/projects/{project_id}/plan", status_code=204)
async def delete_project_plan(project_id: str, request: Request):
    """Clear a project's plan (back to legacy mode).

    Sets plan_json = NULL. The project's existing tasks are NOT
    affected (this is plan-only). Useful for testing, for switching
    back to direct-task mode, or for "I changed my mind" scenarios.

    Audit: project.plan.cleared.
    """
    db = request.app.state.db
    proj = await db.fetchone(
        "SELECT id, plan_json FROM projects WHERE id = ?",
        (project_id,),
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    if proj.get("plan_json") is None:
        # Already cleared — idempotent no-op
        return
    now = _now_iso()
    await db.execute(
        "UPDATE projects SET plan_json = NULL, updated_at = ? WHERE id = ?",
        (now, project_id),
    )
    try:
        from hermes_orch.core.audit import audit_log
        await audit_log(
            db, "project.plan.cleared", actor="operator",
            project_id=project_id,
            payload={},
        )
    except Exception:
        pass


# ===== Phase B: materialize plan → tasks (POST /api/projects/{id}/plan/run) =====


class RunPlanBody(BaseModel):
    """Body for POST /api/projects/{id}/plan/run.

    `archive_existing` controls what happens to the project's
    non-archived tasks when the plan is re-run:
      - true (default): archive the old tasks before creating new
        ones. This is the "rerun the plan" semantic — every Run
        produces a fresh execution row, and the old one is kept
        in history but hidden from the default view.
      - false: keep the old tasks AND add the new ones (additive
        semantics, like the "Apply workflow" feature). The project
        ends up with N+M tasks after the run.
    `name_suffix` is appended to the new task names (no effect on
    existing tasks) — useful when you re-run a plan and want to
    visually distinguish the new execution (e.g. "_run_2").
    """
    archive_existing: bool = True
    name_suffix: str = ""


class RunPlanResponse(BaseModel):
    """Response shape for POST /api/projects/{id}/plan/run.

    `tasks_created` = how many new task rows this run added.
    `tasks_archived` = how many old tasks got archived (0 if
    archive_existing=False, or if there were no prior tasks).
    `task_ids` = the IDs of the NEW tasks (so the UI can deep-link
    to the new execution for visual verification).
    """
    project_id: str
    state: str
    tasks_created: int
    tasks_archived: int
    task_ids: list[str]
    plan_name: str
    plan_version: str
    message: str


@router.post("/projects/{project_id}/plan/run", response_model=RunPlanResponse)
async def run_project_plan(
    project_id: str, body: RunPlanBody, request: Request,
) -> RunPlanResponse:
    """Materialize a project's plan into actual tasks (Phase B).

    This is the "Run plan" button in the plan modal. It:
      1. Loads the project + plan
      2. (Optionally) archives existing non-archived tasks
      3. Creates a new task row per plan step (status=pending,
         depends_on resolved from plan-internal references)
      4. Sets project state → 'ready' so the supervisor's next
         tick dispatches the new tasks
      5. Audits: project.plan.ran + per-task task.created events

    Per the plan-first design, this is the "fork" between intent
    and execution. The plan JSON is immutable per-run; re-running
    the same plan produces a fresh set of tasks (the old ones are
    archived, not deleted, so history is preserved).

    Phase B does NOT do variable substitution — that's Phase D.
    {{var}} placeholders in step.params_template are stored
    verbatim in the task row. Phase B also does NOT validate
    that referenced skills/tools/agents actually exist — that's
    the supervisor's job at dispatch time.
    """
    db = request.app.state.db
    # 1. Load project
    proj = await db.fetchone(
        "SELECT id, state, plan_json FROM projects WHERE id = ?",
        (project_id,),
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    # Terminal states can't be re-run. They need explicit unarchive /
    # undelete first (existing project.py semantics, copied here for
    # the plan-run path).
    if proj["state"] in ("deleted", "archived"):
        raise HTTPException(
            400,
            f"Cannot run plan on a {proj['state']!r} project. "
            f"Restore it first (unarchive / undelete)."
        )
    if proj["state"] in ("completed", "cancelled"):
        raise HTTPException(
            400,
            f"Cannot run plan on a terminal-state project "
            f"(state={proj['state']!r}). Create a new project or "
            f"manually reset state to 'planned'."
        )
    # 2. Load plan
    has_plan, plan, _ = _load_plan_from_row(proj)
    if not has_plan or plan is None:
        raise HTTPException(
            400,
            f"project {project_id} has no plan to run. "
            f"Set a plan first via PUT /api/projects/{project_id}/plan."
        )
    if not plan.steps:
        raise HTTPException(
            400,
            f"plan has no steps. Add at least one step before running."
        )
    # 3. Optionally archive existing non-archived tasks
    now = _now_iso()
    archived_count = 0
    if body.archive_existing:
        # We only archive non-archived, non-running tasks. Active
        # running tasks stay (the user can interrupt them if they
        # want — archiving a running task would orphan the agent).
        # We archive EVERYTHING in non-terminal states
        # (pending/assigned) so the new plan starts clean.
        cur = await db.fetchone(
            "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? "
            "AND archived = 0 AND status IN ('pending', 'assigned', 'failed', 'skipped', 'cancelled', 'completed')",
            (project_id,),
        )
        archived_count = int(cur["n"] or 0) if cur else 0
        await db.execute(
            "UPDATE tasks SET archived = 1, updated_at = ? "
            "WHERE project_id = ? AND archived = 0 "
            "AND status IN ('pending', 'assigned', 'failed', 'skipped', 'cancelled', 'completed')",
            (now, project_id),
        )
    # 4. Resolve depends_on + create task rows. The plan's step
    # `depends_on` is a list of step NAMES within the same plan
    # (e.g. ["fetch-data", "fetch-meta"] for a "summarize" step).
    # We need to convert to task IDs. Two passes:
    #   a. plan-internal: name -> new task id
    #   b. project-external: name -> existing non-archived task id
    #      (lets a plan step depend on a project task with the
    #      same name — same pattern as apply-workflow)
    existing_rows = await db.fetchall(
        "SELECT id, name FROM tasks WHERE project_id = ? AND archived = 0",
        (project_id,),
    )
    existing_name_to_tid: dict[str, str] = {
        r["name"]: r["id"] for r in existing_rows if r.get("name")
    }
    # First pass: assign new task ids
    name_to_tid: dict[str, str] = {}
    for step in plan.steps:
        name_to_tid[step.name] = "t-" + secrets.token_hex(4)
    # Second pass: build task rows with resolved deps
    new_task_ids: list[str] = []
    new_task_rows: list[dict[str, Any]] = []
    for step in plan.steps:
        tid = name_to_tid[step.name]
        new_task_ids.append(tid)
        # Resolve depends_on
        dep_tids: list[str] = []
        unresolved: list[str] = []
        for d in (step.depends_on or []):
            if d in name_to_tid:
                dep_tids.append(name_to_tid[d])
            elif d in existing_name_to_tid:
                dep_tids.append(existing_name_to_tid[d])
            else:
                unresolved.append(d)
        if unresolved:
            try:
                from hermes_orch.core.audit import audit_log
                await audit_log(
                    db, "task.depends_on_unresolved",
                    actor="plan-runner", project_id=project_id, task_id=tid,
                    payload={"step_name": step.name,
                             "unresolved_deps": unresolved,
                             "source": "plan_run"},
                )
            except Exception:
                pass
        # Apply optional name suffix (helps users tell runs apart
        # in the UI when re-running). Suffix is just appended, no
        # kebab validation — operator override.
        task_name = step.name
        if body.name_suffix:
            task_name = f"{task_name}{body.name_suffix}"
        # params stored as JSON. No variable substitution in Phase B.
        params = dict(step.params_template or {})
        # Skill / tool name carry-through (Object Layer refs).
        # Mirrors apply-workflow's _workflow_skill param convention
        # so the supervisor can resolve at dispatch time.
        if step.skill:
            params["_workflow_skill"] = step.skill
        new_task_rows.append({
            "id": tid,
            "project_id": project_id,
            "name": task_name,
            "agent_role": step.agent_role or "",
            "depends_on": json.dumps(dep_tids),
            "on_parent_failure": "skip",
            "status": "pending",
            "priority": "normal",
            "action": step.action or "do_task",
            "params": json.dumps(params),
            "retry_count": 0,
            "max_retries": 2,
            "timeout_seconds": 1800,
            "output_path": step.output_path or "",
            "required_capability": step.required_capability or None,
            "feedback_to": json.dumps([]),
            "is_single_task": 0,
            "archived": 0,
        })
    # Insert all tasks. We do this one-at-a-time so a single
    # bad row doesn't kill the whole run (defensive — params_template
    # is already Pydantic-validated upstream, so bad rows are
    # unlikely, but better safe).
    for t in new_task_rows:
        try:
            await db.insert("tasks", t)
        except Exception as e:
            raise HTTPException(
                500, f"failed to insert task {t['name']!r}: {e}"
            )
    # 5. Set project state → 'ready' (so supervisor's next tick
    # dispatches). Same transition as /api/projects/{id}/run —
    # we reuse the existing flow, no new state introduced.
    await db.execute(
        "UPDATE projects SET state = 'ready', updated_at = ? WHERE id = ?",
        (now, project_id),
    )
    # 6. Audit: per-task created + top-level plan.ran
    try:
        from hermes_orch.core.audit import audit_log
        for t in new_task_rows:
            await audit_log(
                db, "task.created", actor="plan-runner",
                project_id=project_id, task_id=t["id"],
                payload={
                    "agent_role": t["agent_role"],
                    "action": t["action"],
                    "name": t["name"],
                    "source": "run_plan",
                    "plan_name": plan.name,
                },
            )
        await audit_log(
            db, "project.plan.ran", actor="operator",
            project_id=project_id,
            payload={
                "plan_name": plan.name,
                "plan_version": plan.version,
                "task_count": len(new_task_rows),
                "archived_count": archived_count,
                "archive_existing": body.archive_existing,
                "previous_state": proj["state"],
            },
        )
    except Exception:
        pass
    msg = (
        f"Plan {plan.name!r} materialized into {len(new_task_rows)} task(s). "
        f"Supervisor will dispatch on next tick (within ~5s)."
    )
    if archived_count:
        msg += f" {archived_count} existing task(s) archived."
    return RunPlanResponse(
        project_id=project_id,
        state="ready",
        tasks_created=len(new_task_rows),
        tasks_archived=archived_count,
        task_ids=new_task_ids,
        plan_name=plan.name,
        plan_version=plan.version,
        message=msg,
    )


# ===== Phase C: visual plan editor page (GET /api/projects/{id}/plan/visual) =====


@router.get("/projects/{project_id}/plan/visual", response_class=HTMLResponse)
async def plan_visual_page(project_id: str, request: Request) -> HTMLResponse:
    """Render the visual plan editor page (Phase C, 2026-07-27).

    This is the PRIMARY editing surface for plans (the JSON modal in
    the project page is a secondary surface for power users / quick
    edits). The visual page uses drawflow for the canvas, has a side
    panel for step details, a minimap, and a Plan/Text mode toggle.

    URL: /api/projects/{id}/plan/visual
    (mounted at /api prefix via plans_router in main.py)

    The plan JSON is embedded server-side into a data-* attribute on
    the wrap div (data-plan-json), so the page renders correctly even
    before any JS runs. The JS then re-renders from that attribute on
    DOMContentLoaded and wires up drawflow.

    Save = PUT /api/projects/{id}/plan (overwrites plan_json)
    Generate tasks = POST /api/projects/{id}/plan/run (materializes
    the plan into actual task rows; supervisor dispatches next tick)

    Per the Perplexity / user-stated design (2026-07-27):
      - The canvas shows the PLAN (intent), not runtime task status
      - Validation state badges (Ready/Review/etc.) are plan-level
        concerns — out of scope for Phase C
      - "Generate tasks" = "Run plan" in our model (we materialize
        the plan into actual tasks, not synthesize via LLM)
    """
    from fastapi.templating import Jinja2Templates
    db = request.app.state.db
    proj = await db.fetchone(
        "SELECT id, name, plan_json FROM projects WHERE id = ?",
        (project_id,),
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    # Parse plan_json into a dict (so the template's `tojson` filter
    # produces a single layer of encoding). projects.plan_json is a
    # TEXT column holding a JSON string; if we pass it through as a
    # string and then `|tojson` it, the JS gets a double-encoded
    # value (string-within-string) and JSON.parse returns a string
    # instead of an object — then `_plan.steps.map(...)` crashes
    # with "Cannot read properties of undefined (reading 'map')".
    # The fix: parse it here so the template emits a proper object.
    import json
    plan_obj = None
    if proj.get("plan_json"):
        try:
            plan_obj = json.loads(proj["plan_json"])
        except (json.JSONDecodeError, TypeError):
            plan_obj = None
    # Build a context that includes the parsed plan object (None when
    # no plan). The template uses data-plan-json to bootstrap the
    # JS-side plan state.
    proj_view = {
        "id": proj["id"],
        "name": proj["name"],
        "plan": plan_obj,
    }
    # Per user feedback 2026-07-28: the side-panel Agent role field
    # was a free-text <input>; operators want a dropdown of registered
    # profile names so they don't mistype "linux-a-01" (machine id) when
    # they meant "win-agent01" (role name). We distinct-name the
    # profiles here (same as the planner does, after the regression
    # fix in 26e845f) and hand the list to the template.
    profile_rows = await db.fetchall(
        "SELECT DISTINCT name FROM agent_profiles "
        "WHERE name IS NOT NULL AND name != '' ORDER BY name"
    )
    available_roles = [r["name"] for r in profile_rows]
    # Use the same Jinja templates env as the rest of the app
    # (set up in main.py: app.state.templates). We import the type
    # only — the actual `templates` instance is on app.state and
    # shared with dashboard.py.
    # Bug fix 2026-07-27: pass llm_configured into context so the
    # base.html "Mock mode" banner hides when LLM is configured.
    # Without this, plan_visual_page always shows the "running in
    # mock mode" yellow banner even with API key set.
    from hermes_orch.api.dashboard import _llm_configured
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "visual_plan.html",
        {
            "project": proj_view,
            "available_roles": available_roles,
            "active_page": "projects",
            "llm_configured": _llm_configured(
                getattr(request.app.state, "config", None)
            ),
        },
    )


# ===== Phase D: LLM-driven plan generation (2026-07-27) =====
# UI cleanup: the project page used to have a "Generate plan" button
# that called POST /api/projects/{id}/replan which materializes TASKS
# directly. With the plan layer, we want a different flow:
#   - "Generate plan" (now in the plan editor) calls the LLM and
#     gets a plan_json (design-time)
#   - "Run" materializes the plan into tasks (run-time)
# This endpoint is the LLM step. It does NOT save the plan — the
# user reviews in the visual editor first, then clicks "Save".


class FromLlmBody(BaseModel):
    """Body for POST /api/projects/{id}/plan/from-llm.

    `goal` is the natural-language description of what the project
    should achieve. If empty, we use the project's existing goal
    column. `name_suffix` is appended to each step name to avoid
    collisions (e.g. '_draft_1' on first try, '_draft_2' on edit).
    """
    goal: str = ""
    name_suffix: str = ""


@router.post("/projects/{project_id}/plan/from-llm", response_model=ProjectPlanResponse)
async def generate_plan_from_llm(
    project_id: str, body: FromLlmBody, request: Request,
) -> ProjectPlanResponse:
    """Generate a plan_json from a goal using the LLM (Phase D, 2026-07-27).

    Flow:
      1. Load the project. If body.goal is empty, fall back to
         projects.goal. 400 if both are blank.
      2. Load agent roles + their skills so the LLM can pick the
         right role for each step (matches Planner.plan() signature).
      3. Call Planner.plan() — this returns a list of task dicts in
         the same shape that POST /replan used to materialise directly.
         We do NOT call any /tasks/* insert; we just want the structure.
      4. Convert each task to a PlanStep:
           - name: kebab-case the task.name (planner gives snake/kebab,
             we normalise)
           - agent_role: pass through
           - action: 'do_task' (the default task action; user can edit)
           - depends_on: pass through (planner already returns step
             NAMES; we keep names — they get resolved to task ids at
             /plan/run time)
           - params_template: from task.params (parsed JSON)
           - output_path: empty (planner doesn't set this; user can add)
           - skill / tool / required_capability: empty unless the
             planner included them (we don't lose data — if it did,
             it ends up in params_template)
      5. Wrap in ProjectPlan. Don't write to DB.
      6. Return the plan so the client can render it on the canvas.
         The user reviews and clicks "Save" (PUT /plan) to persist.

    Edge cases:
      - No agent roles registered: 400
      - LLM mock mode: returns a mock 2-step plan (good enough for
        UX testing; user can edit on the canvas)
      - LLM real mode: returns whatever the LLM produced; if the
        LLM fails the planner falls back to mock with a logged
        `planner_fell_back_to_mock` event (no exception)
    """
    db = request.app.state.db
    proj = await db.fetchone(
        "SELECT id, name, goal FROM projects WHERE id = ?",
        (project_id,),
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    goal = (body.goal or "").strip() or (proj.get("goal") or "").strip()
    if not goal:
        raise HTTPException(
            400,
            "no goal provided and project has no goal set; "
            "set a goal first or pass one in the request body",
        )
    # Load agent roles. The agent_profiles table has TWO distinct id
    # fields:
    #   - agent_id  = the machine running the role (e.g. "linux-a-01",
    #                 "win-local-1"). One machine can host multiple roles.
    #   - name      = the role itself (e.g. "super", "win-agent01",
    #                 "win-agent02"). This is what the supervisor uses
    #                 to look up the agent_profile when dispatching a
    #                 task. The task row's `agent_role` column is
    #                 matched against `name`, NOT `agent_id`.
    #
    # So for the LLM planner, we MUST pass role NAMES (the "name"
    # column) — not machine ids. Passing agent_ids was a regression:
    # the LLM saw ["linux-a-01", "win-local-1"] as the available roles
    # and dutifully picked "linux-a-01" as the agent_role for every
    # step, even though no profile has that name (it would fail at
    # dispatch). Regressed during the Phase C plan editor refactor
    # (commit 85a87cd) when this code was extracted from supervisor.py
    # to plans.py. supervisor.py still does the right thing
    # (SELECT DISTINCT name) — see core/supervisor.py:157.
    role_rows = await db.fetchall(
        "SELECT name, storage_refs, capabilities FROM agent_profiles "
        "WHERE name IS NOT NULL AND name != ''"
    )
    # Distinct role names (multiple rows for the same name on
    # different machines are deduped).
    available_roles = sorted({r["name"] for r in role_rows if r["name"]})
    if not available_roles:
        raise HTTPException(
            400,
            "no agent roles registered; add an agent profile first",
        )
    # Build role->skills map. Per supervisor.py:44, skills are
    # derived from profile_configs (file_path LIKE 'skills/%').
    # For the plan editor's LLM call, we don't need exact skills
    # (those are runtime concerns) — but we DO want the LLM to know
    # which role has which storage_refs so it can pick the role
    # whose storage alias matches a folder named in the goal
    # (e.g. "use project_temp_folder" → role that has
    # storage_refs[].name == "project_temp_folder"). This is the
    # regression the user noticed 2026-07-28: the LLM no longer
    # saw that win-agent01 had the project_temp_folder alias and
    # was dispatching to a different machine. Fix: include
    # storage_refs in the planner context.
    skill_rows = await db.fetchall(
        "SELECT ap.name AS role, pc.file_path "
        "FROM profile_configs pc "
        "JOIN agent_profiles ap ON ap.id = pc.profile_id "
        "WHERE pc.file_path LIKE 'skills/%' AND pc.status = 'applied' "
        "ORDER BY ap.name ASC, pc.file_path ASC"
    )
    role_skills: dict[str, list[str]] = {}
    for r in skill_rows:
        fp = r["file_path"]
        # file_path is "skills/<name>.md" — strip prefix + suffix
        if fp.startswith("skills/") and fp.endswith(".md"):
            skill = fp[len("skills/"):-len(".md")]
        else:
            skill = fp
        role_skills.setdefault(r["role"], []).append(skill)
    # role_storage: {role_name: [{name, kind, ref, description}, ...]}
    role_storage: dict[str, list[dict]] = {}
    for r in role_rows:
        sref_raw = r.get("storage_refs")
        if not sref_raw:
            continue
        try:
            import json as _json
            srefs = _json.loads(sref_raw) if isinstance(sref_raw, str) else sref_raw
        except (TypeError, ValueError):
            srefs = []
        if isinstance(srefs, list) and srefs:
            role_storage.setdefault(r["name"], []).extend(srefs)
    # role_capabilities: {role_name: {capability_key: true}}
    role_capabilities: dict[str, dict[str, bool]] = {}
    for r in role_rows:
        cap_raw = r.get("capabilities")
        if not cap_raw:
            continue
        try:
            import json as _json
            caps = _json.loads(cap_raw) if isinstance(cap_raw, str) else cap_raw
        except (TypeError, ValueError):
            caps = {}
        if isinstance(caps, dict) and caps:
            role_capabilities.setdefault(r["name"], {}).update(caps)
    # Call the planner. The planner accepts a `role_storage` and
    # `role_capabilities` block; if we pass them, the prompt will
    # include an [AVAILABLE STORAGE BY ROLE] section that helps the
    # LLM pick a role whose storage matches a folder name in the
    # goal (the exact regression the user noticed).
    planner = getattr(request.app.state, "planner", None)
    if planner is None:
        raise HTTPException(500, "planner not initialized")
    task_dicts = await planner.plan(
        goal=goal,
        available_roles=available_roles,
        role_skills=role_skills or None,
        role_capabilities=role_capabilities or None,
        role_storage=role_storage or None,
    )
    # Convert task dicts to PlanStep objects.
    import re as _re
    _KEBAB = _re.compile(r"[^a-z0-9-]+")
    def _to_kebab(s: str) -> str:
        s = (s or "").lower().strip()
        s = _KEBAB.sub("-", s).strip("-")
        return s or "step"
    steps: list[PlanStep] = []
    for t in task_dicts:
        raw_name = str(t.get("name") or t.get("id") or "step")
        name = _to_kebab(raw_name)
        if body.name_suffix:
            name = f"{name}{body.name_suffix}"
        # Planner returns depends_on as a list of step names (or ids
        # — we treat them as opaque names; the validator at save time
        # will check they exist in the plan).
        deps = t.get("depends_on") or []
        # params may be a JSON string or dict. Normalise to dict.
        raw_params = t.get("params") or {}
        if isinstance(raw_params, str):
            try:
                raw_params = json.loads(raw_params)
            except (json.JSONDecodeError, TypeError):
                raw_params = {}
        if not isinstance(raw_params, dict):
            raw_params = {}
        steps.append(PlanStep(
            name=name,
            agent_role=str(t.get("agent_role") or ""),
            action=str(t.get("action") or "do_task"),
            skill="",
            tool="",
            required_capability=str(t.get("required_capability") or ""),
            depends_on=[str(d) for d in deps],
            params_template=raw_params,
            output_path=str(t.get("output_path") or ""),
        ))
    plan = ProjectPlan(
        version=PLAN_VERSION,
        name="",
        # Per user feedback 2026-07-28: the auto-prefix
        # "Generated by LLM from goal:" reads as a debug string to
        # the operator ("這些字應該跟Agent 分析沒關系吧"). Use the
        # goal itself (truncated) as the default — it's the most
        # meaningful one-line description of what the plan does.
        # The operator can still edit it on the canvas toolbar.
        description=goal[:200] if goal else "",
        trigger="manual",
        variables=[],
        steps=steps,
    )
    msg = (
        f"LLM generated {len(steps)} step(s) from goal (len={len(goal)}). "
        f"Review the canvas, then click Save to persist."
    )
    return ProjectPlanResponse(
        project_id=project_id,
        has_plan=True,
        plan=plan,
        updated_at=_now_iso(),
    )
