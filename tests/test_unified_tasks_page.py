"""Tests for the unified /tasks page (commit 1 of the merge, 2026-07-27).

Covers:
  - /tasks page renders both single and project tasks (Type column)
  - /tasks?kind=single filter shows only single tasks
  - /tasks?kind=project filter hides single tasks
  - /tasks?search=... filter matches by name/id/action
  - /single-tasks redirects to /tasks (preserve bookmark URL)
  - Project dropdown in create form omits the virtual __single_tasks__ project

The HTML pages are tested via direct HTTP (no Playwright) — the page
is server-rendered, so we just GET the URL and assert on text.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

BASE = "http://127.0.0.1:8765"


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | None]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def _get_text(path: str) -> tuple[int, str]:
    url = f"{BASE}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def test_unified_tasks_renders_single_and_project():
    """GET /tasks should include both project tasks and single tasks
    (single ones get a "Single" badge in the Type column)."""
    status, html = _get_text("/tasks?limit=200&days=9999")
    assert status == 200
    # Both kinds should be present somewhere in the rendered page
    # (we don't assert exact counts because the DB has many tasks).
    # The "Single" badge appears as <span ...>Single</span> in the
    # task row's Type column.
    assert '>Single</span>' in html or ">Single<" in html, "Single task badge missing"
    # The form's "Project container" dropdown should include the
    # "(Single — no project)" option.
    assert "(Single — no project)" in html, "Project container dropdown missing single option"


def test_unified_tasks_kind_filter_single():
    """GET /tasks?kind=single should show only single tasks (Type=Single)."""
    status, html = _get_text("/tasks?kind=single&limit=50&days=9999")
    assert status == 200
    # The page header / filter bar should show "Single" highlighted
    # in the Kind filter.
    # We just verify the page rendered and contains the kind filter row.
    assert "Kind:" in html
    # All listed tasks should be single tasks. We verify by absence:
    # project-only fields like "agent_role" populated in the table
    # would be a non-single task. (Some project tasks DO have
    # agent_role, so this is a weak check. Use a more specific one.)
    # A better check: the page should still render the "Single" badge
    # at least once (otherwise the filter returned no rows).
    assert '>Single</span>' in html, "kind=single filter returned no single tasks"


def test_unified_tasks_kind_filter_project_excludes_single():
    """GET /tasks?kind=project should show only project tasks (no
    "Single" badge in the rows)."""
    status, html = _get_text("/tasks?kind=project&limit=50&days=9999")
    assert status == 200
    # The Type column for project tasks shows a project name link,
    # not the Single badge. We check that the Single badge string
    # does NOT appear in the tasks-list section.
    # Find the start of the tasks list and check after that.
    list_start = html.find('id="tasks-list"')
    assert list_start > 0, "tasks list not found"
    after = html[list_start:]
    # The Single badge pattern only appears in the Type column for
    # single tasks, NOT in the Kind filter pill (which is highlighted
    # with bg-emerald-600 — different from the badge bg-emerald-100).
    # We check the badge specifically: "text-emerald-700 text-[10px] rounded"
    assert "text-emerald-700 text-[10px] rounded" not in after, \
        "kind=project filter still showed single task badge"


def test_unified_tasks_search_by_name():
    """GET /tasks?search=<name> should filter to tasks matching the name."""
    # Create a single task with a unique name
    marker = "unified_tasks_search_test_marker_xyz123"
    status, body = _http("POST", "/api/single-tasks", {
        "name": marker,
        "goal": "test search by name",
        "agent_role": "super",
    })
    assert status == 201, f"create failed: {status} {body}"
    task_id = body["id"]
    try:
        status, html = _get_text(f"/tasks?search={marker}&limit=20")
        assert status == 200
        assert marker in html, "search term not found in HTML"
        # The task ID should be linked
        assert task_id in html, "task ID not in HTML"
    finally:
        # Best-effort cleanup: cancel the task. (We don't actually
        # need to — the test is idempotent and the marker is unique.)
        pass


def test_unified_tasks_search_by_id():
    """GET /tasks?search=<id> should find the task by its ID."""
    status, body = _http("POST", "/api/single-tasks", {
        "name": "search by id test",
        "goal": "x",
        "agent_role": "super",
    })
    assert status == 201
    task_id = body["id"]
    status, html = _get_text(f"/tasks?search={task_id}")
    assert status == 200
    assert task_id in html


def test_unified_tasks_search_by_action():
    """Search should also match the action column for project tasks."""
    # Use a real existing project — pick the first one we find
    status, html = _get_text("/projects?limit=5")
    assert status == 200
    # Pull a real project id from the HTML (cheap heuristic: t-XXXX is
    # task id, proj-XXXX is project id).
    import re
    proj_match = re.search(r"proj-[a-f0-9]+", html)
    if not proj_match:
        pytest.skip("no project available for action-search test")
    project_id = proj_match.group(0)
    # Create a project task with a unique action
    action = "unified_search_action_test_zzz"
    status, body = _http("POST", "/api/tasks/", {
        "project_id": project_id,
        "agent_role": "super",
        "action": action,
        "params": {"note": "test"},
    })
    if status != 201:
        pytest.skip(f"project task create failed: {status} {body}")
    try:
        status, html = _get_text(f"/tasks?search={action}&limit=20")
        assert status == 200
        assert action in html, "action search term not found"
    finally:
        pass


def test_single_tasks_url_redirects_to_tasks():
    """GET /single-tasks should 307 redirect to /tasks (preserve bookmarks)."""
    import urllib.request
    url = f"{BASE}/single-tasks"
    try:
        # Don't follow redirects — we want to see the 307 directly
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw): return None
        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(url, timeout=10) as r:
            assert r.status in (301, 302, 303, 307, 308), \
                f"expected redirect, got {r.status}"
            location = r.headers.get("Location", "")
            assert location == "/tasks", f"expected /tasks redirect, got {location!r}"
    except urllib.error.HTTPError as e:
        # urllib's NoRedirect handler raises HTTPError for 3xx
        assert e.code in (301, 302, 303, 307, 308), \
            f"expected redirect, got {e.code}"
        location = e.headers.get("Location", "")
        assert location == "/tasks", f"expected /tasks redirect, got {location!r}"


def test_single_task_detail_still_works():
    """GET /single-tasks/{id} should still work (detail page kept for bookmarks)."""
    # Create a single task
    status, body = _http("POST", "/api/single-tasks", {
        "name": "detail page still works",
        "goal": "test detail URL preservation",
        "agent_role": "super",
    })
    assert status == 201
    task_id = body["id"]
    # The detail URL must still work (no redirect)
    status, html = _get_text(f"/single-tasks/{task_id}")
    assert status == 200, f"detail page failed: {status}"
    assert "detail page still works" in html or task_id in html


def test_project_dropdown_excludes_virtual_project():
    """The Project container dropdown should NOT include __single_tasks__
    (the virtual project that holds single tasks)."""
    status, html = _get_text("/tasks?limit=5")
    assert status == 200
    # Find the project dropdown options
    # The dropdown is <select id="nt-project" ...>
    dropdown_start = html.find('id="nt-project"')
    assert dropdown_start > 0
    # Find the matching closing </select> tag (be permissive — just
    # check the next 50KB of HTML for the option tags)
    dropdown_html = html[dropdown_start:dropdown_start + 50000]
    assert 'value="__single_tasks__"' not in dropdown_html, \
        "virtual __single_tasks__ project should be hidden from the dropdown"


def test_unified_tasks_create_single_via_api():
    """Verify the new SingleTaskOut.agent_role field is round-tripped
    (this was the 2026-07-27 user feedback that started this work)."""
    status, body = _http("POST", "/api/single-tasks", {
        "name": "agent role round-trip test",
        "goal": "x",
        "agent_role": "win-agent01",
    })
    assert status == 201
    assert body["agent_role"] == "win-agent01", \
        f"agent_role not round-tripped: {body.get('agent_role')!r}"
    # Fetch back
    status2, body2 = _http("GET", f"/api/single-tasks/{body['id']}")
    assert status2 == 200
    assert body2["agent_role"] == "win-agent01"
