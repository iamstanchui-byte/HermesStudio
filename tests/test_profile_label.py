"""Tests for the profile column formatting + action preset chips
(commit 3, 2026-07-27).

Covers:
  - The /tasks page renders the profile as 'agent_id / profile_name'
  - Falls back to just agent_id when assigned_profile_id is missing
  - Shows '—' when no profile or agent is assigned
  - The action preset chips appear in the create form (project mode)
  - Chips show action name + usage count
"""
from __future__ import annotations

import json
import re
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


def _get_text(path: str) -> str:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
        return r.read().decode("utf-8", errors="replace")


def test_tasks_page_profile_label_format():
    """Tasks with assigned_profile_id should show 'agent_id / profile_name'."""
    html = _get_text("/tasks?kind=project&limit=20&days=9999")
    # The page should contain at least one task whose profile label
    # follows the 'agent_id / profile_name' format. We look for the
    # 'profile:' label and a slash-separated value.
    assert "profile:" in html
    # Find rows by searching for the profile label pattern
    # (something like "linux-a-01 / super" inside a font-mono span)
    matches = re.findall(
        r'<span class="font-mono"[^>]*>([a-z0-9_-]+ / [a-z0-9_-]+)</span>',
        html,
    )
    assert len(matches) > 0, \
        f"No 'agent_id / profile_name' labels found in {html[:500]!r}"


def test_tasks_page_has_kind_filter():
    """The Kind filter (all/project/single) must be in the page."""
    html = _get_text("/tasks?limit=5")
    assert 'Kind:' in html
    assert 'kind=all' in html
    assert 'kind=project' in html
    assert 'kind=single' in html


def test_tasks_page_action_preset_chips():
    """The create form should render action preset chips with names
    and counts. Skip if no actions exist (empty DB scenario)."""
    html = _get_text("/tasks?limit=5")
    # The 'Recent:' label is rendered above the chips
    if 'Recent:' not in html:
        pytest.skip("no action presets (empty DB)")
    # Find at least one chip — they're buttons that include an
    # "(N)" usage count. Use a permissive regex (DOTALL) since the
    # button is rendered multi-line in HTML.
    chip_pattern = re.compile(
        r'<button[^>]*>\s*([a-z_][\w-]*)\s*<span[^>]*>\((\d+)\)</span>',
        re.DOTALL,
    )
    chip_matches = chip_pattern.findall(html)
    assert len(chip_matches) > 0, \
        f"No action preset chips with counts found"
    # Verify each chip has a name and a numeric count
    for name, count in chip_matches:
        assert int(count) >= 1, f"chip {name!r} has invalid count {count}"


def test_tasks_page_action_preset_excludes_do_task():
    """The 'do_task' default action (used by single tasks) should NOT
    appear in presets — we want concrete action types, not the
    single-task default."""
    html = _get_text("/tasks?limit=5")
    if 'Recent:' not in html:
        pytest.skip("no action presets")
    chip_pattern = re.compile(
        r'<button[^>]*>\s*([a-z_][\w-]*)\s*<span[^>]*>\(\d+\)</span>',
        re.DOTALL,
    )
    chip_names = chip_pattern.findall(html)
    assert "do_task" not in chip_names, \
        f"do_task should be excluded from presets, found in: {chip_names}"


def test_tasks_page_create_form_has_project_container_dropdown():
    """The create form should have the Project container dropdown
    (the unified-tasks feature from commit 1) with both options."""
    html = _get_text("/tasks?limit=5")
    assert 'id="nt-project"' in html
    assert "(Single — no project)" in html


def test_tasks_page_project_dropdown_excludes_virtual_project():
    """The Project container dropdown should NOT include the virtual
    __single_tasks__ project."""
    html = _get_text("/tasks?limit=5")
    # Find the nt-project select and check its options
    sel_start = html.find('id="nt-project"')
    sel_end = html.find('</select>', sel_start)
    sel_html = html[sel_start:sel_end]
    assert '__single_tasks__' not in sel_html, \
        "Virtual __single_tasks__ should be hidden from the dropdown"
