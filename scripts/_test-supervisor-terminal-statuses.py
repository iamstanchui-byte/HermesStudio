"""Regression test: supervisor _TERMINAL_TASK_STATUSES constant.

Bug: 2026-07-23 proj-e4c9e5dd had an orphan 'interrupted' task. Supervisor's
any_nonterm SQL omitted 'interrupted' from its terminal set, so the project
stayed in 'running' state forever (12+ hours).

This test verifies the constant is correct AND that the SQL queries use it.
Run with: .venv\Scripts\python.exe scripts/_test-supervisor-terminal-statuses.py
"""
import re
import sys
from pathlib import Path

PROJ_DIR = Path(r"C:\Project\minimax code\hermes-orchestrator")
SUPERVISOR_PY = PROJ_DIR / "src" / "hermes_orch" / "core" / "supervisor.py"


def test_constant_present():
    src = SUPERVISOR_PY.read_text(encoding="utf-8")
    assert "_TERMINAL_TASK_STATUSES" in src, (
        "module-level _TERMINAL_TASK_STATUSES constant missing from supervisor.py"
    )
    # Must include the 5 known terminal statuses
    required = {"completed", "skipped", "cancelled", "failed", "interrupted"}
    match = re.search(
        r"_TERMINAL_TASK_STATUSES\s*=\s*\(([^)]+)\)", src
    )
    assert match, "_TERMINAL_TASK_STATUSES tuple not found"
    items = {s.strip().strip("'\"") for s in match.group(1).split(",")}
    missing = required - items
    assert not missing, f"_TERMINAL_TASK_STATUSES missing: {missing}"


def test_no_hardcoded_terminal_lists():
    """Every 'NOT IN (...)' or 'IN (...)' for terminal task status should
    reference the constant, not be hardcoded. This catches the original
    bug class (forgetting 'interrupted' in one of N parallel SQL queries)."""
    src = SUPERVISOR_PY.read_text(encoding="utf-8")
    # Strip comments first (lines starting with #)
    code_lines = [
        line for line in src.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    # Find any literal list of terminal statuses (NOT IN or IN)
    # that doesn't use _TERMINAL_TASK_STATUSES_SQL
    pattern = re.compile(
        r"(?:NOT\s+IN|IN)\s+\((['\"][^'\"]+['\"]\s*,[^)]+)\)",
        re.IGNORECASE,
    )
    bad = []
    for m in pattern.finditer(code):
        literal = m.group(1)
        # Skip if it's a single-value IN (not a status list)
        items = [s.strip().strip("'\"") for s in literal.split(",")]
        if not any(s in ("completed", "skipped", "cancelled", "failed", "interrupted") for s in items):
            continue
        bad.append((m.start(), literal))
    if bad:
        for offset, literal in bad:
            print(f"  line ~{src[:offset].count(chr(10))+1}: literal = {literal!r}")
        raise AssertionError(
            f"\nFound {len(bad)} hardcoded terminal-status list(s) in supervisor.py. "
            f"\nUse _TERMINAL_TASK_STATUSES_SQL f-string instead. "
            f"\nSee _TERMINAL_TASK_STATUSES constant for the canonical set."
        )


def test_constant_importable():
    """The constant must be importable (so tests / external code can use it)."""
    sys.path.insert(0, str(PROJ_DIR / "src"))
    from hermes_orch.core.supervisor import (
        _TERMINAL_TASK_STATUSES, _TERMINAL_TASK_STATUSES_SQL,
    )
    assert _TERMINAL_TASK_STATUSES == ("completed", "skipped", "cancelled", "failed", "interrupted")
    assert "interrupted" in _TERMINAL_TASK_STATUSES_SQL


if __name__ == "__main__":
    print("[1/3] constant present + has all 5 statuses...")
    test_constant_present()
    print("    OK")
    print("[2/3] no hardcoded terminal-status lists in supervisor.py...")
    test_no_hardcoded_terminal_lists()
    print("    OK")
    print("[3/3] constant importable...")
    test_constant_importable()
    print("    OK")
    print()
    print("ALL PASS")
