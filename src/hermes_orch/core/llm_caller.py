"""Unified LLM call helper (Object Layer Agent contract foundation, 2026-07-26).

Replaces the ad-hoc httpx + parse-logic that was duplicated across
core/planner.py, api/workflows.py (_call_llm_for_workflow_synthesis),
and api/schedules.py (_call_llm_for_skill_synthesis). All three
callers now go through this single helper, which:

  - Loads LLM config from app config
  - Handles MiniMax (OpenAI-compatible) chat completions
  - Optionally requests JSON output via `response_format={"type":"json_object"}`
  - Strips <think> traces and outer code-fence wrappers defensively
  - Returns a parsed dict (JSON mode) or raw text (free mode)
  - Records token usage to the token_usage table when a db is provided

The Agent contract (plan/route/judge/repair/audit) layer is a
*user* of this helper — each contract composes a prompt + JSON
schema, calls LLMCaller, validates against its Pydantic output
model. The helper itself doesn't know about contracts.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger("hermes_orch.llm_caller")


class LLMCaller:
    """Single source of truth for LLM chat-completion calls.

    Usage:
        caller = LLMCaller(cfg, db=db)  # db optional
        # Free-form text response
        text = await caller.call_text("summarize this", system="...")
        # JSON response, validated by the caller as a dict
        data = await caller.call_json("plan this", system="...")

    Failures raise LLMCallError. The caller also records token
    usage rows when a db is provided.
    """

    def __init__(self, cfg: dict[str, Any], db: Any = None):
        llm_cfg = cfg.get("llm", {}) or {}
        self.api_key = (llm_cfg.get("api_key") or "").strip()
        self.base_url = (
            llm_cfg.get("base_url") or "https://api.minimax.io/v1"
        ).rstrip("/")
        self.model = llm_cfg.get("model") or "MiniMax-M3"
        self.timeout = float(llm_cfg.get("timeout_seconds", 120))
        self.mock = bool(llm_cfg.get("mock", True)) or not self.api_key
        self.db = db

    @property
    def is_mock(self) -> bool:
        return self.mock

    async def call_text(
        self, prompt: str, system: str = "", *, max_tokens: int = 4000,
        temperature: float = 0.2, call_label: str = "llm.text",
        call_kind: str = "agent_task", project_id: str | None = None,
    ) -> str:
        """Free-form text completion. Returns the assistant message."""
        if self.mock:
            return self._mock_text(prompt, system)
        text = await self._call_chat(
            prompt, system, max_tokens=max_tokens, temperature=temperature,
            json_mode=False,
        )
        if self.db is not None:
            await self._record_usage(
                call_label=call_label, call_kind=call_kind,
                project_id=project_id, prompt=prompt, response=text,
            )
        return text

    async def call_json(
        self, prompt: str, system: str = "", *, max_tokens: int = 8000,
        temperature: float = 0.2, call_label: str = "llm.json",
        call_kind: str = "agent_task", project_id: str | None = None,
    ) -> dict:
        """JSON completion. Returns a parsed dict; raises on parse failure.

        Defense-in-depth: the LLM is asked for JSON via
        `response_format={"type":"json_object"}` AND we also strip
        <think> / code-fence wrappers + locate the first '{' before
        parsing. The MiniMax API has been known to leak reasoning
        traces or wrap output in fences despite the format hint.
        """
        if self.mock:
            return self._mock_json(prompt, system)
        # System prompt nudges toward clean JSON; caller is still
        # responsible for post-parse validation against its own
        # Pydantic model.
        sys = system or "You output JSON only. No prose, no fences."
        text = await self._call_chat(
            prompt, sys, max_tokens=max_tokens, temperature=temperature,
            json_mode=True,
        )
        data = self._parse_json_response(text)
        if self.db is not None:
            await self._record_usage(
                call_label=call_label, call_kind=call_kind,
                project_id=project_id, prompt=prompt, response=text,
            )
        return data

    # ===== internals =====

    async def _call_chat(
        self, prompt: str, system: str, *, max_tokens: int,
        temperature: float, json_mode: bool,
    ) -> str:
        if not self.api_key:
            raise LLMCallError(
                "LLM api_key not configured — set llm.api_key in config.yaml"
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload, headers=headers,
                )
        except httpx.HTTPError as e:
            raise LLMCallError(f"LLM transport error: {e}") from e
        if r.status_code != 200:
            raise LLMCallError(
                f"LLM returned HTTP {r.status_code}: {r.text[:300]}"
            )
        data = r.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMCallError(f"LLM response shape unexpected: {e}") from e
        if not isinstance(text, str) or not text.strip():
            raise LLMCallError(
                f"LLM returned empty content (type={type(text).__name__})"
            )
        return text

    @staticmethod
    def _parse_json_response(text: str) -> dict:
        # Strip <think>...</think> reasoning traces (some models leak
        # this even in JSON mode)
        text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
        text = text.strip()
        # Strip outer code fence (defensive — MiniMax occasionally
        # wraps the JSON in ```json ... ```)
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        # Find the first '{' (the LLM may add leading text)
        brace = text.find("{")
        if brace < 0:
            raise LLMCallError(
                f"LLM response contained no JSON object: {text[:200]!r}"
            )
        text = text[brace:]
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMCallError(
                f"LLM response was not valid JSON: {e}; text={text[:300]!r}"
            ) from e

    # ===== mock =====

    @staticmethod
    def _mock_text(prompt: str, system: str) -> str:
        # Minimal mock: echo a short stub. Real callers should
        # never reach this in production (cfg has api_key), but
        # we keep it so /api/contracts/{name}/draft is testable
        # without an LLM key.
        return f"[mock LLM response] system={system[:50]!r}..."

    @staticmethod
    def _mock_json(prompt: str, system: str) -> dict:
        # Default mock returns an empty object; each contract
        # should override this for richer mock data.
        return {"_mock": True, "note": "set llm.api_key for real output"}

    # ===== token usage =====

    async def _record_usage(
        self, *, call_label: str, call_kind: str, project_id: str | None,
        prompt: str, response: str,
    ) -> None:
        """Best-effort token usage recording. Failures are logged, not raised.

        Most LLM responses include a `usage` field; we try to read it
        but tolerate missing/incomplete data. For the mock path the
        caller doesn't invoke this.
        """
        try:
            # We don't have access to the raw response here (only the
            # parsed text). The real caller should pass usage via
            # _call_chat_with_usage; for now we estimate.
            prompt_tokens = len(prompt) // 4
            completion_tokens = len(response) // 4
            import secrets as _secrets
            from hermes_orch.utils import now_iso as _now_iso
            row_id = "tu-" + _secrets.token_hex(8)
            await self.db.insert(
                "token_usage",
                {
                    "id": row_id,
                    "agent_id": None,
                    "profile_id": None,
                    "project_id": project_id,
                    "task_id": None,
                    "role": None,
                    "model": self.model,
                    "base_url": self.base_url,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "call_kind": call_kind,
                    "call_label": call_label,
                },
            )
        except Exception as e:  # never let usage-logging break a draft call
            log.debug("token_usage insert failed: %s", e)


class LLMCallError(RuntimeError):
    """Raised by LLMCaller when the LLM call or response parsing fails."""
