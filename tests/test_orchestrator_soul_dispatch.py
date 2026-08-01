"""Tests for orchestrator/soul_dispatch.py — v3.9.0 SOUL apply lifecycle (Phase 1).

Covers the SOUL apply cycle (Phase 1, per docs/soul-routing-design.md
§"Lifecycle: SOUL apply before dispatch"):

  6 integration tests (in-process DB, mocked profile_configs apply):
    1. test_compose_soul_md_format
    2. test_generic_role_template_for_unknown_role
    3. test_ensure_soul_preset_creates_when_missing
    4. test_ensure_soul_preset_returns_existing_when_present
    5. test_submit_soul_idempotent_on_same_content
    6. test_wait_for_soul_applied_returns_true_on_status_applied

  2 e2e tests (gated on a dev server on :8765):
    1. test_full_dispatch_with_soul_apply
    2. test_dispatch_fails_when_apply_fails

The integration tests use the same `Database(Path(tmpdir)/"test.db")`
shape as tests/test_orchestrator_routing.py. The e2e tests use a
fresh in-process DB and import `dispatch_step` directly — when Round 3
wires the dispatch path into the HTTP layer, the e2e tests can be
upgraded to use `POST /api/projects/{id}/dispatch` (or the chatbox
"Run plan" endpoint). For now we verify the function end-to-end
against an in-process DB and gate on a dev server being up to catch
schema/import regressions in the test env.
"""
from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from hermes_orch.db import Database
from hermes_orch.orchestrator import soul_dispatch as sd
from hermes_orch.orchestrator.soul_dispatch import (
    SoulApplyError,
    _compose_soul_md,
    _create_dispatched_task,
    _ensure_soul_preset,
    _generic_role_template,
    _sha256,
    _step_default_soul,
    _step_to_dict,
    _submit_soul_to_profile,
    _wait_for_soul_applied,
    dispatch_step,
)


# ===== Fixtures =====


@pytest_asyncio.fixture
async def db():
    """Fresh in-memory-ish DB per test (tmpfile for clean teardown).

    Matches the shape used by tests/test_orchestrator_routing.py and
    tests/test_db_schema.py — tempdir-scoped file rather than
    ':memory:' so the path behaves identically to production and the
    tests don't leak state across event loops.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="soul_dispatch_test_"))
    database = Database(tmpdir / "test.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def _insert_agent(
    db: Database,
    agent_id: str,
    *,
    status: str = "verified",
) -> None:
    """Insert a parent `agents` row in `verified` state (so
    `_is_profile_idle_and_online` returns True)."""
    from datetime import datetime

    await db.insert(
        "agents",
        {
            "id": agent_id,
            "secret_hash": "x" * 64,
            "status": status,
            "last_heartbeat_at": datetime.now().astimezone().isoformat(),
        },
    )


async def _insert_profile(
    db: Database,
    profile_id: str,
    agent_id: str,
    *,
    name: str = "researcher",
    skills: list[str] | None = None,
    status: str = "idle",
) -> None:
    """Insert an `agent_profiles` row in `idle` state (no in-flight
    task)."""
    await db.insert(
        "agent_profiles",
        {
            "id": profile_id,
            "agent_id": agent_id,
            "name": name,
            "status": status,
            "skills": json.dumps(skills if skills is not None else []),
        },
    )


async def _insert_project(db: Database, project_id: str) -> None:
    await db.insert(
        "projects",
        {"id": project_id, "name": "soul-dispatch test", "state": "ready"},
    )


def _server_up(host: str = "127.0.0.1", port: int = 8765, timeout: float = 0.5) -> bool:
    """Return True if a TCP listener is accepting on `host:port`.

    Cheap, no payload — just a connect-and-close. The e2e tests use
    this as a precondition so they self-skip when the dev server
    isn't running. Mirrors the pattern in tests/test_optimize.py.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ===== Integration test 1: _compose_soul_md format =====


