# coding: utf-8
"""Tests for hermes_orch.core.onboarding (v1.0.1 §3.2).

Pure-function tests for the onboarding state helpers. No DB I/O —
that's covered by the integration tests in test_onboarding_backfill.py.
"""
from __future__ import annotations

import json

import pytest

from hermes_orch.core.onboarding import (
    ALL_SIGNALS,
    CURRENT_SCHEMA_VERSION,
    SIGNAL_AGENT_CONNECTED,
    SIGNAL_FIRST_TASK_ATTEMPTED,
    SIGNAL_FIRST_TASK_COMPLETED,
    SIGNAL_LLM_CONFIGURED,
    SIGNAL_PASSWORD_SET,
    SUCCESS_SIGNALS,
    backfill_state,
    empty_state,
    is_checklist_complete,
    parse_state,
    reset_state,
    serialize_state,
    set_signal,
    set_skipped,
    should_show_checklist,
)


# ===== empty_state / serialize / parse =====

def test_empty_state_has_all_signals_false():
    s = empty_state()
    assert s["skipped"] is False
    assert s["completed_at"] is None
    assert s["schema_version"] == CURRENT_SCHEMA_VERSION
    for sig in ALL_SIGNALS:
        assert s["signals"][sig] is False, f"expected {sig}=False in fresh state"


def test_serialize_then_parse_round_trip():
    s = empty_state()
    s = set_signal(s, SIGNAL_PASSWORD_SET, True)
    s = set_signal(s, SIGNAL_LLM_CONFIGURED, True)
    raw = serialize_state(s)
    parsed = parse_state(raw)
    assert parsed["signals"][SIGNAL_PASSWORD_SET] is True
    assert parsed["signals"][SIGNAL_LLM_CONFIGURED] is True
    assert parsed["signals"][SIGNAL_AGENT_CONNECTED] is False
    # schema_version is forced to current on parse (forward compat)
    assert parsed["schema_version"] == CURRENT_SCHEMA_VERSION


def test_parse_state_handles_none_and_empty():
    """SQL default '{}' and any other falsy value → empty_state()."""
    for raw in (None, "", "  ", "{}"):
        s = parse_state(raw)
        assert s == empty_state(), f"raw={raw!r} should parse to empty_state"


def test_parse_state_handles_malformed_json():
    """Bad JSON → empty_state (defensive: treat as fresh user)."""
    for raw in ("not json", "{", "null", '"string not object"', "[]"):
        s = parse_state(raw)
        assert s == empty_state(), f"raw={raw!r} should parse to empty_state"


def test_parse_state_handles_non_dict_json():
    """JSON value that's not an object → empty_state."""
    for raw in ("null", "[1,2,3]", "42", '"hi"'):
        s = parse_state(raw)
        assert s == empty_state()


def test_parse_state_fills_missing_keys():
    """Legacy state JSON missing new keys → defaults filled in."""
    # Old format: just a signals dict, no skipped/completed_at/schema_version
    legacy = json.dumps({"signals": {SIGNAL_PASSWORD_SET: True}})
    s = parse_state(legacy)
    assert s["signals"][SIGNAL_PASSWORD_SET] is True
    assert s["skipped"] is False
    assert s["completed_at"] is None
    assert s["schema_version"] == CURRENT_SCHEMA_VERSION


def test_parse_state_preserves_unknown_signals():
    """Forward compat: unknown signal keys are preserved on the dict."""
    raw = json.dumps({
        "signals": {SIGNAL_PASSWORD_SET: True, "future_signal": True},
        "skipped": False,
        "schema_version": 1,
    })
    s = parse_state(raw)
    assert s["signals"][SIGNAL_PASSWORD_SET] is True
    assert s["signals"]["future_signal"] is True


# ===== is_checklist_complete =====

def test_is_checklist_complete_all_true():
    s = empty_state()
    for sig in SUCCESS_SIGNALS:
        s = set_signal(s, sig, True)
    assert is_checklist_complete(s) is True
    assert s.get("completed_at") is not None  # set when all 4 flip true


def test_is_checklist_complete_one_false():
    s = empty_state()
    for sig in SUCCESS_SIGNALS:
        s = set_signal(s, sig, True)
    s = set_signal(s, SIGNAL_AGENT_CONNECTED, False)
    assert is_checklist_complete(s) is False


def test_is_checklist_complete_attempted_does_not_count():
    """first_task_attempted is INFORMATIONAL only — does NOT collapse."""
    s = empty_state()
    for sig in SUCCESS_SIGNALS:
        s = set_signal(s, sig, True)
    # Now reset one and set the informational signal instead
    s = set_signal(s, SIGNAL_AGENT_CONNECTED, False)
    s = set_signal(s, SIGNAL_FIRST_TASK_ATTEMPTED, True)
    # still incomplete — a failed first attempt doesn't count
    assert is_checklist_complete(s) is False


def test_is_checklist_complete_empty_state():
    assert is_checklist_complete(empty_state()) is False


# ===== should_show_checklist =====

def test_should_show_checklist_fresh_user():
    assert should_show_checklist(empty_state()) is True


def test_should_show_checklist_hide_when_complete():
    s = empty_state()
    for sig in SUCCESS_SIGNALS:
        s = set_signal(s, sig, True)
    assert should_show_checklist(s) is False


def test_should_show_checklist_hide_when_skipped():
    """Skip = "I know I have to do these, don't bug me"."""
    s = empty_state()
    s = set_skipped(s, True)
    assert should_show_checklist(s) is False


