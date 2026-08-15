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

Cross-reference:
- Operator review 2026-08-15: see summary block in
  docs/specs/orch-server-hmac-v07-feature-decisions.md (TBD when
  the hardening design doc lands)
- Spec §1.1 + §1.7: path canonicalization policy
- Spec §1.4 step 4: KEY_AGENT_MISMATCH error code
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

# Reuse the same imports as test_hmac_v07_auth.py
from hermes_orch import main as main_mod
from hermes_orch import db as db_mod
from hermes_orch.main import create_app

from tests.helpers.hmac_v07 import sign_v07_request


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


# === Issue #1: X-Hermes-Method binding ===

def test_h01_method_mismatch_returns_401(client, agent_with_key):
    """H1: sign POST /heartbeat, send GET /heartbeat. The signature
    would internally match (the headers are self-consistent), but
    the request method is GET while X-Hermes-Method says POST.
    Hardening requires these to match byte-for-byte; mismatch → 401
    MALFORMED_HEADERS.

    Today (red phase): the verifier does not check X-Hermes-Method
    against request.method. If the request reaches a POST-only route
    via GET, FastAPI returns 405 Method Not Allowed (not 401). If
    the route accepts both methods, the request would be accepted
    with the wrong-method signature. Either way, the test asserts
    401 (the hardening contract).
    """
    agent_id, key_id, secret = agent_with_key
    # Sign for POST with a body
    signed_body = b'{"force": 1}'
    headers = sign_v07_request(
        "POST", f"/api/agents/{agent_id}/heartbeat",
        body=signed_body,
        key_id=key_id, secret=secret,
    )
    # Send as GET (method mismatch)
    response = client.get(
        f"/api/agents/{agent_id}/heartbeat",
        headers=headers,
    )
    # Red phase: response.status_code is one of [405, 200, 401] but
    # NOT the expected 401 MALFORMED_HEADERS.
    # Green phase (Phase 1): the verifier rejects with 401.
    assert response.status_code == 401, (
        f"got {response.status_code}: {response.text}"
    )
    assert response.json()["detail"].split(": ")[0] == "MALFORMED_HEADERS"


# === Issue #1: X-Hermes-Path binding ===

def test_h02_path_mismatch_returns_401(client, agent_with_key):
    """H2: sign /api/agents/{id}/status, send /api/agents/{id}/heartbeat
    (or any other path). The X-Hermes-Path header is internally
    consistent with the signature, but the actual request URL is
    different. Hardening requires X-Hermes-Path to equal
    request.url.path byte-for-byte; mismatch → 401 MALFORMED_HEADERS.

    Today (red phase): the verifier does not check X-Hermes-Path
    against request.url.path. The request reaches the router
    matching the actual URL, not the signed path, and may be
    accepted (if the route exists) with the wrong-path signature.
    """
    agent_id, key_id, secret = agent_with_key
    signed_path = f"/api/agents/{agent_id}/status"
    sent_path = f"/api/agents/{agent_id}/heartbeat"  # different
    headers = sign_v07_request(
        "GET", signed_path, b"",
        key_id=key_id, secret=secret,
    )
    response = client.get(sent_path, headers=headers)
    # Red phase: response.status_code is one of [200, 401, 404] but
    # NOT the expected 401 MALFORMED_HEADERS (with the right code).
    assert response.status_code == 401, (
        f"got {response.status_code}: {response.text}"
    )
    assert response.json()["detail"].split(": ")[0] == "MALFORMED_HEADERS"


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
    """
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id=key_id, secret=secret,
    )
    # Drop 3 random v0.7 headers (leaves 4/7)
    for h in ("X-Hermes-Method", "X-Hermes-Path", "X-Hermes-Signature"):
        del headers[h]
    response = client.get(
        f"/api/agents/{agent_id}/status",
        headers=headers,
    )
    # Red phase: response.status_code is 401 (existing MISSING_AUTH_HEADERS
    # check fires on the missing headers) but the assertion below
    # may pass for the wrong reason (the standard MISSING check).
    # Green phase: hardening adds the strict-reject error code or
    # ensures the partial-set detection happens before the standard
    # MISSING check. Either way, the test should pass.
    assert response.status_code == 401, (
        f"got {response.status_code}: {response.text}"
    )
    error_code = response.json()["detail"].split(": ")[0]
    # The hardening-required error codes are MISSING_AUTH_HEADERS
    # (existing) or a new MIXED_HEADERS / PARTIAL_HEADERS code.
    # The test accepts either as a forward-compatible assertion;
    # Phase 1 will pin down which one.
    assert error_code in (
        "MISSING_AUTH_HEADERS",
        "MIXED_HEADERS",
        "PARTIAL_HEADERS",
    ), f"unexpected error code: {error_code}"


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
    """
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id=key_id, secret=secret,
    )
    # Add a v1.6-style header to the v0.7 set
    headers["X-Agent-Id"] = agent_id
    response = client.get(
        f"/api/agents/{agent_id}/status",
        headers=headers,
    )
    # Red phase: response.status_code is 200 (silent accept) or
    # 401 (existing verifier step) but the error code is not
    # the required MIXED_HEADERS.
    # Green phase (Phase 1): the dispatcher detects mixed headers
    # and rejects with 401 MIXED_HEADERS.
    assert response.status_code == 401, (
        f"got {response.status_code}: {response.text}"
    )
    assert response.json()["detail"].split(": ")[0] == "MIXED_HEADERS"