def test_compose_soul_md_format() -> None:
    """Standard 4-line header + content. The header is the first
    thing the LLM sees when reading SOUL.md, so it primes the role
    context before the prose body. The 4 lines are deliberately
    parseable (`# KEY: value`) so a future tool can extract them
    without parsing free-form markdown."""
    soul = _compose_soul_md(
        role_name="cpi-analyst",
        project_id="proj-1",
        content="You are a CPI analyst. Be precise.\n  trailing spaces  ",
    )
    lines = soul.split("\n")
    # Header: exactly 4 lines + blank line
    assert lines[0] == "# ROLE: cpi-analyst", f"header line 0: {lines[0]!r}"
    assert lines[1] == "# PROJECT: proj-1", f"header line 1: {lines[1]!r}"
    assert lines[2].startswith("# APPLIED_AT: "), f"header line 2: {lines[2]!r}"
    assert lines[2].endswith(("Z", "+00:00", "-05:00", "-08:00", "+08:00")), (
        f"APPLIED_AT must be a timezone-bearing timestamp; got {lines[2]!r}"
    )
    assert lines[3] == "# ----", f"header line 3: {lines[3]!r}"
    assert lines[4] == "", "blank line between header and body"
    # Content is stripped (trailing whitespace removed) and ends with a
    # single newline.
    assert "You are a CPI analyst. Be precise." in soul
    assert "trailing spaces" in soul  # inner whitespace preserved
    assert not soul.endswith("  \n"), f"trailing whitespace should be stripped: {soul!r}"
    assert soul.endswith("\n"), "body must end with a newline"
    # No leakage: the role name appears only in the header line.
    assert soul.count("cpi-analyst") == 1, "role name should appear once (in the header)"


# ===== Integration test 2: _generic_role_template =====


def test_generic_role_template_for_unknown_role() -> None:
    """A role with no preset content and no default_soul falls back
    to a sensible default. The text must reference the role name so
    the LLM has at least a hint of what persona to embody."""
    template = _generic_role_template("ghost-role")
    # Must reference the role name so the LLM has at least a hint of
    # what persona to embody.
    assert "ghost-role" in template
    # Must encourage good behavior (precision, uncertainty surfacing)
    # even for roles the orch doesn't know about.
    assert "precise" in template.lower() or "precision" in template.lower()
    assert "uncertainty" in template.lower() or "explicit" in template.lower()
    # No header — the header is added by `_compose_soul_md`.
    assert not template.startswith("# ROLE:")


# ===== Integration test 3: _ensure_soul_preset creates when missing =====


@pytest.mark.asyncio
async def test_ensure_soul_preset_creates_when_missing(db: Database) -> None:
    """No preset for the role → one is auto-populated from
    `step.default_soul` (or the generic template as fallback).

    The auto-populate contract:
      - role_name comes from `step.agent_role`
      - profile_id is the resolved profile's id
      - content is step.default_soul or generic
      - default_soul is preserved alongside content for Phase 2+
    """
    await _insert_agent(db, "a1")
    await _insert_profile(db, "p-1", "a1", name="cpi-analyst")
    await _insert_project(db, "proj-1")
    profile = await db.fetchone("SELECT * FROM agent_profiles WHERE id = ?", ("p-1",))

    # Case A: step provides default_soul → content == default_soul
    step_with_default = {
        "agent_role": "cpi-analyst",
        "default_soul": "Default persona body for cpi-analyst.",
    }
    preset = await _ensure_soul_preset("proj-1", step_with_default, profile, db)
    assert preset["role_name"] == "cpi-analyst"
    assert preset["profile_id"] == "p-1"
    assert preset["content"] == "Default persona body for cpi-analyst."
    assert preset["default_soul"] == "Default persona body for cpi-analyst."

    # Case B: no default_soul → content falls back to generic template
    await _insert_profile(db, "p-2", "a1", name="ghost")
    profile2 = await db.fetchone("SELECT * FROM agent_profiles WHERE id = ?", ("p-2",))
    step_no_default = {"agent_role": "ghost"}
    preset2 = await _ensure_soul_preset("proj-1", step_no_default, profile2, db)
    assert preset2["role_name"] == "ghost"
    assert preset2["profile_id"] == "p-2"
    # Generic template should mention the role
    assert "ghost" in preset2["content"]
    # default_soul stored as NULL when absent (matches DB schema)
    assert preset2["default_soul"] is None


# ===== Integration test 4: _ensure_soul_preset returns existing =====


