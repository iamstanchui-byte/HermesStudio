"""Agent contracts — planning-time LLM hooks (Object Layer Agent Layer, 2026-07-26).

The doc's 5 standardized agent contracts (plan / route / judge /
repair / audit) are implemented as Python classes with:
  - Pydantic input_model + output_model
  - Prompt template (str.format compatible)
  - A registry of all contracts

Per the doc, all 5 contracts are PLANNING-TIME only — the LLM
returns a DRAFT suggestion that the operator reviews and adjusts.
The runtime (depends_on, dispatch, retry, status machine) stays
fully deterministic; the LLM is never in the hot path.

`plan` is the only contract with a working LLM call today. The
others (`route`, `judge`, `repair`, `audit`) are stubs that the
next commit's commits will wire into their respective UI flows.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from hermes_orch.core.contracts.base import Contract, ContractError
from hermes_orch.core.contracts.plan import PlanContract, PlanInput, PlanOutput
from hermes_orch.core.contracts.route import RouteContract, RouteInput, RouteOutput
from hermes_orch.core.contracts.judge import JudgeContract, JudgeInput, JudgeOutput
from hermes_orch.core.contracts.repair import RepairContract, RepairInput, RepairOutput
from hermes_orch.core.contracts.audit import AuditContract, AuditInput, AuditOutput


# Registry: name -> Contract instance
CONTRACTS: dict[str, Contract] = {
    "plan": PlanContract(),
    "route": RouteContract(),
    "judge": JudgeContract(),
    "repair": RepairContract(),
    "audit": AuditContract(),
}


def get_contract(name: str) -> Contract:
    """Look up a contract by name, or raise ContractError."""
    if name not in CONTRACTS:
        raise ContractError(
            f"Unknown contract: {name!r}. "
            f"Available: {sorted(CONTRACTS.keys())}"
        )
    return CONTRACTS[name]


def list_contracts() -> list[dict[str, Any]]:
    """Return the contract registry as a JSON-friendly list (for the API)."""
    out: list[dict[str, Any]] = []
    for c in CONTRACTS.values():
        out.append({
            "name": c.name,
            "description": c.description,
            "input_schema": c.input_model.model_json_schema(),
            "output_schema": c.output_model.model_json_schema(),
            "implemented": c.implemented,
        })
    return out


__all__ = [
    "Contract",
    "ContractError",
    "CONTRACTS",
    "get_contract",
    "list_contracts",
    "PlanContract", "PlanInput", "PlanOutput",
    "RouteContract", "RouteInput", "RouteOutput",
    "JudgeContract", "JudgeInput", "JudgeOutput",
    "RepairContract", "RepairInput", "RepairOutput",
    "AuditContract", "AuditInput", "AuditOutput",
]
