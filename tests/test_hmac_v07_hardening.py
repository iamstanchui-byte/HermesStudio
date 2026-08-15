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


# === Issue #2: Nonce atomic check+record (concurrent replay) ===

def test_h05_add_if_absent_is_atomic_under_concurrent_call():
    """H5: 50 threads call `add_if_absent(SAME_NONCE)` on the same
    InMemoryNonceStore concurrently. Exactly one call must return
    True (the first to record it); the other 49 must return False
    (the nonce was already present).

    This is a direct atomicity test of the new `add_if_absent`
    method. The HTTP-level equivalent (2 concurrent requests with
    the same nonce) is hard to make reliably racy in CPython
    because `is_seen` and `add` are both sync GIL-protected
    critical sections that the OS scheduler rarely interleaves
    between. The unit test for `add_if_absent` is deterministic:
    it asserts the contract that the verifier relies on
    (single atomic check+record).

    Red phase (2026-08-15): `add_if_absent` does not exist on
    InMemoryNonceStore yet — this test will fail with AttributeError
    on `store.add_if_absent`. Green phase (Phase 2 impl): the
    method is added with `threading.Lock` guarding a single
    `if nonce in _seen: return False; else: _seen[nonce] = ...;
    return True` block; the test passes deterministically.

    Future (Phase 7 multi-worker): the same test runs against
    a RedisNonceStore stub using `SET NX TTL` for true cross-
    process atomicity. The contract — "exactly one True for
    concurrent adds" — is the production gate.
    """
    import threading as _threading

    from hermes_orch.auth.nonce_store import InMemoryNonceStore

    store = InMemoryNonceStore(ttl_seconds=300)
    shared_nonce = "concurrent-test-nonce-001"

    results: list[bool] = []
    lock = _threading.Lock()

    def worker():
        r = store.add_if_absent(shared_nonce)
        with lock:
            results.append(r)

    threads = [
        _threading.Thread(target=worker) for _ in range(50)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    true_count = sum(1 for r in results if r is True)
    false_count = sum(1 for r in results if r is False)
    assert true_count == 1, (
        f"expected exactly 1 True (first to record), got {true_count}; "
        f"results: {results}"
    )
    assert false_count == 49, (
        f"expected exactly 49 False (already seen), got {false_count}"
    )
    # The recorded nonce should be exactly 1 entry
    assert len(store) == 1, f"expected 1 entry in store, got {len(store)}"


def test_h05b_add_if_absent_distinguishes_different_nonces():
    """H5b: `add_if_absent` with DIFFERENT nonces returns True for
    each (atomic per-nonce, not global lock). 50 distinct nonces
    across 50 threads all return True.
    """
    import threading as _threading

    from hermes_orch.auth.nonce_store import InMemoryNonceStore

    store = InMemoryNonceStore(ttl_seconds=300)

    results: list[bool] = []
    lock = _threading.Lock()

    def worker(i: int):
        r = store.add_if_absent(f"nonce-{i:04d}")
        with lock:
            results.append(r)

    threads = [
        _threading.Thread(target=worker, args=(i,)) for i in range(50)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    true_count = sum(1 for r in results if r is True)
    assert true_count == 50, (
        f"expected 50 True (all distinct nonces), got {true_count}"
    )
    assert len(store) == 50, (
        f"expected 50 entries in store, got {len(store)}"
    )


# === Issue #3: Enrollment v07 state machine ===

@pytest.mark.parametrize("preflight_status,expected_error_code", [
    ("verified", "ENROLLMENT_STATE_CONFLICT"),
    ("blocked", "ENROLLMENT_STATE_CONFLICT"),
    ("suspended", "ENROLLMENT_STATE_CONFLICT"),
    ("pending", "ENROLLMENT_STATE_CONFLICT"),
    ("bogus_status_xyz", "ENROLLMENT_STATE_CONFLICT"),
])
def test_h06_enrollment_rejects_non_verifying_status(
    client, agent_with_key, preflight_status, expected_error_code
):
    """H6 (parametrized): the v0.7 enrollment endpoint only
    transitions rows whose current status is `verifying`. Any
    other status — `verified`, `blocked`, `suspended`, the
    legacy `pending`, or any unknown / typo'd value — must
    return 409 with `ENROLLMENT_STATE_CONFLICT` (NOT 200, NOT
    silently overwrite).

    Today (red phase): the endpoint runs the UPDATE without
    a status guard, so the row's status flips to `verified`
    regardless of its pre-call value. The test asserts the
    new strict-reject contract: only `verifying` is the
    allowed start state.

    Green phase (Phase 3 impl): the endpoint's UPDATE gains
    `AND status = 'verifying'` and the row count check
    surfaces 409 if 0 rows updated.

    Cross-reference: spec §1.10 "Enrollment v07 state machine".
    """
    import json as _json
    import sqlite3 as _sqlite3

    agent_id, key_id, secret = agent_with_key
    db_path = client.app.state.db.db_path

    # Pre-set the agent's status to the parametrized value
    conn = _sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE agents SET status = ? WHERE id = ?",
            (preflight_status, agent_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Sign + send the v0.7 enrollment request
    body_dict = {
        "agent_name": "test-vm",
        "hostname": "test-host",
        "os_type": "windows-11",
    }
    body_bytes = _json.dumps(body_dict).encode("utf-8")
    headers = sign_v07_request(
        "POST", "/api/enrollment/v07",
        body=body_bytes,
        key_id=key_id, secret=secret,
    )
    headers["Content-Type"] = "application/json"
    response = client.post(
        "/api/enrollment/v07",
        headers=headers,
        content=body_bytes,
    )
    assert response.status_code == 409, (
        f"preflight status={preflight_status!r}: expected 409, "
        f"got {response.status_code}: {response.text}"
    )
    assert (
        response.json()["detail"].split(": ")[0] == expected_error_code
    ), f"got detail: {response.text}"


def test_h07_enrollment_happy_path_requires_verifying_start(
    client, agent_with_key
):
    """H7: when the row's status IS `verifying` (the only allowed
    start state), the v0.7 enrollment endpoint transitions it
    to `verified` and returns 200. This is the happy path that
    the pre-existing test_v07_enrollment_endpoint_marks_agent_verified
    already covers; this hardening test re-asserts it explicitly
    with the state-machine context (preflight = 'verifying').

    Together, H6 + H7 prove the strict-reject contract: only
    `verifying` is allowed; any other status returns 409.
    """
    import json as _json
    import sqlite3 as _sqlite3

    agent_id, key_id, secret = agent_with_key
    db_path = client.app.state.db.db_path

    # Pre-set the agent's status to 'verifying' (the only allowed
    # start state for enrollment)
    conn = _sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE agents SET status = 'verifying' WHERE id = ?",
            (agent_id,),
        )
        conn.commit()
    finally:
        conn.close()

    body_dict = {
        "agent_name": "test-vm",
        "hostname": "test-host",
        "os_type": "windows-11",
    }
    body_bytes = _json.dumps(body_dict).encode("utf-8")
    headers = sign_v07_request(
        "POST", "/api/enrollment/v07",
        body=body_bytes,
        key_id=key_id, secret=secret,
    )
    headers["Content-Type"] = "application/json"
    response = client.post(
        "/api/enrollment/v07",
        headers=headers,
        content=body_bytes,
    )
    assert response.status_code == 200, (
        f"expected 200 from verifying start state, got "
        f"{response.status_code}: {response.text}"
    )
    body = response.json()
    assert body["status"] == "verified"
    assert body["agent_id"] == agent_id


# === Issue #8: AgentStatus enum + polling contract ===

@pytest.mark.parametrize("valid_status", [
    "verifying",
    "verified",
    "blocked",
    "suspended",
])
def test_h08_status_endpoint_returns_canonical_enum(
    client, agent_with_key, valid_status
):
    """H8 (parametrized): the status endpoint returns the
    canonical 4-value enum. For each of the 4 valid values
    (verifying / verified / blocked / suspended), the endpoint
    returns 200 with the status field unchanged. This proves
    the endpoint does not silently coerce unknown values to
    'verifying' or 'unknown' (which is the pre-Phase-6 behavior
    of `row.get('status') or 'unknown'`).

    Today (red phase): the 4 valid statuses already return 200
    (the endpoint just echoes whatever is in the DB). The test
    documents the contract; Phase 6 adds explicit validation.
    """
    import sqlite3 as _sqlite3

    agent_id, key_id, secret = agent_with_key
    db_path = client.app.state.db.db_path

    conn = _sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE agents SET status = ? WHERE id = ?",
            (valid_status, agent_id),
        )
        conn.commit()
    finally:
        conn.close()

    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id=key_id, secret=secret,
    )
    response = client.get(
        f"/api/agents/{agent_id}/status", headers=headers,
    )
    assert response.status_code == 200, (
        f"status={valid_status!r}: expected 200, got "
        f"{response.status_code}: {response.text}"
    )
    body = response.json()
    assert body["status"] == valid_status, (
        f"expected echoed status={valid_status!r}, got {body!r}"
    )


