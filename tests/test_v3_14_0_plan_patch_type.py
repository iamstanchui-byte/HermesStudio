"""v3.14.0 (Phase 3 followup 5): verify the patch whitelist allows
'type' and 'approval' so the chat LLM can flip a do_task step
into a human_approval step (or set the human_approval config)
via apply_plan_patch. Before this fix, the LLM's patch failed
with "field 'type' is not editable" and the user had to
edit the visual editor by hand.
"""
import sys
import pytest

sys.path.insert(0, "C:/Project/minimax code/hermes-orchestrator/src")
from hermes_orch.api.workflows import (
    _apply_step_patch,
    _EDITABLE_STEP_FIELDS,
)


def test_type_is_in_editable_whitelist():
    """type + approval are now in the editable whitelist."""
    assert "type" in _EDITABLE_STEP_FIELDS
    assert "approval" in _EDITABLE_STEP_FIELDS


def test_apply_step_patch_allows_type_change():
    """The LLM can flip a do_task step into a human_approval step."""
    existing = [
        {"name": "check-disk", "agent_role": "super", "action": "check_disk_usage",
         "depends_on": [], "type": "do_task"},
        {"name": "approve", "agent_role": "super", "action": "await_approval",
         "depends_on": ["check-disk"], "type": ""},
    ]
    new_steps, diff = _apply_step_patch(
        existing_steps=existing,
        edit_steps=[{"name": "approve", "patch": {"type": "human_approval"}}],
    )
    # approve step now has type=human_approval
    approve = next(s for s in new_steps if s["name"] == "approve")
    assert approve["type"] == "human_approval"
    # check-disk step unchanged
    check = next(s for s in new_steps if s["name"] == "check-disk")
    assert check["type"] == "do_task"
    # diff summary records the edit
    assert len(diff["edited"]) == 1
    assert diff["edited"][0]["name"] == "approve"


def test_apply_step_patch_allows_approval_subobject():
    """The LLM can set the approval sub-object (on_reject etc.)."""
    existing = [
        {"name": "approve", "agent_role": "super", "action": "await_approval",
         "depends_on": [], "type": "human_approval",
         "approval": {"on_reject": "stop"}},
    ]
    new_steps, diff = _apply_step_patch(
        existing_steps=existing,
        edit_steps=[{
            "name": "approve",
            "patch": {
                "approval": {
                    "summary_template": "Review: {{item}}",
                    "on_reject": "route",
                    "route_to": "fallback",
                },
            },
        }],
    )
    approve = new_steps[0]
    assert approve["approval"]["on_reject"] == "route"
    assert approve["approval"]["route_to"] == "fallback"
    assert approve["approval"]["summary_template"] == "Review: {{item}}"


def test_apply_step_patch_rejects_unknown_field():
    """Sanity: the whitelist still rejects truly unknown fields."""
    existing = [{"name": "s1", "agent_role": "super", "action": "do_thing"}]
    with pytest.raises(ValueError, match="is not editable"):
        _apply_step_patch(
            existing_steps=existing,
            edit_steps=[{"name": "s1", "patch": {"evil_field": "x"}}],
        )


def test_apply_step_patch_still_rejects_name_rename():
    """The 'name' field is intentionally NOT editable (per spec
    'no silent rename' — use remove+add). This test guards
    against accidentally adding it in this fix."""
    existing = [{"name": "s1", "agent_role": "super", "action": "do_thing"}]
    with pytest.raises(ValueError, match="is not editable"):
        _apply_step_patch(
            existing_steps=existing,
            edit_steps=[{"name": "s1", "patch": {"name": "s2"}}],
        )
