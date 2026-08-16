# coding: utf-8
"""Lint test: `agent_cli.py` must not use raw `httpx.<method>` calls.

Why this exists
---------------
2026-08-17 HermesCtl bug-discovery report, Bug #7a:
    7+ call sites in `agent_cli.py` used `httpx.Client` (and earlier
    `httpx.get/post`) directly. These bypassed the `agent_http`
    abstraction layer's v0.7 HMAC header auto-injection, so the
    wrapper silently fell back to v0.6 even when v0.7 was configured.
    The result: 401 / "Server disconnected" / silent misroute.

The contract: all wrapper HTTP goes through `agent_http`
(`agent_http.get`, `agent_http.post`, `agent_http.Client`, etc).
`httpx` is only allowed for:
  - Exception type references (`except httpx.RequestError`)
  - The `agent_http.py` module itself (where the abstraction lives)
  - Test code
  - Comments (stripped before matching)

What this test does
-------------------
A grep-based check (per design decision D4 in the 2026-08-17 plan).
Not as precise as an AST walk, but cheap, readable, and easy to extend
with an allowlist. Fails the suite if any forbidden pattern is found.

If you legitimately need raw httpx in agent_cli.py, add an explicit
allowlist entry below with a comment explaining why -- do NOT silence
the test globally.
"""
from __future__ import annotations

import re
from pathlib import Path

AGENT_CLI_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "hermes_orch" / "agent_cli.py"
)

# Patterns that indicate raw httpx HTTP calls (NOT exception types).
# Each pattern must match a WHOLE WORD on a NON-COMMENT line.
# We strip line comments (`#...`) before matching.
FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Method calls on the httpx module
    re.compile(r"\bhttpx\.(get|post|put|patch|delete|head|options|request)\s*\("),
    # Client() instantiation (for connection pooling) -- the abstraction
    # `agent_http.Client` must be used instead
    re.compile(r"\bhttpx\.Client\s*\("),
    # `import httpx` (we still allow `except httpx.HTTPError` etc.)
    # NOTE: this test does NOT fail on `import httpx`; the agent_cli.py
    # file legitimately needs `httpx.RequestError` / `httpx.HTTPError`
    # exception types. The forbidden patterns above are HTTP calls,
    # not exception class references.
)

# Lines that legitimately mention httpx (e.g. for the exception types
# `httpx.RequestError`, `httpx.HTTPError`, or the `httpx.Response`
# comment). These are ALLOWED and do not fail the test.
# We don't need an explicit allowlist because the patterns above
# only match HTTP method calls, not exception class references.


def _strip_line_comment(line: str) -> str:
    """Strip a Python `#`-style line comment, respecting string literals
    that contain `#` (basic; doesn't handle triple-quoted strings that
    span lines, but those are rare in agent_cli.py and easy to fix
    if a false positive comes up).
    """
    # Naive: split on the first '#' that's not inside a string. We
    # don't try to be perfectly correct because the only thing we
    # lose is comment-only lines that look like httpx calls (none
    # currently exist in agent_cli.py).
    in_str = False
    str_char: str | None = None
    for i, ch in enumerate(line):
        if in_str:
            if ch == str_char and line[i - 1] != "\\":
                in_str = False
                str_char = None
        else:
            if ch in ("'", '"'):
                in_str = True
                str_char = ch
            elif ch == "#":
                return line[:i]
    return line


def test_agent_cli_no_raw_httpx_calls() -> None:
    """`agent_cli.py` must not call `httpx.<method>` or `httpx.Client()`.

    Failure message lists every offending line so the operator can
    decide whether to (a) replace with the agent_http equivalent,
    or (b) add a justified allowlist entry to this test.
    """
    assert AGENT_CLI_PATH.exists(), (
        f"agent_cli.py not found at expected path: {AGENT_CLI_PATH}"
    )
    source = AGENT_CLI_PATH.read_text(encoding="utf-8")

    offenders: list[tuple[int, str, str]] = []  # (line_no, line, pattern)
    for lineno, raw_line in enumerate(source.splitlines(), start=1):
        code = _strip_line_comment(raw_line)
        for pattern in FORBIDDEN_PATTERNS:
            match = pattern.search(code)
            if match:
                offenders.append((lineno, raw_line.rstrip(), match.group(0)))

    if offenders:
        details = "\n".join(
            f"  L{ln}: {line.strip()}\n      matched: {pat!r}"
            for ln, line, pat in offenders
        )
        raise AssertionError(
            f"agent_cli.py contains {len(offenders)} raw httpx call(s). "
            f"Use `agent_http.get/post/...` or `agent_http.Client` instead "
            f"(the v0.7 HMAC auto-inject lives in agent_http, so bypassing "
            f"it silently degrades the wrapper to v0.6). Offending lines:\n"
            f"{details}"
        )
