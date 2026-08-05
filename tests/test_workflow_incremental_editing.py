"""Unit tests for v3.12.6 Workflow Incremental Editing helpers.

These cover the four pure helpers in
`hermes_orch.api.workflows` (no DB, no HTTP) plus the
combined `_apply_step_patch` orchestrator. Endpoint-level
integration tests live in a separate file (TODO Phase 1.3).

Coverage:
  - _check_no_cycle: linear OK, branching OK, cycle detected
  - _check_dangling_refs: dangling depends_on + feedback_to detected
  - _check_path_safety: ".." rejected, absolute rejected, empty OK
  - _compute_field_diff: key-level diff with before/after
  - _apply_step_patch: happy path add/edit/remove, name collision,
    position insertion, dangling ref removal refused, edit field
    whitelist, output_path traversal
"""
from __future__ import annotations

import pytest

from hermes_orch.api.workflows import (
    _apply_step_patch,
    _check_dangling_refs,
    _check_no_cycle,
    _check_path_safety,
    _compute_field_diff,
)


# === _check_no_cycle ===

def test_check_no_cycle_linear():
    steps = [
        {"name": "a", "depends_on": []},
        {"name": "b", "depends_on": ["a"]},
        {"name": "c", "depends_on": ["b"]},
    ]
    ok, err = _check_no_cycle(steps)
    assert ok, err


def test_check_no_cycle_branching():
    steps = [
        {"name": "a", "depends_on": []},
        {"name": "b", "depends_on": ["a"]},
        {"name": "c", "depends_on": ["a"]},
        {"name": "d", "depends_on": ["b", "c"]},
    ]
    ok, err = _check_no_cycle(steps)
    assert ok, err


def test_check_no_cycle_self_loop():
    steps = [
        {"name": "a", "depends_on": ["a"]},
    ]
    ok, err = _check_no_cycle(steps)
    assert not ok
    assert "cycle" in err.lower()


def test_check_no_cycle_two_node_loop():
    steps = [
        {"name": "a", "depends_on": ["b"]},
        {"name": "b", "depends_on": ["a"]},
    ]
    ok, err = _check_no_cycle(steps)
    assert not ok
    assert "cycle" in err.lower()


def test_check_no_cycle_three_node_loop():
    steps = [
        {"name": "a", "depends_on": ["c"]},
        {"name": "b", "depends_on": ["a"]},
        {"name": "c", "depends_on": ["b"]},
    ]
    ok, err = _check_no_cycle(steps)
    assert not ok
    assert "cycle" in err.lower()


# === _check_dangling_refs ===

def test_check_dangling_refs_ok():
    steps = [
        {"name": "a", "depends_on": []},
        {"name": "b", "depends_on": ["a"], "feedback_to": ["a"]},
    ]
    ok, err = _check_dangling_refs(steps)
    assert ok, err


def test_check_dangling_refs_depends_on_missing():
    steps = [
        {"name": "a", "depends_on": ["nope"]},
    ]
    ok, err = _check_dangling_refs(steps)
    assert not ok
    assert "nope" in err
    assert "depends_on" in err


def test_check_dangling_refs_feedback_to_missing():
    steps = [
        {"name": "a", "depends_on": [], "feedback_to": ["nope"]},
    ]
    ok, err = _check_dangling_refs(steps)
    assert not ok
    assert "nope" in err
    assert "feedback_to" in err


# === _check_path_safety ===

def test_check_path_safety_empty_ok():
    ok, err = _check_path_safety("")
    assert ok, err


def test_check_path_safety_relative_ok():
    ok, err = _check_path_safety("out/report.md")
    assert ok, err


def test_check_path_safety_dotdot_rejected():
    ok, err = _check_path_safety("out/../../etc/passwd")
    assert not ok
    assert "traversal" in err.lower()


def test_check_path_safety_unix_absolute_rejected():
    ok, err = _check_path_safety("/etc/passwd")
    assert not ok
    assert "absolute" in err.lower()


def test_check_path_safety_windows_drive_rejected():
    ok, err = _check_path_safety("C:\\Windows\\System32\\config\\SAM")
    assert not ok
    assert "absolute" in err.lower() or "drive" in err.lower()


# === _compute_field_diff ===

def test_compute_field_diff_unchanged():
    before = {"a": 1, "b": 2}
    after = {"a": 1, "b": 2}
    assert _compute_field_diff(before, after) == {}


def test_compute_field_diff_simple():
    before = {"a": 1, "b": 2}
    after = {"a": 1, "b": 99}
    diff = _compute_field_diff(before, after)
    assert diff == {"b": {"before": 2, "after": 99}}


def test_compute_field_diff_added_key():
    before = {"a": 1}
    after = {"a": 1, "b": 2}
    diff = _compute_field_diff(before, after)
    assert diff == {"b": {"before": None, "after": 2}}


