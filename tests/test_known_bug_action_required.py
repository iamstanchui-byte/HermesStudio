"""Regression test for the known bug where the LLM generated
plan steps with empty `action` (2026-07-29, project proj-7916a66e).

The user reported: "試了chatbox, 生成了plan 但只有name 沒有action".
The plan was successfully persisted because `action` was an
optional `str = ""` field. The fix:
  1. PlanStep.action is now REQUIRED (min_length=1) in plans.py
  2. Chat system prompt documents what to put in action and
     lists canonical examples
  3. Pydantic validator rejects whitespace-only or too-short
     actions as defense in depth

This test verifies the schema enforces the new contract.
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
    name = f"action-req-{uuid.uuid4().hex[:8]}"
    s, body = _http("POST", "/api/projects/", {"name": name, "action": "do_step"})
    if s in (200, 201) and isinstance(body, dict) and "id" in body:
        return body["id"]
    pytest.fail(f"create project failed: {s} {body}")


def _delete_project(project_id: str) -> None:
    try:
        _http("DELETE", f"/api/projects/{project_id}")
    except Exception:
        pass


# ===== Schema: action is REQUIRED =====


def test_put_plan_rejects_step_with_empty_action():
    """A step with action="" is rejected with 422."""
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "empty-action",
            "steps": [
                {"name": "x", "agent_role": "super", "action": ""},
            ],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 422, f"expected 422 for empty action, got {s}: {body}"
        # Error mentions action
        assert "action" in str(body).lower()
    finally:
        _delete_project(pid)


def test_put_plan_rejects_step_with_missing_action():
    """A step with no action field at all is rejected with 422."""
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "missing-action",
            "steps": [
                {"name": "x", "agent_role": "super"},  # NO action
            ],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 422, f"expected 422 for missing action, got {s}: {body}"
    finally:
        _delete_project(pid)


def test_put_plan_rejects_step_with_whitespace_only_action():
    """A step with action="   " (whitespace) is rejected."""
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "whitespace-action",
            "steps": [
                {"name": "x", "agent_role": "super", "action": "   "},
            ],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 422
    finally:
        _delete_project(pid)


def test_put_plan_rejects_step_with_single_char_action():
    """A step with action="x" (single char) is rejected (min 2)."""
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "short-action",
            "steps": [
                {"name": "x", "agent_role": "super", "action": "x"},
            ],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 422
    finally:
        _delete_project(pid)


def test_put_plan_accepts_step_with_canonical_verb_action():
    """A step with a canonical verb (e.g. 'fetch_url') is accepted."""
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "ok-action",
            "steps": [
                {"name": "fetch", "agent_role": "super", "action": "fetch_url"},
            ],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 200, f"expected 200, got {s}: {body}"
    finally:
        _delete_project(pid)


def test_put_plan_accepts_step_with_prose_action():
    """A step with a longer prose action ('fetch the bus 93K route...') is accepted."""
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "prose-action",
            "steps": [
                {
                    "name": "fetch-bus-info",
                    "agent_role": "super",
                    "action": "fetch the Hong Kong bus 93K route and arrival times from the public API",
                },
            ],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 200, f"expected 200, got {s}: {body}"
    finally:
        _delete_project(pid)


# ===== The exact scenario from the user's bug report =====


def test_user_bug_scenario_repro():
    """The user's reported scenario: '巴士93K的資訊 send 去 telegram message
    gateway 經 linux super profile' should NOT produce a plan with
    empty actions. This test simulates what the LLM produced and
    confirms the new validator catches it."""
    pid = _create_test_project()
    try:
        # This is the EXACT plan shape the LLM produced in
        # proj-7916a66e (from chat.jsonl): all fields empty except
        # name + agent_role.
        llm_produced_plan = {
            "version": "1.0",
            "name": "bus-93k-to-telegram",
            "steps": [
                {
                    "name": "fetch-bus-93k-info",
                    "agent_role": "super",
                    "skill": "",
                    "tool": "",
                    "depends_on": [],
                },
                {
                    "name": "send-telegram-message",
                    "agent_role": "super",
                    "skill": "",
                    "tool": "",
                    "depends_on": ["fetch-bus-93k-info"],
                },
            ],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": llm_produced_plan})
        # The new contract: this MUST be rejected
        assert s == 422, (
            f"REGRESSION: empty-action plan was accepted! "
            f"This is the bug from proj-7916a66e. Got {s}: {body}"
        )
    finally:
        _delete_project(pid)


def test_user_bug_scenario_with_action_accepted():
    """Same scenario as above, but with proper action fields. Should
    be accepted — verifies the user's workflow IS still possible,
    just requires the action to be filled in."""
    pid = _create_test_project()
    try:
        fixed_plan = {
            "version": "1.0",
            "name": "bus-93k-to-telegram",
            "steps": [
                {
                    "name": "fetch-bus-93k-info",
                    "agent_role": "super",
                    "action": "fetch_bus_route_info",  # the missing field
                    "depends_on": [],
                },
                {
                    "name": "send-telegram-message",
                    "agent_role": "super",
                    "action": "send_telegram_message",  # the missing field
                    "depends_on": ["fetch-bus-93k-info"],
                },
            ],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": fixed_plan})
        assert s == 200, f"expected 200, got {s}: {body}"
        assert body["plan"]["name"] == "bus-93k-to-telegram"
    finally:
        _delete_project(pid)
