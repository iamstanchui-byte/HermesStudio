# coding: utf-8
"""Agent contract API (Object Layer Agent Layer, 2026-07-26).

The doc's 5 planning-time LLM hooks (plan / route / judge /
repair / audit) are exposed via:
  GET  /api/contracts                  — list the registry
  GET  /api/contracts/{name}           — show one contract's schemas
  POST /api/contracts/{name}/draft     — invoke: validate input,
                                          call LLM, validate output

All endpoints are read or design-time invoke — runtime is fully
deterministic, the LLM is never in the dispatch hot path.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hermes_orch.core.contracts import (
    ContractError, get_contract, list_contracts,
)
from hermes_orch.core.llm_caller import LLMCaller

router = APIRouter()


class ContractInfo(BaseModel):
    """Wire format for a contract in the registry."""
    name: str
    description: str
    implemented: bool
    input_schema: dict
    output_schema: dict


class ContractListOut(BaseModel):
    contracts: list[ContractInfo]


class DraftBody(BaseModel):
    """Body for POST /api/contracts/{name}/draft.

    The `input` field is the contract's input model (validated by
    Pydantic when the contract is invoked). We keep it as a free-
    form dict here so the same body shape works for all 5 contracts
    (each contract validates its own input).
    """
    input: dict
    project_id: str | None = None  # for token usage recording


class DraftOut(BaseModel):
    """Wire format for a contract invocation result."""
    contract: str
    implemented: bool
    output: dict
    # Echoed input for audit + UI re-display
    input: dict


@router.get("", response_model=ContractListOut)
async def list_all() -> ContractListOut:
    """List all 5 agent contracts with their input/output JSON schemas."""
    return ContractListOut(
        contracts=[ContractInfo(**c) for c in list_contracts()]
    )


@router.get("/{name}", response_model=ContractInfo)
async def get_one(name: str) -> ContractInfo:
    """Get one contract's metadata + schemas by name."""
    try:
        c = get_contract(name)
    except ContractError as e:
        raise HTTPException(404, str(e))
    return ContractInfo(
        name=c.name,
        description=c.description,
        implemented=c.implemented,
        input_schema=c.input_model.model_json_schema(),
        output_schema=c.output_model.model_json_schema(),
    )


@router.post("/{name}/draft", response_model=DraftOut)
async def draft_contract(name: str, body: DraftBody, request: Request) -> DraftOut:
    """Invoke a contract: validate input, call LLM, return validated output.

    The contract is found by name in the registry. The input dict
    is validated against the contract's input_model. The LLM is
    called via LLMCaller (uses the same config as Planner +
    workflow synthesis). The output is validated against the
    contract's output_model before returning.

    Stubs (route/judge/repair/audit) return 503 when not in mock
    mode — the LLM call is wired only for `plan` for now.
    """
    try:
        c = get_contract(name)
    except ContractError as e:
        raise HTTPException(404, str(e))
    cfg = request.app.state.config
    db = request.app.state.db
    caller = LLMCaller(cfg, db=db)
    try:
        out_model = await c.draft(
            body.input, caller, project_id=body.project_id,
        )
    except ContractError as e:
        # 400 for input validation, 502 for LLM/parse failure
        msg = str(e)
        if "input validation" in msg.lower():
            raise HTTPException(400, msg)
        if "not yet implemented" in msg.lower():
            raise HTTPException(
                503,
                f"{c.name} contract not yet implemented. "
                f"Set llm.mock=true in config.yaml to test stubs, "
                f"or wait for the LLM call to be wired up.",
            )
        raise HTTPException(502, msg)
    return DraftOut(
        contract=c.name,
        implemented=c.implemented,
        output=out_model.model_dump(),
        input=body.input,
    )
