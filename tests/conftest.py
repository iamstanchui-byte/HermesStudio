# coding: utf-8
"""Shared pytest fixtures + autouse env setup (security hotfix 2026-08-11).

Provides:
  - session-scoped autouse `set_test_public_origin` fixture that sets
    `HERMES_ORCH_PUBLIC_ORIGIN` to a valid bare origin so the lifespan
    in `hermes_orch.main` doesn't fail-closed during tests. Tests that
    need to test the FAIL-CLOSED case (test_origin_validation.py)
    use `monkeypatch.delenv("HERMES_ORCH_PUBLIC_ORIGIN", raising=False)`
    or set an invalid value via `monkeypatch.setenv` to override.
  - session-scoped autouse `clean_hermes_orch_env` fixture that clears
    other HERMES_ORCH_* env vars at session start to prevent leakage
    from a developer's local shell into test runs.
  - session-scoped autouse `inject_default_origin_header` fixture that
    monkeypatches httpx.AsyncClient to inject a default `Origin` header
    matching the canonical test public origin. This is needed because
    the B12 hotfix added a CSRF check (Origin/Referer) to all
    admin-mutation routes. Pre-existing test fixtures construct
    AsyncClient without any default headers; without this patch they
    would all 403. Tests that want to exercise the CSRF path (e.g.
    test_endpoint_auth.py) can override the Origin header per-request.
"""
from __future__ import annotations

import os

import pytest


# Canonical test origin. Matches the `test_users_api` / `test_hmac_auth`
# pattern: tests hit `http://test` (httpx ASGITransport base_url), so
# we use 127.0.0.1:8765 as a plausible "expected origin" value. Tests
# that exercise CSRF send `Origin: http://127.0.0.1:8765` and expect
# 2xx; cross-origin tests send a different host and expect 403.
TEST_PUBLIC_ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture(autouse=True, scope="session")
def set_test_public_origin():
    """Session-wide: set HERMES_ORCH_PUBLIC_ORIGIN so the lifespan
    in main.py does not fail-closed at startup. The default config
    in `config.py` ships with `server.public_origin: ""` (intentional —
    the operator MUST set it), so the test environment needs an
    explicit value. Override per-test via `monkeypatch.setenv` /
    `monkeypatch.delenv`."""
    old_value = os.environ.get("HERMES_ORCH_PUBLIC_ORIGIN")
    os.environ["HERMES_ORCH_PUBLIC_ORIGIN"] = TEST_PUBLIC_ORIGIN
    yield
    if old_value is None:
        os.environ.pop("HERMES_ORCH_PUBLIC_ORIGIN", None)
    else:
        os.environ["HERMES_ORCH_PUBLIC_ORIGIN"] = old_value


@pytest.fixture(autouse=True, scope="session")
def clean_hermes_orch_env():
    """Session-wide: clear leftover HERMES_ORCH_* env vars from the
    developer's shell so they don't leak into test runs. The list is
    conservative — only clear vars we KNOW would affect test behavior.
    Tests that need a specific override use monkeypatch."""
    leaked = [
        "HERMES_ORCH_CONFIG",
        "HERMES_ORCH_DB",
        "HERMES_ORCH_BIND_HOST",
        "HERMES_ORCH_PORT",
    ]
    snapshots = {k: os.environ.pop(k, None) for k in leaked}
    yield
    for k, v in snapshots.items():
        if v is not None:
            os.environ[k] = v


@pytest.fixture(autouse=True, scope="session")
def inject_default_origin_header():
    """Inject a default `Origin: http://127.0.0.1:8765` header into
    every httpx.AsyncClient request made from tests.

    Why: the B12 hotfix added a CSRF defense (require_same_origin)
    to the 7 admin-mutation routes. Pre-existing test fixtures
    construct `AsyncClient(transport=..., base_url=...)` without
    any default headers, so admin PUT/POST/DELETE/PATCH calls would
    all 403 ("Missing Origin/Referer"). Rather than modify every
    fixture (tens of files), we patch AsyncClient once globally.

    Tests that exercise the CSRF path (test_endpoint_auth.py,
    test_origin_validation.py) override the Origin per-request via
    the `headers={"Origin": "..."}` kwarg on AsyncClient methods.

    Implementation: wrap AsyncClient.send so the Origin header is
    added if and only if the caller didn't supply one. The
    Content-Length, Transfer-Encoding, Host, and other transport-
    internal headers are left alone.
    """
    import httpx

    original_send = httpx.AsyncClient.send

    async def _send_with_origin(self, request, **kwargs):
        # Add Origin ONLY if the caller didn't supply one. This lets
        # test_endpoint_auth.py override per-request for CSRF tests.
        if "origin" not in {k.lower() for k in request.headers}:
            request.headers["Origin"] = TEST_PUBLIC_ORIGIN
        return await original_send(self, request, **kwargs)

    httpx.AsyncClient.send = _send_with_origin
    try:
        yield
    finally:
        httpx.AsyncClient.send = original_send
