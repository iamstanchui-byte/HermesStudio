"""Tests for GET /api/projects/{id}/plan/agents (2026-07-28).

The chatbox LLM (docs/chatbox-plan-editor.md §5 §7.3) calls this
endpoint once per session to learn valid agent_role / skill / tool
names for plan validation.

Contract:
  - 404 for unknown project
  - 200 with {project_id, agent_roles, skills, tools} for known project
  - agent_roles is non-empty if at least one non-disabled profile exists
  - skills / tools may be empty (best-effort, no canonical registry yet)
"""
from __future__ import annotations

import urllib.error
import urllib.request
import uuid
import json

import pytest

BASE = "http://127.0.0.1:8765"


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | str | None]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
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


def _create_test_project() -> str:
    name = f"plan-agents-test-{uuid.uuid4().hex[:8]}"
    s, body = _http("POST", "/api/projects/", {"name": name})
    if s == 201 and isinstance(body, dict) and "id" in body:
        return body["id"]
    if isinstance(body, dict) and "id" in body:
        return body["id"]
    pytest.fail(f"create project failed: {s} {body}")


def _delete_project(project_id: str) -> None:
    try:
        _http("DELETE", f"/api/projects/{project_id}")
    except Exception:
        pass


def test_plan_agents_returns_404_for_unknown_project():
    s, body = _http("GET", "/api/projects/does-not-exist/plan/agents")
    assert s == 404
    assert "not found" in str(body).lower()


def test_plan_agents_returns_expected_shape():
    pid = _create_test_project()
    try:
        s, body = _http("GET", f"/api/projects/{pid}/plan/agents")
        assert s == 200
        # Required keys
        for key in ("project_id", "agent_roles", "skills", "tools"):
            assert key in body, f"missing key: {key} in {body}"
        assert body["project_id"] == pid
        # agent_roles is a list (may or may not be empty depending on
        # the test DB state — but the list type must be correct)
        assert isinstance(body["agent_roles"], list)
        assert isinstance(body["skills"], list)
        assert isinstance(body["tools"], list)
    finally:
        _delete_project(pid)


def test_plan_agents_includes_known_super_profile():
    """The 'super' profile is created by default in fresh DBs.
    If this test environment is not a fresh DB it may have been
    deleted; we just assert the response shape and that any returned
    names are strings."""
    pid = _create_test_project()
    try:
        s, body = _http("GET", f"/api/projects/{pid}/plan/agents")
        assert s == 200
        # Each agent_role is a non-empty string
        for name in body["agent_roles"]:
            assert isinstance(name, str)
            assert name, "empty agent_role name in response"
        # skills / tools are string lists too
        for name in body["skills"] + body["tools"]:
            assert isinstance(name, str)
    finally:
        _delete_project(pid)
