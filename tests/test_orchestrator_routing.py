# coding: utf-8
"""Tests for orchestrator/routing.py — v3.9.0 SOUL hybrid routing (Phase 1).

Covers the 4-strategy fallback chain in isolation:
  1. Workflow hint pool (target_profiles) — first idle+online wins
  2. Project preset binding (get_soul_preset_by_role) — bound profile wins
  3. Capability match (profile.skills ⊇ step.required_capabilities)
  4. NoProfileAvailable — actionable hint

Plus the orthogonal helpers:
  - offline profile (heartbeat > 90s) is skipped
  - _skills_cover is a strict subset check (no false positives)
  - preset wins over a free-floating capability match (priority check)

Test pattern: in-process `Database(Path(tmpdir)/"test.db")` — same
shape as tests/test_db_schema.py and tests/test_cascade.py. Each
test stands up its own agent + profile rows so the strategies are
exercised in isolation (no cross-test bleed, no global state).
"""
from __future__ import annotations

import json
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from hermes_orch.db import Database
from hermes_orch.orchestrator.routing import (
    NoProfileAvailable,
    _HEARTBEAT_STALE_S,
    _is_profile_idle_and_online,
    _skills_cover,
    resolve_role_to_profile,
)
from hermes_orch.utils import now_aware


# === Fixtures ===

