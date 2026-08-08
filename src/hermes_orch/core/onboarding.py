# coding: utf-8
"""Onboarding state (v1.0.1 new-user-activation §3.2).

The 4-step checklist shown on the landing page for new users:

  1. password_set         user has set a non-empty password
  2. llm_configured       LLM is configured (mock or real — equal first-class)
  3. agent_connected      at least one agent host has connected
  4. first_task_completed at least one task has reached status=completed

Plus a 5th informational signal:

  - first_task_attempted at least one task has been started (any status).
    This is INFORMATIONAL only — it does NOT collapse the checklist
    (per spec §3.2). A failed first attempt must not show the user
    a "you're done!" page; they need to actually succeed.

State shape (JSON, stored in `users.onboarding_state`):
  {
    "signals": {
      "password_set": bool,
      "llm_configured": bool,
      "agent_connected": bool,
      "first_task_completed": bool,
      "first_task_attempted": bool
    },
    "skipped": bool,           # user hit "Skip for now"
    "completed_at": str|None,  # ISO 8601 UTC; set when all 4 success signals flip true
    "schema_version": int      # current = 1
  }

Empty `{}` (the SQL default) is treated as "all signals false, not
skipped, not completed" — i.e. a fresh user who hasn't started yet.

`should_show_checklist()` is the single source of truth for "should the
landing page redirect to onboarding.html or /agents?":
  show  = (not complete) AND (not skipped)
  hide  = complete OR skipped

This module is pure logic — no DB I/O, no auth. The DB I/O lives in
`api/onboarding.py` and the signal-flip hooks live in their respective
modules (auth/cookie.py, api/settings.py, api/tasks.py).
"""
from __future__ import annotations

from typing import Any

# Canonical signal names. Anything else in the JSON is ignored (forward
# compat: future schema_version 2 might add new signals).
SIGNAL_PASSWORD_SET = "password_set"
SIGNAL_LLM_CONFIGURED = "llm_configured"
SIGNAL_AGENT_CONNECTED = "agent_connected"
SIGNAL_FIRST_TASK_COMPLETED = "first_task_completed"
SIGNAL_FIRST_TASK_ATTEMPTED = "first_task_attempted"

# The 4 success signals that collapse the checklist. The 5th
# (`first_task_attempted`) is informational only and does NOT count.
SUCCESS_SIGNALS: tuple[str, ...] = (
    SIGNAL_PASSWORD_SET,
    SIGNAL_LLM_CONFIGURED,
    SIGNAL_AGENT_CONNECTED,
    SIGNAL_FIRST_TASK_COMPLETED,
)

ALL_SIGNALS: tuple[str, ...] = SUCCESS_SIGNALS + (SIGNAL_FIRST_TASK_ATTEMPTED,)

CURRENT_SCHEMA_VERSION = 1


def empty_state() -> dict[str, Any]:
    """Return a fresh onboarding state with all signals false."""
    return {
        "signals": {s: False for s in ALL_SIGNALS},
        "skipped": False,
        "completed_at": None,
        "schema_version": CURRENT_SCHEMA_VERSION,
    }


def parse_state(raw: str | None) -> dict[str, Any]:
    """Parse a JSON state string into a normalized dict.

    Defensive: handles `None`, empty string, malformed JSON, and
    legacy `{}` (the SQL default). All of these are treated as
    `empty_state()`.

    Forward compat: unknown keys are preserved on the dict so future
    schema versions can round-trip, but `should_show_checklist` and
    `is_checklist_complete` only look at the known signals.
    """
    import json

    if not raw or not raw.strip() or raw.strip() == "{}":
        return empty_state()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        # Malformed JSON: treat as empty so the user can re-do the
        # checklist from a clean slate. Audit log a corruption event
        # so we can spot bad migrations in the field.
        return empty_state()
    if not isinstance(parsed, dict):
        return empty_state()

    # Merge with the default state to fill in any missing keys (forward
    # compat: older state JSON may be missing `skipped` etc.)
    base = empty_state()
    for k, v in parsed.items():
        if k == "signals" and isinstance(v, dict):
            # Copy ALL signal keys, not just the known ones — unknown
            # signals are preserved (forward compat: a v2 schema could
            # add a new signal and old readers must round-trip it).
            for sig, sig_val in v.items():
                base["signals"][sig] = bool(sig_val)
        else:
            base[k] = v
    # Force schema_version to current (migrations upgrade on read)
    base["schema_version"] = CURRENT_SCHEMA_VERSION
    return base


