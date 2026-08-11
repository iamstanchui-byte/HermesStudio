# coding: utf-8
"""Tests for the canonical public-origin startup validation
(security hotfix 2026-08-11, R13).

Covers:
  - 5 startup-validation tests (§6.1, R13 of the design doc):
    * public_origin unset prevents startup
    * invalid port prevents startup
    * path component prevents startup
    * query/fragment prevents startup
    * wrong scheme prevents startup
  - 2 unit tests for `validate_public_origin` itself (positive case
    and trailing-slash rejection)

The lifespan hook in `hermes_orch.main` calls `validate_public_origin`
BEFORE any other startup work. If validation fails, the lifespan
raises and the server never binds. These tests pin the contract.

The autouse `set_test_public_origin` conftest fixture sets
`HERMES_ORCH_PUBLIC_ORIGIN` to `http://127.0.0.1:8765` for the
default test environment. Tests in this file use `monkeypatch`
to override it (set to empty, set to invalid, etc.) and verify
the startup fails closed.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch import main as main_mod
from hermes_orch import db as db_mod
from hermes_orch.auth.cookie import ROLE_ADMIN, create_user
from hermes_orch.auth.origin_validation import validate_public_origin
from hermes_orch.main import create_app


# ===== Unit tests for validate_public_origin =====


def test_validate_public_origin_accepts_canonical_form():
    """Valid bare origin with explicit port returns canonical form."""
    assert validate_public_origin("http://127.0.0.1:8765") == "http://127.0.0.1:8765"
    assert validate_public_origin("https://orch.example.com:443") == (
        "https://orch.example.com:443"
    )


def test_validate_public_origin_rejects_trailing_slash():
    """REGRESSION TEST: a trailing '/' makes the path = '/' which
    must be rejected (per the design's mandatory bare-origin contract)."""
    with pytest.raises(ValueError, match="trailing"):
        validate_public_origin("http://127.0.0.1:8765/")


def test_validate_public_origin_rejects_none():
    with pytest.raises(ValueError, match="not set"):
        validate_public_origin(None)


def test_validate_public_origin_rejects_empty_string():
    with pytest.raises(ValueError, match="empty"):
        validate_public_origin("")


def test_validate_public_origin_rejects_whitespace_only():
    with pytest.raises(ValueError, match="empty"):
        validate_public_origin("   ")


def test_validate_public_origin_rejects_no_port():
    with pytest.raises(ValueError, match="port"):
        validate_public_origin("http://127.0.0.1")


def test_validate_public_origin_rejects_invalid_port():
    with pytest.raises(ValueError, match="port"):
        validate_public_origin("http://127.0.0.1:not-a-port")


def test_validate_public_origin_rejects_path():
    with pytest.raises(ValueError, match="path"):
        validate_public_origin("http://127.0.0.1:8765/dashboard")


def test_validate_public_origin_rejects_query():
    with pytest.raises(ValueError, match="query"):
        validate_public_origin("http://127.0.0.1:8765?x=1")


def test_validate_public_origin_rejects_fragment():
    with pytest.raises(ValueError, match="fragment"):
        validate_public_origin("http://127.0.0.1:8765#frag")


def test_validate_public_origin_rejects_userinfo():
    with pytest.raises(ValueError, match="userinfo"):
        validate_public_origin("http://user:pass@127.0.0.1:8765")


def test_validate_public_origin_rejects_wrong_scheme():
    with pytest.raises(ValueError, match="scheme"):
        validate_public_origin("ftp://127.0.0.1:8765")
    with pytest.raises(ValueError, match="scheme"):
        validate_public_origin("ws://127.0.0.1:8765")


def test_validate_public_origin_rejects_out_of_range_port():
    """99999 is out of port range 1..65535."""
    with pytest.raises(ValueError, match="out of range"):
        validate_public_origin("http://127.0.0.1:99999")


def test_validate_public_origin_rejects_missing_hostname():
    with pytest.raises(ValueError, match="hostname"):
        validate_public_origin("http://:8765")


# ===== 5 startup-validation tests (§6.1, R13) =====


@pytest_asyncio.fixture
async def tmp_app(tmp_path, monkeypatch):
    """A fresh app whose DB is redirected to a tmp file.

    Returns (app_factory, transport_factory). Tests use
    `app = app_factory()` then `async with app.router.lifespan_context(app)`.
    Each call to app_factory creates a new app instance, but
    monkeypatch only restores at test teardown, so the Database
    class patch is process-wide for the duration of this fixture.
    """
    test_db = tmp_path / "origin_validation.db"
    orig_db_init = db_mod.Database.__init__

    def patched_db_init(self, db_path):
        orig_db_init(self, test_db)

    monkeypatch.setattr(db_mod.Database, "__init__", patched_db_init)

    def factory():
        return create_app()

    return factory


@pytest.mark.asyncio
async def test_public_origin_unset_prevents_startup(monkeypatch, tmp_app):
    """If HERMES_ORCH_PUBLIC_ORIGIN is unset (or empty), the lifespan
    refuses to start the server."""
    monkeypatch.delenv("HERMES_ORCH_PUBLIC_ORIGIN", raising=False)
    app = tmp_app()
    with pytest.raises(SystemExit) as exc_info:
        async with app.router.lifespan_context(app):
            pass  # should never reach here
    assert "public_origin" in str(exc_info.value).lower() or "FATAL" in str(exc_info.value)


@pytest.mark.asyncio
async def test_public_origin_invalid_port_prevents_startup(monkeypatch, tmp_app):
    """A public_origin with an unparseable port prevents startup."""
    monkeypatch.setenv("HERMES_ORCH_PUBLIC_ORIGIN", "http://127.0.0.1:not-a-port")
    app = tmp_app()
    with pytest.raises(SystemExit) as exc_info:
        async with app.router.lifespan_context(app):
            pass
    assert "FATAL" in str(exc_info.value) or "port" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_public_origin_has_path_prevents_startup(monkeypatch, tmp_app):
    """A public_origin with a path component prevents startup."""
    monkeypatch.setenv("HERMES_ORCH_PUBLIC_ORIGIN", "http://127.0.0.1:8765/dashboard")
    app = tmp_app()
    with pytest.raises(SystemExit) as exc_info:
        async with app.router.lifespan_context(app):
            pass
    assert "FATAL" in str(exc_info.value) or "path" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_public_origin_has_query_or_fragment_prevents_startup(monkeypatch, tmp_app):
    """A public_origin with a query string or fragment prevents startup."""
    for bad in (
        "http://127.0.0.1:8765?x=1",
        "http://127.0.0.1:8765#frag",
    ):
        monkeypatch.setenv("HERMES_ORCH_PUBLIC_ORIGIN", bad)
        app = tmp_app()
        with pytest.raises(SystemExit):
            async with app.router.lifespan_context(app):
                pass


@pytest.mark.asyncio
async def test_public_origin_invalid_scheme_prevents_startup(monkeypatch, tmp_app):
    """A public_origin with a non-http(s) scheme prevents startup."""
    for bad in (
        "ftp://127.0.0.1:8765",
        "ws://127.0.0.1:8765",
        "file://127.0.0.1:8765",
    ):
        monkeypatch.setenv("HERMES_ORCH_PUBLIC_ORIGIN", bad)
        app = tmp_app()
        with pytest.raises(SystemExit):
            async with app.router.lifespan_context(app):
                pass


# ===== Positive: a valid origin allows startup =====


@pytest.mark.asyncio
async def test_valid_public_origin_allows_startup(monkeypatch, tmp_app):
    """A valid bare origin with explicit port allows the lifespan to
    proceed past the validation step."""
    monkeypatch.setenv(
        "HERMES_ORCH_PUBLIC_ORIGIN", "http://127.0.0.1:8765"
    )
    app = tmp_app()
    # Should NOT raise SystemExit. We let the lifespan context manager
    # run far enough to confirm validation passed. We then exit early
    # to avoid spinning up the supervisor / scheduler for the rest
    # of the lifespan (which we don't need for this test).
    # The validation runs synchronously in the first half of the
    # lifespan; if it raises, we'd see SystemExit before reaching
    # the rest.
    try:
        # Use a short-lived ASGI lifespan: enter + immediate exit.
        # If validation fails, SystemExit is raised on enter.
        async with app.router.lifespan_context(app):
            # If we reach here, validation passed.
            assert app.state.public_origin == "http://127.0.0.1:8765"
    except SystemExit as e:
        pytest.fail(f"valid public_origin should not SystemExit: {e}")
