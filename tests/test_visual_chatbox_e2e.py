"""Tests for the chatbox in the visual plan editor (Phase 1.5).

The chatbox panel HTML is embedded in visual_plan.html. This
test verifies:
  1. The visual plan page loads (GET /api/projects/{id}/plan/visual)
  2. The page contains the chat panel elements (#chat-panel, etc.)
  3. The page loads the chatbox.js script
  4. The page sets up ChatboxHooks.onPlanApplied for the visual
     editor to refresh the canvas after apply
  5. The plan page exposes a loadPlan function for the hook to call
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
import uuid

import pytest

BASE = "http://127.0.0.1:8765"


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    url = f"{BASE}{path}"
    data = None
    headers = {}
    if body is not None:
        import json
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def _create_test_project() -> str:
    import json
    name = f"visual-chat-test-{uuid.uuid4().hex[:8]}"
    s, body = _http("POST", "/api/projects/", {"name": name})
    if s == 201:
        return json.loads(body)["id"]
    if s == 200:
        return json.loads(body)["id"]
    pytest.fail(f"create project failed: {s} {body}")


def _delete_project(project_id: str) -> None:
    try:
        _http("DELETE", f"/api/projects/{project_id}")
    except Exception:
        pass


# ===== Visual plan page renders the chat panel =====


def test_visual_plan_page_contains_chat_panel():
    """The /plan/visual page must include the chat-panel elements."""
    pid = _create_test_project()
    try:
        s, html = _http("GET", f"/api/projects/{pid}/plan/visual")
        assert s == 200
        # Chat panel container
        assert 'id="chat-panel"' in html, "chat-panel div not found"
        # Toggle button
        assert 'id="chat-toggle-btn"' in html, "chat-toggle-btn not found"
        # Messages container + input
        assert 'id="chat-messages"' in html, "chat-messages not found"
        assert 'id="chat-input"' in html, "chat-input not found"
        # Apply button container is rendered in JS, not HTML;
        # check for the shared chatbox.js script tag
        assert "/static/chatbox.js" in html, "chatbox.js script not referenced"
    finally:
        _delete_project(pid)


def test_visual_plan_page_loads_chatboxjs_script():
    """The chatbox.js must be loaded as a deferred script so it
    runs after the DOM is ready (the panel elements need to
    exist)."""
    pid = _create_test_project()
    try:
        s, html = _http("GET", f"/api/projects/{pid}/plan/visual")
        assert s == 200
        # The script tag must be deferred
        assert re.search(
            r'<script[^>]*defer[^>]*src="/static/chatbox\.js',
            html,
        ), "expected <script defer src='/static/chatbox.js'>"
    finally:
        _delete_project(pid)


def test_visual_plan_page_wires_on_plan_applied_hook():
    """The page must register a ChatboxHooks.onPlanApplied that
    calls window.vp.loadPlan so the drawflow canvas refreshes
    after the chat applies an update."""
    pid = _create_test_project()
    try:
        s, html = _http("GET", f"/api/projects/{pid}/plan/visual")
        assert s == 200
        # The hook registration block is inline JS
        assert "ChatboxHooks" in html, "ChatboxHooks not set up"
        assert "onPlanApplied" in html, "onPlanApplied hook not registered"
        assert "window.vp.loadPlan" in html, "hook should call window.vp.loadPlan"
    finally:
        _delete_project(pid)


# ===== chatbox.js exposes the public API =====


def test_chatbox_js_exposes_public_api():
    """The shared chatbox module must expose its public functions
    on window.chatbox so the host page can call them. Since this
    is a static JS file we just check the source mentions the
    expected surface — full E2E test would need a headless browser."""
    s, content = _http("GET", "/static/chatbox.js")
    assert s == 200
    for fn in (
        "toggleChatPanel",
        "sendChatMessage",
        "applySuggestion",
        "clearChat",
        "reformatLastAssistant",
        "loadChatHistory",
        "renderChatMessage",
        "renderChatContent",
        "renderSuggestion",
    ):
        assert fn in content, f"chatbox.js missing export: {fn}"
    assert "window.chatbox" in content, "chatbox.js must export on window.chatbox"


# ===== visual_plan.js exposes loadPlan =====


def test_visual_plan_js_exposes_loadplan():
    s, content = _http("GET", "/static/visual_plan.js")
    assert s == 200
    # The new loadPlan function is on window.vp
    assert "loadPlan" in content, "visual_plan.js missing loadPlan"
    # Sanity: it should set _plan and call renderAllSteps
    assert "_plan = " in content, "loadPlan should set _plan"
    assert "renderAllSteps" in content, "loadPlan should call renderAllSteps"


# ===== End-to-end: apply plan via chat apply, then verify
#       the plan is the new one (this is the data flow that
#       the visual editor + chat would observe) =====


def test_e2e_chat_apply_then_load_plan_via_plan_get():
    """Same as test_chatbox_e2e but framed for the visual editor
    scenario: the operator clicks Apply in the chat panel, the
    plan is written, the visual editor (via ChatboxHooks) calls
    loadPlan(newPlan) to refresh the canvas."""
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "visual-chat-e2e",
            "description": "applied via chat from visual editor",
            "trigger": "manual",
            "variables": [],
            "steps": [
                {"name": "fetch", "agent_role": "super", "depends_on": []},
                {"name": "parse", "agent_role": "super", "depends_on": ["fetch"]},
                {"name": "report", "agent_role": "win-agent01",
                 "depends_on": ["parse"]},
            ],
        }
        import json
        s, body = _http("POST", f"/api/projects/{pid}/chat/apply", {
            "suggestion": {"type": "update_plan", "plan": plan},
        })
        assert s == 200, f"apply failed: {s} {body}"
        # GET /plan returns the persisted plan (what visual editor
        # would call loadPlan() with)
        s, body = _http("GET", f"/api/projects/{pid}/plan")
        assert s == 200
        data = json.loads(body)
        assert data["has_plan"] is True
        assert data["plan"]["name"] == "visual-chat-e2e"
        assert len(data["plan"]["steps"]) == 3
        # Verify the visual editor's loadPlan would accept this
        # shape (all required fields present)
        for step in data["plan"]["steps"]:
            assert "name" in step
            assert "depends_on" in step
    finally:
        _delete_project(pid)
