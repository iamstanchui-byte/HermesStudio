"""Tests for _render_dag_section in api/projects.py (Phase 1, 2026-07-28).

The chat endpoint calls this helper after the LLM response to
append a DAG render block (so the user can see the plan shape
without expanding the JSON suggestion chip). This test exercises
the helper in isolation — no LLM, no DB.
"""
from __future__ import annotations

import pytest

from hermes_orch.api.projects import _render_dag_section


# ===== Empty / no-op cases =====


def test_empty_suggestions_returns_empty_string():
    assert _render_dag_section([]) == ""


def test_none_suggestions_returns_empty_string():
    assert _render_dag_section(None) == ""


def test_no_update_plan_suggestion_returns_empty():
    """A create_task suggestion (legacy, but defensively) → no DAG."""
    suggestions = [{"type": "create_task", "name": "x", "action": "y"}]
    assert _render_dag_section(suggestions) == ""


def test_update_plan_with_empty_steps_returns_empty():
    suggestions = [{"type": "update_plan", "plan": {"steps": []}}]
    assert _render_dag_section(suggestions) == ""


def test_update_plan_with_missing_steps_returns_empty():
    suggestions = [{"type": "update_plan", "plan": {"name": "x"}}]
    assert _render_dag_section(suggestions) == ""


def test_update_plan_with_non_dict_plan_returns_empty():
    """Defensive: malformed suggestion should not crash the chat."""
    suggestions = [{"type": "update_plan", "plan": "not a dict"}]
    assert _render_dag_section(suggestions) == ""


# ===== Happy path =====


def test_linear_chain_renders_as_dag():
    suggestions = [{
        "type": "update_plan",
        "plan": {
            "steps": [
                {"name": "fetch", "depends_on": []},
                {"name": "parse", "depends_on": ["fetch"]},
            ],
        },
    }]
    out = _render_dag_section(suggestions)
    # Has the markdown fence
    assert "```text" in out
    assert "```" in out
    # Has the heading
    assert "Current plan" in out
    # Has the DAG body
    assert "fetch" in out
    assert "parse" in out
    assert "└─" in out


def test_branching_renders_full_dag():
    suggestions = [{
        "type": "update_plan",
        "plan": {
            "steps": [
                {"name": "load", "depends_on": []},
                {"name": "fetch-a", "depends_on": ["load"]},
                {"name": "fetch-b", "depends_on": ["load"]},
                {"name": "combine", "depends_on": ["fetch-a", "fetch-b"]},
            ],
        },
    }]
    out = _render_dag_section(suggestions)
    assert "load" in out
    assert "├─" in out
    assert "└─" in out
    assert "│" in out
    # Combine appears under both parents (expanded view)
    assert out.count("combine") == 2


def test_multiple_update_plan_suggestions_each_rendered():
    """If somehow there are 2 update_plan suggestions (shouldn't happen
    in practice but defensive), both are rendered, separated by blank
    lines."""
    suggestions = [
        {
            "type": "update_plan",
            "plan": {"steps": [{"name": "a", "depends_on": []}]},
        },
        {
            "type": "update_plan",
            "plan": {"steps": [{"name": "x", "depends_on": []}]},
        },
    ]
    out = _render_dag_section(suggestions)
    assert "a" in out
    assert "x" in out
    # The two DAGs are joined with a blank line (the renderer's
    # default join)
    assert "\n\n" in out


def test_mixed_suggestion_types_only_renders_update_plan():
    """Other types in the list are skipped; update_plan is rendered."""
    suggestions = [
        {"type": "create_task", "name": "ignored"},
        {
            "type": "update_plan",
            "plan": {"steps": [{"name": "shown", "depends_on": []}]},
        },
        {"type": "some_other_type"},
    ]
    out = _render_dag_section(suggestions)
    assert "shown" in out
    assert "ignored" not in out


# ===== Format details =====


def test_output_starts_with_newline_section_header():
    """The section is appended to the existing assistant text. The
    function should start with a leading newline+blank line so
    it doesn't run into the previous line."""
    suggestions = [{
        "type": "update_plan",
        "plan": {"steps": [{"name": "a", "depends_on": []}]},
    }]
    out = _render_dag_section(suggestions)
    assert out.startswith("\n\nCurrent plan:")


def test_output_ends_with_code_fence_close():
    suggestions = [{
        "type": "update_plan",
        "plan": {"steps": [{"name": "a", "depends_on": []}]},
    }]
    out = _render_dag_section(suggestions)
    assert out.rstrip().endswith("```")
