"""Tests for v3.10.8 / v3.10.9 visual editor keyboard shortcut scoping.

Context (2026-08-02):
  The visual plan editor and visual workflow editor both bind
  keyboard shortcuts at the `document` level. Two related bugs
  reported on the same day:

  1) v3.10.8 — Ctrl+Z / Ctrl+Y hijacked the chatbox textarea's
     native undo. The plan editor's keydown handler fired
     everywhere; even when the chatbox textarea had focus, the
     editor's undo was triggered and the user couldn't undo
     their chat text. Fix: both editors check `isTextField`
     (input / textarea / contenteditable) and bail out early.

  2) v3.10.9 — Ctrl+C / Ctrl+V hijacked the browser's native
     text-copy / text-paste when no card was selected. The
     handlers always called e.preventDefault() even if the
     editor's clipboard was empty (no card selected, no step
     copied). The user couldn't select text on the page
     (step name, error message, etc.) and Ctrl+C to copy it.
     Fix: _copySelectedStep / _pasteClipboard return a boolean
     (true = handled, false = nothing to do); the keydown
     handler only preventDefaults when handled is true.

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


# ===== v3.10.8: undo/redo skips text fields =====


def test_visual_plan_undo_skips_text_fields():
    """The plan editor's Ctrl+Z handler must early-return when
    focus is in a text field, so the browser's native textarea
    undo works in the chatbox."""
    src = _read("visual_plan.js")
    pattern = (
        r"const\s+ctrl\s*=.*?ctrlKey.*?metaKey"
        r".*?"
        r"const\s+isTextField\s*="
        r".*?"
        r"if\s*\(\s*isTextField\s*\)\s*\{[^}]*return"
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


# ===== v3.10.9: Ctrl+C / Ctrl+V only preventDefault when handled =====


def test_visual_plan_copy_paste_conditional_preventdefault():
    """v3.10.9: the keydown handler must only call preventDefault
    when the copy/paste handler actually returned true. The old
    unconditional e.preventDefault() blocked the browser's default
    Ctrl+C / Ctrl+V when no card was selected, so the user
    couldn't copy text on the page."""
    src = _read("visual_plan.js")
    # Look for the pattern: handler returns boolean, then
    # `if (handled) e.preventDefault();`
    pattern = (
        r"_copySelectedStep\(\)"
        r"\s*;\s*"
        r"if\s*\(\s*handled\s*\)\s*e\.preventDefault\(\)"
    )
    assert re.search(pattern, src), (
        "visual_plan.js: v3.10.9 pattern missing — the Ctrl+C "
        "keydown handler must call e.preventDefault() CONDITIONALLY "
        "(only when _copySelectedStep returned true). Unconditional "
        "preventDefault blocks the browser's default text-copy."
    )
    # Same for paste
    pattern_paste = (
        r"_pasteClipboard\(\)"
        r"\s*;\s*"
        r"if\s*\(\s*handled\s*\)\s*e\.preventDefault\(\)"
    )
    assert re.search(pattern_paste, src), (
        "visual_plan.js: v3.10.9 paste pattern missing — the "
        "Ctrl+V keydown handler must call e.preventDefault() "
        "CONDITIONALLY (only when _pasteClipboard returned true)."
    )


def test_visual_workflow_copy_paste_conditional_preventdefault():
    """Same v3.10.9 fix for the workflow editor."""
    src = _read("visual_workflow.js")
    pattern = (
        r"_copySelectedStep\(\)"
        r"\s*;\s*"
        r"if\s*\(\s*handled\s*\)\s*e\.preventDefault\(\)"
    )
    assert re.search(pattern, src), (
        "visual_workflow.js: v3.10.9 pattern missing — the Ctrl+C "
        "keydown handler must call e.preventDefault() CONDITIONALLY."
    )
    pattern_paste = (
        r"_pasteClipboard\(\)"
        r"\s*;\s*"
        r"if\s*\(\s*handled\s*\)\s*e\.preventDefault\(\)"
    )
    assert re.search(pattern_paste, src), (
        "visual_workflow.js: v3.10.9 paste pattern missing — the "
        "Ctrl+V keydown handler must call e.preventDefault() "
        "CONDITIONALLY."
    )


def test_visual_plan_copy_handler_returns_false_on_no_selection():
    """_copySelectedStep must return false (not show a banner)
    when no card is selected — the keydown handler relies on
    this to skip preventDefault."""
    src = _read("visual_plan.js")
    # Look for the early return pattern: if (!selected) return false;
    # inside _copySelectedStep
    pattern = (
        r"function\s+_copySelectedStep\s*\(\s*\)"
        r".*?"
        r"if\s*\(\s*!_selectedNodeName\s*\)"
        r".*?"
        r"return\s+false"
    )
    assert re.search(pattern, src, re.DOTALL), (
        "visual_plan.js: _copySelectedStep must return false "
        "(not show a banner) when no card is selected. This is "
        "the v3.10.9 contract with the keydown handler."
    )


# ===== Sanity: editor undo / copy / paste still work =====


def test_visual_plan_chatbox_undo_compatible():
    """Sanity: visual_plan.js still handles Ctrl+Z outside text
    fields (the editor's own undo must still work)."""
    src = _read("visual_plan.js")
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