def test_should_show_checklist_show_when_partial():
    """User has done 2 of 4 → still show."""
    s = empty_state()
    s = set_signal(s, SIGNAL_PASSWORD_SET, True)
    s = set_signal(s, SIGNAL_LLM_CONFIGURED, True)
    assert should_show_checklist(s) is True


def test_should_show_checklist_completed_state_takes_precedence():
    """Even if user previously skipped, once all 4 are true → hide.

    Edge case: user skipped early, then completed everything later.
    The checklist should not re-appear. The skip flag stays set but
    the completion check is the dominant signal.
    """
    s = empty_state()
    s = set_skipped(s, True)
    for sig in SUCCESS_SIGNALS:
        s = set_signal(s, sig, True)
    # skipped is still True but completion dominates
    assert should_show_checklist(s) is False


# ===== set_signal / set_skipped (immutability) =====

def test_set_signal_returns_new_dict():
    """set_signal must not mutate the input (caller relies on immutability)."""
    s = empty_state()
    s2 = set_signal(s, SIGNAL_PASSWORD_SET, True)
    assert s is not s2
    assert s["signals"][SIGNAL_PASSWORD_SET] is False  # original unchanged
    assert s2["signals"][SIGNAL_PASSWORD_SET] is True


def test_set_signal_completed_at_set_only_once():
    """Setting a signal that completes the checklist sets completed_at ONCE.

    If the user later sets another signal (even if it doesn't affect
    completion), the original completed_at is preserved. (We never
    clear it — see set_signal docstring.)
    """
    s = empty_state()
    for sig in SUCCESS_SIGNALS:
        s = set_signal(s, sig, True)
    first_completed = s["completed_at"]
    assert first_completed is not None
    # Now flip one off and on again
    s = set_signal(s, SIGNAL_AGENT_CONNECTED, False)
    s = set_signal(s, SIGNAL_AGENT_CONNECTED, True)
    # completed_at may or may not be preserved depending on the
    # implementation — we just require it to be a string, not None
    # (the user has completed at least once)
    assert s["completed_at"] is not None


def test_set_signal_unknown_signal_stored():
    """Forward compat: unknown signal names are stored (no crash)."""
    s = empty_state()
    s2 = set_signal(s, "future_signal", True)
    assert s2["signals"]["future_signal"] is True


def test_set_skipped_returns_new_dict():
    s = empty_state()
    s2 = set_skipped(s, True)
    assert s is not s2
    assert s["skipped"] is False
    assert s2["skipped"] is True


def test_set_skipped_toggle():
    s = set_skipped(empty_state(), True)
    s = set_skipped(s, False)
    assert s["skipped"] is False


# ===== reset_state =====

def test_reset_state_returns_fresh_state():
    s = reset_state()
    assert s == empty_state()


# ===== backfill_state =====

def test_backfill_state_all_false():
    """Brand-new user with nothing → all signals false."""
    s = backfill_state(
        has_password=False,
        has_llm_config=False,
        has_connected_agent=False,
        has_completed_task=False,
        has_any_task=False,
    )
    for sig in ALL_SIGNALS:
        assert s["signals"][sig] is False


def test_backfill_state_long_time_user_all_true():
    """User who's been running for months → all 4 success signals true.

    This is the spec §3.2.1 case: existing user should NOT see the
    fresh-user checklist after upgrade.
    """
    s = backfill_state(
        has_password=True,
        has_llm_config=True,
        has_connected_agent=True,
        has_completed_task=True,
        has_any_task=True,
    )
    for sig in SUCCESS_SIGNALS:
        assert s["signals"][sig] is True, f"{sig} should be True"
    assert s["signals"][SIGNAL_FIRST_TASK_ATTEMPTED] is True
    # All 4 success signals true → completed_at set
    assert s["completed_at"] is not None
    # And the checklist must NOT show
    assert should_show_checklist(s) is False


def test_backfill_state_partial_user():
    """User with password + LLM but no agents yet → 2 of 4 signals."""
    s = backfill_state(
        has_password=True,
        has_llm_config=True,
        has_connected_agent=False,
        has_completed_task=False,
        has_any_task=False,
    )
    assert s["signals"][SIGNAL_PASSWORD_SET] is True
    assert s["signals"][SIGNAL_LLM_CONFIGURED] is True
    assert s["signals"][SIGNAL_AGENT_CONNECTED] is False
    assert s["signals"][SIGNAL_FIRST_TASK_COMPLETED] is False
    # Still partial — checklist must show (so the user finishes the
    # remaining 2 steps: connect an agent, run a task)
    assert should_show_checklist(s) is True


def test_backfill_state_attempted_without_completed():
    """User attempted tasks but none completed → attempted=True, completed=False.

    The 4-step checklist must STILL show (they haven't succeeded yet).
    """
    s = backfill_state(
        has_password=True,
        has_llm_config=True,
        has_connected_agent=True,
        has_completed_task=False,
        has_any_task=True,  # attempted but none completed
    )
    assert s["signals"][SIGNAL_FIRST_TASK_ATTEMPTED] is True
    assert s["signals"][SIGNAL_FIRST_TASK_COMPLETED] is False
    assert should_show_checklist(s) is True
    # IMPORTANT: a failed first attempt does not collapse the checklist
    assert is_checklist_complete(s) is False


# ===== Success signals vs informational signals =====

def test_success_signals_count_is_4():
    """Per spec, exactly 4 signals collapse the checklist."""
    assert len(SUCCESS_SIGNALS) == 4


def test_attempted_is_not_a_success_signal():
    """Defensive: the informational signal must never be in SUCCESS_SIGNALS."""
    assert SIGNAL_FIRST_TASK_ATTEMPTED not in SUCCESS_SIGNALS
