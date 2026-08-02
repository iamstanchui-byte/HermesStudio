# coding: utf-8
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
import logging
import re
import secrets
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Header, HTTPException, Request
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

    Per 2026-07-29 (Phase 2.1, see test_known_bug_*.py): the
    LLM used to leave `action` empty because the field was
    optional and the system prompt didn't explain what to put
    there. `action` is now REQUIRED (min_length=1) and the
    chat system prompt documents the canonical examples
    ("fetch_url", "navigate_to_folder", "summarize", etc.).
    Without `action`, the agent has no idea what to do — the
    step is unrunnable.
    """
    name: str = Field(..., min_length=1, max_length=50)
    agent_role: str = ""
    # Required: a short verb phrase describing what the agent does.
    # The LLM is told to mirror workflow_packages.step_template
    # actions: "fetch_url", "navigate_to_folder", "summarize",
    # "send_telegram_message", "create_file", "read_file",
    # "search_web", etc. — kebab/snake-case verbs that the agent
    # can interpret. Free-form prose is also accepted but the
    # canonical form is short and verb-first.
    action: str = Field(..., min_length=1, max_length=200)
    skill: str = ""  # canonical skill name (not row id)
    tool: str = ""   # canonical tool name (not row id)
    required_capability: str = ""
    depends_on: list[str] = Field(default_factory=list)
    # v1.9.4 (2026-07-30, FLIPPED 2026-07-30 in v2.0): feedback_to
    # mirrors workflow_packages step_template. List of step names
    # in the same plan to RE-RUN when THIS step fails (and reset
    # their downstream via depends_on). v2.0 semantic: field is on
    # the FAILING step (matches standard on_failure pattern in
    # AWS Step Functions / Airflow / Temporal). Only fires when
    # the spawned project has max_iterations > 0.
    # Server-side validator drops self-references and dangling
    # names at /plan/run time (matches the workflow apply path).
    # Visual plan editor uses the red output_2 handle / red dashed
    # wire to draw this (mirroring the visual workflow builder).
    feedback_to: list[str] = Field(default_factory=list)
    params_template: dict[str, Any] = Field(default_factory=dict)
    output_path: str = ""
    # v3.10.4 (2026-08-02): LLM-drafted SOUL persona text for this
    # step's role. When the LLM generates a plan, it produces a
    # `default_soul` per step. We pre-seed the project_soul_presets
    # table from this field at plan-save time so the user can see
    # + edit the SOUL on the project page BEFORE clicking [▶ Run].
    # The dispatch path also reads this field as a fallback (see
    # orchestrator.soul_dispatch._step_default_soul).
    # v3.9.0's "both" mode (chat planner) sets this on the step
    # dict via raw JSON; the Pydantic model just round-trips it.
    default_soul: str = ""

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

    @field_validator("action")
    @classmethod
    def _action_nontrivial(cls, v: str) -> str:
        # Defense in depth: catch "n/a", "-", ".", "todo" etc.
        # that the LLM might put as a placeholder. Pydantic
        # already enforces min_length=1, but allow meaningful
        # short actions.
        v = v.strip()
        if not v:
            raise ValueError("step action must not be whitespace-only")
        if len(v) < 2:
            raise ValueError(
                f"step action {v!r} is too short — describe what the "
                f"agent should do (e.g. 'fetch_url', 'summarize')"
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
    # v1.5.3 (2026-07-29): server-side visual_layout, mirrors the
    # workflow_packages.visual_layout field. Persists the
    # drawflow canvas node positions so they survive reloads
    # AND cross-device (localStorage in the previous v1.5 was
    # client-only). Shape: {step_name: {"x": <float>, "y": <float>}}.
    # Missing entries fall back to drawflow's default layout.
    visual_layout: dict[str, dict[str, float]] = Field(
        default_factory=dict
    )

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


class ProjectPlanAgentsResponse(BaseModel):
    """Response shape for GET /api/projects/{id}/plan/agents.

    Returned to the chatbox LLM (docs/chatbox-plan-editor.md §5 §7.3)
    so it can validate the agent_role / skill / tool names it puts in
    PlanStep suggestions. The visual editor (Phase C) does not use this
    — it renders the same list inline in the side panel.
    """
    project_id: str
    agent_roles: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


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


async def _seed_soul_presets_from_plan(
    db: Any,
    project_id: str,
    plan: "ProjectPlan",
    *,
    llm_souls: dict[str, str] | None = None,
    fill_empty_only: bool = False,
) -> dict[str, Any]:
    """Auto-seed `project_soul_presets` rows from a plan's `default_soul`.

    v3.10.4 (2026-08-02) UX fix: when the LLM generates a plan, each
    step carries a `default_soul` (the LLM-drafted persona text).
    Previously, the SOUL only materialized AT dispatch time via
    `_ensure_soul_preset` in orchestrator.soul_dispatch — by the
    time the user could see it in the project page's "Show SOUL
    editor" toggle, the task was already running with the
    auto-generated content. The user had no chance to edit the
    soul before the agent started.

    v3.10.5 (2026-08-02) update: the planner's `default_soul` field
    was replaced with a dedicated LLM call (`_generate_souls_via_llm`
    in orchestrator.soul_dispatch) that runs at "Generate Task" time
    with full project context. Callers pass the LLM output via
    `llm_souls`; if the call failed or wasn't attempted, the helper
    falls back to the step's `default_soul` field (legacy) or the
    generic role template (last resort).

    This helper pre-creates presets at plan-save time so the user
    can see + edit them BEFORE clicking the green [▶ Run] button.

    Behavior:
      1. Iterate over plan.steps. Collect unique (role, default_soul)
         pairs (same role can appear in multiple steps with the
         same soul — we only need one preset per profile).
      2. For each unique role, resolve to a profile using the
         routing engine (same logic dispatch uses).
      3. Insert project_soul_presets row for (project, profile) IF
         no preset exists (or, if `fill_empty_only=True`, only if
         the existing preset's content is empty). This preserves
         user edits.
         content priority:
           1. llm_souls[role] (v3.10.5 — dedicated LLM call)
           2. step.default_soul (legacy v3.10.4 Pydantic field)
           3. _generic_role_template(role) (last-resort fallback)
      4. Skip roles where routing returns no profile (the
         dispatch path will resolve it later; preset creation
         happens lazily on first dispatch).

    Args:
        db: the orchestrator database.
        project_id: the project being seeded.
        plan: the ProjectPlan Pydantic model.
        llm_souls: optional {role: persona_text} override from a
            dedicated LLM SOUL-generation call. Takes priority over
            `step.default_soul`. If a role is missing from this dict
            but exists in the plan, the helper falls through to
            `step.default_soul` then the generic template.
        fill_empty_only: if True, only fill presets that have NO
            content yet. Used by the "Generate SOUL" recovery button
            so it doesn't clobber user edits. Default False (the
            original behavior — never overwrite existing presets).

    Returns a stats dict so the caller can include it in the
    response or audit log. Best-effort: any error is caught and
    logged, never raised (we don't want a soul-seed failure to
    break the plan save).
    """
    import logging
    log = logging.getLogger(__name__)
    stats = {
        "roles_seen": 0,
        "presets_created": 0,
        "presets_skipped_existing": 0,
        "roles_skipped_no_default_soul": 0,
        "roles_skipped_no_profile": 0,
        # v3.10.4 follow-up: count of roles that fell back to the
        # generic role template (because the plan had no
        # `default_soul` for them). The UI mentions this so the
        # user knows to customize the generic text on the project
        # page rather than treating it as a final answer.
        "roles_used_generic_fallback": 0,
        # v3.10.5: count of roles that used content from a
        # dedicated LLM call (the new `_generate_souls_via_llm`
        # path). Distinct from `roles_used_generic_fallback` so the
        # UI can show "X roles got LLM-generated personas, Y used
        # generic" — gives the user a quick read on quality.
        "roles_used_llm_generated": 0,
        "errors": [],
    }
    try:
        # Lazy import — heavy module, only load when actually used
        from hermes_orch.orchestrator.routing import resolve_role_to_profile

        # v3.10.5: priority for persona text per role:
        #   1. llm_souls[role] (dedicated LLM call — the new path)
        #   2. step.default_soul (v3.10.4 Pydantic field, legacy)
        #   3. _generic_role_template(role) (last-resort fallback)
        from hermes_orch.orchestrator.soul_dispatch import (
            _generic_role_template,
        )
        seen_roles: dict[str, str] = {}  # role -> persona_text
        for step in plan.steps:
            role = (step.agent_role or "").strip()
            if not role:
                continue
            # If the caller passed llm_souls, prefer it for this role.
            # The same role across multiple steps gets ONE LLM persona
            # (the LLM call returns one entry per unique role), so the
            # first step to land in seen_roles wins.
            if (
                llm_souls
                and role in llm_souls
                and role not in seen_roles
            ):
                seen_roles[role] = llm_souls[role].strip()
                stats["roles_used_llm_generated"] += 1
                continue
            # Already have this role from a prior step — skip
            # subsequent steps to keep persona deterministic (first
            # wins). The user can hand-merge if needed.
            if role in seen_roles:
                continue
            default_soul = (step.default_soul or "").strip()
            if not default_soul and isinstance(step.params_template, dict):
                # Legacy fallback for plans produced by the chat
                # planner before the v3.10.4 Pydantic field was added.
                default_soul = (
                    str(step.params_template.get("default_soul") or "").strip()
                )
            if not default_soul:
                # v3.10.4 follow-up: fall back to the same generic
                # template the dispatch path uses (see
                # orchestrator.soul_dispatch._ensure_soul_preset).
                # The plan was probably generated before the planner
                # prompt required `default_soul`, OR the LLM
                # skipped it. Either way, the user gets a usable
                # starting point they can edit. Track as a separate
                # stat so the UI can mention "N roles used generic
                # fallback" so the user knows to customize.
                default_soul = _generic_role_template(role)
                stats["roles_used_generic_fallback"] += 1
            seen_roles[role] = default_soul
        stats["roles_seen"] = len(seen_roles)

        for role, default_soul in seen_roles.items():
            try:
                # Resolve the role to a profile using the same
                # routing engine that dispatch uses. If routing
                # fails (e.g., no agent available right now), we
                # skip — dispatch will create the preset later
                # when an agent is available.
                step_like = type("_S", (), {
                    "agent_role": role,
                    "target_profiles": [],
                    "required_capabilities": [],
                })()
                try:
                    profile = await resolve_role_to_profile(
                        project_id, step_like, db,
                    )
                except Exception as e:
                    log.info(
                        "soul seed: skip role=%s (routing failed: %s); "
                        "dispatch will create preset lazily",
                        role, e,
                    )
                    stats["roles_skipped_no_profile"] += 1
                    continue
                if not profile or not profile.get("id"):
                    stats["roles_skipped_no_profile"] += 1
                    continue
                profile_id = profile["id"]
                # Check if a preset already exists for this
                # (project, profile). Behaviour depends on the
                # caller's intent:
                #   - fill_empty_only=False (default, e.g. plan-save):
                #     skip if ANY preset exists (preserve user edits
                #     and avoid clobbering content from a prior
                #     Generate-SOUL run)
                #   - fill_empty_only=True (e.g. "Generate SOUL"
                #     button): skip only if the existing preset has
                #     non-empty content. Empty presets (e.g. the
                #     user deleted the content, or it was never
                #     filled) get refilled with the LLM text.
                existing = await db.fetchone(
                    "SELECT id, content, default_soul "
                    "FROM project_soul_presets "
                    "WHERE project_id = ? AND profile_id = ?",
                    (project_id, profile_id),
                )
                if existing:
                    existing_content = (
                        (existing.get("content") or "").strip()
                        if isinstance(existing, dict)
                        else ""
                    )
                    if not fill_empty_only or existing_content:
                        # Either we're not in fill-empty-only mode
                        # (preserve everything), or the existing
                        # preset has real content (user edited it,
                        # leave alone). Either way, skip.
                        stats["presets_skipped_existing"] += 1
                        continue
                    # fill_empty_only=True AND existing content is
                    # empty → UPDATE the existing row with the new
                    # persona text. The id is preserved so any
                    # pending profile_configs rows (e.g. an in-flight
                    # dispatch) stay consistent.
                    await db.execute(
                        "UPDATE project_soul_presets SET "
                        "content = ?, default_soul = ?, "
                        "role_name = ?, updated_at = ? "
                        "WHERE id = ?",
                        (
                            default_soul,
                            default_soul,
                            role,
                            _now_iso(),
                            existing["id"] if isinstance(existing, dict) else existing[0],
                        ),
                    )
                    stats["presets_created"] += 1
                    continue
                # Create the preset. Content = default_soul; we
                # also store default_soul on the preset so a
                # future "reset to default" UI can find it.
                import uuid as _uuid
                preset_id = str(_uuid.uuid4())
                now = _now_iso()
                await db.insert(
                    "project_soul_presets",
                    {
                        "id": preset_id,
                        "project_id": project_id,
                        "profile_id": profile_id,
                        "role_name": role,
                        "content": default_soul,
                        "default_soul": default_soul,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                stats["presets_created"] += 1
            except Exception as e:
                log.warning(
                    "soul seed: error for role=%s: %s",
                    role, e,
                )
                stats["errors"].append({"role": role, "error": str(e)})
        # Audit the seeding summary (best-effort)
        try:
            from hermes_orch.core.audit import audit_log
            await audit_log(
                db, "project.soul_presets.seeded", actor="operator",
                project_id=project_id,
                payload=stats,
            )
        except Exception:
            pass
    except Exception as e:
        # Never let a soul-seed failure break the plan save
        log.warning("soul seed: top-level error: %s", e)
        stats["errors"].append({"error": str(e)})
    return stats


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


@router.get(
    "/projects/{project_id}/plan/agents",
    response_model=ProjectPlanAgentsResponse,
)
async def get_plan_agents(
    project_id: str, request: Request
) -> ProjectPlanAgentsResponse:
    """Return valid agent_role / skill / tool names for plan validation.

    Added 2026-07-28 for the chatbox plan editor (Phase 1,
    docs/chatbox-plan-editor.md §5 §7.3). The chatbox LLM calls
    this once per session (cached for the conversation lifetime) to
    learn which names it can safely use in PlanStep suggestions,
    avoiding the round-trip-and-fail cycle of suggesting an invalid
    `agent_role` and getting a 400 from PUT.

    Source of truth:
      agent_roles ← agent_profiles.name (filtered to active profiles)
      tools       ← tool_definitions.name (may be empty if no tools
                    registered yet — the LLM should pass "" for now)
      skills      ← unique non-null values found in
                    workflow_packages.step_template[*].skill across
                    all existing packages (best-effort, no canonical
                    skill registry yet). Empty list is valid.

    This endpoint is read-only, project-scoped for path consistency
    with the other /plan/* endpoints, but the data itself is global
    (not per-project).
    """
    db = request.app.state.db
    proj = await db.fetchone(
        "SELECT id FROM projects WHERE id = ?", (project_id,),
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    data = await _compute_plan_agents(db, project_id)
    return ProjectPlanAgentsResponse(**data)


async def _compute_plan_agents(db, project_id: str) -> dict:
    """Compute the agent_role / skill / tool names for a project.

    Shared helper for the /plan/agents endpoint AND the chatbox
    snapshot builder (which needs the same data inline). Returns
    a dict with keys: project_id, agent_roles, skills, tools.
    Does NOT raise 404 — the caller decides whether to.
    """
    profile_rows = await db.fetchall(
        "SELECT name FROM agent_profiles "
        "WHERE status IS NULL OR status != 'disabled' "
        "ORDER BY name"
    )
    agent_roles = [r["name"] for r in profile_rows]
    # tools: from tool_definitions; table may be empty
    tools: list[str] = []
    try:
        tool_rows = await db.fetchall(
            "SELECT name FROM tool_definitions ORDER BY name"
        )
        tools = [r["name"] for r in tool_rows]
    except Exception:
        # table missing or other issue — return empty list
        pass
    # skills: best-effort scan of workflow_packages.step_template
    skills: list[str] = []
    try:
        wf_rows = await db.fetchall(
            "SELECT step_template FROM workflow_packages"
        )
        seen: set[str] = set()
        for row in wf_rows:
            raw = row.get("step_template")
            if not raw:
                continue
            try:
                steps = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(steps, list):
                continue
            for s in steps:
                if not isinstance(s, dict):
                    continue
                skill = s.get("skill")
                if isinstance(skill, str) and skill and skill not in seen:
                    seen.add(skill)
                    skills.append(skill)
    except Exception:
        pass
    return {
        "project_id": project_id,
        "agent_roles": agent_roles,
        "skills": sorted(skills),
        "tools": tools,
    }


# v3.10.4 (2026-08-02): "Generate SOUL" fallback endpoint. The
# primary path auto-seeds project_soul_presets at plan-save time
# (see put_project_plan below). This endpoint exists for the cases
# where the auto-seed couldn't run or the user wants to refresh:
#   1. Plan was saved before v3.10.4 (no auto-seed happened)
#   2. Routing at save time had no idle profile (routing now
#      succeeds after agents came online)
#   3. User edited the plan and wants to re-seed (preserves any
#      manual edits to existing presets — same idempotency as
#      the auto-seed)
#   4. User wants to (re)generate SOUL after the LLM plan was
#      updated externally
class GenerateSoulFromPlanResponse(BaseModel):
    """Response shape for POST /api/projects/{id}/plan/generate-soul.

    Mirrors the stats dict from `_seed_soul_presets_from_plan` so
    the UI can show a confirmation: "Created 3 preset(s), skipped
    2 already-existing, 1 role had no profile available".
    """
    project_id: str
    presets_created: int
    presets_skipped_existing: int
    roles_seen: int
    roles_skipped_no_default_soul: int
    roles_skipped_no_profile: int
    # v3.10.4 follow-up: count of roles that used the generic
    # fallback (because the plan had no `default_soul` for them).
    # The UI surfaces this so the user knows which presets need
    # custom editing.
    roles_used_generic_fallback: int
    # v3.10.5: count of roles whose persona came from a dedicated
    # LLM call (`_generate_souls_via_llm`). Distinct from the
    # generic-fallback count so the UI can show "X roles got
    # LLM-generated personas, Y used the generic template".
    roles_used_llm_generated: int
    # v3.10.5: status of the LLM call so the UI can show
    # "Generated 3 personas via LLM" or "LLM call failed, fell back
    # to generic". Values: "ok" | "failed" | "skipped_mock"
    llm_call_status: str
    errors: list[dict[str, str]]


@router.post(
    "/projects/{project_id}/plan/generate-soul",
    response_model=GenerateSoulFromPlanResponse,
)
async def generate_soul_from_plan(
    project_id: str, request: Request,
) -> GenerateSoulFromPlanResponse:
    """Seed project_soul_presets from the project's plan_json.

    v3.10.5 (2026-08-02): the recovery button now also calls the
    LLM to produce role-specific personas. The "Generate SOUL"
    button has two real use cases:
      1. User deleted an auto-generated SOUL by mistake and wants
         it back. `fill_empty_only=True` refills the empty preset
         without clobbering user edits elsewhere.
      2. The initial seed at plan-save / plan-run time fell back to
         the generic template (e.g. agents were offline so routing
         couldn't resolve profiles). With agents back, the user
         clicks this to (re)generate proper personas.

    In both cases, the LLM has full project context (name +
    description + plan steps) so the personas are specific to the
    project, not generic. If the LLM call fails, we fall through
    to the seed helper's normal step.default_soul / generic path.

    Behavior:
      200 with the stats dict on success (even if 0 presets
        were created — caller can show a friendly message)
      404 if the project doesn't exist
      409 if the project has no plan_json (call /plan/from-llm
        first to create one)

    Side effect: creates / updates project_soul_presets rows for
    any role whose preset is missing OR whose existing content is
    empty. NEVER clobbers non-empty content (user edits preserved).
    No new tasks are created.
    """
    db = request.app.state.db
    proj = await db.fetchone(
        "SELECT id, name, goal, plan_json FROM projects WHERE id = ?",
        (project_id,),
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    has_plan, plan, _ = _load_plan_from_row(proj)
    if not has_plan or plan is None:
        raise HTTPException(
            409,
            "No plan to generate SOUL from. Call POST /api/projects/{id}/plan/from-llm "
            "first, or save a plan via PUT /api/projects/{id}/plan."
        )
    # v3.10.5: call LLM to produce role-specific personas. Failure
    # is non-fatal — the seed helper falls through to step.default_soul
    # then the generic template, so the user always gets something.
    llm_souls: dict[str, str] | None = None
    llm_call_status = "skipped_mock"
    try:
        cfg = getattr(request.app.state, "config", None) or {}
        llm_cfg = (cfg.get("llm") or {}) if isinstance(cfg, dict) else {}
        from hermes_orch.orchestrator.soul_dispatch import (
            _generate_souls_via_llm,
        )
        llm_souls = await _generate_souls_via_llm(
            plan,
            project_name=proj.get("name") or "",
            project_description=proj.get("goal") or "",
            llm_cfg=llm_cfg,
        )
        llm_call_status = "ok" if llm_souls else "empty_response"
    except Exception as e:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "generate-soul: LLM call failed (%s); falling back to "
            "step.default_soul / generic template", e,
        )
        llm_call_status = "failed"
    stats = await _seed_soul_presets_from_plan(
        db, project_id, plan,
        llm_souls=llm_souls,
        fill_empty_only=True,
    )
    return GenerateSoulFromPlanResponse(
        project_id=project_id,
        presets_created=stats["presets_created"],
        presets_skipped_existing=stats["presets_skipped_existing"],
        roles_seen=stats["roles_seen"],
        roles_skipped_no_default_soul=stats["roles_skipped_no_default_soul"],
        roles_skipped_no_profile=stats["roles_skipped_no_profile"],
        roles_used_generic_fallback=stats.get("roles_used_generic_fallback", 0),
        roles_used_llm_generated=stats.get("roles_used_llm_generated", 0),
        llm_call_status=llm_call_status,
        errors=stats["errors"],
    )


@router.put("/projects/{project_id}/plan", response_model=ProjectPlanResponse)
async def put_project_plan(
    project_id: str,
    body: ProjectPlanUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    audit_actor: str = "operator",
) -> ProjectPlanResponse:
    """Write a project's plan (Phase A, optimistic-lock enabled 2026-07-28).

    The plan is stored as JSON in projects.plan_json. Any PUT
    replaces the previous plan (no merge / partial update) — this
    is intentional, the plan is a versioned document and merging
    parts of it gets messy. The previous plan is not preserved as
    history (Phase D may add plan_history; for now, last-write-wins).

    Optimistic lock (chatbox contract, docs/chatbox-plan-editor.md §7.1):
      The `If-Match: <updated_at>` header is **optional** for backward
      compat with the visual editor (Phase C). When provided:
        - If a plan already exists (plan_json non-NULL) and the
          provided value doesn't match the current `updated_at`,
          return 409 Conflict with the current plan in the body
          so the client can show a 3-way merge.
        - If a plan does not exist yet (first PUT), the header is
          ignored (no prior state to lock against).
        - If matching, the write proceeds.
      When omitted, the write proceeds (legacy last-write-wins).
      The chatbox LLM always provides If-Match; the visual editor
      can be upgraded later to do the same.

    Audit: project.plan.updated (actor defaults to "operator"; the
    chatbox apply endpoint passes "operator:chat" so audit logs
    distinguish AI-applied updates from human edits).

    Edge cases:
      - Project not found: 404
      - Invalid plan (Pydantic validation): 422 with the field error
      - Empty plan (steps=[]): valid — represents "no plan yet"
      - Same plan PUT twice: idempotent (just overwrites the row)
      - If-Match mismatch on existing plan: 409 with current_plan
      - First PUT (plan_json IS NULL): If-Match ignored, write proceeds
    """
    db = request.app.state.db
    proj = await db.fetchone(
        "SELECT id, plan_json, updated_at FROM projects WHERE id = ?",
        (project_id,),
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    # Optimistic lock: if the client provided If-Match AND a plan
    # already exists, the value must match the current updated_at.
    has_plan_json = proj.get("plan_json") is not None
    current_updated_at = proj.get("updated_at")
    if has_plan_json and if_match is not None and if_match != current_updated_at:
        # Build a 409 with the current plan so the client can diff
        has_plan, current_plan, _ = _load_plan_from_row(proj)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "plan was modified since you last read it",
                "your_if_match": if_match,
                "current_updated_at": current_updated_at,
                "current_plan": (
                    current_plan.model_dump(mode="json") if current_plan else None
                ),
            },
        )
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
    # v3.10.4 (2026-08-02): auto-seed project_soul_presets from
    # the LLM-drafted `default_soul` on each plan step. The user
    # can then see + edit the SOUL presets on the project page
    # BEFORE clicking the green [▶ Run] button (which dispatches).
    # Without this, the SOUL only materializes AT dispatch time
    # (via `_ensure_soul_preset` in orchestrator.soul_dispatch) —
    # by the time the user sees the soul in the project page's
    # "Show SOUL editor" toggle, the task is already running with
    # the auto-generated content. The user can still edit it on
    # the project page; the dispatch sees the existing preset and
    # uses the edited content.
    #
    # Idempotent: only creates presets that don't already exist
    # (preserves any manual edits the user has made on the
    # project page). Skips steps without a `default_soul`.
    # Skips roles where the routing engine can't resolve a
    # profile (the dispatch path will create the preset later
    # with a generic template).
    await _seed_soul_presets_from_plan(db, project_id, plan)
    # Audit
    try:
        from hermes_orch.core.audit import audit_log
        await audit_log(
            db, "project.plan.updated", actor=audit_actor,
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


# ===== Phase B: reset terminal-state project to planned =====
# (POST /api/projects/{id}/plan/reset)
#
# A project in state='completed' or 'cancelled' can't have its plan
# re-run (see run_project_plan guard below). The user has two options:
#   1. Create a new project
#   2. Reset the current one back to 'planned' and re-run
#
# Per user feedback 2026-07-28: "我加了action 目標, 但再跑就出
# Cannot run plan on a terminal-state project (state='completed')"
# — the plan editor's "▶ Generate tasks" button needs a way out
# of the terminal-state guard. This endpoint is the way out. The
# plan is preserved (not deleted), the previous tasks are kept in
# the DB (already marked archived by the prior run), and the user
# can edit the plan + re-run.
#
# NOT the same as unarchive: unarchive goes from state='archived' to
# state='completed' (existing in projects.py:734-744). This endpoint
# goes from state='completed' or 'cancelled' to state='planned'.
#
# NOT the same as undelete: undelete goes from state='deleted' back
# to its prior state (existing in projects.py:783-799).


class ResetPlanResponse(BaseModel):
    project_id: str
    state: str
    previous_state: str
    plan_steps: int
    message: str


@router.post(
    "/projects/{project_id}/plan/reset",
    response_model=ResetPlanResponse,
)
async def reset_plan_to_planned(
    project_id: str, request: Request,
) -> ResetPlanResponse:
    """Reset a terminal-state (completed/cancelled) project to 'planned'
    so its plan can be re-run. Preserves the plan + existing tasks.
    """
    db = request.app.state.db
    proj = await db.fetchone(
        "SELECT id, state, plan_json FROM projects WHERE id = ?",
        (project_id,),
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    if proj["state"] in ("deleted", "archived"):
        raise HTTPException(
            400,
            f"Cannot reset a {proj['state']!r} project via /plan/reset. "
            f"Use the unarchive / undelete endpoints first.",
        )
    if proj["state"] not in ("completed", "cancelled"):
        raise HTTPException(
            400,
            f"Project is in state {proj['state']!r}; nothing to reset. "
            f"Only completed/cancelled projects need this endpoint.",
        )
    # Parse plan to count steps (for the response message)
    import json as _json
    plan_steps = 0
    if proj.get("plan_json"):
        try:
            plan_obj = _json.loads(proj["plan_json"])
            plan_steps = len(plan_obj.get("steps") or [])
        except (_json.JSONDecodeError, TypeError):
            pass
    previous_state = proj["state"]
    now = _now_iso()
    # Reset to 'planned'. We do NOT clear plan_json or tasks — the
    # user is going to click Generate tasks next, which will
    # archive the existing tasks (with archive_existing=true, the
    # default in visual_plan.js generateTasks) and create new ones.
    # current_iteration stays so the audit log can correlate runs.
    await db.execute(
        "UPDATE projects SET state = 'planned', updated_at = ? WHERE id = ?",
        (now, project_id),
    )
    # Audit the reset so the operator can trace state changes.
    try:
        from hermes_orch.core.audit import audit_log
        await audit_log(
            db, "project.plan.reset", actor="operator",
            project_id=project_id,
            payload={
                "previous_state": previous_state,
                "plan_steps": plan_steps,
                "reason": "operator_initiated",
            },
        )
    except Exception:
        # audit is best-effort; don't fail the reset if it's missing
        pass
    return ResetPlanResponse(
        project_id=project_id,
        state="planned",
        previous_state=previous_state,
        plan_steps=plan_steps,
        message=(
            f"Project reset from {previous_state!r} to 'planned'. "
            f"{plan_steps} plan step(s) preserved. "
            f"Click ▶ Generate tasks to re-run."
        ),
    )


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
        # v1.9.4: resolve feedback_to (step name → task id), with
        # the same self-reference + dangling-name rules as
        # depends_on. A step can't loop back to itself (no-op),
        # and a name that resolves to neither a workflow-internal
        # step nor an existing project task is dropped with an
        # audit event (matches apply-workflow's behavior).
        fb_tids: list[str] = []
        unresolved_fb: list[str] = []
        for f in (step.feedback_to or []):
            if f == step.name:
                continue  # self-ref is a silent no-op
            if f in name_to_tid:
                fb_tids.append(name_to_tid[f])
            elif f in existing_name_to_tid:
                fb_tids.append(existing_name_to_tid[f])
            else:
                unresolved_fb.append(f)
        if unresolved_fb:
            try:
                from hermes_orch.core.audit import audit_log as _al_fb
                await _al_fb(
                    db, "task.feedback_to_unresolved",
                    actor="plan-runner", project_id=project_id, task_id=tid,
                    payload={"step_name": step.name,
                             "unresolved_feedback": unresolved_fb,
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
            "feedback_to": json.dumps(fb_tids),
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
    # 4b. v3.10.5 (2026-08-02): generate SOULs via LLM at "Generate
    # Task" time. The planner's `default_soul` field was unreliable
    # (often returned empty under token pressure) so we now make a
    # dedicated LLM call focused on persona writing with the full
    # project context. Failure is non-fatal — the seed helper falls
    # through to step.default_soul then the generic template.
    #
    # fill_empty_only=True so user-edited presets survive this run.
    # The seed also runs at plan-save (PUT /plan) with
    # fill_empty_only=False; this run re-fills only the empty ones
    # the prior seed couldn't populate (e.g. routing failed because
    # agents were offline, OR the plan predates the LLM SOUL path).
    llm_souls: dict[str, str] | None = None
    try:
        cfg = getattr(request.app.state, "config", None) or {}
        llm_cfg = (cfg.get("llm") or {}) if isinstance(cfg, dict) else {}
        from hermes_orch.orchestrator.soul_dispatch import (
            _generate_souls_via_llm,
        )
        proj_for_llm = await db.fetchone(
            "SELECT name, goal FROM projects WHERE id = ?",
            (project_id,),
        )
        llm_souls = await _generate_souls_via_llm(
            plan,
            project_name=(proj_for_llm or {}).get("name") or "",
            project_description=(proj_for_llm or {}).get("goal") or "",
            llm_cfg=llm_cfg,
        )
    except Exception as e:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "run_project_plan: LLM SOUL gen failed (%s); "
            "falling back to step.default_soul / generic", e,
        )
        # llm_souls stays None → seed helper uses step.default_soul
        # or generic. Tasks are already created, no rollback.
    await _seed_soul_presets_from_plan(
        db, project_id, plan,
        llm_souls=llm_souls,
        fill_empty_only=True,
    )
    # 5. v3.10.4 (2026-08-02): do NOT auto-dispatch. The project
    # stays in 'planned' state so the user can review the new
    # tasks + their SOUL presets (auto-seeded from the plan's
    # `default_soul` fields; see _seed_soul_presets_from_plan in
    # the plan-save handler) before clicking the green [▶ Run]
    # button. Previously this UPDATE flipped state to 'ready'
    # and the supervisor's next tick dispatched — too aggressive
    # for an LLM-generated plan where the user often wants to
    # tweak the auto-seeded SOUL content first.
    #
    # The existing /api/projects/{id}/run endpoint is the
    # explicit dispatch path; it still flips planned→ready.
    # v3.10.4 pre-merge behavior was: this UPDATE ran first, so
    # the project went straight from plan-run to running. Now
    # the user has an explicit review step between plan-run and
    # actual dispatch.
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
    from hermes_orch.api.dashboard import _base_context, _llm_configured
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "visual_plan.html",
        {
            # _base_context supplies current_user_ctx + active_page +
            # llm_configured. Without it, base.html falls back to the
            # "Sign in" link in the topbar even when the user is logged
            # in (v3.4 introduced the user pill, but visual_plan was
            # still passing a hand-built context that omitted it).
            **(await _base_context(request, "projects")),
            "project": proj_view,
            "available_roles": available_roles,
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
    # v3.5.2 fix: dedupe step names BEFORE building PlanStep objects.
    # The LLM (especially MiniMax M3) sometimes returns two steps with
    # the same name (e.g. "langgraph" and "langgraph" both as the name
    # for different agent_role variants). ProjectPlan's field_validator
    # rejects duplicate step names with a Pydantic ValidationError
    # that, before this fix, bubbled up as an unhandled exception and
    # became a 500 HTML page — which the frontend tried to parse as
    # JSON, leading to the cryptic "Failed: Unexpected token 'I',
    # 'Internal S'... is not valid JSON" the user reported on
    # 2026-07-31. Rename duplicates to <name>-2, <name>-3, etc.
    # so the validator passes and the user gets a usable plan.
    _seen_step_names: set[str] = set()
    def _dedupe_step_name(name: str) -> str:
        base = name
        n = 2
        while name in _seen_step_names:
            name = f"{base}-{n}"
            n += 1
        _seen_step_names.add(name)
        return name
    steps: list[PlanStep] = []
    for t in task_dicts:
        raw_name = str(t.get("name") or t.get("id") or "step")
        name = _to_kebab(raw_name)
        if body.name_suffix:
            name = f"{name}{body.name_suffix}"
        # v3.5.2: ensure unique within this plan (see _dedupe_step_name
        # comment above for the MiniMax duplicate-name bug we hit).
        name = _dedupe_step_name(name)
        # Planner returns depends_on as a list of step names (or ids
        # — we treat them as opaque names; the validator at save time
        # will check they exist in the plan).
        deps = t.get("depends_on") or []
        # v1.9.4: planner may also return feedback_to (loop-back).
        # Pass through if present; default to empty list.
        feedback = t.get("feedback_to") or []
        if not isinstance(feedback, list):
            feedback = []
        feedback = [str(f) for f in feedback if f]
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
            feedback_to=feedback,
            params_template=raw_params,
            output_path=str(t.get("output_path") or ""),
        ))
    # v3.5.2 follow-up: resolve depends_on / feedback_to references to
    # the actual step names we just produced.
    #
    # The LLM (especially MiniMax M3) is inconsistent about naming
    # conventions: it often uses kebab-case for `name` ("research-langgraph")
    # but Title Case + space for `depends_on` references
    # ("Research LangGraph"). The planner's own validation at
    # core/planner.py:640 only checks "this ref exists earlier in the
    # plan" — it can't catch a case where the LLM is internally
    # consistent (Title Case everywhere) but the *endpoint* kebab-cases
    # the names (turning "Research LangGraph" into "research-langgraph")
    # while leaving depends_on untouched. Net result: the plan validates
    # and saves, but the canvas can't draw wires because the names
    # don't match. User saw this on proj-56c8e080 with the
    # "LangGraph vs AutoGen vs CrewAI 分析..." goal — 5 steps, 7
    # dangling depends_on refs, no wires.
    #
    # Strategy: for each step's depends_on / feedback_to entry, try
    # (1) exact match, (2) kebab-case the ref then match, (3) lowercase
    # comparison after kebab-casing. If no match, drop the ref with a
    # warning. The user keeps a usable plan (steps still run, just
    # without the broken wire) and we get a logger trail for debugging
    # future LLM regressions.
    if steps:
        _step_names_set: set[str] = {s.name for s in steps}
        # Precompute a lowercased index for the case-insensitive fallback
        # so we don't lowercase every step.name on every ref. Keeps the
        # resolver O(n + m) instead of O(n*m) for n steps and m refs.
        _step_names_lower_index: dict[str, str] = {
            s.name.lower(): s.name for s in steps
        }
        def _resolve_step_ref(ref: str) -> str | None:
            """Resolve a depends_on / feedback_to ref to an actual step
            name in this plan, or None if no match.

            Tries (in order):
              1. exact match against step names
              2. kebab-case the ref, then exact match
              3. case-insensitive match (after kebab-casing both sides)

            Returns the original step name (preserves the convention
            chosen by the endpoint's _to_kebab + dedup pipeline).
            """
            if ref in _step_names_set:
                return ref
            ref_kebab = _to_kebab(ref)
            if ref_kebab in _step_names_set:
                return ref_kebab
            return _step_names_lower_index.get(ref_kebab.lower())
        _resolved_count = 0
        _dropped_count = 0
        for s in steps:
            new_deps: list[str] = []
            for d in s.depends_on:
                resolved = _resolve_step_ref(d)
                if resolved is None:
                    logger.warning(
                        "from-llm: dropping dangling depends_on ref %r "
                        "on step %r (no step with that name in this "
                        "plan; available: %s)",
                        d, s.name, sorted(_step_names_set),
                    )
                    _dropped_count += 1
                    continue
                if resolved != d:
                    logger.info(
                        "from-llm: resolved depends_on ref %r -> %r "
                        "on step %r (LLM used inconsistent casing/"
                        "spacing; normalised to match step name)",
                        d, resolved, s.name,
                    )
                    _resolved_count += 1
                new_deps.append(resolved)
            # PlanStep is a Pydantic v2 BaseModel without
            # model_config = ConfigDict(frozen=True), so the list
            # fields are mutable in place.
            s.depends_on = new_deps
            new_fb: list[str] = []
            for f in s.feedback_to:
                resolved = _resolve_step_ref(f)
                if resolved is None:
                    logger.warning(
                        "from-llm: dropping dangling feedback_to ref %r "
                        "on step %r (no step with that name in this "
                        "plan; available: %s)",
                        f, s.name, sorted(_step_names_set),
                    )
                    _dropped_count += 1
                    continue
                if resolved != f:
                    logger.info(
                        "from-llm: resolved feedback_to ref %r -> %r "
                        "on step %r (LLM used inconsistent casing/"
                        "spacing; normalised to match step name)",
                        f, resolved, s.name,
                    )
                    _resolved_count += 1
                new_fb.append(resolved)
            s.feedback_to = new_fb
        if _resolved_count or _dropped_count:
            logger.info(
                "from-llm: depends_on/feedback_to resolution pass: "
                "%d ref(s) normalised, %d dangling ref(s) dropped",
                _resolved_count, _dropped_count,
            )
    # v3.5.2 safety net: even with dedup, the LLM might produce
    # something that fails another field validator (bad agent_role,
    # missing required field, etc.). If the ProjectPlan constructor
    # raises, return a 400 with a human-readable message so the user
    # gets a real error instead of "Failed: Unexpected token 'I',
    # 'Internal S'... is not valid JSON" (which is what happens when
    # FastAPI's default 500 HTML page bubbles up to a JSON.parse in
    # the frontend). The dedup above handles the most common case
    # (duplicate step names); this catch is the broader safety net
    # for any future Pydantic schema tightening.
    from pydantic import ValidationError as _PydValidationError
    try:
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
    except _PydValidationError as _e:
        # Extract a short, human-readable summary of which field failed
        # and why. Pydantic's .errors() gives us a structured list;
        # we collapse to one short string per error for the response.
        _errs = _e.errors()
        _summary = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}"
            for e in _errs[:5]
        )
        if len(_errs) > 5:
            _summary += f" (+{len(_errs) - 5} more)"
        raise HTTPException(
            400,
            f"LLM returned a plan that failed validation after dedup "
            f"({len(_errs)} error(s)): {_summary}. Edit the goal or "
            f"try again — the LLM may produce a different plan.",
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


# ===== Phase E (v3.8.0, 2026-08-01): Save plan as workflow =====
#
# Per user feedback after v3.7.1: the runtime "Promote to workflow"
# button on the visual project page is the only useful action there,
# and it doesn't belong on the runtime page anyway — the design-time
# source (the plan) lives in the visual plan editor. The new flow:
#
#   - User opens the visual plan editor
#   - User edits the plan (steps, agent_role, action, depends_on, etc.)
#   - User clicks "Save as workflow" (in the toolbar, next to Save)
#   - Modal opens: workflow name + optional description
#   - POST /api/projects/{id}/plan/to-workflow
#       - Load the project's plan
#       - Build evidence block (companion helper in workflows.py)
#       - Call LLM to generalize the plan (add {{var}} placeholders)
#       - Validate the LLM's package
#       - Write workflow_packages row with source_project_id
#   - Redirect user to /workflows/{new_id}
#
# Differences from the OLD `from-project` endpoint (workflows.py):
#   - Source is the plan, not the project's tasks
#   - Project does NOT need to be in a terminal state (a plan can
#     exist on a freshly created project). The OLD endpoint required
#     `state in (completed, failed, cancelled, interrupted)`.
#   - Variables list: starts empty (the plan doesn't declare any
#     yet; the LLM synthesizes them based on the plan's params_template
#     values + operator_hints).
#   - Variable hints: pulled from the plan's own variables[] if the
#     operator added some (currently always empty in Phase A, but
#     reserved for Phase B+ when plan variables are editable in the
#     side panel).
#
# Both endpoints coexist (Q4 sign-off 2026-08-01): the old one
# handles the "I ran a project and now want to template-ize it" path,
# this new one handles the "I designed a plan and want to template-ize
# it before running" path. Different use cases, both legitimate.


class PlanToWorkflowBody(BaseModel):
    """Body for POST /api/projects/{id}/plan/to-workflow.

    `name` is the workflow package name (kebab-case, unique).
    `description` is optional (overrides the LLM-generated one).
    `variable_hints` are optional operator hints fed to the LLM
    (same shape as PromoteToWorkflowBody.variable_hints).
    """
    name: str = Field(
        ..., min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$"
    )
    description: str = Field("", max_length=500)
    variable_hints: list[dict] = Field(default_factory=list)


@router.post(
    "/projects/{project_id}/plan/to-workflow",
    response_model=None,  # returns the WorkflowDetail shape from workflows.py
)
async def save_plan_as_workflow(
    project_id: str, body: PlanToWorkflowBody, request: Request,
) -> dict:
    """Synthesize a workflow package from a project's plan (v3.8.0).

    The plan is the design-time source — the user has been editing
    steps on the canvas, and clicks "Save as workflow" to package
    the plan as a reusable template. LLM generalizes concrete values
    into {{var}} placeholders.

    Does NOT require the project to be in a terminal state — the
    plan can be saved as a workflow even on a freshly-created project
    (the plan is a design-time artifact, separate from execution).
    """
    from fastapi import HTTPException as _HTTPException
    # Late import: avoid circular import (workflows.py imports from
    # plans.py via _project_id/_projects_root/_serialize_plan_md in
    # apply-workflow, so we keep the boundary in one direction).
    from hermes_orch.api.workflows import (
        _call_llm_for_workflow_synthesis,
        _gather_workflow_evidence_from_plan,
        _row_to_workflow_detail,
        _validate_workflow_package,
    )
    from hermes_orch.core.audit import audit_log

    db = request.app.state.db
    cfg = request.app.state.config

    # 1. Load project
    proj = await db.fetchone(
        "SELECT id, name, state, goal, coordinator_role, plan_json "
        "FROM projects WHERE id = ?",
        (project_id,),
    )
    if not proj:
        raise HTTPException(404, f"project {project_id} not found")

    # 2. Load plan (must be non-NULL + non-empty)
    raw = proj.get("plan_json")
    if not raw:
        raise _HTTPException(
            400,
            f"project {project_id} has no plan yet. Open the visual "
            f"plan editor, add some steps, click Save, then try "
            f"Save as workflow again.",
        )
    try:
        plan_dict = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise _HTTPException(
            400,
            f"plan_json is malformed: {e}; the plan must be valid JSON "
            f"before it can be promoted to a workflow.",
        )
    steps = plan_dict.get("steps") or []
    if not steps:
        raise _HTTPException(
            400,
            f"project {project_id} has a plan but it has no steps. "
            f"Add at least one step in the visual plan editor, then "
            f"try Save as workflow again.",
        )

    # 3. Name uniqueness
    existing = await db.fetchone(
        "SELECT id FROM workflow_packages WHERE name = ?", (body.name,)
    )
    if existing:
        raise _HTTPException(
            409,
            f"workflow package name={body.name!r} already exists "
            f"(id={existing['id']}); pick a different name or PATCH "
            f"the existing one.",
        )

    # 4. Build evidence (plan-shaped) + call LLM
    evidence = _gather_workflow_evidence_from_plan(plan_dict, proj)
    llm_cfg = cfg.get("llm", {})

    try:
        pkg = await _call_llm_for_workflow_synthesis(
            evidence, llm_cfg, body.variable_hints
        )
    except _HTTPException:
        raise
    except Exception as e:
        raise _HTTPException(
            502, f"workflow LLM synthesis from plan failed: {type(e).__name__}: {e}"
        )

    # 5. Operator description overrides LLM description if provided.
    if body.description:
        pkg["description"] = body.description

    # Defensive: the LLM sometimes forgets the top-level `description`
    # wrapper key. Synthesize a sensible default rather than failing.
    # Same logic as the from-project endpoint (workflows.py:~920).
    if not pkg.get("description"):
        try:
            n_steps = len(pkg.get("step_template", []))
            first_action = (pkg["step_template"][0].get("action", "")
                            if pkg.get("step_template") else "")
            plan_name = (plan_dict.get("name") or "").strip()
            project_name = (proj.get("name") or proj.get("id") or project_id)
            source_label = (f"plan '{plan_name}' of project " if plan_name
                            else "plan of project ")
            if first_action:
                pkg["description"] = (
                    f"{first_action} workflow (synthesized from "
                    f"{source_label}{project_name}, {n_steps} step"
                    f"{'s' if n_steps != 1 else ''})"
                )
            else:
                pkg["description"] = (
                    f"Workflow synthesized from {source_label}{project_name} "
                    f"({n_steps} step{'s' if n_steps != 1 else ''})"
                )
        except Exception:
            pkg["description"] = (
                f"Workflow synthesized from project {project_id}"
            )

    # 6. Validate the LLM's package
    ok, err = _validate_workflow_package(pkg)
    if not ok:
        # Same UX as the from-project endpoint: include the LLM output
        # in the error so the operator can see what came back.
        llm_dump = json.dumps(pkg, ensure_ascii=False)[:1500]
        raise _HTTPException(
            422,
            f"LLM-produced workflow failed validation: {err}. "
            f"Try again or hand-craft the workflow via PATCH. "
            f"LLM output: {llm_dump}",
        )

    # 7. Write to DB
    wid = f"wf-{secrets.token_hex(6)}"
    now = _now_iso()
    try:
        await db.execute(
            "INSERT INTO workflow_packages "
            "(id, name, version, description, step_template, variables, "
            " source_project_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                wid, body.name, pkg.get("version", "0.1.0"),
                pkg["description"],
                json.dumps(pkg["step_template"], ensure_ascii=False),
                json.dumps(pkg["variables"], ensure_ascii=False),
                project_id, now, now,
            ),
        )
    except Exception as e:
        raise _HTTPException(500, f"DB insert failed: {e}")

    # 8. Audit
    try:
        await audit_log(
            db, "workflow.created", actor="operator", project_id=project_id,
            payload={
                "workflow_id": wid, "name": body.name,
                "step_count": len(pkg["step_template"]),
                "variable_count": len(pkg["variables"]),
                "source": "save-as-workflow-from-plan",
            },
        )
    except Exception:
        pass

    row = await db.fetchone(
        "SELECT * FROM workflow_packages WHERE id = ?", (wid,)
    )
    return _row_to_workflow_detail(row)
