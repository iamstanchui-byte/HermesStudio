"""v3.14.0 (Phase 1) — human_approval workflow step validation tests.

Covers:
  - core.approval_validation: pure-function unit tests
    (validate_approval_object, validate_summary_template_vars,
     validate_route_to_wiring, validate_human_approval_step,
     render_summary_template)
  - api.workflows._validate_workflow_package: end-to-end integration
    tests (saves a workflow package with human_approval step)

These tests cover the ACCEPTANCE CRITERIA from
docs/v3.14.0-workflow-human-approval.md §8 that pertain to Phase 1:
  - AC-1: valid human_approval step saves OK
  - AC-2: summary_template {{var}} matches params_template key → pass
  - AC-3: summary_template {{nonexistent_key}} → fail
  - AC-4: summary_template {{dep_step.field}} where dep_step not in
          depends_on → fail
  - AC-5: summary_template {{dep_step.field}} where dep_step IS in
          depends_on → pass (field is NOT validated, runtime handles it)
  - AC-6: on_reject=route, route_to step exists, D.depends_on includes B
          → pass
  - AC-7: on_reject=route, route_to not in workflow → fail
  - AC-8: on_reject=route, route_to exists but D.depends_on missing B
          → fail

Phase 2 (supervisor gate + APIs) and Phase 3 (UI) are covered by
their own test scripts. The DB migration is exercised in
_dbg_v3140_schema_test.py (one-off, run during dev).

Pure-function tests run in <1s. They do NOT need the server. The
backend integration test imports the validator from the actual code
path (`hermes_orch.api.workflows._validate_workflow_package`) so the
hookup is real.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Path setup so the package can be imported directly. The validator
# lives in `core.approval_validation` which has no FastAPI dependencies,
# so this works without spinning up a server.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from hermes_orch.core.approval_validation import (  # noqa: E402
    render_summary_template,
    validate_approval_object,
    validate_human_approval_step,
    validate_route_to_wiring,
    validate_summary_template_vars,
)


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------


class TestValidateApprovalObject:
    """Unit tests for the `approval` object schema (§4.1)."""

    def test_minimal_valid_approval(self):
        """AC-1 baseline: on_reject='stop' + summary_template is enough."""
        approval = {"on_reject": "stop", "summary_template": "Approve?"}
        errs = validate_approval_object(approval, "approve-step")
        assert errs == []

    def test_all_optional_fields(self):
        """Full payload with route_to + timeout_seconds is OK."""
        approval = {
            "on_reject": "route",
            "route_to": "manual-review",
            "summary_template": "Approve?",
            "timeout_seconds": 86400,
        }
        errs = validate_approval_object(approval, "approve-step")
        assert errs == []

    def test_approval_must_be_dict(self):
        """Passing a string instead of dict → fail."""
        errs = validate_approval_object("not-a-dict", "approve-step")
        assert len(errs) == 1
        assert "approval must be a dict" in errs[0]

    def test_on_reject_invalid_value(self):
        """on_reject='pause' (not in stop/skip/route) → fail."""
        errs = validate_approval_object(
            {"on_reject": "pause", "summary_template": "X"}, "approve-step"
        )
        assert any("on_reject='pause' invalid" in e for e in errs)

    def test_on_reject_missing(self):
        errs = validate_approval_object({"summary_template": "X"}, "approve-step")
        assert any("on_reject=" in e for e in errs)

    def test_route_requires_route_to(self):
        """on_reject='route' without route_to → fail."""
        errs = validate_approval_object(
            {"on_reject": "route", "summary_template": "X"}, "approve-step"
        )
        assert any("route_to" in e for e in errs)

    def test_route_to_must_be_non_empty_string(self):
        errs = validate_approval_object(
            {"on_reject": "route", "route_to": "", "summary_template": "X"},
            "approve-step",
        )
        assert any("route_to" in e for e in errs)

    def test_summary_template_required(self):
        errs = validate_approval_object({"on_reject": "stop"}, "approve-step")
        assert any("summary_template" in e for e in errs)

    def test_summary_template_must_be_string(self):
        errs = validate_approval_object(
            {"on_reject": "stop", "summary_template": 42}, "approve-step"
        )
        assert any("summary_template" in e for e in errs)

    def test_timeout_seconds_must_be_positive_int(self):
        # 0
        errs = validate_approval_object(
            {"on_reject": "stop", "summary_template": "X", "timeout_seconds": 0},
            "approve-step",
        )
        assert any("timeout_seconds" in e for e in errs)
        # negative
        errs = validate_approval_object(
            {"on_reject": "stop", "summary_template": "X", "timeout_seconds": -1},
            "approve-step",
        )
        assert any("timeout_seconds" in e for e in errs)
        # not an int
        errs = validate_approval_object(
            {"on_reject": "stop", "summary_template": "X", "timeout_seconds": "60"},
            "approve-step",
        )
        assert any("timeout_seconds" in e for e in errs)


class TestValidateSummaryTemplateVars:
    """AC-2 / AC-3 / AC-4 / AC-5: summary_template variable resolution."""

    def test_params_key_resolves(self):
        """AC-2: {{client_name}} matches params_template key → pass."""
        errs = validate_summary_template_vars(
            "Approve {{client_name}}",
            params_keys={"client_name", "total"},
            dep_step_names=set(),
        )
        assert errs == []

    def test_params_key_missing_fails(self):
        """AC-3: {{nonexistent_key}} not in params → fail with clear msg."""
        errs = validate_summary_template_vars(
            "Approve {{nonexistent_key}}",
            params_keys={"client_name"},
            dep_step_names=set(),
        )
        assert len(errs) == 1
        assert "nonexistent_key" in errs[0]
        assert "params_template" in errs[0]

    def test_dotted_path_with_valid_dep_step(self):
        """AC-5: {{generate-report.total}} where dep_step IS in depends_on → pass."""
        errs = validate_summary_template_vars(
            "Total: {{generate-report.total}}",
            params_keys=set(),
            dep_step_names={"generate-report", "fetch-data"},
        )
        assert errs == []

    def test_dotted_path_with_invalid_dep_step_fails(self):
        """AC-4: {{missing-step.total}} where dep_step NOT in depends_on → fail."""
        errs = validate_summary_template_vars(
            "Total: {{missing-step.total}}",
            params_keys=set(),
            dep_step_names={"generate-report"},
        )
        assert len(errs) == 1
        assert "missing-step" in errs[0]
        assert "depends_on" in errs[0]

    def test_mixed_vars(self):
        """Multiple vars in one template, some valid some not."""
        errs = validate_summary_template_vars(
            "Approve {{client_name}}: {{generate-report.total}} ({{bogus}})",
            params_keys={"client_name"},
            dep_step_names={"generate-report"},
        )
        # bogus is not in params_keys and not a dotted path — should error
        bogus_errors = [e for e in errs if "bogus" in e and "params_template" in e]
        assert len(bogus_errors) == 1, f"expected 1 error for bogus var, got {errs}"
        # Total: exactly 1 error (for bogus). client_name resolves, and
        # generate-report is a valid dep step.
        assert len(errs) == 1

    def test_no_vars_is_fine(self):
        """Plain text template (no {{}}) → no errors."""
        errs = validate_summary_template_vars(
            "Please approve this step",
            params_keys=set(),
            dep_step_names=set(),
        )
        assert errs == []


class TestValidateRouteToWiring:
    """AC-6 / AC-7 / AC-8: on_reject='route' wiring."""

    def test_route_well_formed(self):
        """AC-6: route_to step exists AND D.depends_on includes B → pass."""
        all_steps = [
            {"name": "do-thing"},
            {
                "name": "approve-step",
                "type": "human_approval",
                "approval": {"on_reject": "route", "route_to": "manual-review"},
                "depends_on": ["do-thing"],
            },
            {
                "name": "manual-review",
                "depends_on": ["approve-step"],  # ← key check
            },
        ]
        errs = validate_route_to_wiring(
            step_name="approve-step",
            approval=all_steps[1]["approval"],
            all_steps=all_steps,
        )
        assert errs == []

    def test_route_to_step_missing(self):
        """AC-7: route_to references a step that doesn't exist → fail."""
        all_steps = [
            {
                "name": "approve-step",
                "approval": {"on_reject": "route", "route_to": "nonexistent"},
            },
        ]
        errs = validate_route_to_wiring(
            step_name="approve-step",
            approval=all_steps[0]["approval"],
            all_steps=all_steps,
        )
        assert any("nonexistent" in e for e in errs)

    def test_route_target_missing_dep(self):
        """AC-8: D.depends_on missing B → fail."""
        all_steps = [
            {
                "name": "approve-step",
                "approval": {"on_reject": "route", "route_to": "manual-review"},
            },
            {
                "name": "manual-review",
                "depends_on": ["some-other-step"],  # ← B missing
            },
        ]
        errs = validate_route_to_wiring(
            step_name="approve-step",
            approval=all_steps[0]["approval"],
            all_steps=all_steps,
        )
        assert any("must include 'approve-step'" in e for e in errs)

    def test_skip_does_no_route_validation(self):
        """on_reject='skip' should NOT trigger route wiring validation."""
        all_steps = [{"name": "approve-step", "approval": {"on_reject": "skip"}}]
        errs = validate_route_to_wiring(
            step_name="approve-step",
            approval=all_steps[0]["approval"],
            all_steps=all_steps,
        )
        assert errs == []