def test_h08b_status_endpoint_rejects_invalid_enum_value(
    client, agent_with_key
):
    """H8b: a typo or non-enum value in the DB (e.g. 'verfid',
    'PENDING', '', or any other string) makes the status
    endpoint return **500 INVALID_AGENT_STATUS** rather than
    200 with a coerced value. The 500 is fail-closed: the
    bootstrapper must not treat unknown statuses as 'verified'.

    Today (red phase): the endpoint's
    `row.get('status') or 'unknown'` returns the typo'd value
    (or the literal string 'unknown' for NULL). The test
    asserts the 500 fail-closed contract.
    """
    import sqlite3 as _sqlite3

    agent_id, key_id, secret = agent_with_key
    db_path = client.app.state.db.db_path

    # Inject a typo into the status field (bypassing the spec's
    # enum; this is the kind of bug a manual SQL update might
    # leave behind)
    conn = _sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE agents SET status = 'verfid' WHERE id = ?",
            (agent_id,),
        )
        conn.commit()
    finally:
        conn.close()

    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id=key_id, secret=secret,
    )
    response = client.get(
        f"/api/agents/{agent_id}/status", headers=headers,
    )
    assert response.status_code == 500, (
        f"typo'd status='verfid' should fail-closed 500, got "
        f"{response.status_code}: {response.text}"
    )
    assert response.json()["detail"].split(": ")[0] == "INVALID_AGENT_STATUS"