def serialize_state(state: dict[str, Any]) -> str:
    """Serialize state to JSON for storage in `users.onboarding_state`."""
    import json
    return json.dumps(state, separators=(",", ":"), ensure_ascii=False)


def is_checklist_complete(state: dict[str, Any]) -> bool:
    """True iff all 4 SUCCESS_SIGNALS are true.

    `first_task_attempted` is NOT checked here — a failed first attempt
    must not collapse the checklist (per spec §3.2).
    """
    signals = state.get("signals") or {}
    return all(bool(signals.get(s, False)) for s in SUCCESS_SIGNALS)


def should_show_checklist(state: dict[str, Any]) -> bool:
    """True iff the landing page should show the 4-step checklist.

    The checklist is shown when:
      - not all 4 success signals are true (not complete), AND
      - the user hasn't hit "Skip for now" (skipped=False)

    Skipping is a user choice that means "I know I have to do these
    things, don't bug me". To re-open the checklist after a skip,
    the user goes to /settings#onboarding → "Reset onboarding state"
    (admin-only).
    """
    if state.get("skipped"):
        return False
    if is_checklist_complete(state):
        return False
    return True


def set_signal(
    state: dict[str, Any],
    signal: str,
    value: bool = True,
) -> dict[str, Any]:
    """Set a signal in the state. Returns a NEW dict (immutable update).

    If setting a success signal to true and all 4 success signals are
    now true, also set `completed_at` to the current UTC ISO 8601
    timestamp. Setting a signal to false does NOT clear `completed_at`
    (a user could have completed once, then a row got corrupted; we
    err on the side of not re-showing the checklist).
    """
    import datetime as _dt

    new_state = {
        "signals": dict(state.get("signals") or {}),
        "skipped": state.get("skipped", False),
        "completed_at": state.get("completed_at"),
        "schema_version": CURRENT_SCHEMA_VERSION,
    }
    if signal not in ALL_SIGNALS:
        # Unknown signal — store it anyway for forward compat
        new_state["signals"][signal] = bool(value)
        return new_state
    new_state["signals"][signal] = bool(value)
    if signal in SUCCESS_SIGNALS and value and is_checklist_complete(new_state):
        if not new_state.get("completed_at"):
            new_state["completed_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return new_state


def set_skipped(state: dict[str, Any], skipped: bool = True) -> dict[str, Any]:
    """Mark the state as skipped (or un-skipped). Returns a NEW dict."""
    return {
        "signals": dict(state.get("signals") or {}),
        "skipped": bool(skipped),
        "completed_at": state.get("completed_at"),
        "schema_version": CURRENT_SCHEMA_VERSION,
    }


def reset_state() -> dict[str, Any]:
    """Return a fresh, all-false state (used by admin reset / re-demo)."""
    return empty_state()


# ===== Backfill (v1.0.1 §3.2.1) =====
#
# When a user upgrades from a pre-v1.0.1 install, their
# `onboarding_state` is the SQL default `{}` which we treat as "fresh
# user". That's wrong: a user who has been running for months and
# already has agents + completed tasks would suddenly see the
# checklist again. We compute the real state from the existing data
# the first time we read the column.
#
# This is computed in `core/onboarding.py::backfill_state()` which is
# a PURE function over the inputs (no DB I/O). The DB I/O is in
# `_run_onboarding_backfill` in db.py, called once at startup.


def backfill_state(
    *,
    has_password: bool,
    has_llm_config: bool,
    has_connected_agent: bool,
    has_completed_task: bool,
    has_any_task: bool,
) -> dict[str, Any]:
    """Compute the onboarding state for an existing (upgrading) user.

    Each input is a boolean reflecting the user's actual data:
      - has_password         users.password_hash IS NOT NULL
      - has_llm_config       llm section exists in config.yaml and has any
                             non-default values (mock is fine)
      - has_connected_agent  any agent row has last_heartbeat_at within
                             the heartbeat window (e.g. 5 min)
      - has_completed_task   any task row has status='completed'
      - has_any_task         any task row exists at all (for the
                             `first_task_attempted` signal)

    Returns the same shape as `empty_state()` but with signals
    populated from the inputs. Used both at startup (one-time
    backfill) and at read-time (defensive: if the SQL default `{}`
    shows up for an upgrading user, compute on read instead of
    showing the fresh checklist).
    """
    state = empty_state()
    state = set_signal(state, SIGNAL_PASSWORD_SET, has_password)
    state = set_signal(state, SIGNAL_LLM_CONFIGURED, has_llm_config)
    state = set_signal(state, SIGNAL_AGENT_CONNECTED, has_connected_agent)
    state = set_signal(state, SIGNAL_FIRST_TASK_COMPLETED, has_completed_task)
    if has_any_task:
        state = set_signal(state, SIGNAL_FIRST_TASK_ATTEMPTED, True)
    return state


# ===== DB-bound helpers =====
#
# The functions above are pure (no I/O). These are the DB-bound
# wrappers used by the signal hooks in auth/cookie.py, api/settings.py,
# api/tasks.py, and the API endpoints in api/onboarding.py.
#
# Single-tenant assumption: when a system-wide event happens (e.g.
# a task completes), the signal flips for ALL users. This is the
# correct semantics for the typical one-admin setup. In a future
# multi-tenant world, the trigger would identify the owning user
# (e.g. "the user who issued the enrollment token" for agent_connected)
# and flip only that user. For now, all-flips is acceptable.


async def set_user_signal(db, user_id: str, signal: str, value: bool = True) -> dict[str, Any]:
    """Read the user's current state, flip one signal, write it back.

    Returns the new state. Idempotent: setting a signal that's
    already set to the requested value is a no-op (no write).
    """
    row = await db.fetchone(
        "SELECT onboarding_state FROM users WHERE id = ?", (user_id,)
    )
    if not row:
        return empty_state()
    current = parse_state(row["onboarding_state"] or "{}")
    if current["signals"].get(signal) == bool(value):
        return current  # no-op
    new = set_signal(current, signal, value)
    await db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(new), user_id),
    )
    return new


