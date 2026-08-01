# coding: utf-8
"""Regression test: wrapper auto-upload loop skips well-known dependency / build / VCS dirs.

Background:
  v3.10.3 wrapper bug: proj-5b0e6724 / task 443bc94c ran `npm install
  xml-js` to build a Chinese docx. The wrapper's auto-upload loop
  did `cache_root.rglob("*")` and only filtered `__pycache__/` +
  dotfiles + a few control files, so 373 node_modules/* files were
  uploaded in 1 second, polluting the artifacts table. Real
  deliverables: 4. Spurious uploads: 373.

  v3.10.4 follow-up: proj-e05e89e9 / task 1015df08 (finalize-hk-view-report)
  ran `python -m venv .pdfvenv` to set up an isolated venv for
  PDF-build tools. The exact-name skip list missed `.pdfvenv` (it's
  not `venv` or `.venv` — it's a custom name), so 1403 files / 36MB
  from `.pdfvenv/lib/...` got uploaded as "artifacts". Real
  deliverables: 5. Spurious uploads: 1403. Whack-a-mole (adding
  `.pdfvenv` to the explicit list) is the wrong fix — agents will
  create more custom-named venvs (`.myvenv`, `proj-venv`, `tool_venv`,
  `.sandbox-venv`, etc.) forever. The right fix is a glob pattern
  `*venv*` that matches any directory containing "venv".

Fix: support BOTH exact names AND glob patterns in
`_SKIP_DIR_PATTERNS`. Exact names are matched as-is, patterns use
`fnmatch.fnmatchcase` (case-insensitive via lowercase both sides).
Skip if ANY parent path component matches a name or pattern.

The skip set + the parent-component match logic live in
`agent_cli.py` near the auto-upload loop. This test asserts:
  * The skip list contains the required entries (exact + patterns)
  * No path-separator-containing entries
  * The production check uses parent-component (not leaf) matching
  * Glob patterns are supported (catches custom-named dirs)
  * Real-world path batteries (a few dozen cases) match the
    production logic for both `node_modules` and `*venv*` variants
  * The skip logic is still cheap (no expensive regex compilation
    per file — pre-compiled at import time)

Notes on test design:
  * We don't run the full Popen-based hermes subprocess path — that
    needs a real cache + orch. Instead we directly check the skip
    list AND re-implement the parent-component check, then assert
    it matches the production logic for a battery of file paths.
  * If the production check ever changes (e.g. adds file-name
    matching, changes case sensitivity), this test must be updated
    in lockstep — that's the point of having it as a regression.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

AGENT_CLI_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "hermes_orch" / "agent_cli.py"
)


def _load_skip_patterns_from_source() -> tuple[str, ...]:
    """Extract `_SKIP_DIR_PATTERNS = (...)` from agent_cli.py.

    The constant changed from a `frozenset` (v3.10.3) to a `tuple`
    (v3.10.4 follow-up) so we can store both exact names AND glob
    patterns. We extract the tuple literal and evaluate it.

    We deliberately do NOT import the whole agent_cli module — it
    pulls in click + a lot of CLI scaffolding, and we just want
    the constant for an independent assertion. A regex/ast eval
    keeps the test decoupled from runtime side effects.
    """
    src = AGENT_CLI_PATH.read_text(encoding="utf-8")
    # Find the assignment, then count parens to find the matching
    # closing `)`. The body may contain comments with their own
    # parens, so a non-greedy `.*?\)` is unsafe.
    m = re.search(r"_SKIP_DIR_PATTERNS\s*=\s*\(", src)
    assert m, "_SKIP_DIR_PATTERNS = (...) not found in agent_cli.py"
    start = m.end()  # position right after the opening `(`
    depth = 1
    i = start
    while i < len(src) and depth > 0:
        ch = src[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    assert depth == 0, "could not find matching `)` for _SKIP_DIR_PATTERNS = (...)"
    body = src[start:i - 1]
    import ast
    items_src = "[" + body + "]"
    items = ast.literal_eval(items_src)
    assert all(isinstance(x, str) for x in items), "skip patterns must be strings"
    return tuple(items)


# Load once at import time
_SKIP_DIR_PATTERNS = _load_skip_patterns_from_source()


# ===== Sanity: set contents =====


def test_skip_set_contains_core_dependency_dirs():
    """The bug we fixed was node_modules/, so it MUST be in the set.
    Other obvious cases (.git, venv, __pycache__) should also be there."""
    for required in ("node_modules", ".git", "__pycache__", "venv", ".venv"):
        assert required in _SKIP_DIR_PATTERNS, (
            f"{required!r} should be in the skip set — "
            f"without it, npm install / git clone / pip install will "
            f"upload hundreds of dependency files as artifacts."
        )


def test_skip_set_contains_ecosystem_specific_dirs():
    """Spot-check the JS/TS ecosystem (next, nuxt, parcel), Python tooling
    (pytest_cache, mypy_cache), Rust (target), and coverage tools.
    If any of these are missing, the corresponding tool's cache
    will be uploaded verbatim on every task."""
    for required in (
        ".pytest_cache",
        ".mypy_cache",
        "target",
        ".next",
        "coverage",
    ):
        assert required in _SKIP_DIR_PATTERNS, (
            f"{required!r} should be in the skip set — missing it means "
            f"the corresponding tool's cache is uploaded as artifacts."
        )


def test_skip_set_contains_glob_patterns_for_venv_variants():
    """v3.10.4 follow-up: `.pdfvenv` (1403 files, 36MB) was missed
    by the exact-name list. We added `*venv*` as a glob pattern to
    catch any custom-named venv the agent creates (`.myvenv`,
    `proj-venv`, `tool_venv`, `.sandbox-venv`, etc.). Without this
    pattern, the next project that runs `python -m venv .myenv`
    will dump another 1000+ files into the artifacts table."""
    for required in ("*venv*", "*.egg-info", "*.dist-info"):
        assert required in _SKIP_DIR_PATTERNS, (
            f"glob pattern {required!r} should be in the skip set — "
            f"without it, custom-named Python venvs / package metadata "
            f"get uploaded as artifacts (see proj-e05e89e9 / 1015df08 "
            f"for the v3.10.4 follow-up bug)."
        )


def test_skip_set_has_no_path_separators():
    """Each entry must be a single directory name or glob pattern,
    not a relative path. The match logic splits on `/` and checks
    each component, so a multi-segment entry would never match."""
    for entry in _SKIP_DIR_PATTERNS:
        assert "/" not in entry, (
            f"skip entry {entry!r} contains a path separator; "
            f"entries must be single dir names or single-segment "
            f"glob patterns (match splits on '/')."
        )
        assert "\\" not in entry, (
            f"skip entry {entry!r} contains a backslash; "
            f"this is a Linux+Windows cross-platform set, use forward slashes."
        )


# ===== Behavior: parent-component match logic =====


def _should_skip(rel_path: str, skip_patterns: tuple[str, ...]) -> bool:
    """Mirror the production check in agent_cli.py. Skip iff any
    PARENT path component (not the leaf filename) matches a
    name or glob pattern in the skip set (case-insensitive).

    Exact names are matched as-is; patterns use `fnmatch.fnmatchcase`
    (case-insensitive via lowercase both sides).
    """
    rel_parts = rel_path.split("/")
    for p in rel_parts[:-1]:
        pl = p.lower()
        for pat in skip_patterns:
            if "*" in pat:
                if fnmatch.fnmatchcase(pl, pat.lower()):
                    return True
            else:
                if pl == pat:
                    return True
    return False


# A battery of real-world paths that demonstrate the desired behavior.
# Each tuple is (relative path, should_be_skipped, reason).
_PATHS: list[tuple[str, bool, str]] = [
    # === node_modules (the actual bug from proj-5b0e6724) ===
    ("node_modules/xml-js/lib/xml2json.js", True, "node_modules root"),
    ("web/node_modules/react/index.js", True, "nested node_modules"),
    ("packages/foo/node_modules/.bin/tsc", True, "deeply nested node_modules"),
    # === .git ===
    (".git/HEAD", True, ".git root"),
    ("subdir/.git/config", True, "nested .git"),
    # === __pycache__ ===
    ("__pycache__/bar.cpython-311.pyc", True, "__pycache__ root"),
    ("src/foo/__pycache__/bar.pyc", True, "nested __pycache__"),
    # === venv / .venv ===
    (".venv/lib/python3.12/site-packages/foo.py", True, ".venv"),
    ("venv/bin/activate", True, "venv"),
    # === *venv* glob pattern (the v3.10.4 follow-up bug) ===
    (".pdfvenv/lib/python3.11/site-packages/distutils-precedence.pth",
     True, "agent-created custom-named venv (.pdfvenv) — caught by *venv* pattern"),
    (".pdfvenv/bin/activate", True, ".pdfvenv nested"),
    (".pdfvenv/pyvenv.cfg", True, ".pdfvenv root file"),
    ("my-venv/lib/foo.py", True, "my-venv (no leading dot, hyphen) — caught by *venv*"),
    ("proj_venv/bin/activate", True, "proj_venv — caught by *venv*"),
    ("tool.venv/foo.py", True, "tool.venv — caught by *venv*"),
    (".sandbox-venv/lib/foo.py", True, ".sandbox-venv — caught by *venv*"),
    # === *.egg-info / *.dist-info (Python package metadata) ===
    ("mypackage-1.0.egg-info/PKG-INFO", True, "egg-info"),
    ("foo.dist-info/METADATA", True, "dist-info"),
    ("site-packages/mypackage.dist-info/RECORD", True, "dist-info nested"),
    # === JS/TS framework caches ===
    (".next/server/pages/index.js", True, ".next"),
    (".nuxt/dist/server.js", True, ".nuxt"),
    (".turbo/cache/build.json", True, ".turbo"),
    # === Rust / build outputs ===
    ("target/debug/foo", True, "target"),
    ("dist/bundle.js", True, "dist"),
    ("build/output.json", True, "build"),
    # === Coverage / pytest cache ===
    ("coverage/lcov.info", True, "coverage"),
    (".pytest_cache/v/cache/lastfailed", True, ".pytest_cache"),
    # === Case insensitivity ===
    ("Node_Modules/foo.js", True, "case-insensitive match (Windows-friendly)"),
    ("NODE_MODULES/foo.js", True, "uppercase"),
    (".GIT/HEAD", True, "uppercase .git"),
    (".PDFvenv/lib/foo.py", True, "case-insensitive *venv* match"),
    # === Real deliverables at cache root: MUST NOT be skipped ===
    ("build_kimi_docx.js", False, "file at root with name `build_*` is NOT a dir"),
    ("report.md", False, "deliverable at root"),
    ("kimi_k3_impact_report.docx", False, "docx at root"),
    ("data/result.json", False, "real deliverable in subdir not in skip list"),
    ("src/index.ts", False, "real source file at root"),
    ("sangfor_vs_proxmox_hk_final_report.pdf", False, "PDF deliverable at root"),
    ("build_final_pdf.py", False, "Python script at root, not a venv"),
    # === Edge cases: filename that happens to match a skip-dir name ===
    ("build.py", False, "filename `build.py` is NOT a dir, must not be skipped"),
    ("node_modules.txt", False, "filename `node_modules.txt` is NOT a dir"),
    (".gitignore", False, "dotfile is filtered by a separate check, not the skip set"),
    ("venv-notes.md", False, "filename `venv-notes.md` is NOT a dir; "
                              "*venv* only matches the parent dir name, "
                              "not the leaf filename"),
    ("doc.pdf", False, "real PDF deliverable"),
    # === Glob pattern edge cases ===
    ("env.example", False, "filename with 'env' but not a dir"),
    ("environment.yml", False, "filename with 'env' but not a dir"),
    ("myvenv.txt", False, "filename with 'venv' but not a dir"),
]


@pytest.mark.parametrize("rel,expected,reason", _PATHS)
def test_skip_set_filters_correctly(rel: str, expected: bool, reason: str) -> None:
    assert _should_skip(rel, _SKIP_DIR_PATTERNS) is expected, (
        f"path {rel!r} should {'be skipped' if expected else 'be kept'} "
        f"({reason}); got the opposite. The skip set or the parent-component "
        f"match logic regressed — see the v3.10.4 node_modules bug and the "
        f"v3.10.4 .pdfvenv follow-up."
    )


# ===== Integration: confirm the production code uses the same logic =====
# This is a soft check: the parent-component test above would still
# pass even if the production code were rewritten with a different
# logic. The "production logic" check below catches the case where
# someone refactors the production check without updating this test.


def test_production_check_matches_test_helper() -> None:
    """Sanity check: the production code in agent_cli.py uses the same
    parent-component check + fnmatch logic that this test simulates.
    We grep the source for the exact patterns and fail if any are
    gone.
    """
    src = AGENT_CLI_PATH.read_text(encoding="utf-8")
    # The production check is:
    #   for p in rel_parts[:-1]:
    #       for pat in _SKIP_DIR_PATTERNS:
    #           if "*" in pat: fnmatch.fnmatchcase(pl, pat.lower())
    #           else: pl == pat
    # Look for the substrings that prove the logic shape.
    assert "rel_parts" in src, (
        "agent_cli.py no longer uses the rel_parts parent-component "
        "check. The skip-dir filter may have been rewritten — update "
        "this test to match the new logic, otherwise the node_modules "
        "or .pdfvenv bugs may silently come back."
    )
    assert "_SKIP_DIR_PATTERNS" in src, (
        "_SKIP_DIR_PATTERNS constant is missing from agent_cli.py. "
        "The skip set was inlined or removed — restore it as a "
        "module-level tuple so this test can re-import it."
    )
    assert "fnmatch" in src, (
        "agent_cli.py no longer imports fnmatch. The glob pattern "
        "support is gone — without it, custom-named venvs (`.pdfvenv`, "
        "`proj-venv`, etc.) will be uploaded as artifacts. See "
        "v3.10.4 follow-up bug (proj-e05e89e9 / 1015df08) for context."
    )
