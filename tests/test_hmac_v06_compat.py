"""Tests for v0.6 + v0.7 dual-format HMAC verification (DRAFT 2026-08-13,
TDD RED PHASE STARTED 2026-08-15, step 3).

Covers the 2 dual-format test cases (T13, T14) that exercise the
Option B migration path described in
docs/specs/orch-server-hmac-v0.7-alignment.md §3.

TDD red phase state (2026-08-15):
- T13 (v0.6 request on /heartbeat): should PASS on current code
  (v0.6 verifier is in place). This is a REGRESSION test that
  protects against accidentally breaking v0.6 when the dual-format
  dispatcher lands in step 7.
- T14 (v0.7 request on /heartbeat): should FAIL on current code
  (v0.6 verifier rejects v0.7 headers; returns 401 MISSING_AUTH_HEADERS).
  This is the RED test that step 7 turns green by adding the
  dual-format dispatcher.

Fixtures are now real (per test_hmac_v07_auth.py pattern) using
create_app() via TestClient + tmp DB. The agent_with_v06 fixture
registers a v0.6 test agent via tests/_hmac_util.register_test_agent
(monkeypatching DB_PATH to point to the same tmp DB). Both T13 and
T14 use the TestClient (in-process) so the test does not require
a live server on 127.0.0.1:8765.

Cross-references:
- v0.6 (v1.6) signing helper: tests/_hmac_util.py (existing).
  3 headers (X-Agent-Id, X-Timestamp, X-Signature), hex HMAC-SHA256.
- v0.7 signing helper: tests/helpers/hmac_v07.py.
  7 headers (X-Hermes-*), base64 HMAC-SHA256.
- Dual-format dispatcher (impl plan §4 step 7): checks the header
  set and routes to the right verifier.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
import time
import pytest
from fastapi.testclient import TestClient

# Real imports (per test_endpoint_auth.py pattern)
from hermes_orch import main as main_mod
from hermes_orch import db as db_mod
from hermes_orch.main import create_app

from tests import _hmac_util
from tests._hmac_util import register_test_agent, unregister_test_agent
from tests.helpers.hmac_v07 import sign_v07_request


# === Fixtures (mirrored from test_hmac_v07_auth.py) ===

@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI TestClient with both v0.6 and v0.7 routes registered.

    Uses a tmp DB for isolation. Also monkeypatches
    `tests._hmac_util.DB_PATH` to the same tmp DB so the
    `register_test_agent` / `unregister_test_agent` helpers
    operate on the test DB, not the live production DB.
    """
    test_db = tmp_path / "test_hmac_v06_compat.db"
    orig_db_init = db_mod.Database.__init__

    def patched_db_init(self, db_path):
        orig_db_init(self, test_db)

    monkeypatch.setattr(db_mod.Database, "__init__", patched_db_init)
    # Also patch _hmac_util.DB_PATH so register_test_agent /
    # unregister_test_agent target the test DB, not the live one.
    # Critical for safety: this prevents the test from mutating
    # production state via the helpers' direct sqlite3.connect(DB_PATH).
    monkeypatch.setattr(_hmac_util, "DB_PATH", test_db)

    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def agent_with_v06(client):
    """Yield (agent_id, secret_str) for a fresh v0.6 test agent.

    The v0.6 client uses a string secret (per hmac.py:91-100, the
    secret is a string). v0.7 uses bytes; this dual-format fixture
    uses a string for v0.6 compatibility.

    Uses tests/_hmac_util.register_test_agent for the DB insert;
    cleanup via unregister_test_agent. The `client` fixture is
    required so the monkeypatched DB_PATH is in effect.
    """
    agent_id = f"win-test-{os.urandom(4).hex()}"
    secret_str = f"this-is-a-test-secret-string-{os.urandom(4).hex()}"
    register_test_agent(agent_id, secret_str)
    try:
        yield (agent_id, secret_str)
    finally:
        unregister_test_agent(agent_id)


# === T13 — v1.6 (v0.6 client) request on POST /heartbeat ===

def test_v06_heartbeat_still_works_during_dual_format(client, agent_with_v06):
    """T13: v1.6 request (X-Agent-Id) on POST /heartbeat must still
    work during the Option B transition. The 2 known agents
    (win-local-1, linux-a-01) keep using v0.6 until the operator
    flips HERMES_HMAC_ACCEPT_V06=false.

    Uses TestClient (in-process). For the red phase (step 3), this
    is a REGRESSION test — the v0.6 verifier is in place, so this
    should PASS now. It protects against accidentally breaking
    v0.6 when the dual-format dispatcher lands in step 7.
    """
    import json as _json
    agent_id, secret = agent_with_v06
    # v0.6 signing: 3 headers, hex HMAC-SHA256, 4-field string-to-sign
    # Format: METHOD\nPATH\nSHA256_HEX(body)\nTIMESTAMP
    body_dict = {"force": 1}
    body_bytes = _json.dumps(body_dict).encode("utf-8")
    ts = str(int(time.time()))
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    msg = f"POST\n/api/agents/{agent_id}/heartbeat\n{body_hash}\n{ts}"
    sig = _hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Id": agent_id,
        "X-Timestamp": ts,
        "X-Signature": sig,
    }
    response = client.post(
        f"/api/agents/{agent_id}/heartbeat",
        headers=headers,
        content=body_bytes,
    )
    assert response.status_code == 200, (
        f"v0.6 request on /heartbeat failed (status={response.status_code}, "
        f"body={response.text}). The dual-format dispatcher should accept v0.6 "
        f"requests during the Option B transition."
    )


# === T14 — v0.7 request on POST /heartbeat (during dual-format) ===

def test_v07_heartbeat_accepts_v07_format(client):
    """T14: v0.7 request (X-Hermes-*) on POST /heartbeat must work
    in Option B migration. The dispatcher routes by header set:
    presence of X-Hermes-Method → v0.7; absence → v0.6.

    Uses TestClient (in-process). For the red phase (step 3), this
    test should FAIL — the v0.6 verifier rejects v0.7 headers
    (returns 401 MISSING_AUTH_HEADERS). Step 7 (dual-format
    dispatcher) turns this green.
    """
    # NOTE: T14 doesn't use agent_with_v06 because in the v0.7
    # world the agent has a different shape (key_id + bytes
    # secret). For the red phase we just need any 200 response;
    # the agent lookup is part of the impl that lands in step 4.
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
        content=b'{"force": 1}',
    )
    assert response.status_code == 200, (
        f"v0.7 request on /heartbeat failed (status={response.status_code}, "
        f"body={response.text}). The dual-format dispatcher should "
        f"accept v0.7 requests during the Option B transition."
    )
