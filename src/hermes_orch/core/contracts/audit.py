# coding: utf-8
"""Audit contract — STUB (design-time only, not yet wired to LLM).

Given a task + result + optional rubric, produce a structured
audit assessment (6 dimensions: correctness, completeness, format
compliance, risk level, confidence, reproducibility).

Per the doc: `Audit` = "用 rubric 檢查輸出品質".
Design-time only — the operator defines the rubric; the runtime
executes the deterministic audit checks.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from hermes_orch.core.contracts.base import Contract


class AuditInput(BaseModel):
    """What the caller passes to the audit contract."""
    task_name: str
    task_action: str
    task_result: dict[str, Any] = Field(default_factory=dict)
    rubric: str = ""


class AuditDimensions(BaseModel):
    """Six audit dimensions per the doc."""
    correctness: float = Field(..., ge=0.0, le=1.0)
    completeness: float = Field(..., ge=0.0, le=1.0)
    format_compliance: float = Field(..., ge=0.0, le=1.0)
    risk_level: float = Field(..., ge=0.0, le=1.0)
    confidence: float = Field(..., ge=0.0, le=1.0)
    reproducibility: float = Field(..., ge=0.0, le=1.0)


class AuditOutput(BaseModel):
    """The contract's structured audit result."""
    dimensions: AuditDimensions
    overall_score: float = Field(..., ge=0.0, le=1.0)
    issues: list[str] = Field(default_factory=list)
    notes: str = ""


class AuditContract(Contract):
    name = "audit"
    description = (
        "Produce a structured audit assessment (6 dimensions) for a "
        "task result. Design-time only — operator defines the rubric."
    )
    implemented = False  # STUB
    input_model = AuditInput
    output_model = AuditOutput
    system_prompt = (
        "You are an audit assistant. Score the task result on six "
        "dimensions (0-1 each) and produce an overall score. "
        "Output JSON only matching AuditOutput."
    )
