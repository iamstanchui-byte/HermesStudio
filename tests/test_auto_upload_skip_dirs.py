# coding: utf-8
"""Regression test: wrapper auto-upload loop skips well-known dependency / build / VCS dirs.

Background (v3.10.4, 2026-08-02):
  proj-5b0e6724 / task 443bc94c ran `npm install xml-js` to build a Chinese
  docx. The wrapper's auto-upload loop did `cache_root.rglob("*")` and
  only filtered `__pycache__/` + dotfiles + a few control files, so
  373 node_modules/* files were uploaded in 1 second, polluting the
  artifacts table. Real deliverables: 4. Spurious uploads: 373.

Fix: skip any file under a known dependency / build / VCS directory.
The skip set lives in `agent_cli.py` near the auto-upload loop
(`_SKIP_DIR_NAMES` frozenset). This test asserts the set + the
parent-component match logic, so a future refactor that loses
the filter is caught immediately.

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

import importlib
import os
import sys
from pathlib import Path

import pytest

AGENT_CLI_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "hermes_orch" / "agent_cli.py"
)


def _load_skip_set_from_source() -> frozenset[str]:
    """Extract `_SKIP_DIR_NAMES = frozenset({...})` from agent_cli.py
    by reading the source and evaluating the set literal.

    We deliberately do NOT import the whole agent_cli module — it pulls
    in click + a lot of CLI scaffolding, and we just want the
    constant for an independent assertion. A regex/exec keeps the
    test decoupled from runtime side effects.
    """
    import re

    src = AGENT_CLI_PATH.read_text(encoding="utf-8")
    # The constant is `_SKIP_DIR_NAMES = frozenset({...})`. Match
    # greedily across lines until the closing `)`. We tolerate any
    # whitespace inside the set.
    m = re.search(
        r"_SKIP_DIR_NAMES\s*=\s*frozenset\(\s*\{(.*?)\}\s*\)",
        src,
        re.DOTALL,
    )
    assert m, "_SKIP_DIR_NAMES = frozenset({...}) not found in agent_cli.py"
    # Parse the set literal. Items are quoted strings (with optional
    # trailing comma). Use ast.literal_eval on a synthesised
    # expression — safer than manual string split.
    import ast

    items_src = "[" + m.group(1) + "]"
    items = ast.literal_eval(items_src)
    assert all(isinstance(x, str) for x in items), "skip set items must be strings"
    return frozenset(items)


# Load once at import time
_SKIP_DIR_NAMES = _load_skip_set_from_source()


# ===== Sanity: set contents =====


def test_skip_set_contains_core_dependency_dirs():
    """The bug we fixed was node_modules/, so it MUST be in the set.
    Other obvious cases (.git, venv, __pycache__) should also be there."""
    for required in ("node_modules", ".git", "__pycache__", "venv", ".venv"):
        assert required in _SKIP_DIR_NAMES, (
            f"{required!r} should be in the auto-upload skip set — "
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
        assert required in _SKIP_DIR_NAMES, (
            f"{required!r} should be in the skip set — missing it means "
            f"the corresponding tool's cache is uploaded as artifacts."
        )


def test_skip_set_has_no_path_separators():
    """Each entry must be a single directory name, not a relative
    path. The match logic splits on `/` and checks each component,
    so a multi-segment entry would never match."""
    for entry in _SKIP_DIR_NAMES:
        assert "/" not in entry, (
            f"skip set entry {entry!r} contains a path separator; "
            f"entries must be single dir names (match splits on '/')."
        )
        assert "\\" not in entry, (
            f"skip set entry {entry!r} contains a backslash; "
            f"this is a Linux+Windows cross-platform set, use forward slashes."
        )


# ===== Behavior: parent-component match logic =====


def _should_skip(rel_path: str, skip_set: frozenset[str]) -> bool:
    """Mirror the production check in agent_cli.py:2689-2734.
    Skip iff any PARENT path component (not the leaf filename)
    case-insensitively matches a name in the skip set.
    """
    rel_parts = rel_path.split("/")
    # The leaf is the last component; the parents are everything before.
    return any(p.lower() in skip_set for p in rel_parts[:-1])


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
    # === Real deliverables at cache root: MUST NOT be skipped ===
    ("build_kimi_docx.js", False, "file at root with name `build_*` is NOT a dir"),
    ("report.md", False, "deliverable at root"),
    ("kimi_k3_impact_report.docx", False, "docx at root"),
    ("data/result.json", False, "real deliverable in subdir not in skip list"),
    ("src/index.ts", False, "real source file at root"),
    # === Edge cases: filename that happens to match a skip-dir name ===
    ("build.py", False, "filename `build.py` is NOT a dir, must not be skipped"),
    ("node_modules.txt", False, "filename `node_modules.txt` is NOT a dir"),
    (".gitignore", False, "dotfile is filtered by a separate check, not the skip set"),
]


@pytest.mark.parametrize("rel,expected,reason", _PATHS)
def test_skip_set_filters_correctly(rel: str, expected: bool, reason: str) -> None:
    assert _should_skip(rel, _SKIP_DIR_NAMES) is expected, (
        f"path {rel!r} should {'be skipped' if expected else 'be kept'} "
        f"({reason}); got the opposite. The skip set or the parent-component "
        f"match logic regressed — see the v3.10.4 node_modules bug."
    )


# ===== Integration: confirm the production code uses the same logic =====
# This is a soft check: the parent-component test above would still
# pass even if the production code were rewritten with a different
# logic. The "production logic" check below catches the case where
# someone refactors the production check without updating this test.


def test_production_check_matches_test_helper() -> None:
    """Sanity check: the production code in agent_cli.py uses the same
    parent-component check that this test simulates. We grep the
    source for the exact pattern and fail if it's gone.
    """
    src = AGENT_CLI_PATH.read_text(encoding="utf-8")
    # The production check is: `if any(p.lower() in _SKIP_DIR_NAMES for p in rel_parts[:-1]):`
    # We look for the substring that proves the logic shape.
    assert "rel_parts" in src, (
        "agent_cli.py no longer uses the rel_parts parent-component "
        "check. The skip-dir filter may have been rewritten — update "
        "this test to match the new logic, otherwise the node_modules "
        "bug may silently come back."
    )
    assert "_SKIP_DIR_NAMES" in src, (
        "_SKIP_DIR_NAMES constant is missing from agent_cli.py. "
        "The skip set was inlined or removed — restore it as a "
        "module-level frozenset so this test can re-import it."
    )