@pytest_asyncio.fixture
async def db():
    """Fresh in-memory-ish DB per test (tmpfile for clean teardown).

    The Database wrapper uses aiosqlite against a real file path — we
    use a tempdir-scoped file rather than ':memory:' so the path
    behaves identically to production and the tests don't leak state
    across event loops.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="routing_test_"))
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
    heartbeat_age_s: float = 0,
) -> None:
    """Insert a parent `agents` row.

    `heartbeat_age_s=0` means the heartbeat is "just now" (online).
    Pass a positive value to simulate a stale wrapper — anything
    > 90s is considered offline by the routing engine.
    """
    now = now_aware()
    hb = (now - timedelta(seconds=heartbeat_age_s)).isoformat()
    await db.insert(
        "agents",
        {
            "id": agent_id,
            "secret_hash": "x" * 64,  # any non-empty value (NOT NULL)
            "status": status,
            "last_heartbeat_at": hb,
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
    current_task_id: str | None = None,
) -> None:
    """Insert an `agent_profiles` row.

    Defaults: idle + no in-flight task. The `skills` list is JSON-
    encoded (matches the schema column type) so the routing engine
    can parse it the same way it would in production.
    """
    await db.insert(
        "agent_profiles",
        {
            "id": profile_id,
            "agent_id": agent_id,
            "name": name,
            "status": status,
            "current_task_id": current_task_id,
            "skills": json.dumps(skills if skills is not None else []),
        },
    )


async def _insert_project(db: Database, project_id: str) -> None:
    await db.insert(
        "projects",
        {"id": project_id, "name": "routing test", "state": "ready"},
    )


async def _insert_preset(
    db: Database,
    preset_id: str,
    project_id: str,
    profile_id: str,
    role_name: str,
) -> None:
    """Bind a role to a profile in `project_soul_presets`."""
    await db.insert(
        "project_soul_presets",
        {
            "id": preset_id,
            "project_id": project_id,
            "profile_id": profile_id,
            "role_name": role_name,
            "content": "preset body",
        },
    )


async def _insert_task(
    db: Database,
    task_id: str,
    project_id: str,
    *,
    assigned_profile_id: str | None = None,
    status: str = "pending",
) -> None:
    """Insert a `tasks` row, optionally with an assigned profile.

    Used to make a profile "busy" in the routing engine's view (via
    a row in the `tasks` table with status IN ('assigned', 'running')
    and `assigned_profile_id` set). A profile with status='busy' but
    no actual task row is NOT considered busy by the router — it
    checks the `tasks` table, not the `agent_profiles.status` column.
    """
    await db.insert(
        "tasks",
        {
            "id": task_id,
            "project_id": project_id,
            "name": f"task-{task_id}",
            "agent_role": "researcher",
            "assigned_profile_id": assigned_profile_id,
            "depends_on": "[]",
            "on_parent_failure": "skip",
            "status": status,
            "priority": "normal",
            "action": "noop",
            "params": "{}",
        },
    )


# === Strategy 1: Workflow hint pool ===

@pytest.mark.asyncio
async def test_workflow_hint_pool_wins(db: Database) -> None:
    """target_profiles non-empty → first idle+online profile from the
    pool is returned, even if a project preset or capability match
    would have picked something else.

    Setup: two profiles in the hint pool, plus a "better" preset-bound
    profile the algorithm should NOT pick. Hint pool wins because the
    workflow author is the domain expert (per design doc Q4).
    """
    await _insert_agent(db, "a1")
    await _insert_agent(db, "a2")
    await _insert_agent(db, "a3")
    await _insert_profile(db, "p-hint-1", "a1", name="researcher")
    await _insert_profile(db, "p-hint-2", "a2", name="researcher")
    await _insert_profile(db, "p-preset", "a3", name="researcher")
    await _insert_project(db, "proj-1")
    # Preset binds role -> p-preset; the routing engine should ignore
    # this and use the hint pool instead.
    await _insert_preset(db, "preset-1", "proj-1", "p-preset", "researcher")

    step = {
        "agent_role": "researcher",
        "target_profiles": ["p-hint-1", "p-hint-2"],
        "required_capabilities": [],
    }
    chosen = await resolve_role_to_profile("proj-1", step, db)
    assert chosen["id"] == "p-hint-1", (
        f"hint pool should return p-hint-1 (first idle in pool); "
        f"got {chosen['id']!r}"
    )


@pytest.mark.asyncio
async def test_workflow_hint_pool_skipped_if_all_busy(db: Database) -> None:
    """All profiles in the hint pool are busy → fall through to next
    strategy. Here, no preset and no other online profiles exist, so
    the capability-match auto-fallback also fails → NoProfileAvailable.
    (The point of the test is the FALL-THROUGH — not raising on the
    busy hint pool alone.)

    "Busy" here = an in-flight task row (status='assigned' or
    'running') with `assigned_profile_id` set. That's the canonical
    signal the routing engine consults (the `agent_profiles.status`
    column mirrors it but can drift during transitions; the
    `tasks` table is the single source of truth).

    `required_capabilities` is set to a tag that no profile carries,
    so strategy 3 (capability match) can't accidentally pick a busy
    profile via the "empty required = any profile" shortcut.
    """
    await _insert_agent(db, "a1")
    await _insert_agent(db, "a2")
    await _insert_profile(db, "p-busy-1", "a1", name="researcher")
    await _insert_profile(db, "p-busy-2", "a2", name="researcher")
    await _insert_project(db, "proj-busy")
    # Create real in-flight tasks so `_is_profile_idle` sees them.
    await _insert_task(
        db, "t-1", "proj-busy", assigned_profile_id="p-busy-1",
        status="assigned",
    )
    await _insert_task(
        db, "t-2", "proj-busy", assigned_profile_id="p-busy-2",
        status="running",
    )

    step = {
        "agent_role": "researcher",
        "target_profiles": ["p-busy-1", "p-busy-2"],
        # A tag no profile carries — forces strategy 3 to also miss.
        "required_capabilities": ["nonexistent_capability"],
    }
    with pytest.raises(NoProfileAvailable) as excinfo:
        await resolve_role_to_profile("proj-busy", step, db)
    # The hint should mention the strategies the operator can use to
    # fix this (so the message is actionable, not just a wall of text).
    assert "target_profiles" in excinfo.value.hint


# === Strategy 2: Project preset binding ===

@pytest.mark.asyncio
async def test_preset_binding_used_when_no_hint(db: Database) -> None:
    """No target_profiles, preset exists → bound profile returned."""
    await _insert_agent(db, "a1")
    await _insert_profile(db, "p-preset", "a1", name="researcher")
    await _insert_project(db, "proj-1")
    await _insert_preset(db, "preset-1", "proj-1", "p-preset", "researcher")

    step = {"agent_role": "researcher"}  # no target_profiles
    chosen = await resolve_role_to_profile("proj-1", step, db)
    assert chosen["id"] == "p-preset"


# === Strategy 3: Capability match ===

@pytest.mark.asyncio
async def test_capability_match_when_no_preset(db: Database) -> None:
    """No hint, no preset, profile has matching skills → returned."""
    await _insert_agent(db, "a1")
    await _insert_profile(
        db, "p-cap", "a1", name="researcher",
        skills=["python", "pandas", "write_file"],
    )
    await _insert_project(db, "proj-cap")

    step = {
        "agent_role": "researcher",
        "required_capabilities": ["python", "pandas"],
    }
    chosen = await resolve_role_to_profile("proj-cap", step, db)
    assert chosen["id"] == "p-cap"


# === Strategy 4: Failure ===

@pytest.mark.asyncio
async def test_fail_with_actionable_hint_when_nothing_matches(db: Database) -> None:
    """No profile satisfies any strategy → NoProfileAvailable with a
    hint the operator can act on (not a bare exception message)."""
    # No agents at all — nothing online.
    await _insert_project(db, "proj-empty")

    step = {
        "agent_role": "ghost-role",
        "required_capabilities": ["nonexistent_capability"],
    }
    with pytest.raises(NoProfileAvailable) as excinfo:
        await resolve_role_to_profile("proj-empty", step, db)
    err = excinfo.value
    assert err.project_id == "proj-empty"
    assert err.role == "ghost-role"
    # Hint should mention the role and at least one fix path
    # (register a profile, add target_profiles, or wait for a slot).
    assert "ghost-role" in err.hint
    assert "Register a profile" in err.hint
    assert "target_profiles" in err.hint


# === Online check (heartbeat freshness) ===

@pytest.mark.asyncio
async def test_offline_profile_skipped(db: Database) -> None:
    """A profile whose parent agent has a stale heartbeat (> 90s) is
    skipped by every strategy. We prove it for strategy 3 (capability
    match) because that's the auto-fallback that would otherwise
    happily pick an offline profile.

    The `_HEARTBEAT_STALE_S` constant is exported from the module —
    use it to compute the exact age so the test stays in sync if the
    constant is tuned.
    """
    # Online profile (the one we expect to be chosen).
    await _insert_agent(db, "a-online")
    await _insert_profile(
        db, "p-online", "a-online", name="researcher",
        skills=["python"],
    )
    # Offline profile: heartbeat just past the staleness window.
    await _insert_agent(db, "a-offline", heartbeat_age_s=_HEARTBEAT_STALE_S + 5)
    await _insert_profile(
        db, "p-offline", "a-offline", name="researcher",
        skills=["python"],
    )
    await _insert_project(db, "proj-mix")

    step = {
        "agent_role": "researcher",
        "required_capabilities": ["python"],
    }
    chosen = await resolve_role_to_profile("proj-mix", step, db)
    assert chosen["id"] == "p-online", (
        f"offline profile (heartbeat > 90s) should be skipped; "
        f"got {chosen['id']!r}"
    )

    # Double-check via the helper directly — defense in depth, and
    # documents the helper's contract for future readers.
    assert await _is_profile_idle_and_online("p-online", db) is True
    assert await _is_profile_idle_and_online("p-offline", db) is False


# === _skills_cover unit check ===

def test_required_capabilities_subset_check() -> None:
    """_skills_cover is a strict subset check: empty required → True,
    empty profile + any required → False, partial overlap → False,
    exact superset → True. These are the four quadrants the helper
    can return True/False on; lock them down so future "optimizations"
    don't break the contract."""
    # True: empty required (no capability filter)
    assert _skills_cover(["a", "b", "c"], []) is True
    # True: required is a strict subset
    assert _skills_cover(["a", "b", "c"], ["b"]) is True
    assert _skills_cover(["a", "b", "c"], ["a", "c"]) is True
    assert _skills_cover(["a", "b", "c"], ["a", "b", "c"]) is True
    # False: required has a capability the profile lacks
    assert _skills_cover(["a"], ["b"]) is False
    assert _skills_cover(["a", "b"], ["a", "c"]) is False
    # False: empty profile + any required
    assert _skills_cover([], ["a"]) is False
    # False: empty profile + empty required
    # (per the docstring: "no capability filter means any profile
    # is fine" — but the helper returns True for the empty/empty
    # case because the required side has no constraints to violate)
    assert _skills_cover([], []) is True


