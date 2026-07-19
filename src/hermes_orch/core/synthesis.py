"""L3 state.md synthesis (Phase 2 of 3-tier memory).

See docs/design/3-tier-memory.md for the full design.

This module provides:
- `StateGenerator` — calls the LLM with the project's facts.md (L2) and
  asks for a structured state.md (L3) synthesis, then writes it via
  MemoryWriter (which handles the state_archive/ side effect).
- `get_state_generator()` — singleton accessor.

Triggered by:
1. Supervisor after `iteration_completed` (per spec) — auto regen
2. `POST /api/projects/{id}/memory/state/regenerate` — manual regen

NOT auto-triggered after every task.completed because:
- LLM cost (~500 tokens per regen) is non-trivial at scale
- L2 (facts.md) is the authoritative cite-able layer; L3 is the
  high-level "what's the state right now" view, most useful at
  decision points (iter review, replan, resume)
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

log = logging.getLogger("hermes_orch.core.synthesis")

# Size caps (bytes). Kept in sync with the design doc.
STATE_MAX_BYTES = 2048           # generated state.md hard cap
STATE_INJECT_MAX_BYTES = 2048    # injected into task prompts

# LLM call params
LLM_MAX_TOKENS = 1024            # output cap; we want under 2KB markdown
LLM_TEMPERATURE = 0.0            # deterministic


class StateGenerator:
    """Generates state.md from facts.md via an LLM call.

    The LLM is the project's configured one (from config.yaml `llm:`
    section). Uses OpenAI-compatible chat completions endpoint. Strips
    any markdown ``` fences from the response (MiniMax M3 sometimes
    wraps the JSON-ish output in code fences).
    """

    def __init__(self, llm_config: dict[str, Any]):
        self.base_url = (
            (llm_config.get("base_url") or "https://api.minimax.io/v1")
            .rstrip("/")
        )
        self.api_key = llm_config.get("api_key", "")
        self.model = llm_config.get("model", "MiniMax-M3")
        self.timeout = float(llm_config.get("timeout_seconds", 60))
        self.mock = bool(llm_config.get("mock", False))

    async def regenerate_state_async(
        self,
        *,
        project_id: str,
        project_meta: dict[str, Any],
        facts_text: str,
        memory_writer: Any,  # MemoryWriter (avoid circular import)
        trigger: str = "iteration_completed",
    ) -> bool:
        """LLM-synthesize state.md from facts.md. Returns True on success.

        Best-effort: returns False on any failure (no api key, no
        facts, LLM call failure, write failure). Logs the reason.
        """
        if self.mock or not self.api_key:
            log.info(
                f"L3 regen skipped (no api_key or mock=True) for {project_id}"
            )
            return False
        if not facts_text or len(facts_text.strip()) < 50:
            log.info(
                f"L3 regen skipped (facts too short, {len(facts_text)} chars) "
                f"for {project_id}"
            )
            return False
        prompt = self._build_prompt(project_meta, facts_text)
        try:
            text = await self._call_llm(prompt)
        except Exception as e:
            log.warning(f"L3 LLM call failed for {project_id}: {e}")
            return False
        if not text:
            return False
        # Enforce size cap (hard truncate; LLM was told 2KB but be safe)
        encoded = text.encode("utf-8")
        if len(encoded) > STATE_MAX_BYTES:
            text = encoded[:STATE_MAX_BYTES].decode("utf-8", errors="replace")
            text += "\n\n[…truncated at 2KB…]"
        ok = memory_writer.write_state(project_id, text)
        if ok:
            log.info(
                f"L3 state regenerated for {project_id} "
                f"({len(text)} bytes, trigger={trigger})"
            )
        return ok

    def _build_prompt(self, project_meta: dict[str, Any], facts_text: str) -> str:
        name = project_meta.get("name") or project_meta.get("id") or "?"
        state = project_meta.get("state", "?")
        cur_iter = project_meta.get("current_iteration", 0)
        max_iter = project_meta.get("max_iterations", 0)
        # Facts cap 8KB (matches design doc). If facts are huge, the
        # tail is more useful (recent task results > old goal text).
        facts_input = facts_text[-8192:]
        return (
            "You are a memory consolidator for a multi-agent project.\n"
            "Given the project's facts.md (L2 of 3-tier memory), produce "
            "a synthesized state.md (L3) for fast human + LLM orientation.\n"
            "Strict cap: 2 KB.\n\n"
            "# facts.md (input)\n"
            f"{facts_input}\n\n"
            "# Project metadata\n"
            f"- name: {name}\n"
            f"- state: {state}\n"
            f"- iter: {cur_iter}/{max_iter}\n\n"
            "# Output schema (strict, follow exactly)\n"
            "```markdown\n"
            f"# Project State: {name}\n\n"
            "> Last regenerated: now by L3 synthesis.\n\n"
            "## Current Status\n"
            f"- State: {state}\n"
            f"- Iter: {cur_iter}/{max_iter}\n\n"
            "## Goal\n"
            "- (one line, copy from facts.md ## Goal)\n\n"
            "## Open Questions\n"
            "- (only include if facts mention data gaps, failures, "
            "or replan hints; otherwise write '(none)')\n\n"
            "## Key Findings (synthesized)\n"
            "- (top 1-3 most important findings, each cited as "
            "`[cite:L2-section]` like `[cite:Task Results]`)\n\n"
            "## Next Steps\n"
            "- (1-3 actionable next steps, or 'Project complete' if all "
            "tasks done and Decision = PASS)\n"
            "```\n\n"
            "Output ONLY the markdown. No preamble, no explanation. "
            "Stay under 2 KB."
        )

    async def _call_llm(self, prompt: str) -> str | None:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a memory consolidator. Output only the "
                        "requested Markdown. No commentary, no preamble. "
                        "Stay under 2 KB."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": LLM_TEMPERATURE,
            "max_tokens": LLM_MAX_TOKENS,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        # OpenAI-compatible shape: choices[0].message.content
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            log.warning(f"L3 LLM response shape unexpected: {e}; data keys={list(data.keys())}")
            return None
        if not isinstance(text, str):
            log.warning(f"L3 LLM response content not str: {type(text)}")
            return None
        # Strip markdown ``` fences (MiniMax M3 sometimes wraps the
        # output in code fences, same trick as the project planner).
        # Also strip <think>...</think> reasoning blocks -- MiniMax M3
        # leaks its chain-of-thought into the visible output. We want
        # the synthesized state.md to be the clean synthesis, not the
        # model's reasoning about the synthesis.
        #
        # LLM fence formats observed in the wild:
        #   1. ```markdown\n# Project State...\n```
        #   2. ```\n# Project State...\n```
        #   3. ```\nmarkdown\n# Project State...\n```  <-- broken!
        # We try a strict fence extraction first, then fall back to
        # line-by-line strip. The "broken" case 3 is why we ALSO
        # strip a leading "markdown\n" / "md\n" line as a defensive
        # fallback.
        text = text.strip()
        text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
        # Strict: extract content between matching ``` fences
        m = re.match(
            r"^```[a-zA-Z]*\s*\n(.*?)\n```\s*$", text, flags=re.DOTALL
        )
        if m:
            text = m.group(1)
        else:
            # Loose: strip leading / trailing fences line by line
            text = re.sub(r"^```(?:markdown|md)?\s*\n", "", text)
            text = re.sub(r"\n```\s*$", "", text)
            # Defensive: broken-fence case (``` on one line, language
            # hint on next). Without this, "markdown\n# Project State"
            # survives and pollutes state.md.
            text = re.sub(r"^(?:markdown|md)\s*\n", "", text, flags=re.MULTILINE)
        return text.strip() or None


# ===== Singleton =====

_gen: StateGenerator | None = None


def get_state_generator() -> StateGenerator:
    """Get the process-wide StateGenerator, lazily initialized from config."""
    global _gen
    if _gen is None:
        from hermes_orch.config import load_config
        cfg = load_config()
        llm_cfg = (cfg.get("llm") or {})
        _gen = StateGenerator(llm_cfg)
    return _gen