# === Issue #6: Unified error JSON contract ===

def test_h09_unified_error_contract_wire_format(client, agent_with_key):
    """H9: a 4xx response from the v0.7 endpoints uses the unified
    wire format per spec §1.12:
        {"error": "CODE", "message": "...", "request_id": "uuid"}
    The legacy `detail` field is preserved for backward compat.

    Today (red phase for the unified contract): FastAPI's default
    HTTPException handler returns `{"detail": "..."}` only —
    no `error`, no `message`, no `request_id`. The test asserts
    the new fields are present and correct.
    """
    import uuid as _uuid

    agent_id, key_id, secret = agent_with_key
    # Drop a required header to trigger MISSING_AUTH_HEADERS
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id=key_id, secret=secret,
    )
    del headers["X-Hermes-Method"]
    response = client.get(
        f"/api/agents/{agent_id}/status", headers=headers,
    )
    assert response.status_code == 401
    body = response.json()
    # Primary contract
    assert "error" in body, f"missing 'error' field: {body!r}"
    assert "message" in body, f"missing 'message' field: {body!r}"
    assert "request_id" in body, f"missing 'request_id' field: {body!r}"
    assert body["error"] == "MISSING_AUTH_HEADERS"
    assert isinstance(body["message"], str) and body["message"]
    # request_id must be a valid UUID4
    _uuid.UUID(body["request_id"], version=4)
    # Legacy backward compat: detail field is also present
    assert "detail" in body
    assert body["detail"] == f"{body['error']}: {body['message']}"


def test_h09b_request_id_header_set_on_responses(client, agent_with_key):
    """H9b: every response (200 + 4xx + 5xx) carries the
    `X-Request-Id` response header matching the body's
    `request_id` field. Operators can grep server logs by
    request_id to correlate with the response.
    """
    agent_id, key_id, secret = agent_with_key
    # 200 path
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id=key_id, secret=secret,
    )
    response_ok = client.get(
        f"/api/agents/{agent_id}/status", headers=headers,
    )
    assert response_ok.status_code == 200
    rid_ok = response_ok.headers.get("X-Request-Id")
    assert rid_ok, f"200 response missing X-Request-Id: {dict(response_ok.headers)}"

    # 401 path
    headers_bad = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id=key_id, secret=secret,
    )
    del headers_bad["X-Hermes-Signature"]
    response_bad = client.get(
        f"/api/agents/{agent_id}/status", headers=headers_bad,
    )
    assert response_bad.status_code == 401
    rid_bad = response_bad.headers.get("X-Request-Id")
    assert rid_bad, f"401 response missing X-Request-Id: {dict(response_bad.headers)}"
    # Two different requests should have two different request_ids
    assert rid_ok != rid_bad, (
        f"request_id should be unique per request, got "
        f"both={rid_ok!r}"
    )


def test_h09c_honors_inbound_request_id_header(client, agent_with_key):
    """H9c: if the client sends an `X-Request-Id` request header
    (e.g. the bootstrapper chains it through to correlate with
    its own logs), the server honors it and returns the same
    value in the response header + the unified body. If the
    header is absent, the server generates a new UUID4.

    Uses the 401 path (drop X-Hermes-Method) so the response
    body is the unified error shape (which includes
    request_id). The 200 success path's body is data fields
    only per spec §1.12; the X-Request-Id header is still
    present on 200 responses (covered by H9b).
    """
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id=key_id, secret=secret,
    )
    del headers["X-Hermes-Method"]  # trigger 401 MISSING_AUTH_HEADERS
    inbound_rid = "test-correlation-id-12345"
    headers["X-Request-Id"] = inbound_rid
    response = client.get(
        f"/api/agents/{agent_id}/status", headers=headers,
    )
    assert response.status_code == 401
    assert response.headers["X-Request-Id"] == inbound_rid
    assert response.json()["request_id"] == inbound_rid


