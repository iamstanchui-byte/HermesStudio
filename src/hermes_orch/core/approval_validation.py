"""v3.14.0 (Phase 1): validation for `type: "human_approval"` workflow steps.

Implements §4.1, §4.5, §4.7.3 of docs/v3.14.0-workflow-human-approval.md.

Three concerns, all at workflow SAVE time (not runtime):
  1. `approval` object structure: on_reject / route_to / summary_template /
     timeout_seconds must be well-formed.
  2. `summary_template` variables: `{{var}}` must match a key in this step's
     `params_template`; `{{step_name.field}}` must reference a real
     upstream dependency (the `field` portion is NOT validated because
     upstream outputs are runtime).
  3. `on_reject = "route"` wiring: `route_to` must reference a real step
     in the same workflow AND that step's `depends_on` must include this
     step (no runtime auto-inject).

Cost: ~150 LOC. The custom regex template render lives in
`render_summary_template()` (used at runtime when creating an
ApprovalRequest) — separate from validation to keep concerns clear.

Why a separate module: keeps workflows.py clean (it already has 2500+
LOC of LLM-synth + dispatch logic), makes the validation unit-testable
without spinning up a FastAPI app, and matches the existing pattern of
`core/audit.py`, `core/soul_dispatch.py`, etc. — domain logic in
`core/`, API in `api/`.
"""
from __future__ import annotations

import re
from typing import Any

# Template variable matcher. Single identifier OR dotted path. Each
# path segment is alphanumeric + underscore + hyphen (kebab-case
# allowed; the existing step-name validation in
# api/workflows.py::_validate_workflow_package enforces kebab-case
# step names so this is consistent).
#
# Matches:
#   {{client_name}}
#   {{generate-report.total}}
#   {{monthly-transport-claim-report.client}}
#
# We intentionally don't allow Jinja2-style filters or expressions —
# the only contract here is "look up this key in a flat dict or walk
# a dotted path".
_SUMMARY_VAR_RE = re.compile(r"\{\{([a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*)\}\}")


# Accepted values for `on_reject`. Used to validate the approval object
# shape; the actual semantics are in the supervisor (Phase 2).
VALID_ON_REJECT = ("stop", "skip", "route")


def validate_approval_object(approval: Any, step_name: str) -> list[str]:
    """Validate the `approval` object of a human_approval step.

    Returns a list of error messages (empty = valid). Caller is
    responsible for prefixing the step name in the error if useful.

    Checks (per §4.1 of the design doc):
      - `approval` is a dict
      - `on_reject` is in {stop, skip, route}
      - `route_to` present iff `on_reject = "route"`; non-empty string
      - `summary_template` is a non-empty string
      - `timeout_seconds` is an int > 0 if present
    """
    errors: list[str] = []
    if not isinstance(approval, dict):
        return [f"step {step_name!r}: approval must be a dict (got {type(approval).__name__})"]

    on_reject = approval.get("on_reject")
    if on_reject not in VALID_ON_REJECT:
        allowed = ", ".join(VALID_ON_REJECT)
        errors.append(
            f"step {step_name!r}: approval.on_reject={on_reject!r} invalid; "
            f"allowed: {allowed}"
        )

    # route_to required iff on_reject == "route"
    if on_reject == "route":
        route_to = approval.get("route_to")
        if not route_to or not isinstance(route_to, str):
            errors.append(
                f"step {step_name!r}: approval.on_reject='route' requires "
                f"approval.route_to to be a non-empty string (got {route_to!r})"
            )

    summary_template = approval.get("summary_template")
    if not summary_template or not isinstance(summary_template, str):
        errors.append(
            f"step {step_name!r}: approval.summary_template must be a non-empty string "
            f"(got {summary_template!r})"
        )

    timeout_seconds = approval.get("timeout_seconds")
    if timeout_seconds is not None:
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            errors.append(
                f"step {step_name!r}: approval.timeout_seconds must be an int (got "
                f"{type(timeout_seconds).__name__}={timeout_seconds!r})"
            )
        elif timeout_seconds <= 0:
            errors.append(
                f"step {step_name!r}: approval.timeout_seconds must be > 0 "
                f"(got {timeout_seconds})"
            )

    return errors


