"""Tests for the ROLE CONTEXT block in `_build_chat_context` (v3.9.0).

The chat LLM gets a per-project snapshot that now includes the
project's SOUL presets as `soul_presets` (Phase 2 UX of
docs/soul-routing-design.md). The LLM uses these to design plan
steps whose `agent_role` matches a preset and whose `action`
aligns with the persona.

These tests verify the *data plumbing only* — the prompt-side
rules ("prefer role_name from presets") are in the system prompt
text and are tested manually / via e2e. What's testable in unit
form is the size cap + presence/absence contract.

Test pattern: in-process `Database(Path(tmpdir)/"test.db")` —
same shape as tests/test_orchestrator_routing.py and
tests/test_orchestrator_soul_dispatch.py. Each test stands up
its own project + agent + profile + preset rows so the
chat-context builder exercises the real code path with no
cross-test bleed.

4 tests:
  1. test_chat_context_includes_soul_presets_when_present
  2. test_chat_context_empty_soul_presets_for_new_project
  3. test_chat_context_truncates_oversized_preset
  4. test_chat_context_truncates_total_above_4kb
"""
from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from hermes_orch.api.projects import (
    _CHAT_SYSTEM_PROMPT,
    _MAX_PRESET_BYTES,
    _MAX_TOTAL_PRESETS_BYTES,
    _build_chat_context,
)
from hermes_orch.db import Database


# ===== Fixtures =====


