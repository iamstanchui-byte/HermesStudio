"""v3.14.0 (Phase 2): approval API endpoints.

Implements §4.6 + §5 of docs/v3.14.0-workflow-human-approval.md.

5 endpoints:

  1. POST /api/workflows/{workflow_id}/steps/{step_name}/approve
  2. POST /api/workflows/{workflow_id}/steps/{step_name}/reject
  3. GET  /api/workflows/{workflow_id}/approvals
  4. GET  /api/inbox/approvals?status=pending
  5. GET  /api/inbox/approvals/{approval_id}

The first 3 are scoped to a single workflow (the workflow id = the
project id in the `projects` table — same convention as the rest of
the code). The last 2 are cross-workflow, used by the dashboard's
inbox icon + badge count.

Auth: all endpoints require the dashboard session cookie (v3.4 auth).
The single-user v1 does not have per-approver allowlists; the
`user_id` column is captured from the session for audit only.

Why a separate module: keeps `api/workflows.py` (already 2500+ LOC)
clean, and matches the existing pattern of `api/agents.py`,
`api/projects.py`, etc. — one module per resource.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

log = logging.getLogger("hermes_orch.api.approvals")

router = APIRouter(prefix="/api", tags=["approvals"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _serialize_approval_row(row: dict) -> dict:
    """Convert an approval_requests DB row to a JSON-friendly dict.

    Truncates `summary` to a reasonable size for the list view (full
    payload comes via the detail endpoint). Keeps all other fields
    verbatim.
    """
    if not row:
        return row
    out = dict(row)
    # Cap summary in list view to avoid bloating the badge poll response
    summary = out.get("summary") or ""
    if len(summary) > 200:
        out["summary"] = summary[:200] + "…"
    # Truncate payload in list view too (detail endpoint returns full)
    if "payload" in out and out.get("payload") and len(out["payload"]) > 200:
        out["payload"] = out["payload"][:200] + "…"
    return out


def _get_current_user_id(request: Request) -> str:
    """Get the current user_id from the session cookie.

    Returns empty string if the user is unauthenticated (shouldn't
    happen in practice — all approval routes require auth via the
    global middleware). Empty string is also the default for system-
    generated events (sweeper, cancel auto-reject).
    """
    try:
        # The session middleware stores user_id on request.state.user.
        user = getattr(request.state, "user", None)
        if user and isinstance(user, dict):
            return user.get("id") or ""
        if user and hasattr(user, "id"):
            return getattr(user, "id", "") or ""
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# (1) POST /api/workflows/{workflow_id}/steps/{step_name}/approve
# ---------------------------------------------------------------------------


@router.post("/workflows/{workflow_id}/steps/{step_name}/approve")
async def approve_step(
    workflow_id: str,
    step_name: str,
    request: Request,
) -> dict:
    """Approve a pending human_approval step.

    Behavior (§5.2 of design doc):
      - Look up the ApprovalRequest by (workflow_id, step_name, status='pending').
      - If not found: 404 (no approval exists for this step).
      - If found but status is already 'approved': 200 + current state
        (idempotent — re-clicking Approve does not double-schedule).
      - If status is 'rejected' or 'expired': 409 Conflict.
      - If status is 'pending': atomic UPDATE (both approval status
        AND task status) and return 200.

    Returns the updated ApprovalRequest as JSON.
    """
    db = request.app.state.db
    user_id = _get_current_user_id(request)
    now = _now_iso()

    # Atomic UPDATE: only succeed if status is still 'pending'.
    # This handles the race with the sweeper / cancel auto-reject.
    apr = await db.fetchone(
        "SELECT id, status, workflow_id, step_name, summary, payload, "
        "created_at, decided_at, reason, user_id "
        "FROM approval_requests WHERE workflow_id = ? AND step_name = ? "
        "AND status = 'pending'",
        (workflow_id, step_name),
    )
    if not apr:
        # Either no approval at all, or already decided.
        any_apr = await db.fetchone(
            "SELECT id, status FROM approval_requests "
            "WHERE workflow_id = ? AND step_name = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (workflow_id, step_name),
        )
        if not any_apr:
            raise HTTPException(404, f"no approval for workflow={workflow_id} step={step_name}")
        # Already decided
        if any_apr["status"] == "approved":
            return _serialize_approval_row(any_apr)  # idempotent 200
        raise HTTPException(
            409, f"approval already {any_apr['status']} (workflow={workflow_id} step={step_name})"
        )

    # Update approval status
    affected = await db.execute(
        "UPDATE approval_requests SET status = 'approved', decided_at = ?, "
        "user_id = ? WHERE id = ? AND status = 'pending'",
        (now, user_id, apr["id"]),
    )
    if not affected or affected == 0:
        # Lost the race to sweeper / another click
        raise HTTPException(409, "approval was concurrently decided")

    # Transition task to completed
    from hermes_orch.core.approval_runtime import apply_approve
    ok = await apply_approve(
        db, workflow_id=workflow_id, step_name=step_name, user_id=user_id
    )
    if not ok:
        # No pending task found (already terminal, or never existed).
        # We've already moved the approval to 'approved' — log but don't
        # fail the API; the next tick will see the state and move on.
        log.warning(
            "approve: no pending task for workflow=%s step=%s "
            "(approval id=%s was already moved to 'approved')",
            workflow_id, step_name, apr["id"],
        )

    return _serialize_approval_row({**apr, "status": "approved", "decided_at": now, "user_id": user_id})


# ---------------------------------------------------------------------------
# (2) POST /api/workflows/{workflow_id}/steps/{step_name}/reject
# ---------------------------------------------------------------------------


@router.post("/workflows/{workflow_id}/steps/{step_name}/reject")
async def reject_step(
    workflow_id: str,
    step_name: str,
    request: Request,
    body: dict | None = None,
) -> dict:
    """Reject a pending human_approval step.

    Request body (optional): { "reason": "optional message" }

    Behavior (§5.2 of design doc):
      - Look up the ApprovalRequest (status='pending' first, else
        any recent one for the 409 case).
      - Atomic UPDATE both approval status and task status per
        `on_reject` semantics (apply_on_reject).
      - 404 / 409 same as approve.
    """
    db = request.app.state.db
    user_id = _get_current_user_id(request)
    now = _now_iso()
    reason = (body or {}).get("reason") or ""

    apr = await db.fetchone(
        "SELECT id, status, workflow_id, step_name, summary, payload, "
        "created_at, decided_at, reason, user_id "
        "FROM approval_requests WHERE workflow_id = ? AND step_name = ? "
        "AND status = 'pending'",
        (workflow_id, step_name),
    )
    if not apr:
        any_apr = await db.fetchone(
            "SELECT id, status FROM approval_requests "
            "WHERE workflow_id = ? AND step_name = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (workflow_id, step_name),
        )
        if not any_apr:
            raise HTTPException(404, f"no approval for workflow={workflow_id} step={step_name}")
        raise HTTPException(
            409, f"approval already {any_apr['status']} (workflow={workflow_id} step={step_name})"
        )

    # Look up the on_reject value from the task's approval config
    task = await db.fetchone(
        "SELECT params FROM tasks WHERE project_id = ? AND name = ?",
        (workflow_id, step_name),
    )
    on_reject = "stop"  # default
    if task:
        try:
            params_obj = json.loads(task.get("params") or "{}")
        except (json.JSONDecodeError, TypeError):
            params_obj = {}
        if isinstance(params_obj, dict):
            approval_cfg = params_obj.get("_workflow_approval") or {}
            if isinstance(approval_cfg, dict):
                on_reject = approval_cfg.get("on_reject") or "stop"

    # Atomic UPDATE approval status
    affected = await db.execute(
        "UPDATE approval_requests SET status = 'rejected', decided_at = ?, "
        "reason = ?, user_id = ? WHERE id = ? AND status = 'pending'",
        (now, reason, user_id, apr["id"]),
    )
    if not affected or affected == 0:
        raise HTTPException(409, "approval was concurrently decided")

    # Apply on_reject semantics
    from hermes_orch.core.approval_runtime import apply_on_reject
    new_task_status = await apply_on_reject(
        db,
        workflow_id=workflow_id,
        step_name=step_name,
        on_reject=on_reject,
        reason=reason,
        user_id=user_id,
    )

    return {
        **_serialize_approval_row({**apr, "status": "rejected", "decided_at": now,
                                    "reason": reason, "user_id": user_id}),
        "task_status": new_task_status,
        "on_reject": on_reject,
    }


# ---------------------------------------------------------------------------
# (3) GET /api/workflows/{workflow_id}/approvals
# ---------------------------------------------------------------------------


@router.get("/workflows/{workflow_id}/approvals")
async def list_workflow_approvals(
    workflow_id: str,
    request: Request,
    status: str | None = Query(None, description="Filter by status: pending / approved / rejected / expired"),
) -> dict:
    """List approval history for a single workflow.

    Used by the workflow detail page (audit trail of all approval
    decisions). For the cross-workflow inbox, see
    GET /api/inbox/approvals (next endpoint).

    Query params:
      - status: optional filter (default = all statuses)
    """
    db = request.app.state.db
    if status is not None and status not in ("pending", "approved", "rejected", "expired"):
        raise HTTPException(400, f"status must be one of pending/approved/rejected/expired (got {status!r})")
    if status:
        rows = await db.fetchall(
            "SELECT id, workflow_id, step_name, status, summary, "
            "created_at, decided_at, reason, user_id "
            "FROM approval_requests WHERE workflow_id = ? AND status = ? "
            "ORDER BY created_at DESC",
            (workflow_id, status),
        )
    else:
        rows = await db.fetchall(
            "SELECT id, workflow_id, step_name, status, summary, "
            "created_at, decided_at, reason, user_id "
            "FROM approval_requests WHERE workflow_id = ? "
            "ORDER BY created_at DESC",
            (workflow_id,),
        )
    return {
        "count": len(rows),
        "items": [_serialize_approval_row(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# (4) GET /api/inbox/approvals?status=pending
# ---------------------------------------------------------------------------


@router.get("/inbox/approvals")
async def list_inbox_approvals(
    request: Request,
    status: str = Query("pending", description="Filter: pending / approved / rejected / expired (default: pending)"),
) -> dict:
    """Cross-workflow inbox of approvals (for the dashboard's inbox icon).

    This is the endpoint polled every 30s by the inbox icon to update
    the badge count. The response is intentionally light: just the
    summary fields, no payload (detail endpoint returns the full
    payload).

    Query params:
      - status: default 'pending' (show pending approvals — drives
        the inbox badge). Other values supported for history view.
    """
    db = request.app.state.db
    if status not in ("pending", "approved", "rejected", "expired"):
        raise HTTPException(400, f"status must be one of pending/approved/rejected/expired (got {status!r})")
    rows = await db.fetchall(
        "SELECT id, workflow_id, step_name, status, summary, "
        "created_at, decided_at, reason, user_id "
        "FROM approval_requests WHERE status = ? "
        "ORDER BY created_at DESC",
        (status,),
    )
    return {
        "count": len(rows),
        "items": [_serialize_approval_row(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# (5) GET /api/inbox/approvals/{approval_id}
# ---------------------------------------------------------------------------


@router.get("/inbox/approvals/{approval_id}")
async def get_approval_detail(
    approval_id: str,
    request: Request,
) -> dict:
    """Get the full approval row (including the payload).

    Used when the user clicks on an inbox row to see the full
    context (email draft, config diff, etc.) before deciding.

    Returns 404 if the approval_id doesn't exist.
    """
    db = request.app.state.db
    apr = await db.fetchone(
        "SELECT id, workflow_id, step_name, status, summary, payload, "
        "created_at, decided_at, reason, user_id "
        "FROM approval_requests WHERE id = ?",
        (approval_id,),
    )
    if not apr:
        raise HTTPException(404, f"approval {approval_id} not found")
    # Full detail: do NOT truncate summary or payload
    return dict(apr)
