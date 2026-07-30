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
    call_kind: str,
    call_label: str | None = None,
) -> None:
    """Insert a single token_usage row. Best-effort: if the DB
    write fails, log but don't raise (token tracking should never
    break a real LLM call or task dispatch).
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
                "call_kind": call_kind,
                "call_label": call_label,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("token_usage insert failed: %s", e)