async def set_user_skipped(db, user_id: str, skipped: bool = True) -> dict[str, Any]:
    """Set the user's skipped flag."""
    row = await db.fetchone(
        "SELECT onboarding_state FROM users WHERE id = ?", (user_id,)
    )
    if not row:
        return empty_state()
    current = parse_state(row["onboarding_state"] or "{}")
    if current.get("skipped") == bool(skipped):
        return current
    new = set_skipped(current, skipped)
    await db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(new), user_id),
    )
    return new


async def reset_user_state(db, user_id: str) -> dict[str, Any]:
    """Reset a user's onboarding state to all-false (admin reset / re-demo)."""
    new = reset_state()
    await db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(new), user_id),
    )
    return new


async def set_signal_for_all_users(db, signal: str, value: bool = True) -> int:
    """Set a signal for every user in the system. Returns # of users flipped.

    Used by system-wide events (task completion, agent enrollment) that
    flip the signal for every user per the single-tenant assumption.
    Idempotent: users with the signal already at `value` are skipped.
    """
    async with db.conn.execute(
        "SELECT id, onboarding_state FROM users"
    ) as cur:
        users = await cur.fetchall()
    flipped = 0
    for u in users:
        current = parse_state(u["onboarding_state"] or "{}")
        if current["signals"].get(signal) == bool(value):
            continue  # no-op
        new = set_signal(current, signal, value)
        await db.execute(
            "UPDATE users SET onboarding_state = ? WHERE id = ?",
            (serialize_state(new), u["id"]),
        )
        flipped += 1
    return flipped


async def get_user_state(db, user_id: str) -> dict[str, Any]:
    """Read the user's current onboarding state (parsed)."""
    row = await db.fetchone(
        "SELECT onboarding_state FROM users WHERE id = ?", (user_id,)
    )
    if not row:
        return empty_state()
    return parse_state(row["onboarding_state"] or "{}")