def test_compute_field_diff_removed_key():
    before = {"a": 1, "b": 2}
    after = {"a": 1}
    diff = _compute_field_diff(before, after)
    assert diff == {"b": {"before": 2, "after": None}}


def test_compute_field_diff_nested_dict_value():
    # params_template is a dict; structural equality is fine.
    before = {"params_template": {"a": 1, "b": 2}}
    after = {"params_template": {"a": 1, "b": 999}}
    diff = _compute_field_diff(before, after)
    assert "params_template" in diff
    assert diff["params_template"]["before"] == {"a": 1, "b": 2}
    assert diff["params_template"]["after"] == {"a": 1, "b": 999}


# === _apply_step_patch — happy paths ===

def test_apply_step_patch_add_append_default():
    existing = [
        {"name": "a", "agent_role": "super", "action": "x",
         "depends_on": [], "feedback_to": [], "params_template": {},
         "output_path": "", "skill": ""},
    ]
    new_steps, diff = _apply_step_patch(
        existing_steps=existing,
        add_steps=[
            {"name": "b", "agent_role": "super", "action": "y",
             "depends_on": ["a"], "feedback_to": [], "params_template": {},
             "output_path": "", "skill": ""},
        ],
    )
    assert len(new_steps) == 2
    assert new_steps[1]["name"] == "b"
    assert new_steps[1]["depends_on"] == ["a"]
    assert diff["added"] == [{"name": "b", "fields": sorted(new_steps[1].keys())}]
    assert diff["edited"] == []
    assert diff["removed"] == []


def test_apply_step_patch_add_with_position_after():
    existing = [
        {"name": "a", "depends_on": []},
        {"name": "c", "depends_on": ["a"]},
    ]
    new_steps, _diff = _apply_step_patch(
        existing_steps=existing,
        add_steps=[{"name": "b", "depends_on": ["a"]}],
        position={"after": "a"},
    )
    assert [s["name"] for s in new_steps] == ["a", "b", "c"]


def test_apply_step_patch_add_with_position_before():
    existing = [
        {"name": "a", "depends_on": []},
        {"name": "c", "depends_on": ["a"]},
    ]
    new_steps, _diff = _apply_step_patch(
        existing_steps=existing,
        add_steps=[{"name": "b", "depends_on": ["a"]}],
        position={"before": "c"},
    )
    assert [s["name"] for s in new_steps] == ["a", "b", "c"]


def test_apply_step_patch_add_position_missing_step_raises():
    existing = [{"name": "a", "depends_on": []}]
    with pytest.raises(ValueError, match="position.after"):
        _apply_step_patch(
            existing_steps=existing,
            add_steps=[{"name": "b", "depends_on": ["a"]}],
            position={"after": "nope"},
        )


def test_apply_step_patch_edit_field():
    existing = [
        {"name": "a", "agent_role": "analyst", "timeout_seconds": 1800,
         "depends_on": []},
    ]
    new_steps, diff = _apply_step_patch(
        existing_steps=existing,
        edit_steps=[{"name": "a", "patch": {"agent_role": "reviewer"}}],
    )
    assert new_steps[0]["agent_role"] == "reviewer"
    assert new_steps[0]["timeout_seconds"] == 1800  # unchanged
    assert diff["edited"][0]["name"] == "a"
    assert diff["edited"][0]["field_diff"]["agent_role"] == {
        "before": "analyst", "after": "reviewer",
    }


def test_apply_step_patch_edit_rejects_name_field():
    existing = [{"name": "a", "depends_on": []}]
    with pytest.raises(ValueError, match="not editable"):
        _apply_step_patch(
            existing_steps=existing,
            edit_steps=[{"name": "a", "patch": {"name": "renamed-a"}}],
        )


def test_apply_step_patch_edit_empty_patch_rejected():
    existing = [{"name": "a", "depends_on": []}]
    with pytest.raises(ValueError, match="empty patch"):
        _apply_step_patch(
            existing_steps=existing,
            edit_steps=[{"name": "a", "patch": {}}],
        )


def test_apply_step_patch_edit_output_path_traversal_rejected():
    existing = [{"name": "a", "depends_on": [], "output_path": ""}]
    with pytest.raises(ValueError, match="traversal"):
        _apply_step_patch(
            existing_steps=existing,
            edit_steps=[{"name": "a",
                          "patch": {"output_path": "../../etc/passwd"}}],
        )


def test_apply_step_patch_remove_unreferenced_ok():
    existing = [
        {"name": "a", "depends_on": []},
        {"name": "b", "depends_on": ["a"]},
    ]
    new_steps, diff = _apply_step_patch(
        existing_steps=existing,
        remove_step_names=["b"],
    )
    # After removing b, a's downstream chain is broken; but a
    # itself is intact. Our validation checks dangling refs (a
    # doesn't reference b), so it's OK to leave a in place.
    assert [s["name"] for s in new_steps] == ["a"]
    assert diff["removed"] == [{"name": "b", "was_referenced_by": []}]