class TestValidateHumanApprovalStep:
    """Top-level entry: combines all sub-validators."""

    def test_all_good(self):
        """AC-1: minimal valid human_approval step → no errors."""
        step = {
            "name": "approve-step",
            "type": "human_approval",
            "approval": {
                "on_reject": "stop",
                "summary_template": "Approve {{client_name}}",
            },
        }
        all_steps = [
            {"name": "do-thing"},
            step,
        ]
        errs = validate_human_approval_step(
            step, all_steps=all_steps, params_keys={"client_name"}
        )
        assert errs == []

    def test_collects_all_errors(self):
        """All sub-validators fire; multiple errors returned together."""
        step = {
            "name": "approve-step",
            "type": "human_approval",
            "approval": {
                "on_reject": "pause",                # invalid
                "summary_template": "{{missing}}",    # unresolved
            },
        }
        all_steps = [step]
        errs = validate_human_approval_step(
            step, all_steps=all_steps, params_keys=set()
        )
        # Should have BOTH the on_reject error AND the missing var error
        assert any("on_reject" in e for e in errs)
        assert any("missing" in e for e in errs)


class TestRenderSummaryTemplate:
    """Runtime rendering (called when creating an ApprovalRequest)."""

    def test_simple_substitution(self):
        assert render_summary_template(
            "Approve {{client_name}}",
            {"client_name": "ACME"},
        ) == "Approve ACME"

    def test_dotted_path(self):
        assert render_summary_template(
            "Total: {{generate-report.total}}",
            {"generate-report": {"total": 1.2, "client_name": "ACME"}},
        ) == "Total: 1.2"

    def test_missing_simple_var(self):
        """Renders as literal <missing:var> placeholder."""
        out = render_summary_template("Approve {{client_name}}", {})
        assert out == "Approve <missing:client_name>"

    def test_missing_dotted_segment(self):
        """Walking into a missing nested key returns placeholder."""
        out = render_summary_template(
            "Total: {{generate-report.missing}}",
            {"generate-report": {"total": 1.2}},
        )
        assert out == "Total: <missing:generate-report.missing>"

    def test_missing_dotted_root(self):
        """If the step name itself isn't in the context, placeholder."""
        out = render_summary_template(
            "Total: {{nonexistent.total}}",
            {"generate-report": {"total": 1.2}},
        )
        assert out == "Total: <missing:nonexistent.total>"

    def test_no_vars_passthrough(self):
        assert render_summary_template("Just text", {}) == "Just text"

    def test_mixed_present_and_missing(self):
        """Present var renders, missing one renders as placeholder."""
        out = render_summary_template(
            "Approve {{client_name}}: {{missing}}",
            {"client_name": "ACME"},
        )
        assert out == "Approve ACME: <missing:missing>"

    def test_non_string_value_coerced(self):
        """Numeric values rendered via str()."""
        out = render_summary_template(
            "Total: {{total}}",
            {"total": 1234.5},
        )
        assert out == "Total: 1234.5"