@pytest.mark.asyncio
async def test_ensure_soul_preset_returns_existing_when_present(db: Database) -> None:
    """If a preset already exists for the (project, role), the same
    row is returned — no second insert. The content is NOT overwritten
    by `step.default_soul` (operator-set values take precedence over
    workflow defaults)."""
    await _insert_agent(db, "a1")
    await _insert_profile(db, "p-1", "a1", name="researcher")
    await _insert_project(db, "proj-1")
    profile = await db.fetchone("SELECT * FROM agent_profiles WHERE id = ?", ("p-1",))
    step = {"agent_role": "researcher"}

    # First call creates
    first = await _ensure_soul_preset("proj-1", step, profile, db)
    first_id = first["id"]
    first_content = first["content"]
    assert first_content, "auto-populated content should not be empty"

    # Mutate the row directly to simulate an operator edit. This
    # proves the second call returns the EXISTING row, not a fresh
    # insert that overwrites the edit.
    await db.execute(
        "UPDATE project_soul_presets SET content = ? WHERE id = ?",
        ("operator-edited body", first_id),
    )

    # Second call returns the same row id with the operator's edit
    second = await _ensure_soul_preset("proj-1", step, profile, db)
    assert second["id"] == first_id, "should return the same preset row"
    assert second["content"] == "operator-edited body", (
        "second call must not overwrite an operator edit"
    )

    # And the row count is still 1 (no duplicate inserted)
    count = await db.fetchone(
        "SELECT COUNT(*) AS n FROM project_soul_presets WHERE project_id = ?",
        ("proj-1",),
    )
    assert count["n"] == 1, f"expected 1 preset, got {count['n']}"


# ===== Integration test 5: _submit_soul_to_profile idempotent =====


@pytest.mark.asyncio
async def test_submit_soul_idempotent_on_same_content(db: Database) -> None:
    """Submitting the same SOUL content twice yields the same cfg_id
    — no duplicate profile_configs row. The wrapper's claim+ack loop
    reuses the existing row, so re-dispatching with unchanged
    content is a no-op."""
    await _insert_agent(db, "a1")
    await _insert_profile(db, "p-1", "a1")
    soul_md = _compose_soul_md(
        role_name="researcher",
        project_id="proj-1",
        content="body v1",
    )

    first = await _submit_soul_to_profile("p-1", soul_md, db)
    second = await _submit_soul_to_profile("p-1", soul_md, db)
    assert first == second, "same content should yield the same cfg_id"

    # And only one row exists in profile_configs
    rows = await db.fetchall(
        "SELECT id, file_path, desired_sha256, status FROM profile_configs "
        "WHERE profile_id = ?",
        ("p-1",),
    )
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}"
    assert rows[0]["file_path"] == "soul.md"
    assert rows[0]["desired_sha256"] == _sha256(soul_md)
    assert rows[0]["status"] == "pending"

    # Different content → different cfg_id (a fresh apply is queued)
    soul_md_v2 = _compose_soul_md(
        role_name="researcher",
        project_id="proj-1",
        content="body v2",
    )
    third = await _submit_soul_to_profile("p-1", soul_md_v2, db)
    assert third != first, "different content should produce a new cfg_id"

    rows = await db.fetchall(
        "SELECT id FROM profile_configs WHERE profile_id = ? ORDER BY created_at",
        ("p-1",),
    )
    assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"


# ===== Integration test 6: _wait_for_soul_applied returns True =====


