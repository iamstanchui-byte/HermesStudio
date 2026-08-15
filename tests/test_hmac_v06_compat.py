"""Tests for v0.6 + v0.7 dual-format HMAC verification (DRAFT 2026-08-13).

This file is a DRAFT for future Day 5+ implementation. It is NOT
executed today. The draft covers the 2 dual-format test cases
(T13, T14) that exercise the Option B migration path described in
docs/specs/orch-server-hmac-v0.7-alignment.md §3.

DRAFT status: not committed to the implementation branch yet. The
test code is the spec for the dual-format support; the actual
implementation follows the TDD red-green-refactor sequence in
docs/specs/orch-server-hmac-v0.7-impl-plan.md §4 step 6.

Cross-references:
- v0.6 (v1.6) signing helper lives in tests/_hmac_util.py
  (existing, pre-DRAFT). It uses 3 headers (X-Agent-Id,
  X-Timestamp, X-Signature) and hex HMAC-SHA256.
- v0.7 signing helper lives in tests/helpers/hmac_v07.py
  (DRAFT, this commit). It uses 7 headers (X-Hermes-*) and
  base64 HMAC-SHA256.
- The dual-format dispatcher in require_hmac_auth checks the
  header set and routes to the right verifier per impl plan §4
  step 6.
"""
from __future__ import annotations

import os
import pytest
from fastapi.testclient import TestClient

# These imports are placeholders; replace at impl time.
# from hermes_orch.main import create_app

from tests._hmac_util import signed_request, register_test_agent, unregister_test_agent
from tests.helpers.hmac_v07 import sign_v07_request


# === Fixtures (mirrored from test_hmac_v07_auth.py) ===

@pytest.fixture
def client():
    """FastAPI TestClient with BOTH v0.6 and v0.7 routes registered.
    Per Option B migration, the dispatcher in require_hmac_auth
    checks the header set and routes to the right verifier.
    """
    raise NotImplementedError(
        "DRAFT — replace with real create_app() at impl time. "
        "The dual-format dispatcher is added per impl plan §4 step 6."
    )


@pytest.fixture
def agent_with_v06():
    """Yield (agent_id, secret_str) for a fresh test agent. The
    v0.6 client uses a string secret (per hmac.py:91-100, the secret
    is a string). v0.7 uses bytes; this dual-format fixture uses
    a string for v0.6 compatibility.

    Uses tests/_hmac_util.register_test_agent for the DB insert.
    """
    agent_id = f"win-test-{os.urandom(4).hex()}"
    secret_str = f"this-is-a-test-secret-string-{os.urandom(4).hex()}"
    # Placeholder: register via the existing helper
    # register_test_agent(agent_id, secret_str)
    # yield (agent_id, secret_str)
    # unregister_test_agent(agent_id)  # cleanup
    raise NotImplementedError(
        "DRAFT — replace with real DB fixture at impl time. The "
        "agent_id + secret_str get registered via "
        "tests/_hmac_util.register_test_agent."
    )


# === T13 — v1.6 (v0.6 client) request on POST /heartbeat ===

def test_v06_heartbeat_still_works_during_dual_format(client, agent_with_v06):
    """T13: v1.6 request (X-Agent-Id) on POST /heartbeat must still
    work during the Option B transition. The 2 known agents
    (win-local-1, linux-a-01) keep using v0.6 until the operator
    flips HERMES_HMAC_ACCEPT_V06=false.
    """
    agent_id, secret = agent_with_v06
    # Use the existing v0.6 signed_request helper
    status, body, headers = signed_request(
        "POST", f"/api/agents/{agent_id}/heartbeat",
        body={"force": 1},
        agent_id=agent_id, secret=secret,
    )
    assert status == 200, (
        f"v0.6 request on /heartbeat failed (status={status}, "
        f"body={body}). The dual-format dispatcher should accept v0.6 "
        f"requests during the Option B transition."
    )


# === T14 — v0.7 request on POST /heartbeat (during dual-format) ===

def test_v07_heartbeat_accepts_v07_format(client):
    """T14: v0.7 request (X-Hermes-*) on POST /heartbeat must work
    in Option B migration. The dispatcher routes by header set:
    presence of X-Hermes-Method → v0.7; absence → v0.6.
    """
    # Placeholder: get the agent's key_id + secret from the test DB
    agent_id = "win-test-1"
    key_id = "key-win-test-1"
    secret = os.urandom(32)

    headers = sign_v07_request(
        "POST", f"/api/agents/{agent_id}/heartbeat",
        body=b'{"force": 1}',
        key_id=key_id, secret=secret,
    )
    response = client.post(
        f"/api/agents/{agent_id}/heartbeat",
        headers=headers,
        json={"force": 1},
    )
    assert response.status_code == 200, (
        f"v0.7 request on /heartbeat failed (status={response.status_code}, "
        f"body={response.json()}). The dual-format dispatcher should "
        f"accept v0.7 requests during the Option B transition."
    )
