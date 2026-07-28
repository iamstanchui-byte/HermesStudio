"""Tests for POST /api/tasks/{id}/promote-to-workflow (commit 2, 2026-07-27).

The new endpoint closes the ad-hoc → reusable loop: a task that
worked well can be lifted into the workflow catalog as a 1-step
template. This is the inverse of "apply workflow to project".

Covers:
  - Happy path: completed task → workflow with 1 step lifted
  - Workflow validator runs (kebab-case step name, allowed fields)
  - 404 for nonexistent task
  - 409 for duplicate workflow name
  - 400 for non-terminal task (still pending)
  - 422 for non-kebab-case name
  - The single_task_detail.html page has the Promote button
  - The single_task_detail.html page omits the Promote button for
    pending tasks (only terminal tasks can be promoted)
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

BASE = "http://127.0.0.1:8765"
DB_PATH = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | str | None]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw or b"null")
            except (json.JSONDecodeError, TypeError):
                return r.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except (json.JSONDecodeError, TypeError):
            return e.code, raw.decode("utf-8", errors="replace")


def _get_text(path: str) -> str:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
        return r.read().decode("utf-8", errors="replace")


def _create_terminal_single_task(name: str, status: str = "completed") -> str:
    """Create a single task and force its status to a terminal state by
    writing directly to SQLite. Bypassing /api/tasks/{id}/start +
    /result avoids a race with the supervisor picking up the task
    (which would set it to 'assigned' / 'running' before we can mark
    it complete)."""
    s, body = _http("POST", "/api/single-tasks", {
        "name": name, "agent_role": "super", "action": "do_step",
    })
    assert s == 201, f"create failed: {s} {body}"
    tid = body["id"]
    # Force terminal status via direct DB write
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "UPDATE tasks SET status = ?, ended_at = datetime('now', 'localtime') "
            "WHERE id = ?",
            (status, tid),
        )
        conn.commit()
    finally:
        conn.close()
    return tid


def test_promote_completed_task_creates_workflow():
    """Happy path: a completed single task gets lifted into a 1-step workflow."""
    marker = f"promote-happy-{uuid.uuid4().hex[:8]}"
    tid = _create_terminal_single_task(marker)
    wf_name = f"promote-happy-wf-{uuid.uuid4().hex[:8]}"
    s, body = _http("POST", f"/api/tasks/{tid}/promote-to-workflow", {
        "name": wf_name, "description": "happy path test",
    })
    assert s == 200, f"promote failed: {s} {body}"
    assert body["name"] == wf_name
    assert body["workflow_id"].startswith("wf-")
    assert body["redirect_url"] == f"/workflows/{body['workflow_id']}"
    # Fetch the workflow and verify the step is there
    s, wf = _http("GET", f"/api/workflows/{body['workflow_id']}")
    assert s == 200
    assert len(wf["step_template"]) == 1
    step = wf["step_template"][0]
    # Step name should be the kebab-case version of the task name
    assert step["name"] == marker  # marker is already kebab-case
    assert step["action"]  # has some action
    assert step["agent_role"] == "super"
    # Allowed workflow step fields only
    ALLOWED = {"name", "agent_role", "action", "depends_on",
               "params_template", "output_path", "skill", "feedback_to"}
    assert set(step.keys()).issubset(ALLOWED), \
        f"step has extra fields: {set(step.keys()) - ALLOWED}"


def test_promote_slugifies_step_name():
    """Task names with spaces or CJK should be slugified into the step
    name (workflow validator requires kebab-case step names)."""
    tid = _create_terminal_single_task("Summarize Today's News!")
    wf_name = f"slug-test-{uuid.uuid4().hex[:8]}"
    s, body = _http("POST", f"/api/tasks/{tid}/promote-to-workflow", {
        "name": wf_name,
    })
    assert s == 200, f"promote failed: {s} {body}"
    s, wf = _http("GET", f"/api/workflows/{body['workflow_id']}")
    step = wf["step_template"][0]
    # Step name should be kebab-case (no spaces, no uppercase)
    assert re.match(r"^[a-z0-9][a-z0-9-]*$", step["name"]), \
        f"step name not kebab-case: {step['name']!r}"


def test_promote_nonexistent_task_returns_404():
    s, body = _http("POST", "/api/tasks/t-does-not-exist/promote-to-workflow", {
        "name": f"any-name-{uuid.uuid4().hex[:8]}",
    })
    assert s == 404
    assert "not found" in str(body).lower()


def test_promote_duplicate_name_returns_409():
    """If the workflow name already exists, refuse (operator can PATCH
    the existing one)."""
    tid1 = _create_terminal_single_task(f"dup-test-1-{uuid.uuid4().hex[:6]}")
    wf_name = f"dup-wf-{uuid.uuid4().hex[:8]}"
    s, _ = _http("POST", f"/api/tasks/{tid1}/promote-to-workflow", {"name": wf_name, "action": "do_step"})
    assert s == 200
    # Second promote with same name
    tid2 = _create_terminal_single_task(f"dup-test-2-{uuid.uuid4().hex[:6]}")
    s, body = _http("POST", f"/api/tasks/{tid2}/promote-to-workflow", {"name": wf_name, "action": "do_step"})
    assert s == 409, f"expected 409, got {s} {body}"
    assert "already exists" in str(body).lower()


def test_promote_non_terminal_task_returns_400():
    """A pending or running task has unsettled action/params —
    refuse until it reaches a terminal state."""
    # Create a task (status = pending by default; supervisor may
    # race to pick it up but our test reads the status fast).
    s, body = _http("POST", "/api/single-tasks", {
        "name": f"pending-promote-{uuid.uuid4().hex[:6]}",
        "agent_role": "super",
    })
    assert s == 201
    tid = body["id"]
    # Force the supervisor to not see it — set its is_single_task=0
    # temporarily so the dispatch loop skips it. Actually simpler:
    # check if supervisor already grabbed it; if so, test still
    # valid because the response will be 400 either way (not in
    # terminal state).
    s, body = _http("POST", f"/api/tasks/{tid}/promote-to-workflow", {
        "name": f"pending-wf-{uuid.uuid4().hex[:8]}",
    })
    assert s == 400, f"expected 400 for non-terminal task, got {s} {body}"
    assert "terminal" in str(body).lower()


def test_promote_invalid_name_pattern_returns_422():
    """Workflow name must be kebab-case (Pydantic validation)."""
    tid = _create_terminal_single_task(f"invalid-name-{uuid.uuid4().hex[:6]}")
    s, body = _http("POST", f"/api/tasks/{tid}/promote-to-workflow", {
        "name": "Has Spaces And Uppercase",  # fails pattern
    })
    assert s == 422
    # Pydantic returns a list of validation errors
    assert isinstance(body, list) or (isinstance(body, dict) and "detail" in body)


def test_single_task_detail_renders_promote_button_for_completed():
    """The detail page should have the Promote button for completed tasks."""
    tid = _create_terminal_single_task(f"detail-btn-{uuid.uuid4().hex[:6]}")
    html = _get_text(f"/single-tasks/{tid}")
    assert "Promote to workflow" in html
    assert "openPromoteModal" in html
    assert "promote-modal" in html


def test_single_task_detail_hides_promote_button_for_pending():
    """Pending tasks have unsettled action/params — the button must NOT
    appear (would fail when clicked).

    To make this test reliable, we INSERT the pending task directly
    via SQLite (not through the API) so the supervisor never sees
    it (we set agent_role to '__no_such_role__' which never matches
    a real profile, so the supervisor's _assign_task would skip it).
    """
    import secrets
    from datetime import datetime
    tid = f"t-test-{secrets.token_hex(4)}"
    name = f"pending-detail-{uuid.uuid4().hex[:6]}"
    now = datetime.now().isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(id, project_id, name, status, agent_role, depends_on, "
            " on_parent_failure, action, params, retry_count, max_retries, "
            " timeout_seconds, feedback_to, archived, is_single_task) "
            "VALUES (?, '__single_tasks__', ?, 'pending', ?, '[]', "
            " 'skip', 'do_task', '{}', 0, 2, 1800, '[]', 0, 1)",
            (tid, name, "__no_such_role__"),
        )
        conn.commit()
    finally:
        conn.close()
    html = _get_text(f"/single-tasks/{tid}")
    # The Promote button's onclick handler is `openPromoteModal('t-...')`.
    # The modal itself (with the same description text) is ALWAYS
    # rendered and just hidden by default — so we check for the
    # button's onclick specifically, not the title text.
    assert f"openPromoteModal('{tid}'" not in html, \
        "Promote button should NOT appear for pending tasks"


def test_promote_failed_task_is_allowed():
    """Failed tasks have settled action/params too (the operator may
    want to lift 'what I tried' as a template to edit)."""
    tid = _create_terminal_single_task(
        f"failed-promote-{uuid.uuid4().hex[:6]}", status="failed",
    )
    wf_name = f"failed-wf-{uuid.uuid4().hex[:8]}"
    s, body = _http("POST", f"/api/tasks/{tid}/promote-to-workflow", {"name": wf_name, "action": "do_step"})
    assert s == 200, f"failed task should be promotable: {s} {body}"
