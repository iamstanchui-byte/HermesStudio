"""Project Plan layer (Phase A foundation, 2026-07-27).

The plan-first architectural shift: a project can carry a "plan"
(structured intent) separate from its "tasks" (actual execution).
The plan is the WHAT, the tasks are the HOW. Per the Perplexity /
user-stated direction, the goal is to remove the archive/complexity
tax by making plans immutable per-run — every click of "Run plan"
materializes a fresh set of tasks from the current plan state.

Phase A is foundation only:
  - Schema: projects.plan_json TEXT (nullable, NULL = legacy mode)
  - Pydantic model: ProjectPlan with steps + variables + object refs
  - API: GET/PUT /api/projects/{id}/plan (JSON only, no editor yet)
  - Audit: project.plan.updated event

Phase B (next): POST /api/projects/{id}/plan/run — materialize the
plan into tasks (the actual "Run" button).

Phase C (later): Visual editor + migration path for legacy projects.

Per the design contract:
  - A plan is project-scoped (1:1 with project)
  - A plan is a JSON document, validated by ProjectPlan
  - Plan steps reference agent roles (string) and skills/tools
    by canonical name (NOT by row id) — same convention as
    workflow_packages.step_template so plans are portable
  - The plan can be empty ({}) which means "no plan yet, use
    legacy direct-task mode" — this is the default for projects
    that haven't opted into the plan layer
  - The plan is NOT visible to the agent runtime until Phase B
    wires the "Run" endpoint. For now, plans are a no-op design
    surface — saving a plan doesn't create any tasks.
"""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
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
