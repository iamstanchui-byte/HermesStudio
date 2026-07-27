"""Tests for the visual plan editor page (Phase C, 2026-07-27).

The page is rendered by GET /api/projects/{id}/plan/visual (the
plans_router is mounted at /api in main.py, so the visual page
URL is /api/projects/{id}/plan/visual). This is different from
the /projects/{id} convention used for the project page proper
— the plan API endpoints are all under /api, and the visual
page stays under /api for consistency.

Covers:
  - 200 OK with valid project + plan
  - 404 for unknown project
  - The page includes the drawflow CDN + visual_plan.js setup
  - The page embeds the plan JSON as a data-* attribute
  - The page has the toolbar (Add step, Validate, Generate, Save)
  - The page has the side panel + minimap DOM
  - For a project without a plan, the data attr is empty (JS
    then loads the empty template)
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
    name = f"vp-test-{uuid.uuid4().hex[:8]}"
    s, body = _http("POST", "/api/projects/", {"name": name})
    return body["id"]


def _put_plan(pid: str, plan: dict) -> None:
    s, _ = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
    assert s == 200


def _delete_project(pid: str) -> None:
    try:
        _http("DELETE", f"/api/projects/{pid}")
    except Exception:
        pass


# ===== Page renders =====


def test_visual_plan_page_404_for_unknown_project():
    try:
        s, _ = _get_text("/api/projects/does-not-exist/plan/visual"), None
    except urllib.error.HTTPError as e:
        assert e.code == 404
        return
    # If we get here, the server didn't 404
    pytest.fail("expected 404 for unknown project")


def test_visual_plan_page_renders_with_plan():
    """A project with a plan should render the page with the plan
    JSON embedded as a data attribute."""
    pid = _create_test_project()
    try:
        _put_plan(pid, {
            "version": "1.0",
            "name": "vp-render-test",
            "steps": [
                {"name": "a", "agent_role": "super", "action": "fetch"},
                {"name": "b", "agent_role": "super", "action": "process",
                 "depends_on": ["a"]},
            ],
        })
        html = _get_text(f"/api/projects/{pid}/plan/visual")
        assert len(html) > 5000, "page seems too short"
        # drawflow CDN setup
        assert "drawflow@0.0.59" in html
        # JS loaded
        assert "visual_plan.js" in html
        # Embedded plan JSON
        assert "vp-render-test" in html
        # Toolbar buttons
        for label in ("Add step", "Apply workflow", "Validate plan",
                      "Generate plan", "Generate tasks", "Save"):
            assert label in html, f"missing toolbar button: {label}"
        # Side panel DOM. The minimap was removed 2026-07-28
        # (no interactivity + was the source of the fade-text bug)
        # so we no longer assert on its id.
        assert 'id="vp-side-panel"' in html
        # JS function bindings
        for fn in ("addStep", "savePlan", "generateTasks", "validatePlan",
                   "toggleJsonMode", "saveStepEdits", "deleteSelectedStep",
                   "openGeneratePlanModal", "generatePlanFromLlm"):
            assert fn in html, f"missing JS function: {fn}"
    finally:
        _delete_project(pid)


def test_visual_plan_page_renders_without_plan():
    """A project without a plan should still render (empty template)."""
    pid = _create_test_project()
    try:
        html = _get_text(f"/api/projects/{pid}/plan/visual")
        assert len(html) > 5000
        # The data-plan-json attribute should be present but empty
        assert 'data-plan-json' in html
        # The toolbar still renders
        assert "Add step" in html
    finally:
        _delete_project(pid)


def test_visual_plan_page_embeds_step_names_in_dom():
    """The plan JSON is embedded as a data attribute, so the JS
    can bootstrap drawflow from the saved state on load."""
    pid = _create_test_project()
    try:
        _put_plan(pid, {
            "version": "1.0",
            "name": "embed-test",
            "steps": [
                {"name": "alpha", "agent_role": "super"},
                {"name": "beta", "agent_role": "super", "depends_on": ["alpha"]},
            ],
        })
        html = _get_text(f"/api/projects/{pid}/plan/visual")
        # Both step names should be in the embedded data attribute
        # (HTML-encoded, so they appear as text in the page)
        assert "alpha" in html
        assert "beta" in html
    finally:
        _delete_project(pid)


def test_visual_plan_page_links_back_to_project():
    """There should be a link back to the project page (so the
    user can navigate from plan editor back to the project)."""
    pid = _create_test_project()
    try:
        html = _get_text(f"/api/projects/{pid}/plan/visual")
        assert f"/projects/{pid}" in html, "no back-to-project link"
    finally:
        _delete_project(pid)
