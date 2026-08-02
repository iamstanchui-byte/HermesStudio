"""Tests for the visual plan editor page (GET /api/projects/{id}/plan/visual).

Context (2026-08-02):
  v3.10.10 added a Generate Tasks modal to the visual plan
  editor. The modal needs the project's current `max_iterations`
  to pre-fill the loop-back-cap input. To pass that value into
  the template, the plan_visual_page handler had to read
  `proj["max_iterations"]` — but the handler's SELECT didn't
  include that column. Result: 500 KeyError on every visit to
  the visual plan editor (user reported on proj-cc43d7ed).

  Fix: extend the SELECT to include max_iterations (and the
  related iter-loop fields for completeness). The plan_visual_page
  endpoint must return 200 with the template that includes
  `data-project-max-iterations="<N>"` on the wrap div.

These tests cover the regression:
  - GET /plan/visual on a project with no plan → 200, max_iter 0
  - GET /plan/visual on a project with plan → 200, data attr matches
  - GET /plan/visual on a project with custom max_iterations → 200,
    pre-fills correctly
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hermes_orch import db as db_mod
from hermes_orch import main as main_mod
from hermes_orch.auth.cookie import ROLE_ADMIN, create_user, get_user_by_username, hash_password

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    orig = db_mod.Database.__init__

    def patched(self, db_path):
        orig(self, test_db)

    monkeypatch.setattr(db_mod.Database, "__init__", patched)

    app = main_mod.create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        await _bootstrap_admin(app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


async def _bootstrap_admin(app):
    db = app.state.db
    existing = await get_user_by_username(db, ADMIN_USERNAME)
    if existing:
        await db.execute(
            "UPDATE users SET password_hash = ?, role = ?, disabled = 0 WHERE id = ?",
            (hash_password(ADMIN_PASSWORD), ROLE_ADMIN, existing["id"]),
        )
        return existing["id"]
    return await create_user(db, username=ADMIN_USERNAME,
                              password=ADMIN_PASSWORD, role=ROLE_ADMIN)


async def _login_admin(ac):
    r = await ac.post("/api/auth/login",
                      json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert r.status_code in (200, 201), r.text


async def _create_project(app, *, max_iterations: int = 0, with_plan: bool = False) -> str:
    db = app.state.db
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    plan_json = (
        '{"version":"1.0","name":"t","steps":[{"name":"a","action":"do_a","agent_role":"super"}]}'
        if with_plan else ""
    )
    await db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary, plan_json) "
        "VALUES (?, ?, '', 'planned', '', '', '', ?, 0, '', ?)",
        (pid, f"plan-visual-test-{pid}", max_iterations, plan_json),
    )
    return pid


# ===== Regression: GET /plan/visual must not 500 =====


@pytest.mark.asyncio
async def test_plan_visual_page_returns_200_for_no_plan_project(client):
    """v3.10.10 fix: the endpoint used to return 500 (KeyError:
    'max_iterations') because the SELECT didn't include that
    column. After the fix it returns 200 with the template,
    which includes the wrap div with data-project-max-iterations.
    """
    ac, app = client
    await _login_admin(ac)
    pid = await _create_project(app, max_iterations=0)

    r = await ac.get(f"/api/projects/{pid}/plan/visual", follow_redirects=False)
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:500]}"
    # The wrap div must include the new data attribute
    assert "data-project-max-iterations" in r.text, (
        "Template missing data-project-max-iterations attribute on "
        "the wrap div. v3.10.10 needs this to pre-fill the loop-back "
        "cap input on the Generate Tasks modal."
    )
    # For a project with max_iterations=0, the data attribute
    # should be "0" (not absent or "3")
    assert 'data-project-max-iterations="0"' in r.text, (
        f"Expected data-project-max-iterations=\"0\" in page; "
        f"the actual attribute appears to be different. The modal "
        f"pre-fill depends on this exact format."
    )


@pytest.mark.asyncio
async def test_plan_visual_page_prefills_custom_max_iterations(client):
    """If the project has a non-zero max_iterations, the data
    attribute must reflect that value. The modal pre-fills with
    this number when the operator clicks Generate tasks."""
    ac, app = client
    await _login_admin(ac)
    pid = await _create_project(app, max_iterations=5, with_plan=True)

    r = await ac.get(f"/api/projects/{pid}/plan/visual", follow_redirects=False)
    assert r.status_code == 200, r.text[:300]
    assert 'data-project-max-iterations="5"' in r.text, (
        "Template should show the project's max_iterations (5) in "
        "the data attribute, but the page is missing it or has the "
        "wrong value. The Generate Tasks modal's pre-fill will be "
        "wrong if this regresses."
    )


@pytest.mark.asyncio
async def test_plan_visual_page_with_plan_loads_correctly(client):
    """When the project has a plan, the template should still
    render correctly (plan JSON parsed into the wrap div)."""
    ac, app = client
    await _login_admin(ac)
    pid = await _create_project(app, max_iterations=3, with_plan=True)

    r = await ac.get(f"/api/projects/{pid}/plan/visual", follow_redirects=False)
    assert r.status_code == 200, r.text[:500]
    # The plan should be embedded in data-plan-json (single-encoded)
    assert "data-plan-json" in r.text
    # The Generate Tasks modal should also be in the page
    assert "vp-generate-tasks-overlay" in r.text, (
        "Template missing the Generate Tasks modal HTML. v3.10.10 "
        "added this modal — without it, the operator sees the old "
        "bare confirm() dialog (silent bug)."
    )


@pytest.mark.asyncio
async def test_plan_visual_page_404_for_unknown_project(client):
    """Sanity: the endpoint must still 404 for unknown project."""
    ac, app = client
    await _login_admin(ac)
    r = await ac.get("/api/projects/proj-nonexistent-xyz/plan/visual",
                     follow_redirects=False)
    assert r.status_code == 404
