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


def _http(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict | list | str | None]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    final_headers = {"Content-Type": "application/json"} if data else {}
    if headers:
        final_headers.update(headers)
    req = urllib.request.Request(
        url, data=data, method=method,
        headers=final_headers,
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
    s, body = _http("POST", "/api/projects/", {"name": name, "action": "do_step"})
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
                {"name": "report_date", "type": "date", "default": "today", "description": "report date", "action": "do_step"},
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
                {"name": "fetch", "agent_role": "super", "action": "do_step"},
                {"name": "fetch", "agent_role": "super-b", "action": "do_step"},  # duplicate
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
                {"name": "report_date", "type": "date", "action": "do_step"},
                {"name": "report_date", "type": "string", "action": "do_step"},  # duplicate
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
            "steps": [{"name": "step-1", "agent_role": "super", "action": "do_step"}],
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
        plan = {"version": "1.0", "name": "to-be-cleared", "steps": [{"name": "x", "action": "do_step"}]}
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
            {"name": "step-a", "action": "do_step"},
            {"name": "step-b", "action": "do_step"},
            {"name": "step-c", "action": "do_step"},
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


# ===== Optimistic lock (If-Match header, 2026-07-28, chatbox contract) =====
#
# Contract (docs/chatbox-plan-editor.md §7.1):
#   - If-Match: <updated_at> is OPTIONAL (backward compat with Phase C visual editor)
#   - When plan_json is NULL (first PUT): If-Match is ignored
#   - When plan_json is non-NULL + If-Match provided + matches: write proceeds
#   - When plan_json is non-NULL + If-Match provided + mismatches: 409 with
#     current_plan in detail body so the client can 3-way merge
#   - When plan_json is non-NULL + If-Match omitted: write proceeds (legacy)


def test_first_put_without_if_match_succeeds():
    """First PUT (no plan yet) should succeed even without If-Match header."""
    pid = _create_test_project()
    try:
        plan = {"version": "1.0", "name": "first", "steps": [{"name": "a", "action": "do_step"}]}
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 200
        assert body["has_plan"] is True
        assert body["updated_at"] is not None
    finally:
        _delete_project(pid)


def test_put_without_if_match_succeeds_when_plan_exists():
    """Backward compat: PUT without If-Match still works when a plan exists.
    The visual editor (Phase C) does not send If-Match; this must not break."""
    pid = _create_test_project()
    try:
        plan = {"version": "1.0", "name": "first", "steps": [{"name": "a", "action": "do_step"}]}
        s, _ = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 200
        # Second PUT without If-Match should still succeed
        plan2 = {"version": "1.0", "name": "second", "steps": [{"name": "b", "action": "do_step"}]}
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan2})
        assert s == 200
        assert body["plan"]["name"] == "second"
    finally:
        _delete_project(pid)


def test_put_with_matching_if_match_succeeds():
    """PUT with correct If-Match (current updated_at) should succeed."""
    pid = _create_test_project()
    try:
        plan = {"version": "1.0", "name": "v1", "steps": [{"name": "a", "action": "do_step"}]}
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 200
        current_uat = body["updated_at"]
        # Second PUT echoing the updated_at we just got
        plan2 = {"version": "1.0", "name": "v2", "steps": [{"name": "a", "action": "do_step"}, {"name": "b", "action": "do_step"}]}
        s, body = _http(
            "PUT",
            f"/api/projects/{pid}/plan",
            {"plan": plan2},
            headers={"If-Match": current_uat},
        )
        assert s == 200
        assert body["plan"]["name"] == "v2"
        # updated_at should be fresh
        assert body["updated_at"] != current_uat
    finally:
        _delete_project(pid)


def test_put_with_stale_if_match_returns_409():
    """PUT with stale If-Match returns 409 with current_plan in detail body."""
    pid = _create_test_project()
    try:
        # 1. First write
        plan_v1 = {"version": "1.0", "name": "v1", "steps": [{"name": "a", "action": "do_step"}]}
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan_v1})
        assert s == 200
        # 2. Second write (changes the plan and updated_at)
        plan_v2 = {"version": "1.0", "name": "v2", "steps": [{"name": "b", "action": "do_step"}]}
        s, _ = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan_v2})
        assert s == 200
        # 3. Third write with STALE If-Match (the v1 updated_at)
        plan_v3 = {"version": "1.0", "name": "v3", "steps": [{"name": "c", "action": "do_step"}]}
        s, body = _http(
            "PUT",
            f"/api/projects/{pid}/plan",
            {"plan": plan_v3},
            headers={"If-Match": "2020-01-01T00:00:00+00:00"},  # stale
        )
        assert s == 409
        # FastAPI wraps the detail in {"detail": {...}}
        detail = body.get("detail") if isinstance(body, dict) else None
        assert detail is not None, f"expected detail in 409 body, got: {body!r}"
        assert detail.get("error") == "plan was modified since you last read it"
        assert detail.get("your_if_match") == "2020-01-01T00:00:00+00:00"
        assert detail.get("current_updated_at") is not None
        # current_plan must reflect the v2 plan (the last successful write)
        cp = detail.get("current_plan")
        assert cp is not None
        assert cp["name"] == "v2"
    finally:
        _delete_project(pid)


