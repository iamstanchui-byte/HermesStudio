"""Unit tests for workflow package validation + variable extraction.

Validates the static rules in api/workflows.py::_validate_workflow_package
and _extract_variables_from_template. The LLM call itself is exercised
by the E2E test (scripts/_test-workflow-e2e.py) — here we only test
the deterministic checks.

Run: .venv\Scripts\python.exe scripts/_test-workflow-validation.py
"""
import copy
import sys
from pathlib import Path

# Add src to path so we can import without `pip install -e`
ROOT = Path(r"C:\Project\minimax code\hermes-orchestrator")
sys.path.insert(0, str(ROOT / "src"))

from hermes_orch.api.workflows import (  # noqa: E402
    _validate_workflow_package, _extract_variables_from_template,
    _VAR_PLACEHOLDER_RE,
)


# ---------- helpers ----------

PASS_COUNT = 0
FAIL_COUNT = 0


def expect_ok(name: str, pkg: dict) -> None:
    global PASS_COUNT, FAIL_COUNT
    ok, err = _validate_workflow_package(pkg)
    if ok:
        PASS_COUNT += 1
        print(f"  PASS  {name}")
    else:
        FAIL_COUNT += 1
        print(f"  FAIL  {name}  ->  {err}")


def expect_fail(name: str, pkg: dict, expected_substr: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT
    ok, err = _validate_workflow_package(pkg)
    if not ok and (not expected_substr or expected_substr in err):
        PASS_COUNT += 1
        print(f"  PASS  {name}  (rejected: {err[:60]})")
    else:
        FAIL_COUNT += 1
        print(f"  FAIL  {name}  ->  ok={ok}, err={err[:120]}")


def mut() -> dict:
    """Deep copy of GOOD_PKG so test mutations don't leak across tests."""
    return copy.deepcopy(GOOD_PKG)


# ---------- a good package (the happy path) ----------

GOOD_PKG = {
    "description": "List files in a Google Drive folder and summarize them.",
    "step_template": [
        {
            "name": "list-folder",
            "agent_role": "win-agent01",
            "action": "list_drive_files",
            "depends_on": [],
            "params_template": {"folder_id": "{{gdrive_folder_id}}"},
            "output_path": "list_files.results.json",
        },
        {
            "name": "summarize",
            "agent_role": "win-agent01",
            "action": "summarize_listing",
            "depends_on": ["list-folder"],
            "params_template": {
                "input_path": "list_files.results.json",
                "language": "{{language}}",
            },
            "output_path": "summary.md",
            # Stage 1.5: skill reference
            "skill": "gdrive-folder-reader",
        },
    ],
    "variables": [
        {"name": "gdrive_folder_id", "type": "string",
         "description": "Google Drive folder ID", "required": True},
        {"name": "language", "type": "choice",
         "description": "Output language", "required": False, "default": "zh-HK"},
    ],
}


# ---------- tests ----------

print("[1] Happy path passes")
expect_ok("valid package", GOOD_PKG)
print("[1b] skill field accepted (Stage 1.5)")
expect_ok("valid package with skill field", GOOD_PKG)

print()
print("[2] Top-level shape")
# Empty description is allowed (LLM may produce empty; operator can PATCH later)
expect_ok("empty description still ok", mut() | {"description": ""})
# Missing description is REJECTED at the validator level — but the call-site
# defensive code in workflows.py auto-fills it before reaching the validator.
# This test verifies the validator itself rejects it (defense in depth — if
# the call-site is bypassed, we still get a clear error).
expect_fail("missing description rejected by validator", mut() | {"description": None}, "description")
expect_fail("missing step_template", mut() | {"step_template": []}, "step_template")
expect_fail("step_template not list", mut() | {"step_template": "not a list"}, "step_template")
expect_fail("missing variables", mut() | {"variables": []}, "variables")

print()
print("[2b] skill field checks (Stage 1.5)")
# Bad skill (not kebab-case)
bad = mut()
bad["step_template"][0]["skill"] = "Bad Skill Name"
expect_fail("skill not kebab-case", bad, "kebab")
# Bad skill (L3 scaffolding) — kebab-case check covers most L3 names
# (they all have underscores/colons that fail kebab-case). The action
# L3 check has a more specific list, but for skill, kebab-case is the
# primary defense. We just verify the action check works.
# (no separate skill L3 test — covered by the kebab-case test above)
# Bad skill (empty string)
bad = mut()
bad["step_template"][0]["skill"] = ""
expect_fail("empty skill", bad, "non-empty")
# Bad skill (too long)
bad = mut()
bad["step_template"][0]["skill"] = "a" * 50
expect_fail("skill too long", bad, "too long")
# Good skill (kebab-case)
good = mut()
good["step_template"][0]["skill"] = "bus"
expect_ok("valid skill name", good)
# Skill with {{var}} should NOT be picked up as a variable
good = mut()
good["step_template"][0]["skill"] = "skill-with-static-name"
expect_ok("skill name is static (not in var extraction)", good)

print()
print("[3] Step-level checks")
# Duplicate name
bad = mut()
bad["step_template"] = list(bad["step_template"]) + [bad["step_template"][0]]
expect_fail("duplicate step name", bad, "duplicate")

# Bad name (not kebab)
bad = mut()
bad["step_template"] = [{**bad["step_template"][0], "name": "List Folder"}]
expect_fail("non-kebab step name", bad, "kebab")

# Forward depends_on
bad = mut()
bad["step_template"][1]["depends_on"] = ["does-not-exist"]
expect_fail("depends_on non-existent", bad, "doesn")

# L3 action names
bad = mut()
bad["step_template"][0]["action"] = "coord_pickup"
expect_fail("L3 action coord_pickup", bad, "L3 scaffolding")
bad["step_template"][0]["action"] = "_iteration_review:1"
expect_fail("L3 action _iteration_review", bad, "L3 scaffolding")

# Extra field
bad = mut()
bad["step_template"][0]["bogus_field"] = "x"
expect_fail("extra field", bad, "extra fields")

print()
print("[4] Variable checks")
# Type invalid
bad = mut()
bad["variables"][0]["type"] = "uuid"
expect_fail("invalid variable type", bad, "type=")

# Bad name
bad = mut()
bad["variables"][0]["name"] = "1bad-name"
expect_fail("invalid variable name", bad, "name=")

print()
print("[5] Cross-checks (the most important)")
# {{var}} in step_template but not in variables
bad = mut()
bad["step_template"][0]["params_template"]["secret"] = "{{undeclared_var}}"
expect_fail("{{var}} without variable entry", bad, "without variables")

# Variable declared but not used
bad = mut()
bad["variables"] = list(bad["variables"]) + [
    {"name": "orphan_var", "type": "string", "description": "x", "required": False}
]
expect_fail("orphan variable", bad, "not used")

print()
print("[6] Anti-L3 dump guards")
for bad_str in ("[cite:task.completed@abc", "DECISION: PASS",
                "DECISION: FAIL", "iteration_completed@1"):
    bad = mut()
    bad["description"] = bad["description"] + " " + bad_str
    expect_fail(f"contains L3 marker: {bad_str!r}", bad, "L3 scaffolding")

print()
print("[7] Variable extraction (_extract_variables_from_template)")
# Tests the regex + walker
steps = [
    {
        "name": "s1",
        "params_template": {"a": "{{foo}}", "b": "{{bar}}"},
        "output_path": "out-{{foo}}.json",
    },
    {
        "name": "s2",
        "params_template": {"nested": {"k": "{{baz}}"}},
        "depends_on": [],
    },
]
vars_found = _extract_variables_from_template(steps)
assert vars_found == ["bar", "baz", "foo"], f"got {vars_found}"
PASS_COUNT += 1
print(f"  PASS  extracts {vars_found} in sorted order")

# Empty case
assert _extract_variables_from_template([]) == []
PASS_COUNT += 1
print("  PASS  empty template yields empty list")

# Regex sanity: {{x}} matches but {x}, { {x} } don't
m = _VAR_PLACEHOLDER_RE.findall("{{a}} and {{ b }} and {c} and { {d} }")
assert m == ["a", "b"], f"got {m}"
PASS_COUNT += 1
print(f"  PASS  regex matches strict {{{{name}}}} only (whitespace tolerant)")

# ---------- summary ----------

print()
print(f"=== {PASS_COUNT} pass / {FAIL_COUNT} fail ===")
if FAIL_COUNT:
    sys.exit(1)
print("ALL PASS")