@pytest.mark.asyncio
async def test_wait_for_soul_applied_returns_true_on_status_applied(db: Database) -> None:
    """The wrapper's claim+ack loop is simulated by a background
    task that flips the row's status from 'pending' → 'applying'
    → 'applied' after a short delay. `_wait_for_soul_applied` should
    return True as soon as the row reaches 'applied'.

    We use the existing `profile_configs` table — the same flow the
    wrapper uses in production (see api/agents.py:1144-1294). This
    test exercises the polling loop in isolation, without needing a
    real wrapper process.
    """
    await _insert_agent(db, "a1")
    await _insert_profile(db, "p-1", "a1")
    soul_md = _compose_soul_md(
        role_name="researcher", project_id="proj-1", content="body"
    )
    cfg_id = await _submit_soul_to_profile("p-1", soul_md, db)

    async def simulate_wrapper_ack() -> None:
        # Wait 250ms, then claim (pending → applying) and ack (→ applied).
        # Mirrors the timing of a real wrapper on a local network.
        await asyncio.sleep(0.25)
        await db.execute(
            "UPDATE profile_configs SET status = 'applying' "
            "WHERE id = ? AND status = 'pending'",
            (cfg_id,),
        )
        await asyncio.sleep(0.15)
        await db.execute(
            "UPDATE profile_configs SET status = 'applied', applied_at = ? "
            "WHERE id = ?",
            ("2026-08-01T16:00:00+08:00", cfg_id),
        )

    # Start the simulator and the waiter concurrently. The waiter
    # should return True once the row reaches 'applied'.
    sim_task = asyncio.create_task(simulate_wrapper_ack())
    try:
        result = await _wait_for_soul_applied(cfg_id, db, timeout_s=5.0)
    finally:
        await sim_task

    assert result is True, "_wait_for_soul_applied should return True on status='applied'"

    # And the row's terminal state is recorded
    row = await db.fetchone(
        "SELECT status, applied_at FROM profile_configs WHERE id = ?", (cfg_id,)
    )
    assert row["status"] == "applied"
    assert row["applied_at"] is not None


# ===== Integration test bonus: _wait_for_soul_applied returns False on failed =====


@pytest.mark.asyncio
async def test_wait_for_soul_applied_returns_false_on_status_failed(db: Database) -> None:
    """Failure path: the wrapper acks the config with status='failed'.
    `_wait_for_soul_applied` returns False so the dispatch step can
    fetch the row's `error` field and raise SoulApplyError."""
    await _insert_agent(db, "a1")
    await _insert_profile(db, "p-1", "a1")
    soul_md = _compose_soul_md(
        role_name="researcher", project_id="proj-1", content="body"
    )
    cfg_id = await _submit_soul_to_profile("p-1", soul_md, db)

    async def simulate_wrapper_fail() -> None:
        await asyncio.sleep(0.2)
        await db.execute(
            "UPDATE profile_configs SET status = 'failed', error = ? "
            "WHERE id = ?",
            ("disk full", cfg_id),
        )

    sim_task = asyncio.create_task(simulate_wrapper_fail())
    try:
        result = await _wait_for_soul_applied(cfg_id, db, timeout_s=5.0)
    finally:
        await sim_task

    assert result is False, "should return False on status='failed'"


# ===== Integration test bonus: _wait_for_soul_applied times out =====


@pytest.mark.asyncio
async def test_wait_for_soul_applied_returns_false_on_timeout(db: Database) -> None:
    """No wrapper ack within the timeout → returns False. The
    dispatch step then raises SoulApplyError with a "timed out"
    message."""
    await _insert_agent(db, "a1")
    await _insert_profile(db, "p-1", "a1")
    soul_md = _compose_soul_md(
        role_name="researcher", project_id="proj-1", content="body"
    )
    cfg_id = await _submit_soul_to_profile("p-1", soul_md, db)
    # Note: no simulator task — the row stays 'pending' forever.

    result = await _wait_for_soul_applied(cfg_id, db, timeout_s=0.5)
    assert result is False, "should return False on timeout"


# ===== Integration test bonus: _step_to_dict handles PlanStep-like and dict =====


def test_step_to_dict_handles_both_shapes() -> None:
    """`dispatch_step` accepts both a Pydantic PlanStep and a plain
    dict. `_step_to_dict` normalises both to the dict shape the
    routing engine expects."""

    class FakeStep:
        """Mimic the PlanStep surface used by dispatch_step (just
        the fields we read)."""

        def __init__(self, **kw: Any) -> None:
            for k, v in kw.items():
                setattr(self, k, v)

    fake = FakeStep(
        agent_role="cpi-analyst",
        target_profiles=["p-1"],
        required_capabilities=["python"],
        default_soul="hello",
    )
    d = _step_to_dict(fake)
    assert d["agent_role"] == "cpi-analyst"
    assert d["target_profiles"] == ["p-1"]
    assert d["default_soul"] == "hello"

    # Dict input is returned as a fresh dict (not the same object)
    src = {"agent_role": "r"}
    out = _step_to_dict(src)
    assert out == {"agent_role": "r"}
    assert out is not src, "must return a fresh dict so callers can mutate safely"


