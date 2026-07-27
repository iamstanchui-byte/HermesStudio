"""Tests for POST /api/projects/{id}/plan/from-llm (Phase D, 2026-07-27).

The LLM-driven plan generator. The user enters a goal, the LLM returns
a draft plan_json (NOT tasks). The user reviews the plan in the visual
editor and clicks "Save" to persist.

Covers:
  - 404 for unknown project
  - 400 if goal is blank and project has no goal
  - 200 returns ProjectPlanResponse with has_plan=True + non-empty steps
  - 200 step names are kebab-case (Pydantic-validated)
  - 200 the returned plan is NOT saved to DB (GET /plan still null)
  - 200 mock mode returns a usable 2-step plan (deterministic, no LLM call)
  - name_suffix appends to each step name
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

import os
BASE = os.environ.get("HERMES_TEST_BASE", "http://127.0.0.1:8765")
DB_PATH = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"

_KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | str | None]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
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


def _create_test_project(name_suffix: str = "") -> str:
    """Create a fresh test project. Optionally pre-set a goal."""
    name = f"from-llm-test-{uuid.uuid4().hex[:8]}"
    body = {"name": name}
    if name_suffix:
        body["goal"] = name_suffix
    s, resp = _http("POST", "/api/projects/", body)
    if s == 201 and isinstance(resp, dict) and "id" in resp:
        return resp["id"]
    if isinstance(resp, dict) and "id" in resp:
        return resp["id"]
    pytest.fail(f"create project failed: {s} {resp}")


def _delete_project(project_id: str) -> None:
    try:
        _http("DELETE", f"/api/projects/{project_id}")
    except Exception:
        pass


# ===== Basic shape =====


def test_from_llm_404_for_unknown_project():
    s, body = _http(
        "POST",
        "/api/projects/does-not-exist/plan/from-llm",
        {"goal": "test"},
    )
    assert s == 404
    assert "not found" in str(body).lower()


def test_from_llm_400_if_no_goal_and_project_has_none():
    """If body.goal is empty AND project.goal is empty, return 400."""
    pid = _create_test_project(name_suffix="")  # no goal
    try:
        s, body = _http("POST", f"/api/projects/{pid}/plan/from-llm", {"goal": ""})
        assert s == 400
        assert "goal" in str(body).lower()
    finally:
        _delete_project(pid)


def test_from_llm_400_if_no_goal_anywhere():
    """Edge case: body.goal is missing entirely + project.goal is None."""
    pid = _create_test_project(name_suffix="")
    try:
        s, body = _http("POST", f"/api/projects/{pid}/plan/from-llm", {})
        assert s == 400
    finally:
        _delete_project(pid)


# ===== Success cases =====


def test_from_llm_returns_plan_with_steps():
    """Happy path: returns has_plan=True, plan.steps has >= 1 step."""
    pid = _create_test_project(name_suffix="Write a daily report on Hong Kong weather")
    try:
        s, body = _http(
            "POST",
            f"/api/projects/{pid}/plan/from-llm",
            {"goal": "Write a daily report on Hong Kong weather"},
        )
        assert s == 200, f"got {s} body={body}"
        assert body["has_plan"] is True
        assert body["plan"] is not None
        assert isinstance(body["plan"]["steps"], list)
        assert len(body["plan"]["steps"]) >= 1
    finally:
        _delete_project(pid)


def test_from_llm_step_names_are_kebab_case():
    """All step names must match ^[a-z0-9]+(-[a-z0-9]+)*$ (PlanStep validator)."""
    pid = _create_test_project(name_suffix="Research Apple's Q3 2026 earnings and write a summary")
    try:
        s, body = _http(
            "POST",
            f"/api/projects/{pid}/plan/from-llm",
            {"goal": "Research Apple's Q3 2026 earnings and write a summary"},
        )
        assert s == 200
        for step in body["plan"]["steps"]:
            assert _KEBAB_RE.match(step["name"]), (
                f"step name not kebab-case: {step['name']!r}"
            )
    finally:
        _delete_project(pid)


def test_from_llm_does_not_save_to_db():
    """CRITICAL: from-llm returns a plan but does NOT persist it.
    GET /plan afterwards should still be has_plan=False.
    The user has to click Save to commit."""
    pid = _create_test_project(name_suffix="Test plan persistence")
    try:
        # Confirm pre-state
        s, pre = _http("GET", f"/api/projects/{pid}/plan")
        assert s == 200
        assert pre["has_plan"] is False
        # Call from-llm
        s, body = _http(
            "POST",
            f"/api/projects/{pid}/plan/from-llm",
            {"goal": "Test plan persistence"},
        )
        assert s == 200
        assert body["has_plan"] is True  # response has plan
        # But DB should NOT have it
        s, post = _http("GET", f"/api/projects/{pid}/plan")
        assert s == 200
        assert post["has_plan"] is False, (
            "from-llm persisted the plan! should be review-then-save only"
        )
    finally:
        _delete_project(pid)


def test_from_llm_uses_project_goal_when_body_empty():
    """If body.goal is empty but project.goal is set, use the project's."""
    pid = _create_test_project(name_suffix="Use this project goal")
    try:
        s, body = _http("POST", f"/api/projects/{pid}/plan/from-llm", {"goal": ""})
        assert s == 200, f"got {s} body={body}"
        assert body["has_plan"] is True
        assert len(body["plan"]["steps"]) >= 1
    finally:
        _delete_project(pid)


def test_from_llm_name_suffix_appends_to_step_names():
    """name_suffix is appended to each step name. Useful for versioning."""
    pid = _create_test_project(name_suffix="Run a quick analysis")
    try:
        s, body = _http(
            "POST",
            f"/api/projects/{pid}/plan/from-llm",
            {"goal": "Run a quick analysis", "name_suffix": "-v2"},
        )
        assert s == 200
        for step in body["plan"]["steps"]:
            assert step["name"].endswith("-v2"), (
                f"step name {step['name']!r} should end with -v2"
            )
    finally:
        _delete_project(pid)


# ===== Mock mode =====


def test_from_llm_mock_mode_returns_deterministic_plan():
    """In mock mode (no API key), the planner returns a fixed 2-step plan.
    This is good enough for UX testing — user can edit on canvas.
    The test verifies the endpoint handles mock mode without crashing."""
    pid = _create_test_project(name_suffix="Mock mode test")
    try:
        s, body = _http(
            "POST",
            f"/api/projects/{pid}/plan/from-llm",
            {"goal": "Mock mode test"},
        )
        # Either real LLM or mock — both should return 200 with a plan
        assert s == 200, f"got {s} body={body}"
        assert body["has_plan"] is True
        # Plan has a description that mentions LLM
        desc = body["plan"].get("description", "")
        assert "LLM" in desc or "Generated" in desc
    finally:
        _delete_project(pid)
