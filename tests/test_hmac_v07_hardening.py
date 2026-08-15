"""v0.7 §1.4 HMAC hardening tests (Phase 1 of security/v07-hardening).

Companion to test_hmac_v07_auth.py. Covers the 4 hardening issues
identified during the 2026-08-15 operator security review:

- Issue #1: X-Hermes-Method + X-Hermes-Path MUST equal the actual
  request method + URL path. Currently the verifier only checks the
  X-Hermes-* headers as a self-contained unit; an attacker could
  sign `POST /api/agents/A/heartbeat` but send `GET /api/agents/B/status`
  with those headers. The signature would still match (the headers
  are internally consistent), but the request would be bound to a
  different agent + endpoint. Hardening forces the verifier to
  compare the X-Hermes-Method against `request.method` and the
  X-Hermes-Path against `request.url.path` byte-for-byte.

- Issue #4: Reject partial v0.7 header sets. The current dispatcher
  (auth/dispatch.py) routes by header presence. If a client sends
  only 4 of 7 v0.7 headers, the dispatcher may either fall through
  to v1.6 (wrong) or fail-open. Hardening forces strict rejection
  of any partial v0.7 set (i.e. ANY X-Hermes-* header present
  requires ALL 7).

- Issue #4b: Reject mixed v0.7 + v1.6 header sets. Same as #4 but
  for cross-protocol mixing. A request with X-Hermes-Method AND
  X-Agent-Id is ambiguous and must be rejected.

- Issue #5: Canonical-path single source of truth. The 4 places that
  define "canonical" (spec §1.1, spec §1.7, verifier §3b,
  _HMAC_PATH_PATTERNS regex) must all agree. Hardening consolidates
  the rule into a single function and uses it everywhere.

These are TDD RED phase tests (added 2026-08-15). They all FAIL
because the verifier does not yet enforce method/path binding.
Phase 1 implementation lands the binding check; these tests turn
green at the end of Phase 1.

H1 + H2 are direct unit tests (calling the verifier function with
a mocked Request object) rather than HTTP integration tests,
because the production endpoints are method-restricted (e.g.
`/heartbeat` is POST-only, `/status` is GET-only), so HTTP
mismatch tests would hit router-level 405 before reaching the
verifier. The unit test approach exercises the binding check
directly.

H3 + H4 are HTTP integration tests because the dispatcher runs
BEFORE the router (FastAPI dependency injection order), so the
strict-reject check happens before any method/path restriction.

Cross-reference:
- Operator review 2026-08-15: see summary block in
  docs/specs/orch-server-hmac-v07-feature-decisions.md (TBD when
  the hardening design doc lands)
- Spec §1.1 + §1.7 + §1.8 + §1.9: header canonicalization +
  binding + mixed/partial rejection
- Spec §1.4 step 4: KEY_AGENT_MISMATCH error code
"""
from __future__ import annotations

import os
import time
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Reuse the same imports as test_hmac_v07_auth.py
from hermes_orch import main as main_mod
from hermes_orch import db as db_mod
from hermes_orch.auth.hmac_v07 import (
    require_hmac_auth_v07,
)
from hermes_orch.main import create_app

from tests.helpers.hmac_v07 import sign_v07_request


# === Unit-test helper: build a mock Request for the v0.7 verifier ===

def _make_mock_request(
    method: str,
    path: str,
    body: bytes,
    db,
    nonce_store=None,
    headers: dict | None = None,
):
    """Build a MagicMock that quacks like a FastAPI Request enough to
    call require_hmac_auth_v07. The mock exposes:
      - request.method (str)
      - request.url.path (str)
      - request.url.query (str)
      - request.body() -> bytes (coroutine)
      - request.app.state.db (the real Database)
      - request.app.state.v07_nonce_store (optional InMemoryNonceStore)
      - request.headers (used by some validators; not strictly required
        by the v0.7 verifier since headers come from FastAPI Header() deps)
    """
    import asyncio

    req = MagicMock()
    req.method = method
    req.url.path = path
    req.url.query = ""
    # request.body() is async; use a coroutine that returns the body
    async def _body():
        return body
    req.body = _body
    req.app.state.db = db
    if nonce_store is not None:
        req.app.state.v07_nonce_store = nonce_store
    if headers is not None:
        # `headers` may be inspected by some middleware but the
        # v0.7 verifier reads its inputs from FastAPI Header() deps,
        # so this is just a defensive stub.
        req.headers = headers
    return req


# === Fixtures (mirrored from test_hmac_v07_auth.py) ===

