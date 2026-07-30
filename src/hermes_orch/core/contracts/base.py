# coding: utf-8
"""Contract base class.

Each Agent contract (plan / route / judge / repair / audit) is a
subclass of `Contract` that knows:
  - Its name + description (for the registry)
  - Input model (Pydantic) — what the caller passes
  - Output model (Pydantic) — what the LLM is asked to produce
  - Prompt template — system + user prompts built from the input
  - Whether it has a real LLM-backed implementation yet
  - A `draft(input)` method that returns the validated output

The LLM call itself is delegated to core.llm_caller.LLMCaller,
which handles the HTTP call, JSON parsing, and token usage
recording. Contracts own the prompt + schema; the caller owns
the transport.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from hermes_orch.core.llm_caller import LLMCaller, LLMCallError


class ContractError(RuntimeError):
    """Raised when a contract can't be invoked (bad name, bad input,
    LLM failure, or output doesn't match the schema)."""


class Contract:
    """Base class for planning-time agent contracts.

    Subclasses set:
      - name: short identifier (matches the key in CONTRACTS)
      - description: human-readable summary
      - input_model: Pydantic model class for the caller-supplied input
      - output_model: Pydantic model class for the LLM-produced output
      - system_prompt: fixed instructions for the LLM
      - build_user_prompt: callable input -> str (the user message)
      - implemented: True if a real LLM call backs this contract;
        False for stubs that return a placeholder.

    The default `draft()` builds the prompt, calls the LLM, parses
    the JSON, and validates against `output_model`. Override
    `draft()` only if you need non-JSON output (e.g. for free-form
    text suggestions).

    Note: we don't use PEP 695 generic class syntax (`class
    Contract[In, Out]:`) because we still target Python 3.11;
    the input/output types are concrete on each subclass anyway.
    """

    name: str = ""
    description: str = ""
    implemented: bool = False

    input_model: type = BaseModel
    output_model: type = BaseModel

    system_prompt: str = "You output JSON only. No prose, no fences."

    def build_user_prompt(self, validated_input: BaseModel) -> str:
        """Build the user message from the validated input.

        Default: JSON-dump the input. Override to add structure
        (e.g. embed available skills as a separate block, like
        plan does).
        """
        return json.dumps(validated_input.model_dump(), ensure_ascii=False, indent=2)

    async def draft(
        self, raw_input: dict[str, Any] | BaseModel, caller: LLMCaller,
        *, project_id: str | None = None,
    ) -> BaseModel:
        """Run the contract end-to-end: validate input, call LLM,
        validate output. Returns the validated output_model instance.

        `raw_input` is either a dict (will be validated against
        input_model) or an already-validated input_model instance.
        """
        # 1. Validate input
        if isinstance(raw_input, BaseModel):
            validated_in = raw_input
        else:
            try:
                validated_in = self.input_model.model_validate(raw_input)
            except ValidationError as e:
                raise ContractError(
                    f"{self.name} input validation failed: {e}"
                ) from e

        if not caller.is_mock and not self.implemented:
            raise ContractError(
                f"{self.name} contract is not yet implemented "
                f"(use mock mode or wire up the LLM call)"
            )

        # 2. Build prompt + call LLM
        user_prompt = self.build_user_prompt(validated_in)
        data = await caller.call_json(
            user_prompt, system=self.system_prompt,
            call_label=f"contract.{self.name}",
            call_kind="agent_task", project_id=project_id,
        )

        # 3. Validate output
        try:
            return self.output_model.model_validate(data)
        except ValidationError as e:
            raise ContractError(
                f"{self.name} output validation failed: {e}; "
                f"raw={json.dumps(data, ensure_ascii=False)[:500]!r}"
            ) from e