def validate_summary_template_vars(
    template: str,
    *,
    params_keys: set[str],
    dep_step_names: set[str],
) -> list[str]:
    """Validate that every `{{var}}` in summary_template can be resolved.

    Two variable forms are accepted:
      - `{{var}}` — must be a key in this step's `params_template` (or
        top-level workflow variables that the runtime substitutes into
        `params`).
      - `{{step_name.field}}` — must reference a real upstream step in
        `dep_step_names`. The `field` portion is NOT validated against
        the upstream step's actual output schema (the field is only
        known at runtime, since the upstream agent's output is
        generated at task completion). A missing field at render time
        falls back to the literal `<missing:step_name.field>`
        placeholder (see `render_summary_template`).

    Returns a list of error messages (empty = all variables resolvable).
    """
    errors: list[str] = []
    for m in _SUMMARY_VAR_RE.finditer(template):
        key = m.group(1)
        if "." in key:
            step_name = key.split(".", 1)[0]
            if step_name not in dep_step_names:
                errors.append(
                    f"summary_template references step {step_name!r} (in {{{{key}}}}) "
                    f"which is not in depends_on"
                )
        else:
            if key not in params_keys:
                errors.append(
                    f"summary_template variable {key!r} (in {{{{key}}}}) "
                    f"not in params_template keys (allowed: {sorted(params_keys)})"
                )
    return errors


def validate_route_to_wiring(
    *,
    step_name: str,
    approval: dict,
    all_steps: list[dict],
) -> list[str]:
    """Validate `on_reject = "route"` end-to-end wiring.

    Checks (§4.5 of the design doc):
      - `route_to` step exists in the same workflow.
      - The route target step's `depends_on` list includes THIS step.
        Runtime does NOT auto-inject dependencies; the user must
        explicitly wire the route target.

    Returns a list of error messages (empty = route wiring is valid).
    """
    errors: list[str] = []
    if approval.get("on_reject") != "route":
        return errors  # nothing to validate for stop / skip

    route_to = approval.get("route_to")
    if not route_to:
        return errors  # already caught by validate_approval_object

    target = None
    for s in all_steps:
        if isinstance(s, dict) and s.get("name") == route_to:
            target = s
            break

    if target is None:
        errors.append(
            f"step {step_name!r}: approval.route_to={route_to!r} does not match any step in this workflow"
        )
        return errors

    target_deps = target.get("depends_on") or []
    if not isinstance(target_deps, list):
        errors.append(
            f"step {step_name!r}: approval.route_to={route_to!r} target has invalid "
            f"depends_on (must be a list, got {type(target_deps).__name__})"
        )
        return errors

    if step_name not in target_deps:
        errors.append(
            f"step {step_name!r}: approval.route_to={route_to!r} target's depends_on "
            f"must include {step_name!r} (got {target_deps})"
        )

    return errors


def validate_human_approval_step(
    step: dict,
    *,
    all_steps: list[dict],
    params_keys: set[str],
) -> list[str]:
    """Top-level entry point: validate a single human_approval step.

    Combines the three sub-validators above. Called from
    `api/workflows.py::_validate_workflow_package` for each step
    that has `type == "human_approval"`.

    Returns a list of error messages (empty = valid).
    """
    errors: list[str] = []
    name = step.get("name", "<unnamed>")
    approval = step.get("approval")
    errors.extend(validate_approval_object(approval, name))

    if isinstance(approval, dict) and isinstance(approval.get("summary_template"), str):
        # Compute dep step names for this step
        deps = step.get("depends_on") or []
        if not isinstance(deps, list):
            deps = []
        dep_step_names = {d for d in deps if isinstance(d, str)}
        errors.extend(
            validate_summary_template_vars(
                approval["summary_template"],
                params_keys=params_keys,
                dep_step_names=dep_step_names,
            )
        )

    if isinstance(approval, dict):
        errors.extend(
            validate_route_to_wiring(
                step_name=name,
                approval=approval,
                all_steps=all_steps,
            )
        )

    return errors


def render_summary_template(template: str, context: dict) -> str:
    """Render a summary_template against a context dict (runtime).

    Separate from `validate_summary_template_vars` — that one runs at
    save time and checks structural resolvability; this one runs at
    ApprovalRequest creation time and substitutes actual values.

    Behavior:
      - `{{var}}` → `context.get(var, "<missing:var>")`
      - `{{step_name.field}}` → walk dotted path; render as
        "<missing:step_name.field>" if any segment is missing.
      - Literal text outside `{{...}}` is passed through unchanged.

    Why a custom regex (not Jinja2): avoid code-execution risk; behavior
    is fully controlled and predictable; the template grammar is tiny
    (single expression: dotted path lookup) so a regex is enough.

    Cost: O(n) in template length, ~5 LOC.
    """
    def replace(m: re.Match) -> str:
        key = m.group(1)
        if "." in key:
            parts = key.split(".")
            cur: Any = context
            for p in parts:
                if not isinstance(cur, dict) or p not in cur:
                    return f"<missing:{key}>"
                cur = cur[p]
            return str(cur)
        return str(context.get(key, f"<missing:{key}>"))

    return _SUMMARY_VAR_RE.sub(replace, template)