def test_apply_step_patch_remove_referenced_refused():
    existing = [
        {"name": "a", "depends_on": []},
        {"name": "b", "depends_on": ["a"]},
    ]
    with pytest.raises(ValueError, match="still referenced"):
        _apply_step_patch(
            existing_steps=existing,
            remove_step_names=["a"],
        )


def test_apply_step_patch_add_name_collision_refused():
    existing = [{"name": "a", "depends_on": []}]
    with pytest.raises(ValueError, match="already exists"):
        _apply_step_patch(
            existing_steps=existing,
            add_steps=[{"name": "a", "depends_on": []}],
        )


def test_apply_step_patch_add_produces_cycle_refused():
    # existing: a -> b
    # add: c depends_on a, then add: a' depends_on c (where a'
    # is a NEW step but the user mistakenly names it the same as
    # the existing a, which collision-refuses; so use a different
    # test: add a step whose name collides after the original a
    # is patched to depend on it -- but the patch is atomic and
    # the order is fixed. Instead, simulate a cycle: a -> b, then
    # try to add c with depends_on = [c] (self-cycle).
    existing = [{"name": "a", "depends_on": []}]
    with pytest.raises(ValueError, match="cycle"):
        # Self-cycle: new step depends on itself
        # But the helper checks cycle only after the insert.
        # Actually a step with depends_on = [self_name] is allowed
        # at insertion (self-reference is a cycle but our insert
        # step doesn't pre-validate that). The final
        # _check_no_cycle catches it. Let's verify:
        _apply_step_patch(
            existing_steps=existing,
            add_steps=[{"name": "c", "depends_on": ["c"]}],
        )


def test_apply_step_patch_remove_unknown_step_refused():
    existing = [{"name": "a", "depends_on": []}]
    with pytest.raises(ValueError, match="not found"):
        _apply_step_patch(
            existing_steps=existing,
            remove_step_names=["nope"],
        )


def test_apply_step_patch_edit_unknown_step_refused():
    existing = [{"name": "a", "depends_on": []}]
    with pytest.raises(ValueError, match="not found"):
        _apply_step_patch(
            existing_steps=existing,
            edit_steps=[{"name": "nope", "patch": {"agent_role": "x"}}],
        )


def test_apply_step_patch_mixed_atomic():
    """A single patch that adds + edits + removes in one call.
    Spec §7.1 requires mixed operations to be atomic (all or
    nothing). The helper enforces this by raising on the first
    invalid sub-op, leaving the existing list untouched on the
    caller's side (since new_steps is built up incrementally
    inside the helper and discarded on raise).

    Scenario: a -> b -> c chain. Patch should:
      - add d depending on b (sibling of c)
      - edit b's role
      - remove c
    Result: a -> b (edited) -> d. c gone.
    """
    existing = [
        {"name": "a", "agent_role": "analyst", "depends_on": []},
        {"name": "b", "agent_role": "analyst", "depends_on": ["a"]},
        {"name": "c", "agent_role": "analyst", "depends_on": ["b"]},
    ]
    new_steps, diff = _apply_step_patch(
        existing_steps=existing,
        add_steps=[
            {"name": "d", "agent_role": "reviewer", "depends_on": ["b"]},
        ],
        edit_steps=[
            {"name": "b", "patch": {"agent_role": "reviewer"}},
        ],
        remove_step_names=["c"],
    )
    assert [s["name"] for s in new_steps] == ["a", "b", "d"]
    assert new_steps[1]["agent_role"] == "reviewer"  # edited
    assert new_steps[2]["name"] == "d"
    assert new_steps[2]["depends_on"] == ["b"]
    assert diff["added"][0]["name"] == "d"
    assert diff["edited"][0]["name"] == "b"
    assert diff["removed"][0]["name"] == "c"


def test_apply_step_patch_mixed_remove_refused_no_partial_apply():
    """If a mixed patch has a refused remove (still referenced),
    no edits should leak through. The helper builds new_steps
    incrementally; we must verify that on raise, the caller's
    original list is unchanged. The diff_summary is the helper's
    local state, so the assertion is: the helper raises BEFORE
    it returns new_steps, so the caller never sees partial state.
    """
    existing = [
        {"name": "a", "agent_role": "x", "depends_on": []},
        {"name": "b", "depends_on": ["a"]},
    ]
    with pytest.raises(ValueError, match="still referenced"):
        _apply_step_patch(
            existing_steps=existing,
            edit_steps=[{"name": "a", "patch": {"agent_role": "y"}}],
            remove_step_names=["a"],
        )
    # existing list must be untouched
    assert existing[0]["agent_role"] == "x"
    assert len(existing) == 2
