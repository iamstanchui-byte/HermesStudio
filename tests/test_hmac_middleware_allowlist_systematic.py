"""Systematic reflection-based test for the HMAC middleware allowlist.

Background
----------
The user-cookie middleware (`src/hermes_orch/main.py:_RequireUserMiddleware`)
gates every /api/* path behind a user session, EXCEPT a hand-curated
list of HMAC-gated agent paths (`_HMAC_PATH_PATTERNS`). If a developer
adds a new endpoint that uses `Depends(require_hmac_auth)` but forgets
to add the matching pattern, the user-cookie middleware returns 401
"Not authenticated" BEFORE the route handler (or `require_hmac_auth`)
ever runs. The wrapper sees a generic 401 with no audit trail and
no clue what to fix. The bug is silent — no exception, no log line
from the broken endpoint, just a dead agent.

This bug class has bitten us twice:
  - v3.5.2 (2026-07-31): /api/tasks/{id}/start was missing from
    the allowlist, 4 tasks sat in `assigned` forever on
    proj-56c8e080.
  - v3.12.1 #7 (2026-08-03): /api/agents/{id}/max_history_config
    was missing — wrapper silently kept its module-level default
    N=6 and never observed value changes.

The existing test
`tests/test_hmac_middleware_allowlist.py` is a WHITELIST test: it
asserts each known path returns the right error code. It does NOT
catch the next developer who adds a new endpoint and forgets the
allowlist. This file adds the safety net that catches that.

Approach
--------
1. Build the FastAPI app via `create_app()`.
2. Walk every `app.routes` entry.
3. For each route that uses `Depends(require_hmac_auth)` in its
   dependencies, build the expected path pattern by replacing
   `{param}` placeholders with `[^/]+` and anchoring with `^`
   and `/?$`.
4. Assert that at least one pattern in `_HMAC_PATH_PATTERNS`
   matches the expected path.

If a developer adds `POST /api/agents/{id}/foo` with
`Depends(require_hmac_auth)` but forgets to add a pattern to
`_HMAC_PATH_PATTERNS`, this test fails with a clear message
listing the missing entry.

What this test does NOT cover
-----------------------------
- It does NOT generate the allowlist automatically. That's the
  v3.12.2 router-driven rebuild tracked separately. For now the
  list is still hand-curated, but this test is the safety net
  that catches drift between the list and the actual route table.
- It does NOT cover user-only routes (those are gated by the
  middleware on purpose; they don't need HMAC).
- It does NOT cover the v3.5.2 allowlist gap retroactively — the
  existing whitelist test in
  `tests/test_hmac_middleware_allowlist.py` still owns that.
"""
from __future__ import annotations

import inspect
import re
from typing import Iterable

import pytest

from hermes_orch.auth.hmac import require_hmac_auth
from hermes_orch.main import _HMAC_PATH_PATTERNS, create_app


@pytest.fixture(scope="module")
def app():
    """Build the FastAPI app once for the module (the route table
    is static across calls)."""
    return create_app()


def _route_uses_hmac(route) -> bool:
    """True if this route depends on `require_hmac_auth` (directly
    or via nested Depends chains — we walk one level deep which
    covers the current codebase's usage).
    """
    for dep in getattr(route, "dependencies", []) or []:
        if dep.dependency is require_hmac_auth:
            return True
    # Also check the function signature for Depends(require_hmac_auth)
    # (covers cases where Depends is in the function signature
    # default, not the route's dependencies list).
    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        try:
            sig = inspect.signature(endpoint)
        except (TypeError, ValueError):
            return False
        for param in sig.parameters.values():
            if param.default is inspect.Parameter.empty:
                continue
            # FastAPI stores Depends as a `FieldInfo` with `.default`
            # being the Depends instance.
            dep = getattr(param.default, "dependency", None)
            if dep is require_hmac_auth:
                return True
    return False


def _expected_path_regex(route) -> str:
    """Build the expected regex pattern from a route's path.

    - Replace `{param}` placeholders with `[^/]+`
    - Anchor with `^...$` (the allowlist patterns all anchor)
    - The query string is stripped by the middleware before
      matching (see main.py comment), so we don't need to
      handle `?foo=bar` here.
    """
    path = route.path
    # Replace path params: /foo/{id}/bar -> /foo/[^/]+/bar
    path_regex = re.sub(r"\{[^/}]+\}", r"[^/]+", path)
    return f"^{path_regex}/?$"


