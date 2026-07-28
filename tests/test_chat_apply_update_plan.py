"""Tests for POST /api/projects/{id}/chat/apply with update_plan
suggestion type (2026-07-28, chatbox-as-plan-editor Phase 0).

Contract (docs/chatbox-plan-editor.md §7.2):
  - Only suggestion type allowed is `update_plan` (create_task /
    run / replan were removed in 2026-07-28).
  - Body: {type: "update_plan", plan: <ProjectPlan>, if_match: "<uat>"}
  - On success: returns {applied, type, project_id, updated_at, step_count}
  - On stale if_match: 409 with current_plan in body
  - On invalid plan shape: 422
  - On missing 'plan' field: 400
  - On unknown suggestion type: 400
  - On unknown project: 404
  - Audit log: actor=operator:chat, event=project.plan.updated
"""
from __future__ import annotations

import json
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
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
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


def _create_test_project() -> str:
    name = f"chat-apply-test-{uuid.uuid4().hex[:8]}"
    s, body = _http("POST", "/api/projects/", {"name": name})
    if s == 201 and isinstance(body, dict) and "id" in body:
        return body["id"]
    if isinstance(body, dict) and "id" in body:
        return body["id"]
    pytest.fail(f"create project failed: {s} {body}")


def _delete_project(project_id: str) -> None:
    try:
        _http("DELETE", f"/api/projects/{project_id}")
    except Exception:
        pass


def _sample_plan(name: str = "chatbox-test") -> dict:
    return {
        "version": "1.0",
        "name": name,
        "description": "test plan from chatbox",
        "trigger": "manual",
        "variables": [],
        "steps": [
            {
                "name": "fetch",
                "agent_role": "super",
                "action": "fetch_data",
                "skill": "",
                "tool": "",
                "required_capability": "",
                "depends_on": [],
                "params_template": {"url": "https://example.com"},
                "output_path": "",
            },
            {
                "name": "summarize",
                "agent_role": "super",
                "action": "summarize",
                "depends_on": ["fetch"],
                "params_template": {},
                "output_path": "",
            },
        ],
    }


# ===== Reject removed types (create_task, run, replan) =====


def test_apply_rejects_create_task_suggestion():
    """create_task is no longer a valid suggestion type (2026-07-28)."""
    pid = _create_test_project()
    try:
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "create_task", "name": "x", "action": "y"},
        })
        assert s == 400
        assert "create_task" in str(body) or "update_plan" in str(body)
    finally:
        _delete_project(pid)


def test_apply_rejects_run_suggestion():
    """run is no longer a valid suggestion type (2026-07-28).
    Dispatch is human-only via the Run button on the dashboard."""
    pid = _create_test_project()
    try:
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "run", "note": "go"},
        })
        assert s == 400
    finally:
        _delete_project(pid)


def test_apply_rejects_replan_suggestion():
    """replan is no longer a valid suggestion type (2026-07-28)."""
    pid = _create_test_project()
    try:
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "replan", "goal": "new goal"},
        })
        assert s == 400
    finally:
        _delete_project(pid)


# ===== update_plan happy path =====


def test_apply_update_plan_creates_plan():
    """update_plan on a project with no plan yet should succeed."""
    pid = _create_test_project()
    try:
        plan = _sample_plan("first-via-chat")
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": plan},
        })
        assert s == 200, f"expected 200 got {s}: {body}"
        assert body["applied"] is True
        assert body["type"] == "update_plan"
        assert body["project_id"] == pid
        assert body["step_count"] == 2
        assert body["updated_at"] is not None
        # Verify the plan was actually written
        s2, body2 = _http("GET", f"/api/projects/{pid}/plan")
        assert s2 == 200
        assert body2["plan"]["name"] == "first-via-chat"
    finally:
        _delete_project(pid)


