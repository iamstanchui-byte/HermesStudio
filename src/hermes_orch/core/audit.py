"""Audit log helper for tracking system events.

Per REVIEW.md §7 — history page shows what happened to projects / tasks / agents.

Usage:
    from hermes_orch.core.audit import audit_log

    await audit_log(
        db, "task.created",
        actor="operator",
        project_id=proj_id,
        task_id=task_id,
        payload={"action": "run_backtest"},
    )
"""
from __future__ import annotations

import json
from typing import Any

from hermes_orch.utils import now_iso as _now_iso


async def audit_log(
    db: Any,
    event_type: str,
    *,
    actor: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    payload: dict | str | None = None,
    created_at: str | None = None,
) -> None:
    """Write a row to the audit_log table.

    Args:
        db: Database instance with .insert() method
        event_type: dot-separated event name (e.g. 'task.created', 'agent.registered')
        actor: who triggered the event ('operator', 'agent:<id>', etc.)
        project_id, task_id, agent_id: optional foreign keys
        payload: dict (auto-serialized to JSON) or string
        created_at: override the auto-stamped timestamp (default: local
            ISO-8601 with offset). We always set this from Python (rather
            than relying on the SQLite DEFAULT CURRENT_TIMESTAMP, which is
            UTC-naive and rendered in the dashboard as if it were local).
    """
    if isinstance(payload, (dict, list)):
        # Defensive: convert Pydantic v2 models to plain dicts
        # before json.dumps. Recursive helper handles nested cases
        # (e.g. list[StorageRef] inside a dict). Pydantic models
        # expose .model_dump(); Pydantic v1 used .dict() — we use
        # model_dump with a try/except for v1 compat (defensive).
        def _normalize(obj):
            # Pydantic v2
            if hasattr(obj, "model_dump") and callable(obj.model_dump):
                try:
                    return obj.model_dump()
                except Exception:
                    pass
            # Pydantic v1 (older hermes-orch versions)
            if hasattr(obj, "dict") and callable(obj.dict):
                try:
                    return obj.dict()
                except Exception:
                    pass
            # Standard types
            if isinstance(obj, dict):
                return {k: _normalize(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_normalize(v) for v in obj]
            if isinstance(obj, set):
                return [_normalize(v) for v in sorted(obj, key=str)]
            return obj
        payload = json.dumps(_normalize(payload), default=str)
    data: dict[str, Any] = {"event_type": event_type}
    if actor is not None:
        data["actor"] = actor
    if project_id is not None:
        data["project_id"] = project_id
    if task_id is not None:
        data["task_id"] = task_id
    if agent_id is not None:
        data["agent_id"] = agent_id
    if payload is not None:
        data["payload"] = payload
    data["created_at"] = created_at if created_at is not None else _now_iso()
    await db.insert("audit_log", data)
    # Mirror to L1 (append-only JSONL trace) — best-effort. The audit_log
    # table is the source of truth; L1 is a derived view that powers
    # the project memory system (3-tier memory design, Phase 1).
    try:
        from hermes_orch.core.memory import get_memory_writer
        get_memory_writer().append_event_L1(
            event_type=event_type,
            actor=actor or "unknown",
            project_id=project_id,
            task_id=task_id,
            payload=payload if isinstance(payload, (dict, list)) else ({"raw": payload} if payload else {}),
        )
    except Exception as e:
        # Don't fail the audit if memory write fails.
        import logging
        logging.getLogger("hermes_orch.core.audit").warning(
            f"L1 trace write failed (event={event_type}): {e}"
        )

