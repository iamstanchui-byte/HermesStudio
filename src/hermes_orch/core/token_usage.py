# coding: utf-8
"""Token usage helper — write one row per LLM call.

Called from:
- `core/planner.py:Planner._call_llm()` after each chat-completion
  (call_kind='planner')
- `core/synthesis.py` for LLM-synthesized state.md / recent.md
  (call_kind='synthesis')
- `api/agents.py:heartbeat()` for wrapper-reported per-task usage
  (call_kind='agent_task')

If the call is missing the `usage` block (some providers don't
return it), pass `prompt_tokens=completion_tokens=total_tokens=0`
and we'll still record the row so the call shows up in the
"calls" count even without token breakdown.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def extract_cache_read_tokens(usage: Any) -> int:
    """Best-effort extract of `cache_read_tokens` from an LLM `usage`
    dict. Handles both Anthropic and OpenAI-compatible shapes (v3.1.2).

    Anthropic:
        usage.cache_read_input_tokens          (int)
        # Optional: cache_creation_input_tokens (also a cache "hit"
        # in the sense that it doesn't cost full input price), but
        # we only count the read for now — creation is a one-time
        # cost that the LLM doesn't classify as a hit.

    OpenAI compatible:
        usage.prompt_tokens_details.cached_tokens   (int)
        # Some proxies (incl. MiniMax for the Anthropic-compatible
        # endpoint) expose the Anthropic shape directly. We check
        # the Anthropic key first so proxies that return both work.

    Returns 0 if the dict is missing, malformed, or the field is
    not present. Never raises — the caller wraps this in a try/except
    and we don't want usage-extraction to break a real LLM call.
    """
    if not isinstance(usage, dict):
        return 0
    # Anthropic direct: cache_read_input_tokens at top level
    val = usage.get("cache_read_input_tokens")
    if isinstance(val, (int, float)) and val > 0:
        return int(val)
    # OpenAI compatible: nested under prompt_tokens_details.cached_tokens
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        val = details.get("cached_tokens")
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    return 0


async def record_token_usage(
    db: Any,
    *,
    agent_id: str | None = None,
    profile_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    role: str | None = None,
    model: str,
    base_url: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cache_read_tokens: int = 0,  # v3.1.2: prompt cache hit tokens
    call_kind: str,
    call_label: str | None = None,
) -> None:
    """Insert a single token_usage row. Best-effort: if the DB
    write fails, log but don't raise (token tracking should never
    break a real LLM call or task dispatch).

    v3.1.2: `cache_read_tokens` is the count of prompt tokens served
    from the LLM's prompt cache (Anthropic: cache_read_input_tokens,
    OpenAI compatible: prompt_tokens_details.cached_tokens). 0 when
    the provider doesn't report cache or the call wasn't a cache hit.
    Kept separate from prompt_tokens so the dashboard can show "new
    input" vs "cache hit" side by side.
    """
    try:
        await db.insert(
            "token_usage",
            {
                "id": str(uuid.uuid4()),
                "agent_id": agent_id,
                "profile_id": profile_id,
                "project_id": project_id,
                "task_id": task_id,
                "role": role,
                "model": model,
                "base_url": base_url,
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "total_tokens": int(total_tokens or 0),
                "cache_read_tokens": int(cache_read_tokens or 0),  # v3.1.2
                "call_kind": call_kind,
                "call_label": call_label,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("token_usage insert failed: %s", e)