def test_apply_update_plan_with_matching_if_match_succeeds():
    """If-Match echoing the current updated_at allows the write."""
    pid = _create_test_project()
    try:
        plan1 = _sample_plan("v1")
        s, body1 = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": plan1},
        })
        assert s == 200
        uat = body1["updated_at"]
        plan2 = _sample_plan("v2")
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {
                "type": "update_plan",
                "plan": plan2,
                "if_match": uat,
            },
        })
        assert s == 200, f"expected 200 got {s}: {body}"
        assert body["step_count"] == 2
    finally:
        _delete_project(pid)


def test_apply_update_plan_with_stale_if_match_returns_409():
    """Stale If-Match via chat apply returns 409 with current_plan in body.
    The chatbox frontend should catch this and offer a 3-way merge UI."""
    pid = _create_test_project()
    try:
        # Establish a plan
        plan1 = _sample_plan("v1")
        s, _ = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": plan1},
        })
        assert s == 200
        # Another update (changes updated_at)
        plan2 = _sample_plan("v2")
        s, _ = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": plan2},
        })
        assert s == 200
        # Now apply with a STALE if_match
        plan3 = _sample_plan("v3")
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {
                "type": "update_plan",
                "plan": plan3,
                "if_match": "2020-01-01T00:00:00+00:00",
            },
        })
        assert s == 409
        detail = body.get("detail") if isinstance(body, dict) else None
        assert detail is not None
        assert "modified" in detail.get("error", "").lower()
        assert detail.get("current_plan") is not None
        assert detail["current_plan"]["name"] == "v2"
    finally:
        _delete_project(pid)


# ===== update_plan error cases =====


def test_apply_update_plan_missing_plan_field_returns_400():
    pid = _create_test_project()
    try:
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan"},
        })
        assert s == 400
        assert "plan" in str(body).lower()
    finally:
        _delete_project(pid)


def test_apply_update_plan_invalid_plan_shape_returns_422():
    """Pydantic validation fails on non-kebab step name."""
    pid = _create_test_project()
    try:
        bad_plan = {
            "version": "1.0",
            "name": "ok-name",
            "steps": [
                {"name": "Not Kebab", "agent_role": "super"},  # invalid
            ],
        }
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": bad_plan},
        })
        assert s == 422
        assert "kebab" in str(body).lower() or "pattern" in str(body).lower()
    finally:
        _delete_project(pid)


def test_apply_unknown_suggestion_type_returns_400():
    pid = _create_test_project()
    try:
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "totally_made_up"},
        })
        assert s == 400
        assert "update_plan" in str(body)
    finally:
        _delete_project(pid)


def test_apply_unknown_project_returns_404():
    s, body = _http("POST", "/api/projects/does-not-exist/chat/apply", {
        "suggestion": {"type": "update_plan", "plan": _sample_plan()},
    })
    assert s == 404


def test_apply_update_plan_if_match_must_be_string():
    pid = _create_test_project()
    try:
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {
                "type": "update_plan",
                "plan": _sample_plan(),
                "if_match": 12345,  # not a string
            },
        })
        assert s == 400
        assert "if_match" in str(body).lower() or "string" in str(body).lower()
    finally:
        _delete_project(pid)


# ===== Audit log =====


def test_apply_update_plan_writes_audit_with_chat_actor():
    """Audit log should show actor=operator:chat (not operator)."""
    pid = _create_test_project()
    try:
        s, _ = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": _sample_plan("audit-test")},
        })
        assert s == 200
        conn = sqlite3.connect(str(DB_PATH))
        try:
            rows = conn.execute(
                "SELECT actor, event_type FROM audit_log "
                "WHERE project_id = ? AND event_type = 'project.plan.updated' "
                "ORDER BY created_at DESC LIMIT 1",
                (pid,),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        actor, event_type = rows[0]
        assert actor == "operator:chat", f"expected operator:chat, got {actor!r}"
        assert event_type == "project.plan.updated"
    finally:
        _delete_project(pid)
