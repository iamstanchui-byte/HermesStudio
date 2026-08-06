"""v3.14.0 (Phase 2): approval_runtime — supervisor-side helpers.

Implements §4.1, §4.4, §4.6, §4.7, §4.8 of docs/v3.14.0-workflow-human-approval.md.

This module contains the RUNTIME side of human approval. Phase 1
(`core.approval_validation`) covers SAVE-time validation; this module
covers the supervisor's tick-time behavior:

  1. `create_approval_request(...)` — when a human_approval step is
     ready, create the ApprovalRequest row + emit event. Do NOT
     dispatch an agent task.

  2. `apply_on_reject(...)` — when a user Rejects, transition the
     `tasks.status` per `on_reject` semantics (stop / skip / route).

  3. `sweep_expired_approvals()` — periodic check for pending requests
     past their `timeout_seconds`. Set status='expired' and apply
     `on_reject`. Called by the supervisor on every tick (NOT a
     separate cron / worker — lazy check).

  4. `auto_reject_pending_approvals(...)` — when a workflow is
     cancelled, reject all pending approvals with reason='workflow_cancelled'.

  5. `emit_event(...)` — generic event hook for `approval.*` events.
     Phase 2: write to audit log only. Future: publish to in-memory
     queue / Redis pub-sub for Slack / email integration.

Why a separate module: keeps `core/supervisor.py` from ballooning with
domain-specific logic, and matches the existing pattern of
`core/soul_dispatch.py`, `core/routing.py`, etc.

Cost estimate: ~200 LOC. Combined with Phase 1's approval_validation
(280 LOC) and Phase 3's inbox UI (deferred), the total backend
estimate is ~350-450 LOC (matches the v3.14.0 design doc §6).
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from typing import Any

log = logging.getLogger("hermes_orch.approval_runtime")


# Payload cap (per design doc §4.9). Match the existing orchestrator
# task output cap. The cap is on the SERIALIZED STRING length (the
# `payload` column is TEXT).
PAYLOAD_CAP_BYTES = 15 * 1024 * 1024  # 15MB

# ApprovalRequest state values
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

# tasks.status values that come out of apply_on_reject
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_SKIPPED = "skipped"

# on_reject valid values (also in approval_validation.VALID_ON_REJECT)
ON_REJECT_STOP = "stop"
ON_REJECT_SKIP = "skip"
ON_REJECT_ROUTE = "route"


def _now_iso() -> str:
    """Local-time ISO 8601, second precision. Matches the rest of the codebase."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _short_id(n: int = 6) -> str:
    """Short opaque ID like 'apr-a1b2c3'. Used for the approval_requests.id PK."""
    return secrets.token_hex(n // 2)[:n]


def _flatten_upstream_outputs(dep_tasks: list[dict]) -> dict[str, Any]:
    """Flatten upstream task outputs into a dotted-path dict.

    For each dep task, take its `output` JSON column and merge into
    a single dict keyed by `step_name` (the task's `name`). The
    render_summary_template function then resolves `{{step_name.field}}`
    by walking this dict.

    Example:
      Input tasks: [
          {"name": "generate-report", "output": '{"client_name": "ACME", "total": 1.2}'}
      ]
      Output: {"generate-report": {"client_name": "ACME", "total": 1.2}}
    """
    out: dict[str, Any] = {}
    for t in dep_tasks:
        name = t["name"] if "name" in t.keys() else ""
        if not name:
            continue
        raw = t["result"] if "result" in t.keys() else ""
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning(
                "approval_runtime: dep task %s has unparseable output, skipping",
                t["id"] if "id" in t.keys() else "?",
            )
            continue
        if isinstance(parsed, dict):
            out[name] = parsed
    return out


def build_summary(
    template: str,
    *,
    params: dict[str, Any],
    dep_tasks: list[dict],
) -> str:
    """Render a summary_template into a summary string.

    Imports `render_summary_template` lazily to keep this module
    self-contained on first import (it doesn't pull in the validation
    regexes that other modules may not need).

    The render context is:
      - `params` (this step's `tasks.params` after run_workflow substitution)
      - flattened upstream `dep_tasks` outputs (dotted path by step name)
    """
    from hermes_orch.core.approval_validation import render_summary_template
    context: dict[str, Any] = {**(params or {})}
    context.update(_flatten_upstream_outputs(dep_tasks))
    return render_summary_template(template, context)


# ---------------------------------------------------------------------------
# (1) create_approval_request — supervisor creates pending ApprovalRequest
# ---------------------------------------------------------------------------


async def create_approval_request(
    db: Any,
    *,
    task: dict[str, Any],
) -> dict[str, Any] | None:
    """Create an ApprovalRequest for a `human_approval` task.

    Called from the supervisor tick when:
      - task.action == 'human_approval'
      - all task.depends_on tasks are completed/skipped
      - no pending ApprovalRequest exists for this (workflow_id, step_name)

    Behavior (§4.4 + §4.6 of the design doc):
      - Generate apr-{short_id}
      - Read `params` from the task row (already substituted at
        run_workflow time)
      - Read upstream task outputs (flattened by step name)
      - Render the approval.summary_template against this context
      - Insert ApprovalRequest with status='pending'
      - Emit `approval.created` event (audit log only in Phase 2)
      - Return the inserted row (so the supervisor can log it);
        return None if the request is already created (idempotency).

    The task itself stays in 'pending' execution state — the
    supervisor does NOT dispatch it. When the user Approves / Rejects
    via the API (Phase 2's POST endpoints), the API handler calls
    `apply_on_reject` (or sets task.status='completed' for approve).
    """
    # Idempotency: if a pending approval already exists for this
    # (workflow_id, step_name), don't create a duplicate. The unique
    # invariant is `(workflow_id, step_name, status='pending')`.
    existing = await db.fetchone(
        "SELECT id FROM approval_requests "
        "WHERE workflow_id = ? AND step_name = ? AND status = ?",
        (task["project_id"], task["name"] or "", STATUS_PENDING),
    )
    if existing:
        return None

    # Load approval config from task.params._workflow_approval
    # (stored by run_workflow at workflow run time).
    try:
        params_obj = json.loads(task["params"] or "{}")
    except (json.JSONDecodeError, TypeError):
        params_obj = {}
    approval_cfg = (
        params_obj.get("_workflow_approval")
        if isinstance(params_obj, dict) else None
    ) or {}
    if not isinstance(approval_cfg, dict):
        approval_cfg = {}

    summary_template = approval_cfg.get("summary_template") or ""

    # Read upstream task outputs
    dep_task_ids = []
    try:
        deps = json.loads(task["depends_on"] or "[]")
    except (json.JSONDecodeError, TypeError):
        deps = []
    if isinstance(deps, list):
        dep_task_ids = [d for d in deps if isinstance(d, str)]
    dep_tasks: list[dict] = []
    if dep_task_ids:
        placeholders = ",".join("?" for _ in dep_task_ids)
        rows = await db.fetchall(
            f"SELECT id, name, result FROM tasks WHERE id IN ({placeholders})",
            tuple(dep_task_ids),
        )
        dep_tasks = list(rows)

    # Render summary
    summary = build_summary(
        summary_template, params=params_obj, dep_tasks=dep_tasks
    )

    apr_id = "apr-" + _short_id(6)
    now = _now_iso()
    await db.insert(
        "approval_requests",
        {
            "id": apr_id,
            "workflow_id": task["project_id"],
            "step_name": task["name"] or "",
            "status": STATUS_PENDING,
            "summary": summary,
            "payload": "",
            "created_at": now,
            "decided_at": None,
            "reason": "",
            "user_id": "",
        },
    )

    # Audit log (Phase 2: just audit_log; future: emit_event also pushes
    # to a real event bus for Slack / email integration).
    await _audit_log_event(
        db, "approval.created",
        workflow_id=task["project_id"],
        step_name=task["name"] or "",
        approval_id=apr_id,
        status=STATUS_PENDING,
        summary=summary,
    )

    return {
        "id": apr_id,
        "workflow_id": task["project_id"],
        "step_name": task["name"] or "",
        "status": STATUS_PENDING,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# (2) apply_on_reject — transition tasks.status per on_reject semantics
# ---------------------------------------------------------------------------


async def apply_on_reject(
    db: Any,
    *,
    workflow_id: str,
    step_name: str,
    on_reject: str,
    reason: str = "",
    user_id: str = "",
) -> str:
    """Transition the task to the appropriate state per `on_reject`.

    Returns the resulting `tasks.status` value.

    Behavior (§4.3 + §5.2 of the design doc):
      - 'stop'  → tasks.status = 'failed', failure_reason = 'rejected_by_human'.
                  The supervisor's normal cascade rule applies (downstream
                  steps see the failed dep and get auto-cancelled, per
                  the existing _find_ready_tasks unsatisfiable-dep
                  handling). The project will eventually be marked
                  'failed' by the existing _drive_project_tasks flow
                  when all tasks are terminal.
      - 'skip'  → tasks.status = 'skipped'. Downstream steps see
                  skipped as success-equivalent (§4.5) and become
                  ready.
      - 'route' → tasks.status = 'skipped'. Same downstream effect
                  as 'skip'; the route target step D is reached via
                  D.depends_on(B) (set at workflow save time, see
                  §4.5 wiring validation). The supervisor's normal
                  ready-task check picks it up on the next tick.

    Note: 'expired' is treated the same as 'rejected' for the
    apply_on_reject path (caller passes on_reject value; sweeper in
    (3) decides which to call).
    """
    # Read the task id by (workflow_id, step_name) — the active task
    # for this step is the one currently in pending state.
    task_row = await db.fetchone(
        "SELECT id, status, name FROM tasks "
        "WHERE project_id = ? AND name = ? AND status IN ('pending', 'running')",
        (workflow_id, step_name),
    )
    if not task_row:
        log.warning(
            "apply_on_reject: no pending task for workflow=%s step=%s",
            workflow_id, step_name,
        )
        return ""

    task_id = task_row["id"]
    now = _now_iso()

    if on_reject == ON_REJECT_STOP:
        await db.execute(
            "UPDATE tasks SET status = ?, failure_reason = ?, updated_at = ? "
            "WHERE id = ? AND status IN ('pending', 'running')",
            (TASK_FAILED, "rejected_by_human", now, task_id),
        )
        new_status = TASK_FAILED
    elif on_reject in (ON_REJECT_SKIP, ON_REJECT_ROUTE):
        # Both 'skip' and 'route' produce the same task.status = 'skipped'.
        # The difference is the WIRING (route target's depends_on),
        # which is set at workflow save time, not at apply time.
        await db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? "
            "WHERE id = ? AND status IN ('pending', 'running')",
            (TASK_SKIPPED, now, task_id),
        )
        new_status = TASK_SKIPPED
    else:
        log.error(
            "apply_on_reject: unknown on_reject=%r (workflow=%s step=%s); "
            "defaulting to 'stop'",
            on_reject, workflow_id, step_name,
        )
        await db.execute(
            "UPDATE tasks SET status = ?, failure_reason = ?, updated_at = ? "
            "WHERE id = ? AND status IN ('pending', 'running')",
            (TASK_FAILED, "rejected_by_human", now, task_id),
        )
        new_status = TASK_FAILED

    # Audit log
    await _audit_log_event(
        db, "approval.rejected" if on_reject != ON_REJECT_STOP
            else "approval.rejected",
        workflow_id=workflow_id,
        step_name=step_name,
        approval_id=None,
        status="rejected",
        on_reject=on_reject,
        reason=reason,
        user_id=user_id,
    )

    return new_status


async def apply_approve(
    db: Any,
    *,
    workflow_id: str,
    step_name: str,
    user_id: str = "",
) -> bool:
    """Mark a human_approval task as completed (approved).

    Returns True if the task was transitioned, False if no pending
    task was found (the caller may have hit a 409 race; surface to
    the API as 'already decided').

    Behavior (§4.3 + §5.2):
      - tasks.status = 'completed'
      - tasks.completion_reason = 'approved_by_human'
      - ApprovalRequest.status already 'approved' (set by API handler
        atomically with this function).
    """
    task_row = await db.fetchone(
        "SELECT id, status, name FROM tasks "
        "WHERE project_id = ? AND name = ? AND status = 'pending'",
        (workflow_id, step_name),
    )
    if not task_row:
        return False
    task_id = task_row["id"]
    now = _now_iso()
    await db.execute(
        "UPDATE tasks SET status = ?, completion_reason = ?, updated_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (TASK_COMPLETED, "approved_by_human", now, task_id),
    )
    await _audit_log_event(
        db, "approval.approved",
        workflow_id=workflow_id,
        step_name=step_name,
        approval_id=None,
        status="approved",
        user_id=user_id,
    )
    return True


# ---------------------------------------------------------------------------
# (3) sweep_expired_approvals — timeout sweeper (called by supervisor tick)
# ---------------------------------------------------------------------------


async def sweep_expired_approvals(db: Any) -> int:
    """Find pending approvals past their `timeout_seconds` and expire them.

    Returns the count of approvals that were expired by this sweep.
    Designed to be called by the supervisor on every tick (or every
    Nth tick) — does NOT introduce a separate cron / background worker.

    The timeout comes from `tasks.params._workflow_approval.timeout_seconds`.
    Default: 86400 (24h), applied if missing or zero.

    Per §4.6.1 of the design doc: the update is atomic
    (`WHERE status = 'pending'`) so a user Approve / Reject that
    races with the sweeper has exactly one winner. After expire, we
    call `apply_on_reject` with the same on_reject as a Reject would
    have used (sweeper does NOT have a separate on_expire path; v1
    treats expired == rejected).
    """
    # Load all pending approvals
    pending = await db.fetchall(
        "SELECT id, workflow_id, step_name, created_at FROM approval_requests "
        "WHERE status = ?",
        (STATUS_PENDING,),
    )
    if not pending:
        return 0

    # Load matching tasks to find each task's timeout_seconds config
    # (stored in tasks.params._workflow_approval.timeout_seconds).
    # One query, indexed by name.
    task_rows = await db.fetchall(
        "SELECT name, project_id, params FROM tasks "
        "WHERE project_id IN ({}) AND status = 'pending'".format(
            ",".join("?" for _ in {p["workflow_id"] for p in pending})
        ) if pending else "SELECT 1 WHERE 0",
        tuple({p["workflow_id"] for p in pending}),
    ) if pending else []

    # Build (workflow_id, step_name) -> timeout_seconds
    by_key: dict[tuple[str, str], int] = {}
    for t in task_rows:
        try:
            params_obj = json.loads(t["params"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(params_obj, dict):
            continue
        approval_cfg = params_obj.get("_workflow_approval")
        if not isinstance(approval_cfg, dict):
            continue
        ts = approval_cfg.get("timeout_seconds")
        if not isinstance(ts, int) or ts <= 0:
            ts = 86400  # default 24h
        by_key[(t["project_id"], t["name"] if "name" in t.keys() else "")] = ts

    now = _now_iso()
    expired_count = 0
    for apr in pending:
        timeout = by_key.get(
            (apr["workflow_id"], apr["step_name"] or ""), 86400
        )
        # Compare ISO strings lexicographically — works as long as
        # both are in the same format (YYYY-MM-DDTHH:MM:SS).
        # For precise comparison, parse to datetime.
        try:
            created_dt = datetime.strptime(apr["created_at"], "%Y-%m-%dT%H:%M:%S")
            elapsed = (datetime.now() - created_dt).total_seconds()
        except (TypeError, ValueError):
            continue
        if elapsed < timeout:
            continue
        # Atomic expire: only succeed if status is still 'pending'.
        affected = await db.execute(
            "UPDATE approval_requests SET status = ?, decided_at = ?, "
            "reason = ?, user_id = ? WHERE id = ? AND status = ?",
            (STATUS_EXPIRED, now, "timeout", "system", apr["id"], STATUS_PENDING),
        )
        # `db.execute` returns the rowcount. If the row was already
        # decided (race), affected is 0 — skip apply_on_reject.
        if not affected or affected == 0:
            continue
        # Apply on_reject semantics (treat expired as rejected).
        # Look up the task's approval config to get the on_reject value.
        task = await db.fetchone(
            "SELECT params FROM tasks WHERE project_id = ? AND name = ?",
            (apr["workflow_id"], apr["step_name"] or ""),
        )
        on_reject = ON_REJECT_STOP  # default
        if task:
            try:
                params_obj = json.loads(task["params"] or "{}")
            except (json.JSONDecodeError, TypeError):
                params_obj = {}
            if isinstance(params_obj, dict):
                approval_cfg = params_obj.get("_workflow_approval") or {}
                if isinstance(approval_cfg, dict):
                    on_reject = approval_cfg.get("on_reject") or ON_REJECT_STOP
        await apply_on_reject(
            db,
            workflow_id=apr["workflow_id"],
            step_name=apr["step_name"] or "",
            on_reject=on_reject,
            reason="timeout",
            user_id="system",
        )
        await _audit_log_event(
            db, "approval.expired",
            workflow_id=apr["workflow_id"],
            step_name=apr["step_name"] or "",
            approval_id=apr["id"],
            status=STATUS_EXPIRED,
            on_reject=on_reject,
            reason="timeout",
        )
        expired_count += 1

    return expired_count


# ---------------------------------------------------------------------------
# (4) auto_reject_pending_approvals — workflow cancel hook
# ---------------------------------------------------------------------------


async def auto_reject_pending_approvals(
    db: Any,
    *,
    workflow_id: str,
) -> int:
    """Reject all pending approvals for a workflow (cancel propagation).

    Called by the project cancel flow (§4.6.3 of the design doc):
      - Workflow enters 'stopping' state
      - Auto-reject all pending ApprovalRequests with
        reason='workflow_cancelled'
      - The corresponding human_approval tasks become 'skipped'
        (NOT 'failed' — the workflow is already in stopping state,
        so we don't escalate to a failure)

    Returns the count of approvals that were auto-rejected.
    """
    pending = await db.fetchall(
        "SELECT id, step_name FROM approval_requests "
        "WHERE workflow_id = ? AND status = ?",
        (workflow_id, STATUS_PENDING),
    )
    if not pending:
        return 0

    now = _now_iso()
    count = 0
    for apr in pending:
        affected = await db.execute(
            "UPDATE approval_requests SET status = ?, decided_at = ?, "
            "reason = ?, user_id = ? WHERE id = ? AND status = ?",
            (
                STATUS_REJECTED, now,
                "workflow_cancelled", "system",
                apr["id"], STATUS_PENDING,
            ),
        )
        if not affected or affected == 0:
            continue
        # For workflow cancel, we ALWAYS skip (not stop) — the workflow
        # is already being cancelled, no need to escalate to failure.
        await apply_on_reject(
            db,
            workflow_id=workflow_id,
            step_name=apr["step_name"] if "step_name" in apr.keys() else "",
            on_reject=ON_REJECT_SKIP,
            reason="workflow_cancelled",
            user_id="system",
        )
        await _audit_log_event(
            db, "approval.rejected",
            workflow_id=workflow_id,
            step_name=apr["step_name"] or "",
            approval_id=apr["id"],
            status=STATUS_REJECTED,
            on_reject=ON_REJECT_SKIP,
            reason="workflow_cancelled",
        )
        count += 1

    return count


# ---------------------------------------------------------------------------
# (5) emit_event — generic event hook (Phase 2: audit log only)
# ---------------------------------------------------------------------------


async def _audit_log_event(
    db: Any,
    event: str,
    *,
    workflow_id: str,
    step_name: str,
    approval_id: str | None = None,
    status: str = "",
    summary: str = "",
    on_reject: str = "",
    reason: str = "",
    user_id: str = "",
) -> None:
    """Write an `approval.*` event to the audit log.

    Phase 2: just audit_log. Future (per design doc §4.8): also push
    to an internal pub/sub (e.g. Redis) so Slack / email integrations
    can subscribe. The interface stays the same — `emit_event`
    consumers only need to know the event name + payload.

    This function is private (underscore prefix) because the public
    surface is `emit_event` (when we add it) — but for Phase 2 every
    call site passes through here directly, so we expose it as
    `_audit_log_event` for internal use.
    """
    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "step_name": step_name,
        "status": status,
    }
    if approval_id is not None:
        payload["approval_id"] = approval_id
    if summary:
        payload["summary"] = summary[:500]  # truncate for the audit log
    if on_reject:
        payload["on_reject"] = on_reject
    if reason:
        payload["reason"] = reason
    if user_id:
        payload["user_id"] = user_id
    # Lazy import to avoid hard dependency on the audit module
    # (the audit module imports db, etc., and we want to keep this
    # module testable in isolation).
    from hermes_orch.core.audit import audit_log
    try:
        await audit_log(
            db,
            event,
            actor="approval_runtime",
            project_id=workflow_id,
            payload=payload,
        )
    except Exception as e:
        # Audit log failures should not break the workflow.
        log.warning("audit_log for %s failed: %s", event, e)
