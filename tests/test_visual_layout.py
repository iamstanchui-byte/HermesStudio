"""Tests for the visual_layout field on workflow_packages (Phase 2.5, 2026-07-26).

The visual_layout is a {step_name: {x, y}} dict that persists the
visual editor's card positions. It's separate from step_template so
the structural data and the display data don't get tangled. Tests:

  1. _validate_visual_layout: schema check on the PATCH body field.
  2. PATCH /api/workflows/{id} with visual_layout persists it.
  3. PATCH without visual_layout keeps the existing value (None = no touch).
  4. GET /api/workflows/{id} returns the visual_layout.
  5. Invalid visual_layout returns 422 (not silently swallowed).
"""
import json
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hermes_orch.api.workflows import _validate_visual_layout, router as workflows_router
from hermes_orch.db import Database


# ----- helpers -----

async def _new_db() -> Database:
    tmpdir = tempfile.mkdtemp(prefix="visual_layout_test_")
    db = Database(Path(tmpdir) / "test.db")
    await db.connect()
    return db


async def _make_workflow(db: Database, name: str = "test-wf-1") -> str:
    """Insert a workflow row directly (bypass LLM-synth path). Returns id."""
    wid = f"wf-test-{name}"
    await db.execute(
        "INSERT INTO workflow_packages "
        "(id, name, version, description, step_template, variables, visual_layout, "
        " source_project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            wid, name, "0.1.0", "test desc",
            json.dumps([
                {"name": "a", "agent_role": "x", "action": "do_a", "depends_on": []},
                {"name": "b", "agent_role": "x", "action": "do_b", "depends_on": ["a"]},
            ]),
            json.dumps([]),
            json.dumps({"a": {"x": 50, "y": 50}, "b": {"x": 350, "y": 200}}),
            None, "2026-07-26T00:00:00", "2026-07-26T00:00:00",
        ),
    )
    return wid


# ----- async context helper -----
# We use an async context manager instead of a pytest fixture because
# the project doesn't ship a pytest config (pytest-asyncio would need
# `asyncio_mode = "auto"` for the @pytest.fixture async pattern to
# work without errors). An async context manager is the same shape
# without the fixture-async friction.
from contextlib import asynccontextmanager


@asynccontextmanager
async def _app_client():
    """Yield (ac, db) for a minimal FastAPI app wired to a fresh DB."""
    db = await _new_db()
    app = FastAPI()
    app.include_router(workflows_router)
    app.state.db = db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, db
    await db.close()


# ----- _validate_visual_layout unit tests -----

def test_validate_visual_layout_none() -> None:
    """None is valid (= field not sent, server keeps existing)."""
    ok, err = _validate_visual_layout(None)
    assert ok and err is None


def test_validate_visual_layout_empty_dict() -> None:
    """Empty dict is valid (clears all positions)."""
    ok, err = _validate_visual_layout({})
    assert ok and err is None


def test_validate_visual_layout_normal() -> None:
    """The expected {step_name: {x, y}} shape passes."""
    ok, err = _validate_visual_layout({
        "step-a": {"x": 50, "y": 100},
        "step-b": {"x": 350.5, "y": 200.0},
    })
    assert ok and err is None


def test_validate_visual_layout_orphan_step() -> None:
    """Orphan step name (not in any current step_template) is allowed.

    Reason: keeping orphans is harmless — the visual editor just
    ignores them on render. Saves us from having to coordinate a
    step-rename + position-cleanup transaction.
    """
    ok, err = _validate_visual_layout({"renamed-step": {"x": 50, "y": 50}})
    assert ok and err is None


def test_validate_visual_layout_not_dict() -> None:
    """Top-level non-dict rejected."""
    ok, err = _validate_visual_layout("not a dict")
    assert not ok and "must be a dict" in err


def test_validate_visual_layout_bad_x() -> None:
    """Non-numeric x is rejected."""
    ok, err = _validate_visual_layout({"step": {"x": "fifty", "y": 50}})
    assert not ok and "numeric" in err


def test_validate_visual_layout_missing_y() -> None:
    """Missing y is rejected (both must be present)."""
    ok, err = _validate_visual_layout({"step": {"x": 50}})
    assert not ok and "numeric" in err


def test_validate_visual_layout_bool_rejected() -> None:
    """True/False are int subclasses in Python but semantically not
    positions — reject them to catch a common copy-paste mistake."""
    ok, err = _validate_visual_layout({"step": {"x": True, "y": 50}})
    assert not ok and "numeric" in err


def test_validate_visual_layout_empty_key() -> None:
    """Empty step name rejected."""
    ok, err = _validate_visual_layout({"": {"x": 50, "y": 50}})
    assert not ok and "non-empty string" in err


def test_validate_visual_layout_pos_must_be_dict() -> None:
    """pos value must be a dict, not a list / number."""
    ok, err = _validate_visual_layout({"step": [50, 50]})
    assert not ok and "must be a dict" in err


