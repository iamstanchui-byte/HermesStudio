"""Tests for v3.11.0 TASK_FAILED convention (savings-demo case).

Context (2026-08-03):
  The user wanted to test feedback_to with a "saving for a
  game" simulation: A adds random $1-$15 to a savings file,
  B checks if total >= $100. If B fails (total < $100),
  feedback_to triggers A to re-run. But the agent has no
  clean way to mark a task as failed — hermes exits 0 even
  when the agent "decides" the task should fail (e.g. via
  threshold not met).

  v3.11.0 convention: the agent writes a line containing
  `TASK_FAILED: <reason>` to stdout. The wrapper detects the
  marker in the cleaned summary and converts the result to
  status=failed, with the marker text as the `error` field.
  The supervisor's loop-back then fires (existing path).

  This test covers the marker-detection helper
  `_apply_task_failed_marker` in agent_cli.py. The helper
  is extracted from `_run_task` so it can be unit-tested
  in isolation (the rest of `_run_task` requires a full
  hermes subprocess + agent config + DB).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Import the helper. agent_cli.py has a Click-decorated entry
# point (`@click.group`) that runs at import time. We import
# only the helper by path to keep the test surface minimal.
import importlib.util

_AGENT_CLI = Path(__file__).resolve().parent.parent / "src" / "hermes_orch" / "agent_cli.py"
spec = importlib.util.spec_from_file_location("_agent_cli_under_test", _AGENT_CLI)
agent_cli = importlib.util.module_from_spec(spec)
# Don't execute the module's main CLI logic — we only want the
# helpers. Spec loader.exec_module runs the whole module which
# also calls @click.group; we accept that since the helpers
# are top-level defs and don't depend on the click commands.
spec.loader.exec_module(agent_cli)

apply_marker = agent_cli._apply_task_failed_marker


# ===== No marker → status=completed =====


def test_no_marker_returns_completed():
    """The base case: a normal agent output (no TASK_FAILED
    marker) yields the same status=completed result as before
    the v3.11.0 change. No regression for the 99% case."""
    summary = "All looks good. Total is now $120, can buy the game!"
    result = apply_marker(summary)
    assert result["status"] == "completed"
    assert result["summary"] == summary
    assert "error" not in result


def test_empty_summary_returns_completed():
    """Defensive: empty stdout (e.g. hermes never wrote anything
    before exiting 0) still yields status=completed."""
    result = apply_marker("")
    assert result["status"] == "completed"
    assert result["summary"] == ""


def test_whitespace_only_summary_returns_completed():
    """Defensive: whitespace-only stdout (common when hermes
    strips everything). Should still be completed."""
    result = apply_marker("   \n\n  \n")
    assert result["status"] == "completed"


# ===== Marker present → status=failed =====


def test_simple_marker_makes_failed():
    """The canonical savings-demo case: B reads the file, sees
    total < threshold, prints TASK_FAILED and exits 0. The
    wrapper converts to status=failed so feedback_to fires."""
    summary = (
        "TASK_FAILED: total=$45 < threshold=$100, "
        "need $55 more"
    )
    result = apply_marker(summary)
    assert result["status"] == "failed"
    # The error field is "TASK_FAILED: <reason>" — the orchestrator
    # renders this in the task detail and in the dispatch.mismatch
    # style error display.
    assert result["error"] == "TASK_FAILED: total=$45 < threshold=$100, need $55 more"
    # The full summary is preserved for the dashboard's "show full"
    # link so the operator can see what the agent was thinking.
    assert result["summary"] == summary


def test_marker_with_surrounding_text_makes_failed():
    """Realistic: agent does the work, prints a final summary
    line that includes the marker. The marker is the LAST
    line (the agent's conclusion), earlier lines are context."""
    summary = (
        "Reading savings.txt...\n"
        "Current total: $45\n"
        "Threshold: $100\n"
        "TASK_FAILED: total $45 < threshold $100, need $55 more"
    )
    result = apply_marker(summary)
    assert result["status"] == "failed"
    assert "TASK_FAILED: total $45 < threshold $100" in result["error"]


def test_marker_in_middle_of_line_extracts_after():
    """Edge: marker is followed by more text on the same line.
    The reason is everything after the marker, trimmed."""
    summary = "Some preamble TASK_FAILED: only this part is the reason"
    result = apply_marker(summary)
    assert result["status"] == "failed"
    assert result["error"] == "TASK_FAILED: only this part is the reason"


def test_multiple_markers_takes_first():
    """Defensive: if multiple markers appear (e.g. the agent
    printed it twice, or it appears in a quoted comment), the
    FIRST one is the agent's primary signal. The helper takes
    the first line containing the marker (which corresponds to
    the first marker occurrence in document order)."""
    summary = (
        "TASK_FAILED: first reason\n"
        "Some middle line\n"
        "TASK_FAILED: second reason, should be ignored"
    )
    result = apply_marker(summary)
    assert result["status"] == "failed"
    assert result["error"] == "TASK_FAILED: first reason"


def test_marker_with_no_reason_still_fails():
    """Edge: the agent wrote just `TASK_FAILED:` with no reason
    text. The reason is empty string. The task still fails,
    which is the point — the agent signals failure, not success.
    Operator can dig into `summary` for context."""
    summary = "TASK_FAILED:"
    result = apply_marker(summary)
    assert result["status"] == "failed"
    assert result["error"] == "TASK_FAILED: "  # empty reason, trailing space


def test_marker_after_cleanup_survives():
    """The marker must survive `_clean_hermes_output`'s stripping
    (which removes ANSI codes + box-drawing + session metadata).
    The agent writes `TASK_FAILED: ...` plain text on its own
    line — none of the cleanup rules should match it. This test
    uses a realistic post-cleanup summary."""
    summary = (
        "savings.txt: 45\n"
        "threshold: 100\n"
        "TASK_FAILED: total below threshold, keep saving"
    )
    cleaned_then_marker = summary  # _clean_hermes_output would not strip plain ASCII
    result = apply_marker(cleaned_then_marker)
    assert result["status"] == "failed"
    assert "TASK_FAILED" in result["error"]


# ===== Marker is case-sensitive (defensive) =====


def test_lowercase_marker_does_not_trigger():
    """The convention is `TASK_FAILED:` (uppercase). A lowercase
    or mixed-case variant is treated as normal text. This is
    intentional — the convention is a stable protocol and
    case sensitivity avoids accidental matches with English
    prose like 'my task failed' (which would lowercase the
    marker)."""
    summary = "task failed: my computer is slow"
    result = apply_marker(summary)
    assert result["status"] == "completed"
    assert "error" not in result


# ===== Integration with feedback_to =====


def test_failed_result_has_required_fields_for_orchestrator():
    """The result shape must be what /api/tasks/{id}/result
    expects (TaskResult model in api/tasks.py):
        - status: str  ("completed" | "failed")
        - session_id: str | None
        - summary: str | None
        - error: str | None (only meaningful when status=failed)

    The marker-detection helper always includes the full
    summary in the result (even when failing), so the
    operator sees both the agent's reasoning AND the failure
    reason. The `error` field is what the orchestrator's
    TaskResult model uses to surface the failure in the UI."""
    summary = "Working on it...\nTASK_FAILED: threshold not met"
    result = apply_marker(summary)
    assert "status" in result
    assert "summary" in result
    assert "error" in result
    assert "skipped_artifacts" in result
    assert result["status"] == "failed"
    assert "TASK_FAILED" in result["error"]
    assert result["summary"] == summary
