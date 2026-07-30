# coding: utf-8
"""Judge contract — STUB (design-time only, not yet wired to LLM).

Given a task's goal + result, judge whether the result meets the
goal. Returns a structured assessment (pass/fail + score +
criteria met/missed).

Per the doc: `Judge` = "對結果做分類、打分、風險評估".
Design-time only — the operator defines the rubric up front; the
runtime (when wired) just executes the deterministic checks.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from hermes_orch.core.contracts.base import Contract


class JudgeInput(BaseModel):
    """What the caller passes to the judge contract."""
    task_name: str
    task_goal: str
    task_action: str
    task_params: dict[str, Any] = Field(default_factory=dict)
    task_result: dict[str, Any] = Field(default_factory=dict)
    rubric: str = ""  # free-form acceptance criteria, optional


class JudgeOutput(BaseModel):
    """The contract's structured judgment."""
    passed: bool
    score: float = Field(..., ge=0.0, le=1.0)
    criteria_met: list[str] = Field(default_factory=list)
    criteria_missed: list[str] = Field(default_factory=list)
    notes: str = ""


class JudgeContract(Contract):
    name = "judge"
    description = (
        "Judge whether a task's result meets its goal. Returns "
        "passed/score/criteria met or missed. Design-time only."
    )
    implemented = False  # STUB
    input_model = JudgeInput
    output_model = JudgeOutput
    system_prompt = (
        "You are a task-result judge. Compare the result to the goal "
        "and rubric; output a structured judgment matching JudgeOutput. "
        "JSON only."
    )
