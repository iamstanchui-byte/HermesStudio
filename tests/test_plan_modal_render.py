"""Tests for the project page Plan modal render (Phase A, 2026-07-27).

Covers:
  - The "📋 Plan" button is in the project page toolbar
  - The Plan modal is in the DOM (with textarea + save/cancel buttons)
  - The page still works for projects that have no plan yet
  - The plan-modal text area loads the empty template when has_plan=false
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
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
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


def _get_text(path: str) -> str:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
        return r.read().decode("utf-8", errors="replace")


def _create_test_project() -> str:
    name = f"plan-modal-test-{uuid.uuid4().hex[:8]}"
    s, body = _http("POST", "/api/projects/", {"name": name})
    return body["id"]


def _delete_project(pid: str) -> None:
    try:
        _http("DELETE", f"/api/projects/{pid}")
    except Exception:
        pass


def test_project_page_has_plan_button():
    """The project page toolbar should include a '📋 Plan' button."""
    pid = _create_test_project()
    try:
        html = _get_text(f"/projects/{pid}")
        # The Plan button is in the toolbar with the cyan border
        # and the 'openPlanModal' onclick handler.
        assert "openPlanModal" in html
        assert "📋 Plan" in html or "Plan" in html
    finally:
        _delete_project(pid)


def test_project_page_has_plan_modal_dom():
    """The Plan modal DOM elements should be in the page even when
    hidden (so openPlanModal can populate them)."""
    pid = _create_test_project()
    try:
        html = _get_text(f"/projects/{pid}")
        # The modal contains the textarea + buttons
        assert 'id="plan-modal-overlay"' in html
        assert 'id="plan-json"' in html
        assert 'id="plan-submit-btn"' in html
        assert 'id="plan-status"' in html
        assert 'id="plan-error"' in html
        # The Plan modal title
        assert "Project plan" in html
        # The clear plan button
        assert "clearPlan()" in html or "Clear plan" in html
    finally:
        _delete_project(pid)


def test_project_page_has_plan_js_handlers():
    """The Plan modal JS functions should be in the page."""
    pid = _create_test_project()
    try:
        html = _get_text(f"/projects/{pid}")
        for fn in ("openPlanModal", "closePlanModal", "submitPlan", "clearPlan"):
            assert f"function {fn}" in html, f"missing function {fn}"
    finally:
        _delete_project(pid)


def test_plan_api_returns_empty_for_new_project():
    """End-to-end: a new project should have has_plan=False, plan=None
    via the API (the modal then loads the empty template)."""
    pid = _create_test_project()
    try:
        s, body = _http("GET", f"/api/projects/{pid}/plan")
        assert s == 200
        assert body["has_plan"] is False
        assert body["plan"] is None
    finally:
        _delete_project(pid)


def test_project_page_has_visual_plan_button():
    """Phase C: the project page toolbar should include a '🎨 Visual'
    button that links to the visual plan editor. The visual editor
    is the primary editing surface; the JSON modal is secondary."""
    pid = _create_test_project()
    try:
        html = _get_text(f"/projects/{pid}")
        # The Visual button is an <a> tag linking to the visual editor
        assert f'href="/api/projects/{pid}/plan/visual"' in html, \
            "missing 'Visual' link to the plan editor"
        assert "Visual" in html
    finally:
        _delete_project(pid)