@pytest_asyncio.fixture
async def db():
    """Fresh in-memory-ish DB per test (tmpfile for clean teardown).

    Same shape as tests/test_orchestrator_routing.py —
    `Database(Path(tmpdir)/"test.db")` against a real file path so
    the path behaves identically to production and the tests don't
    leak state across event loops.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="chat_context_soul_"))
    database = Database(tmpdir / "test.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def _insert_project(db: Database, project_id: str) -> None:
    await db.insert(
        "projects",
        {"id": project_id, "name": "chat-context-soul test", "state": "ready"},
    )


async def _insert_agent(db: Database, agent_id: str) -> None:
    """Insert a parent `agents` row (verified). Required because
    `agent_profiles` has a FK to `agents`."""
    from datetime import datetime

    await db.insert(
        "agents",
        {
            "id": agent_id,
            "secret_hash": "x" * 64,
            "status": "verified",
            "last_heartbeat_at": datetime.now().astimezone().isoformat(),
        },
    )


async def _insert_profile(
    db: Database,
    profile_id: str,
    agent_id: str,
    *,
    name: str = "researcher",
) -> None:
    """Insert an `agent_profiles` row. Required because
    `project_soul_presets` has a FK to `agent_profiles`."""
    await db.insert(
        "agent_profiles",
        {
            "id": profile_id,
            "agent_id": agent_id,
            "name": name,
            "status": "idle",
            "skills": json.dumps([]),
        },
    )


async def _insert_preset(
    db: Database,
    preset_id: str,
    project_id: str,
    profile_id: str,
    role_name: str,
    *,
    content: str = "default persona body",
    default_soul: str | None = None,
    updated_at: str | None = None,
) -> None:
    """Insert a `project_soul_presets` row. `updated_at` is explicit
    so we can control the ORDER BY in the chat context query (most
    recently updated first). Defaults to now-ish."""
    from hermes_orch.utils import now_iso

    await db.insert(
        "project_soul_presets",
        {
            "id": preset_id,
            "project_id": project_id,
            "profile_id": profile_id,
            "role_name": role_name,
            "content": content,
            "default_soul": default_soul,
            "updated_at": updated_at or now_iso(),
        },
    )


# ===== Test 1: presets are included with role_name + content =====


@pytest.mark.asyncio
async def test_chat_context_includes_soul_presets_when_present(db: Database) -> None:
    """2 presets in DB → context has both, with role_name + content
    + default_soul populated. Order is most-recently-updated first."""
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    agent_id = "agent-1"
    profile_a = f"prof-{uuid.uuid4().hex[:6]}"
    profile_b = f"prof-{uuid.uuid4().hex[:6]}"
    preset_a = f"preset-{uuid.uuid4().hex[:6]}"
    preset_b = f"preset-{uuid.uuid4().hex[:6]}"
    await _insert_project(db, project_id)
    await _insert_agent(db, agent_id)
    await _insert_profile(db, profile_a, agent_id, name="cpi-analyst")
    await _insert_profile(db, profile_b, agent_id, name="researcher")
    # Insert b first (older), then a (newer) so a comes first when
    # ORDER BY updated_at DESC.
    await _insert_preset(
        db, preset_b, project_id, profile_b,
        role_name="researcher",
        content="You are a researcher. Be thorough.",
        default_soul="Default researcher voice",
        updated_at="2026-08-01T10:00:00+00:00",
    )
    await _insert_preset(
        db, preset_a, project_id, profile_a,
        role_name="cpi-analyst",
        content="You are a CPI analyst. Be precise.",
        default_soul="Default analyst voice",
        updated_at="2026-08-01T11:00:00+00:00",
    )
    ctx = await _build_chat_context(project_id, db)
    assert ctx is not None
    soul_presets = ctx["soul_presets"]
    assert len(soul_presets) == 2
    # Newer first
    assert soul_presets[0]["role_name"] == "cpi-analyst"
    assert soul_presets[0]["content"] == "You are a CPI analyst. Be precise."
    assert soul_presets[0]["default_soul"] == "Default analyst voice"
    assert soul_presets[0]["profile_id"] == profile_a
    assert soul_presets[1]["role_name"] == "researcher"
    assert soul_presets[1]["content"] == "You are a researcher. Be thorough."
    assert soul_presets[1]["default_soul"] == "Default researcher voice"
    # No truncation
    assert ctx["truncated"] is False


# ===== Test 2: empty list for projects with no presets =====


@pytest.mark.asyncio
async def test_chat_context_empty_soul_presets_for_new_project(db: Database) -> None:
    """A brand-new project with zero presets → context has
    `soul_presets: []` and `truncated: false`. The LLM treats this
    as 'no role context, design freely'."""
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    await _insert_project(db, project_id)
    ctx = await _build_chat_context(project_id, db)
    assert ctx is not None
    assert ctx["soul_presets"] == []
    assert ctx["truncated"] is False
    # Sanity: other top-level keys still present (didn't break the snapshot)
    assert "project" in ctx
    assert "plan" in ctx
    assert "agents_info" in ctx
    assert "audit_tail" in ctx
    assert "plan_updated_at" in ctx


# ===== Test 3: per-preset content cap (1 KB) =====


@pytest.mark.asyncio
async def test_chat_context_truncates_oversized_preset(db: Database) -> None:
    """A preset with 2 KB content → content is truncated to ≤ 1 KB
    in the snapshot, and `truncated: true` is set. The LLM
    should treat the persona as a hint (it's a fragment)."""
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    agent_id = "agent-1"
    profile_id = f"prof-{uuid.uuid4().hex[:6]}"
    preset_id = f"preset-{uuid.uuid4().hex[:6]}"
    await _insert_project(db, project_id)
    await _insert_agent(db, agent_id)
    await _insert_profile(db, profile_id, agent_id)
    # 2 KB content (2048 bytes of ASCII)
    oversized_content = "x" * 2048
    assert len(oversized_content.encode("utf-8")) == 2048
    await _insert_preset(
        db, preset_id, project_id, profile_id,
        role_name="cpi-analyst",
        content=oversized_content,
        default_soul="short default",
    )
    ctx = await _build_chat_context(project_id, db)
    assert ctx is not None
    soul_presets = ctx["soul_presets"]
    assert len(soul_presets) == 1
    # Per-preset cap honored
    assert len(soul_presets[0]["content"].encode("utf-8")) <= _MAX_PRESET_BYTES
    # The truncated content should be a prefix of the original
    # (we drop the tail, not arbitrarily rewrite).
    assert oversized_content.startswith(soul_presets[0]["content"])
    # default_soul + role_name are NOT truncated (only `content` is)
    assert soul_presets[0]["role_name"] == "cpi-analyst"
    assert soul_presets[0]["default_soul"] == "short default"
    # Truncation flag set
    assert ctx["truncated"] is True


# ===== Test 4: total cap (4 KB) drops presets from the end =====


@pytest.mark.asyncio
async def test_chat_context_truncates_total_above_4kb(db: Database) -> None:
    """5 presets × ~1 KB each → total capped to ≤ 4 KB. The
    trailing presets are dropped (we never partially truncate a
    single preset to fit). `truncated: true` is set."""
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    agent_id = "agent-1"
    await _insert_project(db, project_id)
    await _insert_agent(db, agent_id)
    # 5 presets × ~1 KB content each. Per-preset cap is 1024 bytes;
    # we use content of exactly 1024 bytes so the per-preset cap
    # doesn't kick in (it's ≤ 1024, not > 1024).
    preset_size = 1024
    n_presets = 5
    inserted_ids: list[str] = []
    for i in range(n_presets):
        profile_id = f"prof-{i}-{uuid.uuid4().hex[:6]}"
        preset_id = f"preset-{i}-{uuid.uuid4().hex[:6]}"
        await _insert_profile(db, profile_id, agent_id, name=f"role-{i}")
        await _insert_preset(
            db, preset_id, project_id, profile_id,
            role_name=f"role-{i}",
            content="a" * preset_size,
            default_soul="",
            # Older presets first, newer last — so the cap drops
            # from the END (newer first by updated_at DESC).
            updated_at=f"2026-08-01T10:0{i}:00+00:00",
        )
        inserted_ids.append(preset_id)
    ctx = await _build_chat_context(project_id, db)
    assert ctx is not None
    soul_presets = ctx["soul_presets"]
    # The total bytes (sum of json.dumps of each preset) must be
    # ≤ 4 KB. The exact count of dropped presets depends on the
    # per-preset JSON overhead (role_name + profile_id + default_soul
    # fields), but with 1024-byte content we can fit ~3 presets
    # (each ~1100 bytes serialized) before hitting the 4 KB cap.
    total = sum(
        len(json.dumps(p, ensure_ascii=False).encode("utf-8"))
        for p in soul_presets
    )
    assert total <= _MAX_TOTAL_PRESETS_BYTES, (
        f"total {total} bytes exceeds 4 KB cap; got {len(soul_presets)} presets"
    )
    # Some presets must have been dropped
    assert len(soul_presets) < n_presets
    # Truncation flag set
    assert ctx["truncated"] is True
    # The kept presets are a contiguous prefix in DESC updated_at
    # order (we process newest-first and drop from the end, so
    # the older tail is sacrificed). With role-4 as the newest,
    # the first kept preset should be role-4, and the kept
    # names should be a descending sequence with no gaps.
    kept_names = [p["role_name"] for p in soul_presets]
    assert kept_names[0] == "role-4", f"newest should be first, got {kept_names}"
    for i, name in enumerate(kept_names):
        expected_idx = n_presets - 1 - i
        assert name == f"role-{expected_idx}", (
            f"contiguous DESC prefix broken at index {i}: "
            f"got {name}, expected role-{expected_idx}"
        )


# ===== Bonus: confirm the system prompt advertises ROLE CONTEXT =====


def test_chat_system_prompt_mentions_role_context() -> None:
    """Defensive: confirm `_CHAT_SYSTEM_PROMPT` was updated with
    the ROLE CONTEXT section + the 6 rules. If a future refactor
    drops the section, this test catches it before the LLM starts
    designing free-form roles again."""
    assert "ROLE CONTEXT" in _CHAT_SYSTEM_PROMPT
    assert "soul_presets" in _CHAT_SYSTEM_PROMPT
    # The 6 rules are surfaced as numbered items
    assert "1." in _CHAT_SYSTEM_PROMPT  # Prefer role_name
    assert "2." in _CHAT_SYSTEM_PROMPT  # Copy default_soul
    assert "3." in _CHAT_SYSTEM_PROMPT  # Empty = design freely
    assert "4." in _CHAT_SYSTEM_PROMPT  # Don't add presets in update_plan
    assert "5." in _CHAT_SYSTEM_PROMPT  # Truncation handling
    assert "6." in _CHAT_SYSTEM_PROMPT  # v3.10.0: LLM drafts default_soul for new roles
    # Rule 4 explicit text
    assert "update_plan" in _CHAT_SYSTEM_PROMPT
    assert "_ensure_soul_preset" in _CHAT_SYSTEM_PROMPT
    # Rule 6 (v3.10.0) explicit text — both-preset-and-llm behavior
    assert "v3.10.0" in _CHAT_SYSTEM_PROMPT
    assert "NO preset" in _CHAT_SYSTEM_PROMPT or "no preset" in _CHAT_SYSTEM_PROMPT