@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI TestClient with the orchestrator's create_app() — using
    a tmp DB so the test is isolated. Same pattern as the v0.7 auth
    tests in test_hmac_v07_auth.py.
    """
    test_db = tmp_path / "test_hmac_v07_hardening.db"
    orig_db_init = db_mod.Database.__init__

    def patched_db_init(self, db_path):
        orig_db_init(self, test_db)

    monkeypatch.setattr(db_mod.Database, "__init__", patched_db_init)
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def agent_with_key(client, tmp_path, monkeypatch):
    """Yield (agent_id, key_id, secret_bytes) for a fresh test agent
    inserted into the tmp DB. Mirrors the fixture in
    test_hmac_v07_auth.py.
    """
    import sqlite3 as _sqlite3
    import time as _time
    import hashlib as _h

    agent_id = f"win-test-{uuid.uuid4().hex[:8]}"
    key_id = f"key-{agent_id}"
    secret = os.urandom(32)
    secret_str = secret.hex()
    secret_hash = _h.sha256(secret_str.encode("utf-8")).hexdigest()
    now = _time.strftime("%Y-%m-%dT%H:%M:%S")

    db_path = client.app.state.db.db_path
    conn = _sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "DELETE FROM agent_profiles WHERE agent_id = ?", (agent_id,)
        )
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        conn.execute(
            "INSERT INTO agents (id, secret_hash, hmac_secret, "
            "hmac_key_id, status, created_at) "
            "VALUES (?, ?, ?, ?, 'verified', ?)",
            (agent_id, secret_hash, secret_str, key_id, now),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        yield (agent_id, key_id, secret)
    finally:
        conn = _sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "DELETE FROM agent_profiles WHERE agent_id = ?",
                (agent_id,),
            )
            conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            conn.commit()
        finally:
            conn.close()


# === Issue #1: X-Hermes-Method binding (unit test) ===

@pytest.mark.asyncio
async def test_h01_method_mismatch_returns_401(client, agent_with_key):
    """H1 (unit test): sign POST /heartbeat, but the mock Request
    reports method=GET. The signature would internally match (the
    headers are self-consistent), but the request method is GET
    while X-Hermes-Method says POST. Hardening requires these to
    match; mismatch → 401 MALFORMED_HEADERS.

    This is a unit test (not HTTP integration) because the
    production endpoints are method-restricted: an HTTP-level
    test with method mismatch would hit router-level 405 before
    reaching the verifier. The unit test exercises the binding
    check directly via the require_hmac_auth_v07 dependency.

    Today (red phase): the verifier does not check X-Hermes-Method
    against request.method. The unit test would either:
      - Return the agent_id (200) if all other checks pass — the
        signature matches because X-Hermes-Method is internally
        consistent with the other headers.
      - Or fail on a different check (e.g. KEY_AGENT_MISMATCH).
    Green phase (Phase 1): the verifier rejects with 401
    MALFORMED_HEADERS.
    """
    agent_id, key_id, secret = agent_with_key
    # Sign for POST with a body
    signed_body = b'{"force": 1}'
    signed_headers = sign_v07_request(
        "POST", f"/api/agents/{agent_id}/heartbeat",
        body=signed_body,
        key_id=key_id, secret=secret,
    )
    # Build a mock Request that reports method=GET (mismatch)
    mock_req = _make_mock_request(
        method="GET",  # mismatch with X-Hermes-Method=POST
        path=f"/api/agents/{agent_id}/heartbeat",  # path matches
        body=signed_body,
        db=client.app.state.db,
    )

    with pytest.raises(HTTPException) as exc_info:
        await require_hmac_auth_v07(
            request=mock_req,
            x_hermes_method=signed_headers["X-Hermes-Method"],
            x_hermes_path=signed_headers["X-Hermes-Path"],
            x_hermes_body_sha256=signed_headers["X-Hermes-Body-SHA256"],
            x_hermes_key_id=signed_headers["X-Hermes-Key-Id"],
            x_hermes_timestamp=signed_headers["X-Hermes-Timestamp"],
            x_hermes_nonce=signed_headers["X-Hermes-Nonce"],
            x_hermes_signature=signed_headers["X-Hermes-Signature"],
        )
    assert exc_info.value.status_code == 401
    assert "MALFORMED_HEADERS" in str(exc_info.value.detail)


# === Issue #1: X-Hermes-Path binding (unit test) ===

@pytest.mark.asyncio
async def test_h02_path_mismatch_returns_401(client, agent_with_key):
    """H2 (unit test): sign /status, but the mock Request reports
    url.path=/heartbeat. The X-Hermes-Path header is internally
    consistent with the signature, but the actual request URL is
    different. Hardening requires X-Hermes-Path to equal
    request.url.path byte-for-byte; mismatch → 401 MALFORMED_HEADERS.

    Same as H1: this is a unit test because HTTP-level path
    mismatch would hit router-level 404 (or 405) before reaching
    the verifier. The unit test exercises the binding check
    directly.
    """
    agent_id, key_id, secret = agent_with_key
    signed_path = f"/api/agents/{agent_id}/status"
    sent_path = f"/api/agents/{agent_id}/heartbeat"  # different
    headers = sign_v07_request(
        "GET", signed_path, b"",
        key_id=key_id, secret=secret,
    )
    # Build a mock Request that reports path=/heartbeat (mismatch)
    mock_req = _make_mock_request(
        method="GET",  # method matches (X-Hermes-Method=GET)
        path=sent_path,  # mismatch with X-Hermes-Path=/status
        body=b"",
        db=client.app.state.db,
    )

    with pytest.raises(HTTPException) as exc_info:
        await require_hmac_auth_v07(
            request=mock_req,
            x_hermes_method=headers["X-Hermes-Method"],
            x_hermes_path=headers["X-Hermes-Path"],
            x_hermes_body_sha256=headers["X-Hermes-Body-SHA256"],
            x_hermes_key_id=headers["X-Hermes-Key-Id"],
            x_hermes_timestamp=headers["X-Hermes-Timestamp"],
            x_hermes_nonce=headers["X-Hermes-Nonce"],
            x_hermes_signature=headers["X-Hermes-Signature"],
        )
    assert exc_info.value.status_code == 401
    assert "MALFORMED_HEADERS" in str(exc_info.value.detail)


# === Issue #4: partial v0.7 header set ===

def test_h03_partial_v07_header_set_returns_401(client, agent_with_key):
    """H3: 4/7 v0.7 headers present (some missing). The current
    dispatcher (auth/dispatch.py) routes by ANY X-Hermes-* header
    presence, which can fall through to v1.6 (wrong) or fail-open.
    Hardening requires ALL 7 v0.7 headers to be present if ANY
    v0.7 header is present. Missing 3 headers → 401 with strict
    error code (NOT 200 with silent v1.6 fallback).

    Today (red phase): the missing headers trigger the standard
    MISSING_AUTH_HEADERS check, but the dispatcher still routes
    to v0.7 (not v1.6) because some X-Hermes-* headers are there.
    The test asserts 401 with the hardening-required error code.

    Uses POST /api/agents/{id}/heartbeat (the dispatcher-protected
    route) because the v0.7-only /status endpoint bypasses the
    dispatcher entirely and would never surface MIXED_HEADERS.
    """
    agent_id, key_id, secret = agent_with_key
    signed_body = b'{"force": 1}'
    headers = sign_v07_request(
        "POST", f"/api/agents/{agent_id}/heartbeat",
        body=signed_body,
        key_id=key_id, secret=secret,
    )
    # Drop 3 random v0.7 headers (leaves 4/7)
    for h in ("X-Hermes-Method", "X-Hermes-Path", "X-Hermes-Signature"):
        del headers[h]
    response = client.post(
        f"/api/agents/{agent_id}/heartbeat",
        headers=headers,
        content=signed_body,
    )
    assert response.status_code == 401, (
        f"got {response.status_code}: {response.text}"
    )
    # Phase 1 implementation: the dispatcher surfaces a uniform
    # `MIXED_HEADERS` code for any non-strict header set (partial
    # v0.7, partial v0.6, mixed v0.6+v0.7). This replaces the
    # previous behavior of falling through to the v0.7 verifier
    # which would have returned MISSING_AUTH_HEADERS on the
    # missing headers.
    assert response.json()["detail"].split(": ")[0] == "MIXED_HEADERS", (
        f"expected MIXED_HEADERS but got: {response.text}"
    )


# === Issue #4b: mixed v0.7 + v1.6 header set ===

def test_h04_mixed_v07_v06_headers_returns_401(client, agent_with_key):
    """H4: All 7 v0.7 headers + v1.6 X-Agent-Id header. The current
    dispatcher routes by X-Hermes-Method presence (v0.7), so the
    X-Agent-Id is silently ignored. Hardening requires STRICT
    rejection: any v1.6-style header present alongside v0.7
    headers → 401 MIXED_HEADERS (no fallthrough, no silent accept).

    Today (red phase): the v0.7 path is taken, the X-Agent-Id is
    ignored, and the request may be accepted (200) or fail at a
    later check. The test asserts 401 with the strict MIXED_HEADERS
    code.

    Uses POST /api/agents/{id}/heartbeat (the dispatcher-protected
    route) because the v0.7-only /status endpoint bypasses the
    dispatcher entirely and would never surface MIXED_HEADERS.
    """
    agent_id, key_id, secret = agent_with_key
    signed_body = b'{"force": 1}'
    headers = sign_v07_request(
        "POST", f"/api/agents/{agent_id}/heartbeat",
        body=signed_body,
        key_id=key_id, secret=secret,
    )
    # Add a v1.6-style header to the v0.7 set
    headers["X-Agent-Id"] = agent_id
    response = client.post(
        f"/api/agents/{agent_id}/heartbeat",
        headers=headers,
        content=signed_body,
    )
    assert response.status_code == 401, (
        f"got {response.status_code}: {response.text}"
    )
    assert response.json()["detail"].split(": ")[0] == "MIXED_HEADERS"
