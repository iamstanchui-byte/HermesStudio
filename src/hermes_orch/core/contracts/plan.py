# coding: utf-8
"""Plan contract — the one real (LLM-backed) contract for now.

Takes a project description + available skills + optional
operator hints, returns a workflow package draft (step_template +
variables + description). This is the contract that the
promote-to-workflow LLM call (api/workflows.py) will be
refactored to use in a follow-up commit.

Per the doc: plan is a DESIGN-TIME hook. The LLM returns a
draft that the operator reviews and adjusts before promoting
to a real workflow package. The runtime never calls this
contract.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from hermes_orch.core.contracts.base import Contract


class PlanInput(BaseModel):
    """What the caller passes to the plan contract."""
    project_name: str = Field(..., min_length=1)
    project_goal: str = Field(..., min_length=1)
    accept_criteria: str = ""
    # Existing tasks in the project (so the contract can decide
    # whether to keep them or propose a fresh structure).
    existing_tasks: list[dict[str, Any]] = Field(default_factory=list)
    # Available skills from the Object Layer. The contract should
    # use these as the "what can the agents actually do" menu.
    available_skills: list[dict[str, Any]] = Field(default_factory=list)
    # Available tools from the Object Layer (capability-only for
    # planning; the contract doesn't install tools, just notes
    # which capabilities the workflow can lean on).
    available_tools: list[dict[str, Any]] = Field(default_factory=list)
    # Free-form operator hints: "report must include X" or
    # "schedule runs at 9am HKT". Mirrors the operator_hints
    # field on the existing workflow_synthesis endpoint.
    operator_hints: list[dict[str, Any]] = Field(default_factory=list)


class PlanStep(BaseModel):
    """One step in the suggested workflow step_template."""
    name: str
    agent_role: str
    action: str
    depends_on: list[str] = Field(default_factory=list)
    params_template: dict[str, Any] = Field(default_factory=dict)
    output_path: str = ""
    skill: str = ""  # optional reference to a skill in the Object Layer
    feedback_to: list[str] = Field(default_factory=list)


class PlanVariable(BaseModel):
    """One {{var}} placeholder in the suggested step_template."""
    name: str
    type: str = "string"  # string | number | boolean
    description: str = ""
    required: bool = False
    default: Any = None


class PlanOutput(BaseModel):
    """The contract's structured output — a workflow package draft."""
    name: str = Field(..., min_length=1)
    description: str = ""
    step_template: list[PlanStep] = Field(default_factory=list)
    variables: list[PlanVariable] = Field(default_factory=list)


SYSTEM_PROMPT = """\
You are a workflow planning assistant. Given a project description,
the existing task list (if any), and the available skills/tools in
the Object Layer, produce a reusable workflow package draft.

Rules:
  1. Output a single JSON object matching the PlanOutput schema:
     {name, description, step_template, variables}
  2. Every step must have: name, agent_role, action, depends_on,
     params_template. Use agent_role values that match a registered
     agent profile (e.g. "win-agent01", "linux-agent01") when
     available; otherwise use a generic role like "default".
  3. Mark every value that would change on a re-run as a
     {{var}} placeholder, not a literal. Examples: dates, file
     names, account numbers, query parameters.
  4. Don't include reasoning, prose, or markdown. JSON only.
  5. If the project has existing tasks, prefer to keep their
     structure unless the operator hints say otherwise.
  6. Use depends_on (forward references) for ordering, not
     feedback_to. feedback_to is reserved for failure-recovery
     loops; the planner can add it later if needed.
  7. Variables list: one entry per UNIQUE {{var}} you used in
     the step_template. Don't add variables you didn't use.
"""


class PlanContract(Contract):
    name = "plan"
    description = (
        "Analyze a project and draft a workflow package (step_template "
        "+ variables + description). Design-time only — operator reviews "
        "and adjusts the draft before promoting."
    )
    implemented = True  # LLM-backed
    input_model = PlanInput
    output_model = PlanOutput
    system_prompt = SYSTEM_PROMPT

    def build_user_prompt(self, validated_in: PlanInput) -> str:
        # Structured rather than a raw JSON dump — the LLM does
        # better with labeled blocks (matches how the existing
        # workflow_synthesis prompt formats its evidence).
        skills_block = (
            json.dumps(validated_in.available_skills, ensure_ascii=False, indent=2)
            if validated_in.available_skills else "[]"
        )
        tools_block = (
            json.dumps(validated_in.available_tools, ensure_ascii=False, indent=2)
            if validated_in.available_tools else "[]"
        )
        tasks_block = (
            json.dumps(validated_in.existing_tasks, ensure_ascii=False, indent=2)
            if validated_in.existing_tasks else "[]"
        )
        hints_block = (
            json.dumps(validated_in.operator_hints, ensure_ascii=False, indent=2)
            if validated_in.operator_hints else "[]"
        )
        return f"""\
Project name: {validated_in.project_name}
Project goal: {validated_in.project_goal}
Accept criteria: {validated_in.accept_criteria or "(none)"}

Existing tasks in the project:
{tasks_block}

Available skills (Object Layer):
{skills_block}

Available tools (Object Layer):
{tools_block}

Operator hints:
{hints_block}

Output a JSON object matching PlanOutput:
  - name (kebab-case, e.g. "monthly-report-pipeline")
  - description (one sentence)
  - step_template: list of steps with name, agent_role, action,
    depends_on, params_template, optional output_path / skill
  - variables: list of {{var}} placeholders used in step_template
"""