def test_put_with_if_match_ignored_when_plan_is_null():
    """If-Match on a project with no plan yet is ignored (no prior state to lock)."""
    pid = _create_test_project()
    try:
        plan = {"version": "1.0", "name": "first", "steps": [{"name": "a", "action": "do_step"}]}
        # Send a stale If-Match on a fresh project — should still succeed
        s, body = _http(
            "PUT",
            f"/api/projects/{pid}/plan",
            {"plan": plan},
            headers={"If-Match": "anything-stale-here"},
        )
        assert s == 200
        assert body["plan"]["name"] == "first"
    finally:
        _delete_project(pid)


# ===== v1.5.3 (2026-07-29): server-side visual_layout =====


def test_plan_visual_layout_roundtrips():
    """PUT a plan with a visual_layout, then GET it back and confirm
    the {step_name: {x, y}} map survives. Mirrors the
    workflow_packages.visual_layout field so visual_plan.js can
    persist canvas positions server-side."""
    pid = _create_test_project()
    try:
        layout = {
            "fetch-data": {"x": 120, "y": 80},
            "summarize": {"x": 380, "y": 220},
        }
        plan = {
            "version": "1.0",
            "name": "layout-test",
            "description": "",
            "trigger": "manual",
            "variables": [],
            "steps": [
                {
                    "name": "fetch-data",
                    "agent_role": "super",
                    "action": "fetch",
                    "depends_on": [],
                    "params_template": {},
                },
                {
                    "name": "summarize",
                    "agent_role": "super",
                    "action": "summarize",
                    "depends_on": ["fetch-data"],
                    "params_template": {},
                },
            ],
            "visual_layout": layout,
        }
        s, _ = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 200
        s, body = _http("GET", f"/api/projects/{pid}/plan")
        assert s == 200
        assert body["plan"]["visual_layout"] == layout
    finally:
        _delete_project(pid)


def test_plan_without_visual_layout_defaults_to_empty_dict():
    """An old plan written before v1.5.3 has no visual_layout field.
    The Pydantic model should default to {} so visual_plan.js can
    always read plan.visual_layout without checking for None."""
    pid = _create_test_project()
    try:
        plan = {
            "version": "1.0",
            "name": "no-layout",
            "description": "",
            "trigger": "manual",
            "variables": [],
            "steps": [
                {
                    "name": "only-step",
                    "agent_role": "super",
                    "action": "do_step",
                    "depends_on": [],
                    "params_template": {},
                },
            ],
            # No visual_layout key — simulates a pre-v1.5.3 plan
        }
        s, _ = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan})
        assert s == 200
        s, body = _http("GET", f"/api/projects/{pid}/plan")
        assert s == 200
        # Round-trip: missing field comes back as an empty dict,
        # not None and not missing. The frontend relies on this so
        # the "load persisted layout" code can do
        # `const layout = plan.visual_layout || {}`.
        assert body["plan"]["visual_layout"] == {}
    finally:
        _delete_project(pid)


def test_plan_visual_layout_survives_step_changes():
    """If the user adds/removes steps, the visual_layout should
    only contain entries for steps that still exist. (drawflow
    ignores layout entries for unknown step names anyway, so
    server-side the behavior is "set whatever the client sent".)
    This test just confirms no server-side filtering happens."""
    pid = _create_test_project()
    try:
        # First write: one step with one position
        plan1 = {
            "version": "1.0",
            "name": "p1",
            "description": "",
            "trigger": "manual",
            "variables": [],
            "steps": [{
                "name": "old-step",
                "agent_role": "super",
                "action": "do_step",
                "depends_on": [],
                "params_template": {},
            }],
            "visual_layout": {"old-step": {"x": 50, "y": 50}},
        }
        _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan1})
        # Second write: rename the step + keep the position
        plan2 = {
            "version": "1.0",
            "name": "p2",
            "description": "",
            "trigger": "manual",
            "variables": [],
            "steps": [{
                "name": "new-step",
                "agent_role": "super",
                "action": "do_step",
                "depends_on": [],
                "params_template": {},
            }],
            "visual_layout": {"new-step": {"x": 200, "y": 300}},
        }
        s, body = _http("PUT", f"/api/projects/{pid}/plan", {"plan": plan2})
        assert s == 200
        assert body["plan"]["visual_layout"] == {"new-step": {"x": 200, "y": 300}}
    finally:
        _delete_project(pid)
