"""Route contract — STUB (design-time only, not yet wired to LLM).

Given a task description (name, action, params) and the available
skills/tools/agents, suggest which skill + agent_role to use.

Per the doc: `Route` = "在多個 skill / app / tool 中選最合適的一個".
Design-time only — operator reviews the suggestion and either
accepts it or picks a different one.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from hermes_orch.core.contracts.base import Contract


class RouteInput(BaseModel):
    """What the caller passes to the route contract."""
    task_name: str
    task_action: str
    task_params: dict[str, Any] = Field(default_factory=dict)
    required_capability: str = ""
    available_skills: list[dict[str, Any]] = Field(default_factory=list)
    available_tools: list[dict[str, Any]] = Field(default_factory=list)
    available_agents: list[dict[str, Any]] = Field(default_factory=list)


class RouteOutput(BaseModel):
    """The contract's suggested routing decision."""
    recommended_skill: str = ""
    recommended_agent: str = ""
    rationale: str = ""
    alternatives: list[str] = Field(default_factory=list)


class RouteContract(Contract):
    name = "route"
    description = (
        "Given a task, suggest which skill + agent_role to use. "
        "Design-time only — operator reviews the suggestion."
    )
    implemented = False  # STUB — wire up when the route UI ships
    input_model = RouteInput
    output_model = RouteOutput
    system_prompt = (
        "You are a task-routing assistant. Given a task and the "
        "available skills/tools/agents, suggest the best fit. "
        "Output JSON only matching the RouteOutput schema."
    )
