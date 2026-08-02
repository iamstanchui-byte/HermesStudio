"""Tests for v3.10.8 visual editor keyboard shortcut scoping.

Context (2026-08-02):
  The visual plan editor and visual workflow editor both bind
  keyboard shortcuts at the `document` level. The plan editor's
  handler at `visual_plan.js:_bindGlobalShortcuts` previously
  fired Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z in EVERY text field,
  including:
    - the chatbox textarea (docked at the bottom of the
      project page)
    - the side-panel form fields (step name, description, etc.)
  This meant typing in the chatbox and pressing Cmd+Z would
  silently invoke the plan editor's undo (reverting the most
  recent plan change) instead of undoing the chat text. The
  user couldn't undo their chat message.

  v3.10.8 fix: both editors now check `isTextField` (input /
  textarea / contenteditable) and bail out early so the
  browser's native text-field undo works as expected.

These tests are source-grep tests, not full browser tests.
The project's Python test suite doesn't have a JS runtime;
we verify the right pattern is present in the JS source.
If the JS code is ever refactored and the test breaks, the
human will need to confirm the text-field guard is preserved
(visual review of the keydown handler is fast).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "src" / "hermes_orch" / "static"


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_visual_plan_undo_skips_text_fields():
    """The plan editor's Ctrl+Z handler must early-return when
    focus is in a text field, so the browser's native textarea
    undo works in the chatbox."""
    src = _read("visual_plan.js")
    # Find the keydown handler block (the one that handles
    # Ctrl+Z / Ctrl+Y). We look for the early-return on
    # isTextField AFTER the ctrl check.
    pattern = (
        r"const\s+ctrl\s*=.*?ctrlKey.*?metaKey"          # const ctrl = ...
        r".*?"                                           # any other code
        r"const\s+isTextField\s*="                       # const isTextField
        r".*?"                                           # = expression
        r"if\s*\(\s*isTextField\s*\)\s*\{[^}]*return"    # early return on isTextField
    )
    assert re.search(pattern, src, re.DOTALL), (
        "visual_plan.js no longer has the v3.10.8 text-field "
        "early-return in the Ctrl+Z keydown handler. Re-check "
        "_bindGlobalShortcuts — the chatbox undo will be hijacked "
        "by the plan editor again."
    )


def test_visual_workflow_undo_skips_text_fields():
    """Same fix for the workflow editor."""
    src = _read("visual_workflow.js")
    # The workflow editor has a simpler structure: ctrl check,
    # then isTextField check, then return.
    pattern = (
        r"const\s+ctrl\s*=.*?ctrlKey.*?metaKey"
        r".*?"
        r"const\s+isTextField\s*="
        r".*?"
        r"if\s*\(\s*isTextField\s*\)\s*return"
    )
    assert re.search(pattern, src, re.DOTALL), (
        "visual_workflow.js no longer has the v3.10.8 text-field "
        "early-return in the Ctrl+Z keydown handler. The chatbox "
        "undo will be hijacked by the workflow editor again."
    )


def test_visual_plan_chatbox_undo_compatible():
    """Sanity: visual_plan.js still handles Ctrl+Z outside text
    fields (the editor's own undo must still work)."""
    src = _read("visual_plan.js")
    # _undo() is called from the keydown handler
    assert "_undo()" in src, (
        "visual_plan.js: _undo() call missing — the editor's "
        "Ctrl+Z behavior is broken"
    )


def test_visual_workflow_chatbox_undo_compatible():
    """Same for the workflow editor."""
    src = _read("visual_workflow.js")
    assert "_undo()" in src, (
        "visual_workflow.js: _undo() call missing — the editor's "
        "Ctrl+Z behavior is broken"
    )
