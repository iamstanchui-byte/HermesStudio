"""Tests for the v2.1 parameter hints endpoint.

POST /api/workflows/{id}/suggest-vars extracts {{var}}
placeholders from step params and infers types from literal
values. These tests cover the pure helpers + an end-to-end
call through the FastAPI app.

The helpers are pure (no DB needed). The endpoint test
needs a live server (uses TestClient).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import pytest


DB = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"


# ===== pure helper tests (no server needed) =====


def test_extract_placeholders_in_simple_string():
    from hermes_orch.api.workflows import _extract_placeholders_in_value
    out = set()
    _extract_placeholders_in_value("{{foo}} and {{bar}}", out)
    assert out == {"foo", "bar"}


def test_extract_placeholders_handles_whitespace():
    from hermes_orch.api.workflows import _extract_placeholders_in_value
    out = set()
    _extract_placeholders_in_value("{{ foo }} {{  bar  }}", out)
    assert out == {"foo", "bar"}


def test_extract_placeholders_nested_dict_list():
    from hermes_orch.api.workflows import _extract_placeholders_in_value
    out = set()
    _extract_placeholders_in_value(
        {"a": "{{x}}", "b": ["{{y}}", "no_var", {"nested": "{{z}}"}]},
        out,
    )
    assert out == {"x", "y", "z"}


def test_extract_placeholders_none_safe():
    from hermes_orch.api.workflows import _extract_placeholders_in_value
    out = set()
    _extract_placeholders_in_value(None, out)
    _extract_placeholders_in_value(42, out)
    _extract_placeholders_in_value([1, 2, 3], out)
    assert out == set()


def test_infer_type_int():
    from hermes_orch.api.workflows import _infer_type_from_literal
    assert _infer_type_from_literal("42") == "int"
    assert _infer_type_from_literal("-7") == "int"
    assert _infer_type_from_literal("0") == "int"


def test_infer_type_float():
    from hermes_orch.api.workflows import _infer_type_from_literal
    assert _infer_type_from_literal("3.14") == "float"
    assert _infer_type_from_literal("-2.5") == "float"


def test_infer_type_bool():
    from hermes_orch.api.workflows import _infer_type_from_literal
    assert _infer_type_from_literal("true") == "bool"
    assert _infer_type_from_literal("false") == "bool"
    assert _infer_type_from_literal("True") == "bool"


def test_infer_type_list_string():
    from hermes_orch.api.workflows import _infer_type_from_literal
    assert _infer_type_from_literal("AAPL,GOOG,MSFT") == "list[string]"
    assert _infer_type_from_literal("a,b") == "list[string]"


def test_infer_type_string_fallback():
    from hermes_orch.api.workflows import _infer_type_from_literal
    assert _infer_type_from_literal("hello") == "string"
    assert _infer_type_from_literal("bus 87d") == "string"


def test_collect_literal_values_for_placeholder():
    from hermes_orch.api.workflows import _collect_literal_values_for_placeholder
    step = {
        "name": "fetch",
        "params_template": {
            "symbols": "{{tickers}}",
            "interval": "1d",
            "fallback": "AAPL,GOOG",
        },
    }
    out = _collect_literal_values_for_placeholder(step, "tickers")
    # We collect literal values from keys OTHER than the one
    # containing the placeholder.
    assert "1d" in out
    assert "AAPL,GOOG" in out
    assert out[0] in ("1d", "AAPL,GOOG")


# ===== end-to-end endpoint test (needs live server) =====


def test_suggest_vars_endpoint_returns_extracted_placeholders():
    """End-to-end: insert a workflow with {{var}} placeholders
    directly into the DB (the API doesn't have a POST /workflows
    endpoint — workflows are created via promote-from-project
    or by the test fixture below), then call the suggest-vars
    endpoint and verify the response shape.
    """
    import urllib.request, urllib.error

    wf_id = f"wf-suggest-{uuid.uuid4().hex[:6]}"
    step_template = [
        {
            "name": "fetch-stocks",
            "agent_role": "r1",
            "action": "fetch stock data",
            "depends_on": [],
            "params_template": {
                "symbols": "{{tickers}}",
                "interval": "1d",
            },
            "feedback_to": [],
            "output_path": "",
            "skill": "",
            "required_capability": "",
        },
        {
            "name": "analyze",
            "agent_role": "r1",
            "action": "analyze",
            "depends_on": ["fetch-stocks"],
            "params_template": {
                # {{lookback_days}} placeholder, with a literal
                # sibling in the same step ("30") so the helper
                # can infer type=int.
                "lookback": "{{lookback_days}}",
                "default_period": "30",
            },
            "feedback_to": [],
            "output_path": "",
            "skill": "",
            "required_capability": "",
        },
    ]
    variables = []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(str(DB))
    try:
        conn.execute(
            "INSERT INTO workflow_packages "
            "(id, name, description, version, step_template, variables, "
            " source_project_id, visual_layout, created_at, updated_at) "
            "VALUES (?, ?, '', '0.1.0', ?, ?, NULL, '{}', ?, ?)",
            (wf_id, f"suggest-test-{wf_id}", json.dumps(step_template),
             json.dumps(variables), now, now),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        # Now call suggest-vars
        req2 = urllib.request.Request(
            f"http://127.0.0.1:8765/api/workflows/{wf_id}/suggest-vars",
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req2, timeout=15) as r:
            data = json.loads(r.read())
        assert data["workflow_id"] == wf_id
        suggestions = data["suggestions"]
        # Should find both `tickers` and `lookback_days`.
        names = {s["name"] for s in suggestions}
        assert "tickers" in names
        assert "lookback_days" in names
        # Type inference
        by_name = {s["name"]: s for s in suggestions}
        # tickers is used only in fetch-stocks (no default in that
        # step) → type falls back to "string" (no literal siblings
        # to infer from).
        assert by_name["tickers"]["type"] == "string"
        # lookback_days has sibling literal "30" in analyze →
        # type=int.
        assert by_name["lookback_days"]["type"] == "int"
        # used_in list is correct
        assert "fetch-stocks" in by_name["tickers"]["used_in"]
        assert "analyze" in by_name["lookback_days"]["used_in"]
    finally:
        # Cleanup
        conn = sqlite3.connect(str(DB))
        try:
            conn.execute("DELETE FROM workflow_packages WHERE id = ?", (wf_id,))
            conn.commit()
        finally:
            conn.close()


def test_suggest_vars_marks_already_defined():
    """Variables that already exist in the workflow's variables
    list should be marked with already_defined=True so the UI
    doesn't double-add them."""
    import urllib.request, urllib.error

    wf_id = f"wf-suggest-already-{uuid.uuid4().hex[:6]}"
    step_template = [
        {
            "name": "step1",
            "agent_role": "r1",
            "action": "x",
            "depends_on": [],
            "params_template": {"k": "{{my_var}}"},
            "feedback_to": [],
        },
    ]
    variables = [
        {"name": "my_var", "type": "string", "default": "hello", "required": True},
    ]
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(str(DB))
    try:
        conn.execute(
            "INSERT INTO workflow_packages "
            "(id, name, description, version, step_template, variables, "
            " source_project_id, visual_layout, created_at, updated_at) "
            "VALUES (?, ?, '', '0.1.0', ?, ?, NULL, '{}', ?, ?)",
            (wf_id, f"suggest-already-{wf_id}", json.dumps(step_template),
             json.dumps(variables), now, now),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        req2 = urllib.request.Request(
            f"http://127.0.0.1:8765/api/workflows/{wf_id}/suggest-vars",
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req2, timeout=15) as r:
            data = json.loads(r.read())
        assert len(data["suggestions"]) == 1
        s = data["suggestions"][0]
        assert s["name"] == "my_var"
        assert s["already_defined"] is True
    finally:
        conn = sqlite3.connect(str(DB))
        try:
            conn.execute("DELETE FROM workflow_packages WHERE id = ?", (wf_id,))
            conn.commit()
        finally:
            conn.close()


def test_suggest_vars_handles_workflow_with_no_placeholders():
    """A workflow with no {{var}} placeholders should return an
    empty suggestions list (not an error)."""
    import urllib.request, urllib.error

    wf_id = f"wf-suggest-empty-{uuid.uuid4().hex[:6]}"
    step_template = [
        {
            "name": "step1",
            "agent_role": "r1",
            "action": "x",
            "depends_on": [],
            "params_template": {"static": "literal_value"},
            "feedback_to": [],
        },
    ]
    variables = []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(str(DB))
    try:
        conn.execute(
            "INSERT INTO workflow_packages "
            "(id, name, description, version, step_template, variables, "
            " source_project_id, visual_layout, created_at, updated_at) "
            "VALUES (?, ?, '', '0.1.0', ?, ?, NULL, '{}', ?, ?)",
            (wf_id, f"suggest-empty-{wf_id}", json.dumps(step_template),
             json.dumps(variables), now, now),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        req2 = urllib.request.Request(
            f"http://127.0.0.1:8765/api/workflows/{wf_id}/suggest-vars",
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req2, timeout=15) as r:
            data = json.loads(r.read())
        assert data["suggestions"] == []
        assert data["already_defined_count"] == 0
    finally:
        conn = sqlite3.connect(str(DB))
        try:
            conn.execute("DELETE FROM workflow_packages WHERE id = ?", (wf_id,))
            conn.commit()
        finally:
            conn.close()


def test_suggest_vars_404_for_unknown_workflow():
    """Unknown workflow id returns 404 (not 500)."""
    import urllib.request, urllib.error
    req = urllib.request.Request(
        f"http://127.0.0.1:8765/api/workflows/wf-does-not-exist-{uuid.uuid4().hex[:6]}/suggest-vars",
        method="POST",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=15)
    assert exc.value.code == 404
