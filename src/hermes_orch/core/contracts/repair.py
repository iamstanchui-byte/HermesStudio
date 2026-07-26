"""Repair contract — STUB (design-time only, not yet wired to LLM).

Given a task + its failure mode + attempt history, suggest a
retry/repair strategy (which skill to try next, which params to
change, whether to escalate to a human).

Per the doc: `Repair` = "在流程失敗時提出修復策略".
Design-time only — the operator reviews the strategy and the
runtime applies it deterministically.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from hermes_orch.core.contracts.base import Contract


class RepairInput(BaseModel):
    """What the caller passes to the repair contract."""
    task_name: str
    task_action: str
    failure_mode: str
    attempt_history: list[dict[str, Any]] = Field(default_factory=list)
    available_skills: list[dict[str, Any]] = Field(default_factory=list)
    max_attempts: int = 3


class RepairOutput(BaseModel):
    """The contract's suggested repair strategy."""
    strategy: str  # "retry_same" | "switch_skill" | "adjust_params" | "escalate" | "abandon"
    next_skill: str = ""
    next_params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    should_escalate_to_human: bool = False


class RepairContract(Contract):
    name = "repair"
    description = (
        "Given a failed task, suggest a repair strategy (retry, "
        "switch skill, adjust params, or escalate). Design-time only."
    )
    implemented = False  # STUB
    input_model = RepairInput
    output_model = RepairOutput
    system_prompt = (
        "You are a task-failure repair assistant. Given the failure "
        "mode and attempt history, suggest the next action. Output "
        "JSON only matching RepairOutput."
    )
