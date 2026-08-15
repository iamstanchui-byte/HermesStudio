"""Tests for v0.7 §1.4 bound-metadata HMAC verification (DRAFT 2026-08-13).

This file is a DRAFT for future Day 5+ implementation. It is NOT
executed today. The draft maps the 16 acceptance test cases (T1-T16)
from docs/specs/orch-server-hmac-v0.7-alignment.md §6 to runnable
pytest code, plus the 2 dual-format test cases (T13, T14) that exercise
the Option B migration path. The 2 dual-format tests live in
tests/test_hmac_v06_compat.py.

DRAFT status: not committed to the implementation branch yet. The
test code is the spec; the actual implementation follows the
TDD red-green-refactor sequence in
docs/specs/orch-server-hmac-v0.7-impl-plan.md §4.

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

# These imports are placeholders; the actual module names will be
# decided during implementation per the impl plan §5.
# from hermes_orch.main import create_app

from tests.helpers.hmac_v07 import sign_v07_request
from tests.helpers.nonce_store import InMemoryNonceStore


# === Fixtures ===

@pytest.fixture
def client():
    """FastAPI TestClient with the v0.7 routes registered.

    The actual app is created via create_app() (existing pattern per
    the B12 hotfix tests in tests/test_endpoint_auth.py). The
    v0.7 routes are added when the implementation lands.
    """
    # Placeholder; replace with real create_app() when implementation lands.
    # from hermes_orch.main import create_app
    # app = create_app()
    # with TestClient(app) as c:
    #     yield c
    raise NotImplementedError(
        "DRAFT — replace with real create_app() at impl time. "
        "The v0.7 routes (GET /api/agents/{id}/status, dual-format "
        "support on /heartbeat, GET /{id}) are added per impl plan §4."
    )


@pytest.fixture
def agent_with_key():
    """Yield (agent_id, key_id, secret_bytes) for a fresh test agent.

    The agent row is inserted into the test DB with a fresh HMAC key
    bound (per the new agents.hmac_key_id UNIQUE column). The test
    cleans up after itself.
    """
    agent_id = f"win-test-{uuid.uuid4().hex[:8]}"
    key_id = f"key-{agent_id}"
    secret = os.urandom(32)
    # Placeholder: insert into test DB
    # yield (agent_id, key_id, secret)
    # cleanup: remove the row
    raise NotImplementedError(
        "DRAFT — replace with real DB fixture at impl time. The "
        "production shape is per impl plan §5: new column "
        "agents.hmac_key_id (UNIQUE)."
    )


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
    assert response.status_code == 200
    assert response.json() == {"status": "verified"}


# === T2 — Missing X-Hermes-Method ===

def test_v07_missing_method_header_returns_401(client, agent_with_key):
    """T2: drop the X-Hermes-Method header; expect 401 MISSING_AUTH_HEADERS."""
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"", key_id, secret,
    )
    del headers["X-Hermes-Method"]
    response = client.get(f"/api/agents/{agent_id}/status", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "MISSING_AUTH_HEADERS"


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
    assert response.json()["error"] == "MISSING_AUTH_HEADERS"


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
    assert response.json()["error"] == "TIMESTAMP_OUT_OF_WINDOW"


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
    assert response.json()["error"] == "TIMESTAMP_OUT_OF_WINDOW"


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
    assert response.json()["error"] == "UNKNOWN_KEY_ID"


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
    assert response.json()["error"] == "KEY_AGENT_MISMATCH"


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
    assert response.json()["error"] == "BODY_HASH_MISMATCH"


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
    assert response.json()["error"] == "INVALID_SIGNATURE"


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
    assert response2.json()["error"] == "NONCE_REPLAY"


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
    assert response.json()["error"] == "MALFORMED_HEADERS"


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
