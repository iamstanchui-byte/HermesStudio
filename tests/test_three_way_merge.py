"""Tests for the 3-way merge resolution flow (Phase 2, 2026-07-29).

The chatbox uses an optimistic lock on the plan (If-Match
header). When a user tries to apply a plan whose if_match is
stale, the server returns 409 with the server's current plan
in the body. The chatbox UI then shows a 3-way merge box with
two options: "Use server's plan" (discard the user's draft) or
"Force my draft" (re-apply with no if_match to bypass the lock).

This test verifies the SERVER-SIDE contract: 409 response shape,
the force-apply path bypassing the lock, and that the resolved
plan is persisted.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid

import pytest

BASE = "http://127.0.0.1:8765"


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | str | None]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
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


def _create_test_project() -> str:
    name = f"merge-test-{uuid.uuid4().hex[:8]}"
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


def _plan_v1(pid: str) -> dict:
    return {
        "version": "1.0",
        "name": "v1",
        "description": "first version",
        "trigger": "manual",
        "variables": [],
        "steps": [
            {"name": "step-a", "agent_role": "super", "depends_on": []},
        ],
    }


def _plan_v2(pid: str) -> dict:
    return {
        "version": "1.0",
        "name": "v2",
        "description": "second version",
        "trigger": "manual",
        "variables": [],
        "steps": [
            {"name": "step-b", "agent_role": "super", "depends_on": []},
            {"name": "step-c", "agent_role": "super", "depends_on": ["step-b"]},
        ],
    }


def _plan_v3(pid: str) -> dict:
    return {
        "version": "1.0",
        "name": "v3",
        "description": "force-applied version",
        "trigger": "manual",
        "variables": [],
        "steps": [
            {"name": "step-d", "agent_role": "super", "depends_on": []},
        ],
    }


# ===== 409 response shape =====


def test_apply_with_stale_if_match_returns_409_with_current_plan():
    """Server returns 409 + current_plan in body so the chatbox
    can show the merge UI."""
    pid = _create_test_project()
    try:
        # Establish v1
        s, _ = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": _plan_v1(pid)},
        })
        assert s == 200
        # Update to v2 (changes updated_at)
        s, _ = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": _plan_v2(pid)},
        })
        assert s == 200
        # Try to apply v3 with STALE if_match
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {
                "type": "update_plan",
                "plan": _plan_v3(pid),
                "if_match": "2020-01-01T00:00:00+00:00",  # stale
            },
        })
        assert s == 409
        # 409 body has detail.current_plan
        detail = body.get("detail") if isinstance(body, dict) else None
        assert detail is not None
        assert "current_plan" in detail
        assert "your_if_match" in detail
        assert "current_updated_at" in detail
        # current_plan reflects v2 (the last successful write)
        assert detail["current_plan"]["name"] == "v2"
        assert len(detail["current_plan"]["steps"]) == 2
    finally:
        _delete_project(pid)


def test_409_includes_step_count_for_chatbox_summary():
    """The 409 current_plan includes steps[] so the chatbox can
    show a count without re-fetching."""
    pid = _create_test_project()
    try:
        s, _ = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": _plan_v1(pid)},
        })
        s, _ = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": _plan_v2(pid)},
        })
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {
                "type": "update_plan",
                "plan": _plan_v3(pid),
                "if_match": "2020-01-01T00:00:00+00:00",
            },
        })
        assert s == 409
        detail = body["detail"]
        assert isinstance(detail["current_plan"]["steps"], list)
        assert len(detail["current_plan"]["steps"]) == 2
    finally:
        _delete_project(pid)


# ===== Force-apply bypasses the lock =====


def test_force_apply_with_null_if_match_succeeds_after_conflict():
    """"Force my draft" — re-apply with if_match=null. The server
    treats null as "no prior state" and skips the lock check,
    effectively overwriting. (Security note: this is intended for
    the human-triggered 3-way merge 'Force my draft' button, not
    a general bypass.)"""
    pid = _create_test_project()
    try:
        # Establish v1
        s, _ = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": _plan_v1(pid)},
        })
        # Update to v2
        s, _ = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": _plan_v2(pid)},
        })
        # Force-apply v3 (skip lock)
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {
                "type": "update_plan",
                "plan": _plan_v3(pid),
                "if_match": None,
            },
        })
        assert s == 200, f"force apply failed: {s} {body}"
        # Verify v3 is now persisted
        s, body = _http("GET", f"/api/projects/{pid}/plan")
        assert body["plan"]["name"] == "v3"
        assert len(body["plan"]["steps"]) == 1
    finally:
        _delete_project(pid)


def test_force_apply_audit_uses_chat_actor():
    """The force-apply path also goes through apply_chat_suggestion
    so the audit log records actor=operator:chat (not operator)."""
    pid = _create_test_project()
    try:
        s, _ = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": _plan_v1(pid)},
        })
        # Force-apply v3 with null if_match
        s, _ = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {
                "type": "update_plan",
                "plan": _plan_v3(pid),
                "if_match": None,
            },
        })
        # Check audit log
        import sqlite3
        from pathlib import Path
        db_path = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT actor, event_type FROM audit_log "
                "WHERE project_id = ? AND event_type = 'project.plan.updated' "
                "ORDER BY created_at DESC",
                (pid,),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) >= 2  # at least v1 and v3
        for actor, _event in rows:
            assert actor == "operator:chat", f"expected operator:chat, got {actor!r}"
    finally:
        _delete_project(pid)


# ===== End-to-end merge flow =====


def test_e2e_merge_flow_keep_then_force():
    """Full merge flow:
    1. Apply v1
    2. (External) apply v2 — simulates someone else editing
    3. Try to apply v3 with stale if_match → 409 with v2 in body
    4. Force-apply v3 (with if_match=null) → succeeds
    5. Verify v3 is the current plan
    """
    pid = _create_test_project()
    try:
        # Step 1: apply v1
        s, _ = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": _plan_v1(pid)},
        })
        assert s == 200
        # Step 2: external edit (someone else changes the plan)
        s, _ = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": _plan_v2(pid)},
        })
        assert s == 200
        # Step 3: chat tries to apply v3 with stale if_match
        # (the LLM-side if_match would be the v1 updated_at, but for
        # the test we use a definitely-stale value)
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {
                "type": "update_plan",
                "plan": _plan_v3(pid),
                "if_match": "2020-01-01T00:00:00+00:00",  # definitely stale
            },
        })
        assert s == 409
        # The current_plan in the 409 is v2
        assert body["detail"]["current_plan"]["name"] == "v2"
        # Step 4: user chooses "Force my draft" — re-apply with null
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {
                "type": "update_plan",
                "plan": _plan_v3(pid),
                "if_match": None,
            },
        })
        assert s == 200
        # Step 5: verify v3 is now the current plan
        s, body = _http("GET", f"/api/projects/{pid}/plan")
        assert body["plan"]["name"] == "v3"
    finally:
        _delete_project(pid)