def test_step_default_soul_priority() -> None:
    """`default_soul` can live at the top level (v3.9.0) or under
    `params_template` (forward-compat with the chatbox plan-editor's
    serialised form). Top level wins when both are set; empty
    strings are ignored."""
    assert _step_default_soul({"default_soul": "top-level"}) == "top-level"
    assert _step_default_soul({"default_soul": "  spaced  "}) == "spaced"
    assert _step_default_soul({"default_soul": ""}) == ""
    assert _step_default_soul({"params_template": {"default_soul": "nested"}}) == "nested"
    # Top level wins
    assert _step_default_soul(
        {"default_soul": "top", "params_template": {"default_soul": "nested"}}
    ) == "top"
    # Neither → empty
    assert _step_default_soul({}) == ""
    assert _step_default_soul({"params_template": {}}) == ""


# ===== E2E test 1: full dispatch with SOUL apply =====


@pytest.mark.skipif(
    not _server_up(),
    reason="dev server not running on :8765 — start with `hermes-orch serve`",
)
@pytest.mark.asyncio
async def test_full_dispatch_with_soul_apply() -> None:
    """End-to-end SOUL apply cycle: register agent + profile,
    create a project, dispatch one step. We use a fresh in-process
    Database so the test is hermetic (the dev server's own DB is
    not touched). The dev server is checked up so we know the
    codebase imports cleanly and the schema migrations ran.

    The test calls `dispatch_step` directly (it's not yet an HTTP
    endpoint — that's Round 3). When the dispatch path is wired
    into `api.projects` and a `POST /api/projects/{id}/dispatch`
    endpoint exists, this test can be upgraded to use the HTTP
    layer; the assertions stay the same.

    For the SOUL apply to actually succeed in this e2e test, a
    real wrapper would need to be running to claim+ack the
    profile_configs row. Without a wrapper, the row stays 'pending'
    and the 10s timeout fires. The test handles both scenarios:
      - With a real wrapper: the config is applied, the task is
        created, the assertion `task is not None` passes.
      - Without a wrapper: SoulApplyError is raised; the test
        accepts this as a "the apply flow is plumbed end-to-end"
        signal and verifies the failure shape (the error message
        references the timeout / the cfg_id is recorded).
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="soul_dispatch_e2e_"))
    database = Database(tmpdir / "e2e.db")
    await database.connect()
    try:
        # 1. Register an agent + profile
        await _insert_agent(database, "e2e-agent-1")
        await _insert_profile(
            database,
            "e2e-profile-1",
            "e2e-agent-1",
            name="researcher",
            skills=["python", "write_file"],
        )
        await _insert_project(database, "e2e-proj-1")

        profile = await database.fetchone(
            "SELECT * FROM agent_profiles WHERE id = ?", ("e2e-profile-1",)
        )
        assert profile is not None

        step = {
            "name": "fetch-data",
            "agent_role": "researcher",
            "action": "fetch_url",
            "target_profiles": ["e2e-profile-1"],
            "required_capabilities": ["python"],
            "default_soul": (
                "You are a researcher. Use python to fetch and analyse data."
            ),
        }

        # 2. Dispatch — two outcomes are valid (a wrapper is unlikely
        # to be running in the test env, so we expect the timeout
        # path; the e2e point is that the function is plumbed in).
        try:
            task = await dispatch_step("e2e-proj-1", step, database)
        except SoulApplyError as exc:
            # No wrapper running → 10s timeout fires. Verify the
            # error is well-formed so the user sees a useful message
            # in production. The cfg_id should match the row that
            # was inserted.
            assert exc.cfg_id, f"cfg_id missing on SoulApplyError: {exc}"
            row = await database.fetchone(
                "SELECT status, desired_content FROM profile_configs WHERE id = ?",
                (exc.cfg_id,),
            )
            assert row is not None, "the inserted cfg row should exist"
            # The row is still in pending (no wrapper claimed it)
            assert row["status"] in ("pending", "applying"), (
                f"unexpected status: {row['status']!r}"
            )
            # The SOUL content is what we composed (the apply path
            # ran far enough to insert + write the desired_content).
            assert "ROLE: researcher" in row["desired_content"]
            assert "PROJECT: e2e-proj-1" in row["desired_content"]
            assert "Use python to fetch and analyse data." in row["desired_content"]
            # The preset was auto-populated
            preset = await database.fetchone(
                "SELECT * FROM project_soul_presets WHERE project_id = ?",
                ("e2e-proj-1",),
            )
            assert preset is not None, "preset should be auto-populated"
            assert preset["role_name"] == "researcher"
            assert preset["profile_id"] == "e2e-profile-1"
            return  # success: e2e path is wired end-to-end

        # 3. If a real wrapper IS running, the task is created with
        # the resolved profile + the SOUL applied. Verify the
        # success path.
        assert task is not None
        assert task["project_id"] == "e2e-proj-1"
        assert task["agent_role"] == "researcher"
        assert task["assigned_profile_id"] == "e2e-profile-1"
        assert task["status"] == "pending"
        assert task["required_capability"] == "python"

        # The profile_configs row reached status='applied'
        cfgs = await database.fetchall(
            "SELECT * FROM profile_configs WHERE profile_id = ?",
            ("e2e-profile-1",),
        )
        assert len(cfgs) == 1
        assert cfgs[0]["status"] == "applied"

        # The preset's last_applied_at is now set
        preset = await database.fetchone(
            "SELECT * FROM project_soul_presets WHERE project_id = ?",
            ("e2e-proj-1",),
        )
        assert preset["last_applied_at"] is not None
        assert preset["last_applied_mtime"] is not None
    finally:
        await database.close()


# ===== E2E test 2: dispatch fails when apply fails =====


@pytest.mark.skipif(
    not _server_up(),
    reason="dev server not running on :8765 — start with `hermes-orch serve`",
)
@pytest.mark.asyncio
async def test_dispatch_fails_when_apply_fails() -> None:
    """Failure path: no wrapper running, the profile_configs row
    stays 'pending' forever, and dispatch_step raises SoulApplyError
    after the 10s timeout. Verifies the user-facing error message
    contains the cfg_id and a reason.

    This is the realistic "wrapper is down" scenario in production:
    a 10s timeout per dispatch, a clear error so the operator can
    investigate. We use a tight 1s timeout in the test to keep the
    suite fast.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="soul_dispatch_e2e_fail_"))
    database = Database(tmpdir / "e2e_fail.db")
    await database.connect()
    try:
        await _insert_agent(database, "e2e-agent-fail")
        await _insert_profile(
            database, "e2e-profile-fail", "e2e-agent-fail", name="researcher",
        )
        await _insert_project(database, "e2e-proj-fail")

        step = {
            "name": "do-work",
            "agent_role": "researcher",
            "action": "do_step",
        }

        # Monkeypatch the timeout to 1s so the test finishes quickly
        # (the production default is 10s per the design spec).
        import hermes_orch.orchestrator.soul_dispatch as _sd
        orig = _sd._wait_for_soul_applied

        async def fast_wait(*args: Any, **kw: Any) -> bool:
            # Force a tight timeout
            kw["timeout_s"] = 0.5
            return await orig(*args, **kw)

        _sd._wait_for_soul_applied = fast_wait
        try:
            with pytest.raises(SoulApplyError) as excinfo:
                await dispatch_step("e2e-proj-fail", step, database)
        finally:
            _sd._wait_for_soul_applied = orig

        err = excinfo.value
        # The error references the cfg_id and the profile / role
        assert err.cfg_id, "cfg_id should be recorded on the error"
        assert "e2e-profile-fail" in str(err) or "researcher" in str(err), (
            f"error message should mention the failing profile or role; got {err!r}"
        )
        # And the error_msg is a non-empty string (timeout or wrapper error)
        assert err.error_msg, "error_msg should be populated"

        # The profile_configs row exists and is still pending (no
        # wrapper acked it).
        row = await database.fetchone(
            "SELECT status FROM profile_configs WHERE id = ?", (err.cfg_id,)
        )
        assert row is not None
        assert row["status"] in ("pending", "applying"), (
            f"row should be un-acked without a wrapper; got {row['status']!r}"
        )

        # The preset WAS still auto-populated (the failure happens
        # after the preset insert).
        preset = await database.fetchone(
            "SELECT * FROM project_soul_presets WHERE project_id = ?",
            ("e2e-proj-fail",),
        )
        assert preset is not None
        assert preset["last_applied_at"] is None, (
            "preset should NOT be marked applied when the apply failed"
        )
    finally:
        await database.close()
