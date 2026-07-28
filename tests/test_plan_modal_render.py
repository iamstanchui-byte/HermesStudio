"""Tests for the project page toolbar + plan API.

UI cleanup 2026-07-27: the project page toolbar was simplified. The
"Plan" (JSON modal) button was removed — the visual plan editor is
the primary surface. The "Plan editor" link (renamed from "Visual")
links to /api/projects/{id}/plan/visual.

This test now covers:
  - The "Plan editor" link is in the project page toolbar
  - The plan API still works (GET /api/projects/{id}/plan)

The JSON plan modal + its DOM + JS are no longer tested here — they
were removed along with the Plan button. The plan API itself is
fully covered in test_project_plan.py and test_run_plan.py.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid

import pytest

BASE = os.environ.get("HERMES_TEST_BASE", "http://127.0.0.1:8765")


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
    s, body = _http("POST", "/api/projects/", {"name": name, "action": "do_step"})
    return body["id"]


def _delete_project(pid: str) -> None:
    try:
        _http("DELETE", f"/api/projects/{pid}")
    except Exception:
        pass


def test_project_page_has_plan_editor_link():
    """The project page toolbar should include a '🎨 Plan editor' link
    to the visual plan editor (the primary editing surface). UI
    cleanup 2026-07-27 renamed the button from 'Visual' to
    'Plan editor' to disambiguate from the task-canvas Visual view
    that was also removed."""
    pid = _create_test_project()
    try:
        html = _get_text(f"/projects/{pid}")
        # The link is an <a> tag with the visual editor URL
        assert f'href="/api/projects/{pid}/plan/visual"' in html, \
            "missing 'Plan editor' link to the visual editor"
        # The link's label is "Plan editor" (not "Visual")
        assert "Plan editor" in html, "missing 'Plan editor' label"
    finally:
        _delete_project(pid)


def test_project_page_no_legacy_plan_button():
    """The 'Plan' (JSON modal) button should NOT be on the project
    page anymore. The visual plan editor covers the same surface
    and the user said they don't read JSON."""
    pid = _create_test_project()
    try:
        html = _get_text(f"/projects/{pid}")
        # No button onclick for the removed openPlanModal
        assert 'onclick="openPlanModal(' not in html, \
            "old Plan (JSON) button still present"
        # No plan modal in the DOM
        assert 'id="plan-modal-overlay"' not in html, \
            "old plan modal still in DOM"
    finally:
        _delete_project(pid)


def test_plan_api_returns_empty_for_new_project():
    """End-to-end: a new project should have has_plan=False, plan=None
    via the API. The visual plan editor then loads the empty template."""
    pid = _create_test_project()
    try:
        s, body = _http("GET", f"/api/projects/{pid}/plan")
        assert s == 200
        assert body["has_plan"] is False
        assert body["plan"] is None
    finally:
        _delete_project(pid)


def test_project_page_has_visual_task_canvas_link():
    """UI cleanup 2026-07-27: the visual task canvas button was removed
    from the project page toolbar. The page is still reachable via
    a small 'Open visual task canvas' link at the bottom of the
    toolbar (for power users who want the drawflow canvas)."""
    pid = _create_test_project()
    try:
        html = _get_text(f"/projects/{pid}")
        # The small link is a regular <a> with a 'task canvas' hint
        assert f'href="/projects/{pid}/visual"' in html, \
            "missing 'Open visual task canvas' link"
        assert "task canvas" in html or "visual task" in html, \
            "missing link label"
    finally:
        _delete_project(pid)
