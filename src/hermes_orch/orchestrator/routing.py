# coding: utf-8
"""SOUL routing engine — hybrid role→profile resolver (v3.9.0, Phase 1).

Picks the right `agent_profiles` row for a workflow step, using a
4-strategy fallback chain:

  1. Workflow hint pool  (`step.target_profiles`, if non-empty)
  2. Project preset binding (`project_soul_presets.role_name == step.agent_role`)
  3. Capability match     (`profile.skills ⊇ step.required_capabilities`)
  4. Fail with `NoProfileAvailable` + an actionable hint

The result is the `agent_profiles` row dict (matches what
`Database.fetchone` returns) — the orchestrator/soul_dispatch.py
downstream re-uses it as-is (or wraps it in `AgentProfile` for typed
access if it needs to).

Design doc: `docs/soul-routing-design.md` §"Algorithm: hybrid routing".

Why "hybrid" not "LLM-routed": every dispatch would pay an LLM round
trip + token cost, and the deterministic strategies cover 100% of
expected cases (workflow hint > project preset > capability match).
LLM is reserved for plan generation, not per-task routing decisions.

Public surface (importable by `orchestrator/soul_dispatch.py`):
  - `resolve_role_to_profile(project_id, step, db) -> dict`
  - `class NoProfileAvailable(Exception)` with `.hint` attribute

All other functions are module-private (`_` prefix). The 90s heartbeat
window matches `core/supervisor.py`'s stale-agent cutoff and
`api/dashboard.py`'s online-count query — keep them in sync if you
change one.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Mapping

from hermes_orch.api.projects import get_soul_preset_by_role
from hermes_orch.db import Database
from hermes_orch.utils import now_aware, now_iso


# === Constants ===

# Heartbeat freshness window. A profile is "online" only if its parent
# agent's `last_heartbeat_at` is within this many seconds of now AND
# the agent status is 'verified'. Matches the dashboard's green-dot
# logic and the supervisor's stale-agent sweep.
_HEARTBEAT_STALE_S = 90

# SQL fragment used by `_is_profile_idle`. A profile is "idle" when
# it has no tasks in 'assigned' or 'running' state. This is the same
# pair the supervisor's _assign_task treats as "in flight" (see
# core/supervisor.py:_assign_task); the wrapper's heartbeat path
# eventually promotes 'assigned' → 'running' → terminal and clears
# agent_profiles.current_task_id alongside.
_IDLE_TASK_QUERY = (
    "SELECT COUNT(*) AS n FROM tasks "
    "WHERE assigned_profile_id = ? AND status IN ('assigned', 'running')"
)


# === Custom exception ===

class NoProfileAvailable(Exception):
    """Raised when the routing engine cannot find any profile to
    handle a workflow step.

    Attributes:
        project_id: the project the dispatch is for.
        role: the workflow step's `agent_role` (what the project asked for).
        hint: user-facing actionable message — register a profile, or
            add `target_profiles` to the workflow step. The API layer
            surfaces this string to the operator verbatim.
    """

    def __init__(self, project_id: str, role: str, hint: str) -> None:
        super().__init__(
            f"No profile available for project={project_id!r} role={role!r}"
        )
        self.project_id = project_id
        self.role = role
        self.hint = hint


# === Public API ===

async def resolve_role_to_profile(
    project_id: str,
    step: Any,
    db: Database,
) -> dict[str, Any]:
    """Pick the best `agent_profiles` row for a workflow step.

    Tries strategies in order until one yields an idle+online profile:

      1. If `step.target_profiles` is non-empty, return the first
         idle+online profile from that list (workflow author wins).
      2. Else, look up the project's preset for `step.agent_role` and
         return its bound profile if idle+online.
      3. Else, scan all online profiles for one whose `skills` covers
         `step.required_capabilities` and is idle.
      4. Else, raise `NoProfileAvailable` with an actionable hint.

    Args:
        project_id: the project owning the dispatch.
        step: any object with `agent_role` (str), and optionally
            `target_profiles` (list[str]) and `required_capabilities`
            (list[str]). Pydantic `PlanStep` and plain dicts both work
            (defensive `getattr` / `__getitem__`). New fields added by
            the v3.9.0 visual plan editor (per the design doc) flow
            through here unchanged.
        db: connected `Database` instance (the orchestrator shares
            one DB with the API layer — no separate connection).

    Returns:
        The `agent_profiles` row dict (same shape as
        `Database.fetchone(...)` returns). Callers may wrap it in
        `AgentProfile` (api.agents._row_to_profile) if they need a
        typed object.

    Raises:
        NoProfileAvailable: no profile in the fleet satisfies any
            strategy. The `.hint` attribute carries the user-facing
            next-step message.
    """
    role = _step_field(step, "agent_role", "")
    if not role:
        # Should be caught earlier (workflow validation), but a bare
        # empty role gives us nothing to route on — fail loudly.
        raise NoProfileAvailable(
            project_id=project_id,
            role="",
            hint=(
                "Workflow step has no `agent_role`. Edit the step in the "
                "visual plan editor and set `agent_role` to the role this "
                "step should run as (e.g. 'cpi-analyst')."
            ),
        )

    # 1. Workflow hint pool — author says "use one of these".
    hint_profiles: list[str] = list(
        _step_field(step, "target_profiles", None) or []
    )
    for pid in hint_profiles:
        if await _is_profile_idle_and_online(pid, db):
            row = await _get_profile_row(pid, db)
            if row is not None:
                return row
    # Hint pool exhausted (or empty) — fall through.

    # 2. Project preset binding — user may have hand-bound a profile.
    preset = await get_soul_preset_by_role(db, project_id, role)
    if preset:
        bound_pid = preset.get("profile_id")
        if bound_pid and await _is_profile_idle_and_online(bound_pid, db):
            row = await _get_profile_row(bound_pid, db)
            if row is not None:
                return row

    # 3. Capability match — scan all online profiles, find one whose
    #    skills ⊇ step.required_capabilities AND is idle.
    #
    # v3.10.1 (2026-08-02) BUGFIX: prefer profiles whose `name` exactly
    # matches the step's `agent_role` before falling back to the
    # alphabetical-by-id first-match. Without this preference, a step
    # with empty `required_capabilities` (the common case for
    # general-purpose roles like "super" or "win-agent01") would
    # silently land on the lexicographically first idle profile in
    # the system — which is usually the WRONG agent (e.g. a step
    # requesting `agent_role="win-agent01"` ended up on
    # `linux-a-01/super-b` because `l < w` in agent_id sort order, and
    # `linux-a-01/super-b` came up first as both idle and skills⊇[]).
    # The matching-by-name scan runs first; only if NO profile has
    # `name == role` do we fall back to the legacy skills-match loop
    # (so workflows that rely on skills for routing keep working).
    required: list[str] = list(
        _step_field(step, "required_capabilities", None) or []
    )
    # 3a. Prefer same-name profile — if any agent has registered a
    # profile with the exact name the step requested, that's almost
    # always what the operator wants.
    for prof in await _list_online_profiles(db):
        if prof.get("name") != role:
            continue
        if not await _is_profile_idle(prof["id"], db):
            continue
        if _skills_cover(_parse_skills(prof.get("skills")), required):
            return prof
    # 3b. Legacy capability-only match (kept for skills-based routing
    # of roles that don't have a same-name profile registered).
    for prof in await _list_online_profiles(db):
        if not await _is_profile_idle(prof["id"], db):
            continue
        if _skills_cover(_parse_skills(prof.get("skills")), required):
            return prof

    # 4. No match — fail with an actionable hint.
    raise NoProfileAvailable(
        project_id=project_id,
        role=role,
        hint=(
            f"No idle+online profile can satisfy role={role!r} for project "
            f"{project_id!r}. Fix one of:\n"
            f"  - Register a profile with `agent_role`={role!r} "
            f"(set skills so it matches what this step needs).\n"
            f"  - Or add `target_profiles` to this workflow step to "
            f"restrict routing to a specific profile pool.\n"
            f"  - Or wait for an in-flight task on an existing profile to "
            f"complete (check the /agents page for online + idle profiles)."
        ),
    )


# === Private helpers ===

async def _list_online_profiles(db: Database) -> list[dict[str, Any]]:
    """Return all `agent_profiles` rows whose parent agent is online.

    "Online" = `agents.status = 'verified'` AND
    `agents.last_heartbeat_at >= now - 90s` (see `_HEARTBEAT_STALE_S`).

    Ordered by (agent_id, profile_id) for deterministic selection when
    multiple profiles tie (the first idle+online match wins).

    Note: this function deliberately does NOT check the tasks table
    for in-flight tasks. The capability-match strategy (step 3) calls
    `_is_profile_idle` per-row before returning — that keeps the
    candidate list small (only online profiles) while still doing the
    authoritative busy check before committing to a profile.
    """
    cutoff = (now_aware() - timedelta(seconds=_HEARTBEAT_STALE_S)).isoformat()
    return await db.fetchall(
        "SELECT ap.* FROM agent_profiles ap "
        "JOIN agents a ON a.id = ap.agent_id "
        "WHERE a.status = 'verified' AND a.last_heartbeat_at >= ? "
        "ORDER BY a.id, ap.id",
        (cutoff,),
    )


async def _is_profile_idle(profile_id: str, db: Database) -> bool:
    """True iff the profile has no in-flight task (assigned/running).

    The `agent_profiles.current_task_id` column is the fast signal, but
    it can lag the `tasks` table during transitions (a task is
    'running' before the profile row is updated, etc.). For the
    routing decision we want a *correct* answer, not the fastest one —
    so we count active rows in the `tasks` table.
    """
    row = await db.fetchone(_IDLE_TASK_QUERY, (profile_id,))
    return not (row and int(row.get("n") or 0) > 0)


async def _is_profile_idle_and_online(
    profile_id: str, db: Database
) -> bool:
    """Combined check: profile exists, parent agent is online, and the
    profile has no in-flight task.

    Used by the workflow-hint and preset-binding strategies (steps 1
    and 2 of the hybrid algorithm). The capability-match strategy
    (step 3) re-checks both conditions inline so it can return the
    full row alongside the boolean decision.
    """
    cutoff = (now_aware() - timedelta(seconds=_HEARTBEAT_STALE_S)).isoformat()
    row = await db.fetchone(
        "SELECT ap.id FROM agent_profiles ap "
        "JOIN agents a ON a.id = ap.agent_id "
        "WHERE ap.id = ? "
        "AND a.status = 'verified' "
        "AND a.last_heartbeat_at >= ?",
        (profile_id, cutoff),
    )
    if row is None:
        return False
    return await _is_profile_idle(profile_id, db)


def _skills_cover(
    profile_skills: list[str],
    required_capabilities: list[str],
) -> bool:
    """True iff `required_capabilities` is a subset of `profile_skills`.

    Order-insensitive. Empty `required_capabilities` is satisfied by
    any profile (no capability filter means "any profile is fine").
    An entry missing from both lists still returns False — empty
    `profile_skills` and a non-empty requirement never matches.

    This is the "set cover" check the capability-match strategy uses.
    The matching is exact-string (not substring / regex) — the design
    doc keeps the skill taxonomy loose but exact-match avoids the
    "python" matching "python3" or "cpython" footgun. The workflow
    author is expected to declare full canonical names.
    """
    if not required_capabilities:
        return True
    if not profile_skills:
        return False
    have = set(profile_skills)
    need = set(required_capabilities)
    return need.issubset(have)


def _now_iso() -> str:
    """Local-time ISO-8601 timestamp (with offset).

    Thin re-export of `hermes_orch.utils.now_iso`. Lives at module
    scope so tests can monkeypatch a fixed clock without reaching
    into the utils module.
    """
    return now_iso()


def _step_field(step: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a Pydantic-style object or a dict.

    Pydantic models support `getattr` natively; dicts need
    `__getitem__`. This helper bridges the two so the routing
    function works with both `PlanStep` instances (the validated
    LLM contract) and the LLM's in-memory draft (plain dicts).

    New fields added by the v3.9.0 visual plan editor
    (`target_profiles`, `required_capabilities`, `default_soul`)
    are not yet on `PlanStep` — they return `default` until that
    schema bump lands. That's the backward-compat behavior the
    design doc calls out in the migration plan.
    """
    if step is None:
        return default
    if isinstance(step, Mapping):
        return step.get(name, default)
    return getattr(step, name, default)


def _parse_skills(raw: Any) -> list[str]:
    """Parse `agent_profiles.skills` (JSON text) into a list[str].

    Defensive against the three real-world shapes the column can take:

      - `None` (pre-migration DB; the column doesn't exist) → `[]`
      - JSON string `'["web_search", "python"]'` (normal) → list
      - Python `list` (in-memory test fixture) → returned as-is
      - Malformed JSON (corrupted write) → `[]` (matches the
        `_row_to_profile` parser's defensive behavior; we don't
        want a bad skill string to block all routing)
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(s) for s in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(s) for s in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
    return []


async def _get_profile_row(
    profile_id: str, db: Database
) -> dict[str, Any] | None:
    """Fetch a single `agent_profiles` row by id.

    Returns None if the row was deleted between the
    idle+online check and the fetch (a rare race; the caller
    handles the None by falling through to the next strategy).
    """
    return await db.fetchone(
        "SELECT * FROM agent_profiles WHERE id = ?", (profile_id,)
    )
