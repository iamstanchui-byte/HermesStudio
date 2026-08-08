# coding: utf-8
"""Tests for v1.0.1 starter catalog core (loading + parsing).

Tests `core/starters.py`: load_catalog() + Starter shape.
The API + clone flow is in test_starters_api.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_orch.core.starters import (
    SERVER_HEALTHCHECK_ACTION,
    Starter,
    StarterDisplay,
    load_catalog,
)


# ===== load_catalog =====

def test_load_catalog_returns_4_starters():
    """The bundled catalog has exactly 4 starters per spec §3.4."""
    catalog = load_catalog()
    assert len(catalog) == 4, f"expected 4 starters, got {len(catalog)}: {list(catalog)}"
    assert set(catalog.keys()) == {
        "research-brief", "system-health", "backtest-stater", "daily-monitor",
    }


def test_each_starter_has_required_fields():
    """All starters have name, version, display, step_template, variables."""
    for name, s in load_catalog().items():
        assert isinstance(s, Starter)
        assert s.name == name
        assert s.version  # non-empty
        assert isinstance(s.display, StarterDisplay)
        assert s.display.title  # non-empty
        assert s.display.description  # non-empty
        assert s.display.icon  # non-empty
        assert isinstance(s.step_template, list)
        assert len(s.step_template) >= 1
        assert isinstance(s.variables, list)


def test_system_health_starter_uses_healthcheck_action():
    """Spec §3.5: system-health is the one with the magic _server_healthcheck action."""
    s = load_catalog()["system-health"]
    actions = [step.get("action") for step in s.step_template]
    assert SERVER_HEALTHCHECK_ACTION in actions, (
        f"system-health should have {SERVER_HEALTHCHECK_ACTION!r} step action, "
        f"got {actions}"
    )


def test_other_starters_do_not_use_healthcheck_action():
    """The magic action is reserved for the system-health starter only."""
    for name in ("research-brief", "backtest-stater", "daily-monitor"):
        s = load_catalog()[name]
        for step in s.step_template:
            assert step.get("action") != SERVER_HEALTHCHECK_ACTION, (
                f"{name} should not have {SERVER_HEALTHCHECK_ACTION!r} step action"
            )


def test_each_starter_step_has_action_and_agent_role():
    """Every step in step_template has at least an action + agent_role."""
    for name, s in load_catalog().items():
        for i, step in enumerate(s.step_template):
            assert "action" in step, f"{name} step {i} missing action"
            assert "agent_role" in step, f"{name} step {i} missing agent_role"
            assert "depends_on" in step, f"{name} step {i} missing depends_on"


def test_variables_have_name_and_type():
    """Each variable in the variables array has name + type (required fields)."""
    for name, s in load_catalog().items():
        for var in s.variables:
            assert "name" in var, f"{name}: variable missing name: {var}"
            assert "type" in var, f"{name}: variable {var.get('name')} missing type"


def test_research_brief_requires_topic():
    """The research-brief starter requires the `topic` variable."""
    s = load_catalog()["research-brief"]
    topic_vars = [v for v in s.variables if v.get("name") == "topic"]
    assert len(topic_vars) == 1
    assert topic_vars[0].get("required") is True


def test_backtest_starter_optional_strategy():
    """The backtest-stater has a `strategy` variable with a default + not required."""
    s = load_catalog()["backtest-stater"]
    strategy_vars = [v for v in s.variables if v.get("name") == "strategy"]
    assert len(strategy_vars) == 1
    assert strategy_vars[0].get("required", False) is False
    assert "default" in strategy_vars[0]


# ===== Starter summary/detail dicts =====

def test_to_summary_dict_excludes_template():
    """Summary dict (list view) must NOT include step_template / variables
    (those are big; the list view is for the gallery cards)."""
    s = load_catalog()["research-brief"]
    summary = s.to_summary_dict()
    assert "step_template" not in summary
    assert "variables" not in summary
    # But the user-facing display fields are there
    for k in ("name", "version", "title", "description", "icon",
              "category", "mock_mode_supported", "estimated_minutes"):
        assert k in summary, f"summary missing {k!r}"


def test_to_detail_dict_includes_template():
    """Detail dict (single view) DOES include step_template + variables."""
    s = load_catalog()["research-brief"]
    detail = s.to_detail_dict()
    assert "step_template" in detail
    assert "variables" in detail
    assert "required_capability" in detail


# ===== Catalog dir / packaging =====

def test_catalog_dir_path_is_sensible():
    """The catalog dir is at src/hermes_orch/starters/."""
    from hermes_orch.core.starters import _catalog_dir
    p = _catalog_dir()
    assert p.name == "starters"
    assert p.parent.name == "hermes_orch"
    # And it actually exists
    assert p.exists(), f"catalog dir missing: {p}"
    # And it has YAML files
    yamls = list(p.glob("*.yaml"))
    assert len(yamls) >= 4, f"expected >=4 YAMLs in {p}, got {len(yamls)}"
