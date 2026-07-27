"""Tests for the Project Plan layer (Phase A foundation, 2026-07-27).

Covers:
  - GET /api/projects/{id}/plan returns 404 for unknown project
  - GET returns has_plan=False, plan=None for projects with NULL plan_json
  - PUT creates a plan (validates the Pydantic model)
  - GET returns the plan after PUT
  - PUT rejects invalid plans (Pydantic 422)
  - PUT rejects non-kebab-case step names
  - PUT rejects duplicate step names
  - PUT rejects duplicate variable names
  - DELETE clears the plan (back to NULL)
  - DELETE on already-cleared is idempotent
  - Idempotent PUT (writing same plan twice)
  - The /api/projects/{id} endpoint still works (no regression)
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

BASE = "http://127.0.0.1:8765"
DB_PATH = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"


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


def _create_test_project() -> str:
    """Create a fresh test project (will have NULL plan_json by default)."""
    name = f"plan-test-{uuid.uuid4().hex[:8]}"
    s, body = _http("POST", "/api/projects/", {"name": name})
    if s == 201:
        return body["id"]
    # POST returned the project directly (not wrapped)
    if isinstance(body, dict) and "id" in body:
        return body["id"]
    pytest.fail(f"create project failed: {s} {body}")


def _delete_project(project_id: str) -> None:
    """Best-effort cleanup. Failures are OK (we're tearing down tests)."""
    try:
        _http("DELETE", f"/api/projects/{project_id}")
    except Exception:
        pass


# ===== GET /api/projects/{id}/plan =====


def test_get_plan_for_unknown_project_returns_404():
    s, body = _http("GET", "/api/projects/does-not-exist/plan")
    assert s == 404
    assert "not found" in str(body).lower()


def test_get_plan_returns_null_for_new_project():
    """A fresh project (no plan) should return has_plan=False, plan=None."""
    pid = _create_test_project()
    try:
        s, body = _http("GET", f"/api/projects/{pid}/plan")
        assert s == 200
        assert body["has_plan"] is False
        assert body["plan"] is None
    finally:
        _delete_project(pid)


# ===== PUT /api/projects/{id}/plan =====


def test_put_plan_creates_new_plan():
    """PUT on a project with no plan should set it."""
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "test-plan",
            "description": "test plan",
            "trigger": "manual",
            "variables": [
                {"name": "report_date", "type": "date", "default": "today", "description": "report date"},
            ],
            "steps": [
                {
                    "name": "fetch-data",
                    "agent_role": "super",
                    "action": "fetch",
                    "skill": "",
                    "tool": "",
                    "required_capability": "",
                    "depends_on": [],
                    "params_template": {"url": "https://example.com"},
                    "output_path": "",
                },
                {
                    "name": "summarize",
                    "agent_role": "super",
                    "action": "summarize",
                    "depends_on": ["fetch-data"],
                    "params_template": {},
                },
            ],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 200
        assert body["has_plan"] is True
        assert body["plan"]["name"] == "test-plan"
        assert len(body["plan"]["steps"]) == 2
        assert body["plan"]["steps"][0]["name"] == "fetch-data"
        # Verify GET returns the same plan
        s2, body2 = _http("GET", f"/api/projects/{pid}/plan")
        assert s2 == 200
        assert body2["plan"]["name"] == "test-plan"
    finally:
        _delete_project(pid)


def test_put_plan_rejects_non_kebab_step_name():
    """Step names must be kebab-case (validator contract)."""
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "test",
            "steps": [
                {
                    "name": "Not Kebab Case",  # invalid
                    "agent_role": "super",
                },
            ],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 422
        # Pydantic returns a list of validation errors
        assert "kebab" in str(body).lower() or "pattern" in str(body).lower()
    finally:
        _delete_project(pid)


def test_put_plan_rejects_duplicate_step_names():
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "test",
            "steps": [
                {"name": "fetch", "agent_role": "super"},
                {"name": "fetch", "agent_role": "super-b"},  # duplicate
            ],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 422
        assert "unique" in str(body).lower()
    finally:
        _delete_project(pid)


def test_put_plan_rejects_duplicate_variable_names():
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "test",
            "variables": [
                {"name": "report_date", "type": "date"},
                {"name": "report_date", "type": "string"},  # duplicate
            ],
            "steps": [],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 422
        assert "unique" in str(body).lower()
    finally:
        _delete_project(pid)


def test_put_plan_rejects_non_kebab_plan_name():
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "Not Kebab",  # invalid
            "steps": [],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 422
    finally:
        _delete_project(pid)


def test_put_plan_allows_empty_steps():
    """Empty plan ({steps: []}) is valid — means 'no plan yet, switch to direct mode'."""
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "empty-test",
            "steps": [],
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 200
        assert body["has_plan"] is True
        assert body["plan"]["steps"] == []
    finally:
        _delete_project(pid)


def test_put_plan_404_for_unknown_project():
    plan = {"version": "1.0", "name": "test", "steps": []}
    s, body = _http("PUT", "/api/projects/does-not-exist/plan", {"plan": plan})
    assert s == 404


def test_put_plan_is_idempotent():
    """Writing the same plan twice should produce the same result."""
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "idempotent-test",
            "steps": [{"name": "step-1", "agent_role": "super"}],
        }
        s1, _ = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        s2, body2 = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s1 == 200
        assert s2 == 200
        assert body2["plan"]["name"] == "idempotent-test"
    finally:
        _delete_project(pid)