# === Issue #7: HERMES_HMAC_ACCEPT_V06 flag + deprecation ===

def test_h10_v06_accepted_by_default(client, agent_with_key, monkeypatch):
    """H10: with `HERMES_HMAC_ACCEPT_V06` unset (default `true`),
    a v0.6 (X-Agent-Id) request to a dual-format route is
    accepted by the dispatcher. The default behavior preserves
    the pre-Phase-4 contract: v0.6 + v0.7 both work on the
    heartbeat + GET /{id} routes.

    Today (red phase for the flag specifically): the dispatcher
    has no flag at all — v0.6 always works. The test
    documents that the default behavior is preserved when the
    flag is unset.
    """
    import hashlib as _h
    import sqlite3 as _sqlite3
    import time as _time

    agent_id, key_id, secret = agent_with_key
    # Build a v0.6 request directly (X-Agent-Id + X-Timestamp +
    # X-Signature). The test fixture already populates
    # hmac_secret in the DB.
    timestamp = str(int(_time.time()))
    path = f"/api/agents/{agent_id}/heartbeat"
    body = b'{"force": 1}'
    body_sha256 = _h.sha256(body).hexdigest()
    # v0.6 secret is the hex string of the v0.7 secret bytes
    # (per the test fixture convention)
    secret_hex = secret.hex()
    string_to_sign = f"POST\n{path}\n{body_sha256}\n{timestamp}"
    import hmac as _hmac
    sig = _hmac.new(
        secret_hex.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        _h.sha256,
    ).hexdigest()

    # Default flag (unset) → v0.6 works
    monkeypatch.delenv("HERMES_HMAC_ACCEPT_V06", raising=False)
    response = client.post(
        path,
        headers={
            "X-Agent-Id": agent_id,
            "X-Timestamp": timestamp,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
        content=body,
    )
    assert response.status_code == 200, (
        f"default flag=true: v0.6 should work, got "
        f"{response.status_code}: {response.text}"
    )


def test_h11_v06_rejected_when_flag_false(
    client, agent_with_key, monkeypatch
):
    """H11: with `HERMES_HMAC_ACCEPT_V06=false`, a v0.6
    (X-Agent-Id) request returns 401 `V0_6_DEPRECATED`. The
    dispatcher checks the flag BEFORE routing to the v0.6
    verifier. v0.7 requests (with X-Hermes-Method) are NOT
    affected by the flag — they always work.

    Today (red phase): the dispatcher has no flag at all;
    v0.6 always works. The test asserts the new strict-reject
    behavior when the operator flips the flag to false.
    """
    import hashlib as _h
    import sqlite3 as _sqlite3
    import time as _time
    import hmac as _hmac

    agent_id, key_id, secret = agent_with_key
    timestamp = str(int(_time.time()))
    path = f"/api/agents/{agent_id}/heartbeat"
    body = b'{"force": 1}'
    body_sha256 = _h.sha256(body).hexdigest()
    secret_hex = secret.hex()
    string_to_sign = f"POST\n{path}\n{body_sha256}\n{timestamp}"
    sig = _hmac.new(
        secret_hex.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        _h.sha256,
    ).hexdigest()

    monkeypatch.setenv("HERMES_HMAC_ACCEPT_V06", "false")
    response = client.post(
        path,
        headers={
            "X-Agent-Id": agent_id,
            "X-Timestamp": timestamp,
            "X-Signature": sig,
            "Content-Type": "application/json",
        },
        content=body,
    )
    assert response.status_code == 401, (
        f"flag=false: v0.6 should be rejected, got "
        f"{response.status_code}: {response.text}"
    )
    assert response.json()["detail"].split(": ")[0] == "V0_6_DEPRECATED"


def test_h12_v07_unaffected_by_v06_flag(
    client, agent_with_key, monkeypatch
):
    """H12: v0.7 requests are NOT affected by the
    `HERMES_HMAC_ACCEPT_V06` flag. Whether the flag is
    `true` or `false`, v0.7 requests work normally. The
    flag controls only the v0.6 path.

    Today (red phase): the dispatcher has no flag at all,
    so v0.7 always works. The test documents that the new
    flag does not regress v0.7.
    """
    agent_id, key_id, secret = agent_with_key
    # Sign a v0.7 GET /status request
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id=key_id, secret=secret,
    )

    # With flag=false, v0.7 should still work
    monkeypatch.setenv("HERMES_HMAC_ACCEPT_V06", "false")
    response = client.get(
        f"/api/agents/{agent_id}/status", headers=headers,
    )
    assert response.status_code == 200, (
        f"flag=false: v0.7 should NOT be affected, got "
        f"{response.status_code}: {response.text}"
    )
    body = response.json()
    assert body["status"] in ("verifying", "verified", "blocked", "suspended")