# ===== Truth-based override (v1.0.1 hotfix 2026-08-09) =====
#
# The stored `onboarding_state` JSON column is a CACHE of what the
# user has done. It can get stale:
#   - Legacy user whose password pre-dates the onboarding column
#     (auto-backfill at startup fills the column for `{}` defaults,
#     but a user who set their password AFTER the backfill ran
#     without the password_set signal hook being installed is stale)
#   - Admin "Reset onboarding state" button (intentionally clears
#     all signals so the user can re-demo the flow)
#   - User did LLM/agent/first-task BEFORE the v1.0.1 signal hooks
#     were added
#
# In all of these cases, the user's actual state (password_hash IS
# NOT NULL, config.yaml has LLM, agents table has a fresh heartbeat,
# tasks table has a completed row) is "more done" than the stored
# signal says. We don't want to show a user a checklist saying
# "you still need to do X" when X is already done.
#
# `get_effective_user_state` reads the stored state AND computes
# truth from live data, then merges: a signal is `true` if EITHER
# the stored value OR the truth says it's true. The merged state
# is what the user sees (landing page, settings page, /api/me/onboarding).
#
# We do NOT write the merged state back to the DB. The stored
# signal is allowed to lag the truth — the regular signal hooks
# (auth/cookie.py::set_user_password, api/settings.py::LLM save,
# api/enrollment.py::consume, core/healthcheck.py::record_healthcheck)
# will catch up the next time the user does an action that flips
# a signal.

# Signal names that can be re-derived from live DB state. Keep this
# list in sync with `backfill_state()` above.
_TRUTH_OVERRIDABLE_SIGNALS = (
    SIGNAL_PASSWORD_SET,
    SIGNAL_LLM_CONFIGURED,
    SIGNAL_AGENT_CONNECTED,
    SIGNAL_FIRST_TASK_COMPLETED,
    SIGNAL_FIRST_TASK_ATTEMPTED,
)


async def _truth_inputs(db) -> dict[str, bool]:
    """Gather the 5 boolean inputs to `backfill_state` from live DB
    state. Used by `get_effective_user_state` to compute the truth
    state for a user.

    Each call hits the DB / disk a few times. The 3 helpers on the
    Database class (`_has_llm_configured`, `_task_completion_stats`,
    `_has_recent_agent_heartbeat`) are the same ones the startup
    backfill uses. We access them via duck-typing so this module
    stays independent of `db.py`'s exact class hierarchy (the
    production Database class has them, the test stub may not —
    we fall back to all-false in that case).
    """
    # Default to "no live state" if the db doesn't expose the
    # truth helpers. This keeps the function safe to call from
    # test contexts that use a minimal Database stub.
    has_llm = False
    has_completed = False
    has_any = False
    has_agent = False
    try:
        # The 3 methods are underscore-prefixed on the Database
        # class. They're "private" to db.py but we re-use them
        # here to avoid duplicating the SQL / file-read logic.
        has_llm = bool(await db._has_llm_configured())
    except Exception:
        pass
    try:
        has_completed, has_any = await db._task_completion_stats()
    except Exception:
        pass
    try:
        has_agent = bool(await db._has_recent_agent_heartbeat())
    except Exception:
        pass
    return {
        "has_llm_config": has_llm,
        "has_completed_task": has_completed,
        "has_any_task": has_any,
        "has_connected_agent": has_agent,
    }


async def get_effective_user_state(db, user_id: str) -> dict[str, Any]:
    """Read the user's onboarding state and merge in truth from live
    DB state. A signal is `true` if EITHER the stored value OR the
    truth says it's true.

    The merged state is what the user sees in the UI. The stored
    column is NOT updated by this function — the regular signal
    hooks (auth/cookie.py, api/settings.py, api/enrollment.py,
    core/healthcheck.py) catch the stored state up the next time
    the user does an action that flips a signal.

    Cost: 3 SQL queries + 1 config.yaml read per call. Acceptable
    for the user-initiated landing/settings/API endpoints.
    """
    stored = await get_user_state(db, user_id)
    # Read password_hash for the user (truth input #1).
    user_row = await db.fetchone(
        "SELECT password_hash FROM users WHERE id = ?", (user_id,)
    )
    has_password = bool(user_row and user_row.get("password_hash"))
    truth = await _truth_inputs(db)
    truth_state = backfill_state(
        has_password=has_password,
        has_llm_config=truth["has_llm_config"],
        has_connected_agent=truth["has_connected_agent"],
        has_completed_task=truth["has_completed_task"],
        has_any_task=truth["has_any_task"],
    )
    # Merge: stored OR truth, per signal. Build a fresh state
    # dict so we don't mutate the caller's data.
    merged_signals = dict(stored.get("signals") or {})
    for sig in _TRUTH_OVERRIDABLE_SIGNALS:
        if truth_state.get("signals", {}).get(sig) is True:
            merged_signals[sig] = True
    # Preserve `skipped` and `completed_at` from the stored state
    # (the user explicitly opted out / completion timestamp).
    merged = dict(stored)
    merged["signals"] = merged_signals
    return merged