def _iter_hmac_routes(app) -> Iterable:
    """Yield every route in `app` that uses `Depends(require_hmac_auth)`."""
    for route in app.routes:
        if not hasattr(route, "path"):
            continue
        if not hasattr(route, "methods"):
            continue
        if _route_uses_hmac(route):
            yield route


def test_hmac_routes_covered_by_allowlist(app):
    """Every HMAC-gated route must be matched by at least one
    pattern in `_HMAC_PATH_PATTERNS`. Otherwise the user-cookie
    middleware will silently return 401 'Not authenticated' before
    the route handler runs, and the wrapper will see a generic
    401 with no clue what to fix.
    """
    missing: list[str] = []
    for route in _iter_hmac_routes(app):
        path = route.path
        # Convert {param} placeholders to a single example character
        # so the path is a real string the allowlist patterns can
        # fullmatch against. The `[^/]+` segment in the allowlist
        # regex matches any non-slash chars including the example
        # `X` we substitute here.
        path_example = re.sub(r"\{[^/}]+?\}", "X", path)
        # For `{path:path}` style params (FastAPI path-converters that
        # capture multi-segment paths), the allowlist pattern usually
        # uses `^/.../files/` (no $), meaning the trailing path can
        # be any depth. Try a multi-segment variant for those.
        candidates = [
            path_example,
            path_example.rstrip("/"),
            path_example + "/",
            # multi-segment variant for path-converters (e.g. X/Y/Z)
            re.sub(r"\{[^/}]+?(:path)?\}", "X/Y/Z", path),
            re.sub(r"\{[^/}]+?(:path)?\}", "X/Y/Z", path) + "/",
        ]
        candidates = list({c for c in candidates if c})
        # Use `pat.match` (not `fullmatch`) to match the middleware's
        # actual behavior in `main.py:_RequireUserMiddleware.dispatch`:
        # it calls `pat.match(path)` after stripping the query string,
        # so trailing chars past the pattern are allowed (matters for
        # patterns without `$` like `^/api/projects/[^/]+/files/`).
        matched = False
        for pat in _HMAC_PATH_PATTERNS:
            for candidate in candidates:
                if pat.match(candidate):
                    matched = True
                    break
            if matched:
                break
        if not matched:
            methods = ",".join(route.methods or [])
            missing.append(
                f"  {methods:18s} {route.path:60s} "
                f"-> variants tried: {candidates}"
            )

    assert not missing, (
        "\n\nHMAC-gated routes missing from _HMAC_PATH_PATTERNS allowlist:\n"
        + "\n".join(missing)
        + "\n\nThe user-cookie middleware will return 401 'Not authenticated' "
        "BEFORE these route handlers run. The wrapper will see a generic "
        "401 with no audit trail and no idea what's broken.\n\n"
        "Fix: add a `re.compile(r\"^/.../...\")` entry to "
        "`src/hermes_orch/main.py:_HMAC_PATH_PATTERNS` for each missing "
        "path. Use `[^/]+` for path parameters and `/?$` for optional "
        "trailing slashes."
    )


def test_allowlist_patterns_are_valid_regex():
    """The patterns themselves must compile. If someone typos a
    `re.compile` argument, FastAPI won't catch it at startup — the
    bug would only surface when the middleware tries to match.
    """
    import re as _re
    for pat in _HMAC_PATH_PATTERNS:
        assert isinstance(pat, _re.Pattern), (
            f"pattern {pat!r} is not a compiled re.Pattern"
        )
        # Round-trip: compile the pattern's source again to confirm
        # it still parses (catches `re.compile(r"..." + bad_var)`).
        try:
            _re.compile(pat.pattern)
        except _re.error as e:
            pytest.fail(f"pattern {pat.pattern!r} fails to re-compile: {e}")


def test_allowlist_contains_max_history_config():
    """Regression test for the v3.12.1 #7 incident. The
    `/api/agents/{id}/max_history_config` endpoint must stay in
    the allowlist — if a future refactor removes it, the wrapper
    silently keeps its module-level default N and never observes
    a value change from `config.yaml` or `ProjectPlan.max_history_turns`.
    """
    assert any(
        "max_history_config" in pat.pattern for pat in _HMAC_PATH_PATTERNS
    ), (
        "/max_history_config missing from _HMAC_PATH_PATTERNS. "
        "Without it, the wrapper's per-tick config-poll cycle "
        "silently keeps its module-level default N and never "
        "observes a value change. See the v3.12.1 #7 incident."
    )