# ----- API tests -----

@pytest.mark.asyncio
async def test_get_workflow_returns_visual_layout() -> None:
    """GET detail returns the saved visual_layout (parsed, not JSON string)."""
    async with _app_client() as (ac, db):
        wid = await _make_workflow(db)
        r = await ac.get(f"/api/workflows/{wid}")
        assert r.status_code == 200
        body = r.json()
        assert "visual_layout" in body, "detail must include visual_layout field"
        assert body["visual_layout"] == {
            "a": {"x": 50, "y": 50},
            "b": {"x": 350, "y": 200},
        }


@pytest.mark.asyncio
async def test_patch_visual_layout_persists() -> None:
    """PATCH with new visual_layout persists it."""
    async with _app_client() as (ac, db):
        wid = await _make_workflow(db)
        new_layout = {
            "a": {"x": 100, "y": 200},
            "b": {"x": 400, "y": 500},
        }
        r = await ac.patch(
            f"/api/workflows/{wid}",
            json={"visual_layout": new_layout},
        )
        assert r.status_code == 200, r.text
        # Verify by re-reading
        r2 = await ac.get(f"/api/workflows/{wid}")
        assert r2.json()["visual_layout"] == new_layout


@pytest.mark.asyncio
async def test_patch_without_visual_layout_keeps_existing() -> None:
    """PATCH without visual_layout (None) keeps the existing value.
    Important so non-visual PATCHes (description etc.) don't wipe
    the saved positions."""
    async with _app_client() as (ac, db):
        wid = await _make_workflow(db)
        original = {"a": {"x": 50, "y": 50}, "b": {"x": 350, "y": 200}}
        # PATCH just the description
        r = await ac.patch(
            f"/api/workflows/{wid}",
            json={"description": "new desc"},
        )
        assert r.status_code == 200, r.text
        # Visual layout must be unchanged
        r2 = await ac.get(f"/api/workflows/{wid}")
        assert r2.json()["visual_layout"] == original


@pytest.mark.asyncio
async def test_patch_visual_layout_empty_dict_clears() -> None:
    """PATCH with visual_layout={} explicitly clears all positions
    (this is what the 'Reset layout' button does)."""
    async with _app_client() as (ac, db):
        wid = await _make_workflow(db)
        r = await ac.patch(
            f"/api/workflows/{wid}",
            json={"visual_layout": {}},
        )
        assert r.status_code == 200, r.text
        r2 = await ac.get(f"/api/workflows/{wid}")
        assert r2.json()["visual_layout"] == {}


@pytest.mark.asyncio
async def test_patch_visual_layout_invalid_rejected() -> None:
    """Invalid visual_layout returns 422, does not modify the row."""
    async with _app_client() as (ac, db):
        wid = await _make_workflow(db)
        r = await ac.patch(
            f"/api/workflows/{wid}",
            json={"visual_layout": {"a": {"x": "bad", "y": 50}}},
        )
        assert r.status_code == 422, r.text
        assert "visual_layout" in r.text.lower()
        # Existing layout must be unchanged
        r2 = await ac.get(f"/api/workflows/{wid}")
        assert r2.json()["visual_layout"] == {
            "a": {"x": 50, "y": 50},
            "b": {"x": 350, "y": 200},
        }


@pytest.mark.asyncio
async def test_patch_visual_layout_alongside_other_fields() -> None:
    """PATCH that updates step_template + visual_layout together
    persists both (the visual editor's normal Save path)."""
    async with _app_client() as (ac, db):
        wid = await _make_workflow(db)
        new_steps = [
            {"name": "a", "agent_role": "x", "action": "do_a", "depends_on": []},
            {"name": "b", "agent_role": "x", "action": "do_b", "depends_on": ["a"]},
            {"name": "c", "agent_role": "x", "action": "do_c", "depends_on": ["b"]},
        ]
        new_layout = {
            "a": {"x": 50, "y": 50},
            "b": {"x": 350, "y": 50},
            "c": {"x": 650, "y": 50},
        }
        r = await ac.patch(
            f"/api/workflows/{wid}",
            json={"step_template": new_steps, "visual_layout": new_layout},
        )
        assert r.status_code == 200, r.text
        r2 = await ac.get(f"/api/workflows/{wid}")
        body = r2.json()
        assert len(body["step_template"]) == 3
        assert body["step_template"][2]["name"] == "c"
        assert body["visual_layout"] == new_layout


# Note: the "legacy row with NULL visual_layout" case is not unit-
# tested here because the column has NOT NULL DEFAULT '{}' — a fresh
# test DB can't be coerced into a NULL state via the public API. The
# try/except KeyError in _row_to_workflow_detail is defensive code
# for the real-world case of a DB created before the migration ran
# (where the column may not exist in the SELECT * result set). To
# test that path you'd need to open a pre-migration DB, which is
# outside the scope of a per-PR unit test.