# ===== DELETE /api/projects/{id}/plan =====


def test_delete_plan_clears_back_to_null():
    pid = _create_test_project()
    try:
        # First, set a plan
        plan = {"version": "1.0", "name": "to-be-cleared", "steps": [{"name": "x"}]}
        s, _ = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 200
        # Verify has_plan=True
        s, body = _http("GET", f"/api/projects/{pid}/plan")
        assert body["has_plan"] is True
        # Delete
        s, _ = _http("DELETE", f"/api/projects/{pid}/plan")
        assert s == 204
        # Verify has_plan=False
        s, body = _http("GET", f"/api/projects/{pid}/plan")
        assert body["has_plan"] is False
        assert body["plan"] is None
    finally:
        _delete_project(pid)


def test_delete_plan_is_idempotent_on_already_cleared():
    pid = _create_test_project()
    try:
        # Project has NULL plan_json (default). DELETE should be a no-op.
        s, _ = _http("DELETE", f"/api/projects/{pid}/plan")
        assert s == 204  # idempotent
    finally:
        _delete_project(pid)


def test_delete_plan_404_for_unknown_project():
    s, body = _http("DELETE", "/api/projects/does-not-exist/plan")
    assert s == 404


# ===== Audit log integration =====


def test_put_plan_writes_audit_log():
    """PUT /plan should emit a project.plan.updated audit event."""
    pid = _create_test_project()
    try:
        plan = {"version": "1.0", "name": "audit-test", "steps": [
            {"name": "step-a"},
            {"name": "step-b"},
            {"name": "step-c"},
        ]}
        s, _ = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 200
        # Check audit log
        conn = sqlite3.connect(str(DB_PATH))
        try:
            rows = conn.execute(
                "SELECT event_type, project_id, payload FROM audit_log "
                "WHERE project_id = ? AND event_type = 'project.plan.updated' "
                "ORDER BY created_at DESC LIMIT 1",
                (pid,),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1, "expected exactly one plan.updated audit event"
        event_type, project_id, payload_raw = rows[0]
        assert event_type == "project.plan.updated"
        assert project_id == pid
        import json as _json
        payload = _json.loads(payload_raw) if payload_raw else {}
        assert payload.get("name") == "audit-test"
        assert payload.get("step_count") == 3
    finally:
        _delete_project(pid)


# ===== Regression: existing endpoints still work =====


def test_existing_project_endpoint_still_works():
    """The plan layer must not break /api/projects/{id} or other
    existing endpoints."""
    pid = _create_test_project()
    try:
        s, body = _http("GET", f"/api/projects/{pid}")
        assert s == 200
        assert body["id"] == pid
        # No plan yet — fields are None / default
        assert body.get("state")  # has a state
    finally:
        _delete_project(pid)