# === Priority: preset beats free-floating capability match ===

@pytest.mark.asyncio
async def test_prefers_preset_over_capability_match(db: Database) -> None:
    """A preset-bound profile (strategy 2) wins over a different
    profile whose skills satisfy the required capabilities (strategy 3).

    The user's explicit binding is more authoritative than the
    auto-fallback — the workflow author may have set up the preset
    because the capability match would pick a profile they don't
    want (e.g. GPU host vs CPU host, both with the right skills).
    """
    # The "right" profile per capability match.
    await _insert_agent(db, "a-cap")
    await _insert_profile(
        db, "p-cap", "a-cap", name="researcher",
        skills=["python", "pandas"],
    )
    # The preset-bound profile — a different agent, NO matching skills,
    # but the user bound the role to it explicitly.
    await _insert_agent(db, "a-preset")
    await _insert_profile(
        db, "p-preset", "a-preset", name="researcher",
        skills=[],  # no capabilities — would lose capability match
    )
    await _insert_project(db, "proj-prio")
    await _insert_preset(
        db, "preset-1", "proj-prio", "p-preset", "researcher",
    )

    step = {
        "agent_role": "researcher",
        "required_capabilities": ["python", "pandas"],
    }
    chosen = await resolve_role_to_profile("proj-prio", step, db)
    assert chosen["id"] == "p-preset", (
        f"preset binding should win over capability match; "
        f"got {chosen['id']!r}"
    )