# ---------------------------------------------------------------------------
# Integration: end-to-end through _validate_workflow_package
# ---------------------------------------------------------------------------


class TestWorkflowValidationIntegration:
    """End-to-end: human_approval step in a workflow package.

    Calls `hermes_orch.api.workflows._validate_workflow_package` with
    realistic workflow payloads. Verifies the validation hookup is
    correctly wired (AC-1 through AC-8 via the actual entry point).
    """

    @staticmethod
    def _pkg(steps):
        """Build a minimal valid workflow package with these steps."""
        return {
            "description": "Test workflow",
            "step_template": steps,
            "variables": [],
        }

    def test_ac1_valid_human_approval_step(self):
        """AC-1: minimal valid step → validation passes."""
        from hermes_orch.api.workflows import _validate_workflow_package
        steps = [
            {"name": "do-thing", "action": "do_task"},
            {
                "name": "approve-step",
                "type": "human_approval",
                "depends_on": ["do-thing"],
                "approval": {
                    "on_reject": "stop",
                    "summary_template": "Approve?",
                },
            },
        ]
        ok, err = _validate_workflow_package(self._pkg(steps))
        assert ok, f"expected valid, got error: {err}"

    def test_ac3_summary_template_missing_var_fails(self):
        """AC-3: {{var}} not in params → 422 error."""
        from hermes_orch.api.workflows import _validate_workflow_package
        steps = [
            {
                "name": "approve-step",
                "type": "human_approval",
                "approval": {
                    "on_reject": "stop",
                    "summary_template": "Approve {{nonexistent}}",
                },
            },
        ]
        ok, err = _validate_workflow_package(self._pkg(steps))
        assert not ok
        assert "nonexistent" in err
        assert "params_template" in err

    def test_ac4_dotted_path_wrong_dep_fails(self):
        """AC-4: {{unknown-step.field}} not in depends_on → 422 error."""
        from hermes_orch.api.workflows import _validate_workflow_package
        steps = [
            {"name": "do-thing", "action": "do_task"},
            {
                "name": "approve-step",
                "type": "human_approval",
                "depends_on": ["do-thing"],
                "approval": {
                    "on_reject": "stop",
                    "summary_template": "X: {{unknown-step.field}}",
                },
            },
        ]
        ok, err = _validate_workflow_package(self._pkg(steps))
        assert not ok
        assert "unknown-step" in err
        assert "depends_on" in err

    def test_ac5_dotted_path_valid_dep_passes(self):
        """AC-5: {{known-step.field}} where known-step IS in depends_on → pass.

        Note: the FIELD portion is NOT validated (it's runtime-only).
        """
        from hermes_orch.api.workflows import _validate_workflow_package
        steps = [
            {"name": "do-thing", "action": "do_task"},
            {
                "name": "approve-step",
                "type": "human_approval",
                "depends_on": ["do-thing"],
                "approval": {
                    "on_reject": "stop",
                    "summary_template": "X: {{do-thing.anything-at-all}}",
                },
            },
        ]
        ok, err = _validate_workflow_package(self._pkg(steps))
        assert ok, f"expected valid, got error: {err}"

    def test_ac7_route_to_nonexistent_fails(self):
        """AC-7: on_reject='route' with route_to NOT in workflow → 422 error."""
        from hermes_orch.api.workflows import _validate_workflow_package
        steps = [
            {
                "name": "approve-step",
                "type": "human_approval",
                "approval": {
                    "on_reject": "route",
                    "route_to": "nonexistent-step",
                    "summary_template": "X",
                },
            },
        ]
        ok, err = _validate_workflow_package(self._pkg(steps))
        assert not ok
        assert "nonexistent-step" in err

    def test_ac8_route_target_missing_dep_fails(self):
        """AC-8: route_to exists but its depends_on missing B → 422 error."""
        from hermes_orch.api.workflows import _validate_workflow_package
        steps = [
            {
                "name": "approve-step",
                "type": "human_approval",
                "approval": {
                    "on_reject": "route",
                    "route_to": "manual-review",
                    "summary_template": "X",
                },
            },
            {
                "name": "manual-review",
                "depends_on": ["some-other-step"],
            },
        ]
        ok, err = _validate_workflow_package(self._pkg(steps))
        assert not ok
        assert "manual-review" in err
        assert "approve-step" in err

    def test_ac6_route_well_formed_passes(self):
        """AC-6: route_to step exists AND D.depends_on includes B → pass."""
        from hermes_orch.api.workflows import _validate_workflow_package
        steps = [
            {
                "name": "approve-step",
                "type": "human_approval",
                "approval": {
                    "on_reject": "route",
                    "route_to": "manual-review",
                    "summary_template": "X",
                },
            },
            {
                "name": "manual-review",
                "depends_on": ["approve-step"],
            },
        ]
        ok, err = _validate_workflow_package(self._pkg(steps))
        assert ok, f"expected valid, got error: {err}"

    def test_legacy_workflow_unchanged(self):
        """v3.13.x workflow with `action: "do_task"` (no `type`) still valid."""
        from hermes_orch.api.workflows import _validate_workflow_package
        steps = [
            {"name": "step-a", "action": "do_task"},
            {"name": "step-b", "action": "do_task", "depends_on": ["step-a"]},
        ]
        ok, err = _validate_workflow_package(self._pkg(steps))
        assert ok, f"legacy workflow broke: {err}"

    def test_human_approval_summary_template_must_use_params_keys(self):
        """summary_template can ONLY reference this step's params_template
        keys. Top-level workflow variables are NOT in the context
        (they get substituted INTO params_template VALUES at run
        time, not added as new keys).

        This is per design doc §4.7.1: "params: 本 step 的
        params_template / runtime params".
        """
        from hermes_orch.api.workflows import _validate_workflow_package
        # Reference a top-level variable (workflow_var) — should fail.
        steps = [
            {
                "name": "approve-step",
                "type": "human_approval",
                "params_template": {"step_param": "value"},
                "approval": {
                    "on_reject": "stop",
                    "summary_template": "{{step_param}} and {{workflow_var}}",
                },
            },
        ]
        pkg = {
            "description": "Test",
            "step_template": steps,
            "variables": [{"name": "workflow_var", "type": "string"}],
        }
        ok, err = _validate_workflow_package(pkg)
        assert not ok, "expected validation to fail (workflow_var not in params)"
        assert "workflow_var" in err

        # Same template but using only params_template key — should pass.
        steps_ok = [
            {
                "name": "approve-step",
                "type": "human_approval",
                "params_template": {"step_param": "value"},
                "approval": {
                    "on_reject": "stop",
                    "summary_template": "Approve {{step_param}}",
                },
            },
        ]
        ok, err = _validate_workflow_package(self._pkg(steps_ok))
        assert ok, f"got error: {err}"
