"""Tests for src/hermes_orch/dag_render.py (Phase 1, 2026-07-28).

The renderer is a pure function — no DB, no I/O. Tests are
snapshot-style: capture the exact output for representative cases
so we catch any rendering regression when the algorithm changes.
"""
from __future__ import annotations

import pytest

from hermes_orch.dag_render import render_plan_dag


# ===== Empty / single-step cases =====


def test_empty_returns_placeholder():
    out = render_plan_dag([])
    assert out == "(empty plan — no steps yet)"


def test_single_step_no_deps():
    steps = [{"name": "only-step", "depends_on": [], "action": "do_step"}]
    out = render_plan_dag(steps)
    assert out == "only-step"


# ===== Linear chain =====


def test_linear_chain_three_steps():
    steps = [
        {"name": "fetch", "depends_on": [], "action": "do_step"},
        {"name": "parse", "depends_on": ["fetch"], "action": "do_step"},
        {"name": "report", "depends_on": ["parse"], "action": "do_step"},
    ]
    out = render_plan_dag(steps)
    expected = (
        "fetch\n"
        "└─ parse\n"
        "    └─ report"
    )
    assert out == expected


# ===== Branching (fan-out) =====


def test_branching_two_children():
    """1 step → 2 parallel children → 1 grandchild (diamond, expanded)."""
    steps = [
        {"name": "load", "depends_on": [], "action": "do_step"},
        {"name": "fetch-a", "depends_on": ["load"], "action": "do_step"},
        {"name": "fetch-b", "depends_on": ["load"], "action": "do_step"},
        {"name": "combine", "depends_on": ["fetch-a", "fetch-b"], "action": "do_step"},
    ]
    out = render_plan_dag(steps)
    expected = (
        "load\n"
        "├─ fetch-a\n"
        "│   └─ combine\n"
        "└─ fetch-b\n"
        "    └─ combine"
    )
    assert out == expected


# ===== Multiple roots (no shared parent) =====


def test_two_roots_with_subtrees():
    steps = [
        {"name": "alpha", "depends_on": [], "action": "do_step"},
        {"name": "alpha-child", "depends_on": ["alpha"], "action": "do_step"},
        {"name": "beta", "depends_on": [], "action": "do_step"},
    ]
    out = render_plan_dag(steps)
    expected = (
        "alpha\n"
        "└─ alpha-child\n"
        "beta"
    )
    assert out == expected


# ===== Deterministic order (children sorted alphabetically) =====


def test_children_rendered_in_alphabetical_order():
    """Children must be sorted so output is deterministic across runs."""
    steps = [
        {"name": "root", "depends_on": [], "action": "do_step"},
        {"name": "z-child", "depends_on": ["root"], "action": "do_step"},
        {"name": "a-child", "depends_on": ["root"], "action": "do_step"},
        {"name": "m-child", "depends_on": ["root"], "action": "do_step"},
    ]
    out = render_plan_dag(steps)
    expected = (
        "root\n"
        "├─ a-child\n"
        "├─ m-child\n"
        "└─ z-child"
    )
    assert out == expected


# ===== Multiple roots, deterministic order =====


def test_roots_rendered_in_alphabetical_order():
    steps = [
        {"name": "zebra", "depends_on": [], "action": "do_step"},
        {"name": "alpha", "depends_on": [], "action": "do_step"},
        {"name": "middle", "depends_on": [], "action": "do_step"},
    ]
    out = render_plan_dag(steps)
    assert out == "alpha\nmiddle\nzebra"


# ===== Warnings: unknown dep, duplicates, cycles =====


def test_warning_for_unknown_dep():
    steps = [
        {"name": "a", "depends_on": [], "action": "do_step"},
        {"name": "b", "depends_on": ["nonexistent"], "action": "do_step"},
    ]
    out = render_plan_dag(steps)
    # Unknown dep is flagged in the warning block, and 'b' has no
    # parent in children, so it appears as a top-level root.
    assert "⚠" in out
    assert "nonexistent" in out
    # The DAG still renders what it can:
    assert "a" in out
    assert "b" in out


def test_warning_for_duplicate_step_name():
    steps = [
        {"name": "dup", "depends_on": [], "action": "do_step"},
        {"name": "dup", "depends_on": [], "action": "do_step"},  # duplicate
        {"name": "other", "depends_on": [], "action": "do_step"},
    ]
    out = render_plan_dag(steps)
    assert "⚠" in out
    assert "duplicate" in out
    assert "dup" in out
    assert "other" in out


def test_warning_for_cycle():
    """A → B → A: both are roots of cycles, render still produces output."""
    steps = [
        {"name": "a", "depends_on": ["b"], "action": "do_step"},
        {"name": "b", "depends_on": ["a"], "action": "do_step"},
    ]
    out = render_plan_dag(steps)
    assert "⚠" in out
    assert "cycle" in out
    # Both step names should still appear
    assert "a" in out
    assert "b" in out


# ===== show_agent_role toggle =====


def test_show_agent_role_appends_role_in_parens():
    steps = [
        {"name": "fetch", "agent_role": "super", "action": "do_step", "depends_on": []},
        {"name": "parse", "agent_role": "win-agent01", "action": "do_step", "depends_on": ["fetch"]},
    ]
    out = render_plan_dag(steps, show_agent_role=True)
    expected = (
        "fetch  (super)\n"
        "└─ parse  (win-agent01)"
    )
    assert out == expected


def test_show_agent_role_false_default():
    steps = [
        {"name": "fetch", "agent_role": "super", "action": "do_step", "depends_on": []},
    ]
    out = render_plan_dag(steps)  # default False
    assert "super" not in out
    assert "fetch" in out


# ===== Accepts both dicts and objects =====


def test_accepts_dicts_and_objects():
    """The renderer must accept both Pydantic-like objects and dicts.
    This lets the LLM render its in-memory draft (dicts) AND the
    validated ProjectPlan (Pydantic objects) with the same code."""

    class FakeStep:
        def __init__(self, name, depends_on, agent_role=""):
            self.name = name
            self.depends_on = depends_on
            self.agent_role = agent_role

    steps = [
        FakeStep("alpha", []),
        FakeStep("beta", ["alpha"], agent_role="super"),
    ]
    out = render_plan_dag(steps)
    assert out == "alpha\n└─ beta"


# ===== Empty depends_on vs missing key =====


def test_step_with_no_depends_on_attribute_treated_as_root():
    """Defensive: a step without depends_on is a root (no children relation)."""

    class BareStep:
        def __init__(self, name):
            self.name = name

    steps = [BareStep("lonely"), BareStep("solo")]
    out = render_plan_dag(steps)
    assert out == "lonely\nsolo"
