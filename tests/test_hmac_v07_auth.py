"""Tests for v0.7 §1.4 bound-metadata HMAC verification (DRAFT 2026-08-13,
TDD RED PHASE STARTED 2026-08-15).

Maps the 16 acceptance test cases (T1-T16) from
docs/specs/orch-server-hmac-v0.7-alignment.md §6 to runnable pytest
code, plus the 2 dual-format test cases (T13, T14) that exercise the
Option B migration path. The 2 dual-format tests live in
tests/test_hmac_v06_compat.py.

TDD red phase (2026-08-15): fixtures are now real (use create_app()
via TestClient + tmp DB per the test_endpoint_auth.py pattern).
Tests still FAIL because the v0.7 verifier and the
GET /api/agents/{id}/status endpoint don't exist yet. That is
expected and correct: per the TDD red-green-refactor sequence in
docs/specs/orch-server-hmac-v0.7-impl-plan.md §4, step 2 is the
red phase. Implementation lands in step 4 (hmac_v07.py), step 6
(status endpoint), step 7 (dual-format), step 8 (enrollment v07).

Cross-reference: the sign_v07_request helper in
tests/helpers/hmac_v07.py MUST stay in sync with the bootstrapper's
Wait-ForEnrollment function at
installer/bootstrapper/install-orch-client.ps1 (line ~285). Both
implementations compute the same canonical input + HMAC-SHA256
signature; if they diverge, the bootstrapper cannot complete
enrollment.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Optional

import pytest
from fastapi.testclient import TestClient

# Real imports (per test_endpoint_auth.py pattern)
from hermes_orch import main as main_mod
from hermes_orch import db as db_mod
from hermes_orch.main import create_app

from tests.helpers.hmac_v07 import sign_v07_request
from tests.helpers.nonce_store import InMemoryNonceStore


# === Fixtures ===

@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI TestClient with the orchestrator's create_app() — using
    a tmp DB so the test is isolated. The autouse
    `set_test_public_origin` fixture in tests/conftest.py sets
    HERMES_ORCH_PUBLIC_ORIGIN so the lifespan doesn't fail-closed at
    startup.

    For the TDD red phase (step 2): the v0.7 routes don't exist yet,
    so tests that hit them will get 404. That's the red phase
    behavior we want — the test fails because the implementation
    is missing, and the failure message points to the missing
    route. Step 6 (impl plan §4) adds the v0.7 routes and turns
    these failures green.
    """
    test_db = tmp_path / "test_hmac_v07.db"
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
    inserted into the tmp DB.

    Step 4 + 5 update (2026-08-15): now does a real DB insert. The
    agent row has BOTH the v1.6 hmac_secret (string) AND the v0.7
    hmac_key_id (UNIQUE). The `client` fixture is required so the
    monkeypatched Database points at the test DB.
    """
    import sqlite3 as _sqlite3
    import time as _time
    import hashlib as _h

    agent_id = f"win-test-{uuid.uuid4().hex[:8]}"
    key_id = f"key-{agent_id}"
    secret = os.urandom(32)
    secret_str = secret.hex()  # v0.7 helper encodes bytes as hex string
    secret_hash = _h.sha256(secret_str.encode("utf-8")).hexdigest()
    now = _time.strftime("%Y-%m-%dT%H:%M:%S")

    # Get the tmp DB path from the Database instance (which has
    # been monkeypatched to use the tmp path by the client fixture).
    db_path = client.app.state.db.db_path
    conn = _sqlite3.connect(str(db_path))
    try:
        # Idempotent: delete + insert (matches the v1.6
        # register_test_agent pattern in tests/_hmac_util.py).
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
        # Cleanup: remove the test agent row
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


@pytest.fixture
def nonce_store():
    """InMemoryNonceStore test double (impl plan §1.3).

    Used by the nonce-replay test (T10).
    """
    return InMemoryNonceStore(ttl_seconds=300)


# === T1 — Happy path ===

def test_v07_happy_path_returns_200(client, agent_with_key):
    """T1: bootstrapper signs a GET /api/agents/{id}/status with
    valid headers; expect 200 + {"status": "verified"}.
    """
    agent_id, key_id, secret = agent_with_key
    # Placeholder: set up the test agent with status=verified
    # setup_test_agent(client, agent_id, key_id, secret, status="verified")

    headers = sign_v07_request(
        method="GET",
        path=f"/api/agents/{agent_id}/status",
        body=b"",
        key_id=key_id,
        secret=secret,
    )
    response = client.get(f"/api/agents/{agent_id}/status", headers=headers)
    if response.status_code != 200:
        print(f"\n!!! T1 FAILED: status={response.status_code}, body={response.text}")
        for k, v in headers.items():
            print(f"  HEADER {k}: {v[:80]}")
    assert response.status_code == 200, f"got {response.status_code}: {response.text}"
    # The route returns {agent_id, status, last_heartbeat_at};
    # the test only checks the status field (the others are
    # implementation detail and may grow over time).
    body = response.json()
    assert body.get("status") == "verified", f"body={body}"


# === T2 — Missing X-Hermes-Method ===

def test_v07_missing_method_header_returns_401(client, agent_with_key):
    """T2: drop the X-Hermes-Method header; expect 401 MISSING_AUTH_HEADERS."""
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"", key_id, secret,
    )
    del headers["X-Hermes-Method"]
    response = client.get(f"/api/agents/{agent_id}/status", headers=headers)
    if response.status_code != 401 or "error" not in response.json():
        print(f"\n!!! T2: status={response.status_code}, body={response.text}")
    assert response.status_code == 401, f"got {response.status_code}: {response.text}"
    assert response.json()["detail"].split(": ")[0] == "MISSING_AUTH_HEADERS"


# === T3 — Missing X-Hermes-Signature ===

def test_v07_missing_signature_header_returns_401(client, agent_with_key):
    """T3: drop the X-Hermes-Signature header; expect 401 MISSING_AUTH_HEADERS."""
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"", key_id, secret,
    )
    del headers["X-Hermes-Signature"]
    response = client.get(f"/api/agents/{agent_id}/status", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"].split(": ")[0] == "MISSING_AUTH_HEADERS"


# === T4 — Timestamp 600s in the past ===

def test_v07_old_timestamp_returns_401(client, agent_with_key):
    """T4: timestamp 600s in the past; expect 401 TIMESTAMP_OUT_OF_WINDOW."""
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id, secret,
        timestamp=int(time.time()) - 600,
    )
    response = client.get(f"/api/agents/{agent_id}/status", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"].split(": ")[0] == "TIMESTAMP_OUT_OF_WINDOW"


# === T5 — Timestamp 600s in the future ===

def test_v07_future_timestamp_returns_401(client, agent_with_key):
    """T5: timestamp 600s in the future; expect 401 TIMESTAMP_OUT_OF_WINDOW."""
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id, secret,
        timestamp=int(time.time()) + 600,
    )
    response = client.get(f"/api/agents/{agent_id}/status", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"].split(": ")[0] == "TIMESTAMP_OUT_OF_WINDOW"


# === T6 — Unknown X-Hermes-Key-Id ===

def test_v07_unknown_key_id_returns_401(client):
    """T6: X-Hermes-Key-Id does not exist in the agents table;
    expect 401 UNKNOWN_KEY_ID. The secret bytes are dummy; the
    lookup fails before signature verification.
    """
    headers = sign_v07_request(
        "GET", "/api/agents/win-test-1/status", b"",
        key_id="key-does-not-exist",
        secret=b"doesn't-matter",
    )
    response = client.get("/api/agents/win-test-1/status", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"].split(": ")[0] == "UNKNOWN_KEY_ID"


# === T7 — Key-Id binds to a different agent ===

def test_v07_key_agent_mismatch_returns_403(client, agent_with_key):
    """T7: the X-Hermes-Key-Id maps to agent A, but the URL agent_id
    is B. Per v0.7 §1.4 key-id-to-agent rule; expect 403
    KEY_AGENT_MISMATCH.
    """
    agent_a = ("win-test-1", "key-a", os.urandom(32))
    agent_b = ("win-test-2", "key-b", os.urandom(32))
    # Placeholder: insert both agents into the test DB
    # setup_test_agent(client, *agent_a)
    # setup_test_agent(client, *agent_b)

    # Sign with A's key but request B's URL
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_b[0]}/status", b"",
        key_id=agent_a[1], secret=agent_a[2],
    )
    response = client.get(f"/api/agents/{agent_b[0]}/status", headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"].split(": ")[0] == "KEY_AGENT_MISMATCH"


# === T8 — Body hash mismatch ===

def test_v07_body_hash_mismatch_returns_401(client, agent_with_key):
    """T8: sign with body=A, send body=B; the X-Hermes-Body-SHA256
    header is bound to the signed body, not the sent body.
    Expect 401 BODY_HASH_MISMATCH.
    """
    agent_id, key_id, secret = agent_with_key
    # The /heartbeat endpoint accepts a JSON body
    signed_body = b'{"force": 1}'
    sent_body = b'{"different": "body"}'

    headers = sign_v07_request(
        "POST", f"/api/agents/{agent_id}/heartbeat",
        body=signed_body,
        key_id=key_id, secret=secret,
    )
    # Send a different body with the headers from the signed body
    response = client.post(
        f"/api/agents/{agent_id}/heartbeat",
        headers=headers,
        content=sent_body,
    )
    assert response.status_code == 401
    assert response.json()["detail"].split(": ")[0] == "BODY_HASH_MISMATCH"


# === T9 — Signature mismatch ===

def test_v07_signature_mismatch_returns_401(client, agent_with_key):
    """T9: sign with secret=A, verify against agent whose stored
    secret is B. Expect 401 INVALID_SIGNATURE.
    """
    agent_id, key_id, _ = agent_with_key
    # Use a different secret for signing than the agent has stored
    wrong_secret = os.urandom(32)
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id=key_id, secret=wrong_secret,
    )
    response = client.get(f"/api/agents/{agent_id}/status", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"].split(": ")[0] == "INVALID_SIGNATURE"


# === T10 — Nonce replay ===

def test_v07_nonce_replay_returns_401(client, agent_with_key, nonce_store):
    """T10: send the same nonce twice within the timestamp window;
    the second request should fail with NONCE_REPLAY.

    This is the protection against replay attacks: even if an attacker
    captures a valid request, the nonce store rejects the second use.
    """
    agent_id, key_id, secret = agent_with_key
    # First request: nonce is fresh
    headers1 = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id=key_id, secret=secret,
    )
    response1 = client.get(f"/api/agents/{agent_id}/status", headers=headers1)
    assert response1.status_code == 200

    # Second request: replay the same headers (same nonce)
    response2 = client.get(f"/api/agents/{agent_id}/status", headers=headers1)
    assert response2.status_code == 401
    assert response2.json()["detail"].split(": ")[0] == "NONCE_REPLAY"


# === T11 — Query string on signed endpoint ===

def test_v07_query_string_rejected(client, agent_with_key):
    """T11: v0.7 §1.4 forbids query strings on signed endpoints.
    A request with a query string in the path should be rejected.
    """
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status?foo=bar", b"",
        key_id=key_id, secret=secret,
    )
    response = client.get(
        f"/api/agents/{agent_id}/status?foo=bar",
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"].split(": ")[0] == "MALFORMED_HEADERS"


# === T12 — Path normalization (parametrized) ===

@pytest.mark.parametrize("path_variant,should_pass", [
    ("/api/agents/win-test-1/status", True),       # canonical form
    ("/api/agents//win-test-1/status", False),    # double slash
    ("/API/AGENTS/WIN-TEST-1/STATUS", False),     # uppercase
    ("/api/agents/win-test-1/status/", True),      # trailing slash
])
def test_v07_path_normalization(
    client, agent_with_key, path_variant, should_pass
):
    """T12: the server must accept the exact canonical form the
    client signs. Deviations (double slash, uppercase) fail.
    Trailing slash is accepted (the server normalizes).
    """
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request(
        "GET", path_variant, b"",
        key_id=key_id, secret=secret,
    )
    response = client.get(path_variant, headers=headers)
    if should_pass:
        assert response.status_code == 200, (
            f"Expected canonical path '{path_variant}' to pass, "
            f"got {response.status_code}: {response.json()}"
        )
    else:
        assert response.status_code in (400, 401, 403), (
            f"Expected non-canonical path '{path_variant}' to fail, "
            f"got {response.status_code}: {response.json()}"
        )


# === T15 — Bootstrapper Wait-ForEnrollment end-to-end (integration) ===

@pytest.mark.integration
def test_v07_enrollment_poll_end_to_end(client, agent_with_key):
    """T15: simulate the bootstrapper's Wait-ForEnrollment polling
    loop. The agent transitions from 'pending' to 'verified' during
    the poll. The bootstrapper sees 'verified' within 60s and stops.

    Integration test: runs in the integration suite, not the unit suite.
    """
    agent_id, key_id, secret = agent_with_key
    # Placeholder: set up the agent in 'pending' state, then trigger
    # the orchestrator's enrollment-completion logic.
    # For the draft, just verify the polling shape:
    for attempt in range(12):  # 12 × 5s = 60s
        headers = sign_v07_request(
            "GET", f"/api/agents/{agent_id}/status", b"",
            key_id=key_id, secret=secret,
        )
        response = client.get(
            f"/api/agents/{agent_id}/status", headers=headers,
        )
        if response.status_code == 200 and response.json().get("status") == "verified":
            return  # success
        time.sleep(5)
    pytest.fail("Agent did not reach 'verified' status within 60s")


# === T16 — Cert mismatch (bootstrapper layer, not orch server) ===

# NOTE: T16 is tested at the BOOTSTRAPPER layer, not the orch server
# layer. The bootstrapper rejects the orch's TLS cert before sending
# the HMAC request (per the orchestrator's TLS cert verification at
# the TLS handshake). The orch server sees a normal request.
# See installer/bootstrapper/install-orch-client.ps1
# Wait-ForEnrollment function for the bootstrapper's T16 coverage.


# === Step 8: T-v07-enrollment (POST /api/enrollment/v07) ===

def test_v07_enrollment_endpoint_marks_agent_verified(
    client, agent_with_key
):
    """Step 8: POST /api/enrollment/v07 with a v0.7 signed body
    marks the agent as 'verified' and returns 200.

    Per the v0.7 spec §4: the agent presents its hmac_key_id +
    hmac_secret via the 7 X-Hermes-* headers (verifier), and
    this endpoint updates the agent row's status from
    'verifying' to 'verified' and stamps last_heartbeat_at.

    The test pre-sets the agent status to 'verifying' (the
    agent_with_key fixture inserts with 'verified' for the
    status-endpoint tests; here we override). Then POST to the
    enrollment endpoint and verify:
      - 200 + {"status": "verified", "agent_id": ...}
      - The agent's status in the DB is now 'verified'
    """
    import json as _json
    import sqlite3 as _sqlite3

    agent_id, key_id, secret = agent_with_key

    # Pre-set the agent's status to 'verifying' (override the
    # fixture's default 'verified' insert; this test simulates
    # the operator's pre-provisioning state).
    db_path = client.app.state.db.db_path
    conn = _sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE agents SET status = 'verifying' WHERE id = ?",
            (agent_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # Build a v0.7 signed request with a JSON body. The body
    # includes agent_name, hostname, os_type. The verifier
    # checks the body SHA-256 against the body's actual bytes.
    body_dict = {
        "agent_name": "test-vm-01",
        "hostname": "test-host-01",
        "os_type": "windows-11",
    }
    body_bytes = _json.dumps(body_dict).encode("utf-8")

    headers = sign_v07_request(
        "POST", "/api/enrollment/v07",
        body=body_bytes,
        key_id=key_id, secret=secret,
    )
    # v0.7 verifier uses its own X-Hermes-* headers; we add
    # Content-Type here so FastAPI parses the body as JSON.
    headers["Content-Type"] = "application/json"
    response = client.post(
        "/api/enrollment/v07",
        headers=headers,
        content=body_bytes,
    )
    assert response.status_code == 200, (
        f"v0.7 enrollment failed (status={response.status_code}, "
        f"body={response.text}). The endpoint should mark the "
        f"agent as verified and return 200."
    )
    body = response.json()
    assert body.get("status") == "verified", f"body={body}"
    assert body.get("agent_id") == agent_id, f"body={body}"

    # Verify the DB row was updated
    conn = _sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT status, hostname, os_type, name FROM agents "
            "WHERE id = ?",
            (agent_id,),
        ).fetchone()
        assert row[0] == "verified", f"status in DB: {row[0]}"
        assert row[1] == "test-host-01", f"hostname in DB: {row[1]}"
        assert row[2] == "windows-11", f"os_type in DB: {row[2]}"
        assert row[3] == "test-vm-01", f"name in DB: {row[3]}"
    finally:
        conn.close()


# === Notes for the actual implementation (Day 5+) ===

# 1. The fixtures above (client, agent_with_key, nonce_store) need to be
#    implemented in conftest.py or a similar shared fixture file. The
#    draft keeps them inline for self-containedness.
#
# 2. The sign_v07_request helper should be extracted to
#    tests/helpers/hmac_v07.py per the impl plan §2.
#
# 3. The InMemoryNonceStore test double should be extracted to
#    tests/helpers/nonce_store.py per the impl plan §2.
#
# 4. The actual create_app() / FastAPI app is from hermes_orch.main
#    (existing pattern). The v0.7 routes (GET /api/agents/{id}/status)
#    are added in the impl plan §4 step 4.
#
# 5. The 2 dual-format tests (T13, T14) live in a separate file
#    tests/test_hmac_v06_compat.py.
