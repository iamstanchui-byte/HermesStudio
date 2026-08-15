# Orch Server HMAC v0.7 Implementation Plan (TDD Design)

**Date:** 2026-08-13
**Author:** Mavis (operator-approved direction: design server-side TDD
plan for the v0.7 §1.4 HMAC implementation)
**Status:** PROPOSAL for review
**Supersedes:** none (additive to `orch-server-hmac-v0.7-alignment.md` PR #5)
**Scope:** TDD test design for the server-side implementation of the
v0.7 §1.4 bound-metadata HMAC verification. Maps each of the 16
test cases (T1-T16) from the spec §6 to a concrete unit test, lays
out the implementation sequence, and identifies the new files.
This is a PLAN for future implementation; no code is changed here.

---

## 0. Why this plan

The spec at `docs/specs/orch-server-hmac-v0.7-alignment.md` §6 lists
16 acceptance test cases. To implement them with confidence, the
test scaffolding must be designed up front: FastAPI test client,
mock HMAC signing helper, in-memory LRU nonce store fixture,
dual-format coverage (v1.6 + v0.7 both work during the transition).
Without the scaffolding, the implementation will diverge from the
spec's test cases and miss edge cases.

This plan locks in the test infrastructure + test-by-test design
BEFORE writing implementation code (TDD red-green-refactor).

---

## 1. Test infrastructure

### 1.1 FastAPI test client (existing pattern)

`src/hermes_orch/api/...` already uses FastAPI's `TestClient` (per
the v1.0 test pattern). Reuse:

```python
from fastapi.testclient import TestClient
from hermes_orch.main import create_app

@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c
```

### 1.2 Mock HMAC signing helper (NEW for v0.7)

A test helper that produces the same `X-Hermes-*` headers as the
v0.7.1 bootstrapper's `Wait-ForEnrollment`. Lives in
`tests/helpers/hmac_v07.py` (new file):

```python
def sign_v07_request(
    method: str,
    path: str,
    body: bytes,
    key_id: str,
    secret: bytes,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Return the 7 X-Hermes-* headers that would make this request
    pass v0.7 §1.4 verification. The test client sends these
    headers along with the request."""
    timestamp = timestamp or int(time.time())
    nonce = nonce or uuid.uuid4().hex
    body_sha256_hex = hashlib.sha256(body or b"").hexdigest()
    canonical = f"{method.upper()}\n{path}\n{body_sha256_hex}\n{timestamp}\n{nonce}"
    sig = base64.b64encode(
        hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    return {
        "X-Hermes-Method": method.upper(),
        "X-Hermes-Path": path,
        "X-Hermes-Body-SHA256": body_sha256_hex,
        "X-Hermes-Key-Id": key_id,
        "X-Hermes-Timestamp": str(timestamp),
        "X-Hermes-Nonce": nonce,
        "X-Hermes-Signature": sig,
    }
```

Symmetric to the bootstrapper's `Wait-ForEnrollment` (per
`installer/bootstrapper/install-orch-client.ps1` `Wait-ForEnrollment`
function). The two implementations MUST stay in sync — add a comment
in both files cross-referencing each other.

### 1.3 In-memory LRU nonce store fixture (NEW for v0.7)

A test-double for the production nonce store. Lives in
`tests/helpers/nonce_store.py` (new file):

```python
class InMemoryNonceStore:
    """Test double for the production nonce store. TTL-based
    eviction mirrors the real LRU's behavior."""
    def __init__(self, ttl_seconds: int = 300):
        self._store: dict[str, float] = {}
        self._ttl = ttl_seconds

    def check_and_record(self, nonce: str, now: float | None = None) -> bool:
        now = now or time.time()
        self._evict_expired(now)
        if nonce in self._store:
            return False  # replay
        self._store[nonce] = now + self._ttl
        return True

    def _evict_expired(self, now: float):
        self._store = {k: v for k, v in self._store.items() if v > now}
```

The production nonce store (out of scope for this plan) will have
the same interface so tests are reusable.

### 1.4 Agent + key_id fixture

A test-double for the `agents` table that maps `key_id` → agent row
(per the new `agents.hmac_key_id` UNIQUE column):

```python
@pytest.fixture
def agent_with_key():
    """Yield (agent_id, key_id, secret) for a test agent with a
    fresh HMAC key bound. The agent row is inserted into the test
    DB; the test cleans up."""
    return ("win-test-1", "key-win-test-1", os.urandom(32))
```

### 1.5 Dual-format test coverage

The dual-format migration (spec §3 Option B) requires that:
- v1.6 requests (X-Agent-Id, X-Timestamp, X-Signature) STILL work
  on the 2 existing routes (heartbeat, GET /{id})
- v0.7 requests (X-Hermes-*) work on the new route
  (GET /api/agents/{id}/status) AND on the 2 existing routes

This is covered by:
- T13 (v1.6 request on heartbeat — must work)
- T14 (v0.7 request on heartbeat — must work, per Option B)

---

## 2. Test file layout

```
tests/
├── test_hmac_v07_auth.py          (NEW — 16 tests, one per T1-T16)
├── test_hmac_v06_compat.py       (NEW — T13 + T14 dual-format)
├── helpers/
│   ├── __init__.py
│   ├── hmac_v07.py               (NEW — sign_v07_request helper)
│   └── nonce_store.py            (NEW — InMemoryNonceStore fixture)
```

Total: 2 new test files + 2 new helper files. ~600 lines of test code
+ ~100 lines of helper code.

The new files mirror the implementation layout
(`src/hermes_orch/auth/hmac_v07.py`):

```
src/hermes_orch/auth/
├── hmac.py                       (existing v1.6 — unchanged in this impl)
├── hmac_v07.py                   (NEW — v0.7 §1.4 verification)
└── nonce_store.py                (NEW — in-memory LRU, swappable for prod)
```

---

## 3. T1-T16 test designs (one section per test)

Each test follows the existing test pattern in
`tests/test_endpoint_auth.py` (the B12 hotfix's test file). Style:
pytest, FastAPI TestClient, descriptive function names, `assert`
statements that print the failure context (per the existing pattern).

### T1 — Happy path

```python
def test_v07_happy_path_returns_200(client, agent_with_key):
    """Bootstrapper signs a GET /api/agents/win-test-1/status with
    valid headers; expects 200 + {"status": "verified"}."""
    agent_id, key_id, secret = agent_with_key
    # Set up the test agent with status=verified
    setup_test_agent(client, agent_id, key_id, secret, status="verified")

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
```

### T2 — Missing `X-Hermes-Method`

```python
def test_v07_missing_method_header_returns_401(client, agent_with_key):
    """Drop the X-Hermes-Method header; expect 401
    MISSING_AUTH_HEADERS."""
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request("GET", f"/api/agents/{agent_id}/status", b"", key_id, secret)
    del headers["X-Hermes-Method"]
    response = client.get(f"/api/agents/{agent_id}/status", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "MISSING_AUTH_HEADERS"
```

### T3 — Missing `X-Hermes-Signature`

Same as T2 but drop `X-Hermes-Signature`.

### T4 — Timestamp 600s in the past

```python
def test_v07_old_timestamp_returns_401(client, agent_with_key):
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id, secret,
        timestamp=int(time.time()) - 600,
    )
    response = client.get(f"/api/agents/{agent_id}/status", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "TIMESTAMP_OUT_OF_WINDOW"
```

### T5 — Timestamp 600s in the future

Same as T4 but with `timestamp=int(time.time()) + 600`.

### T6 — Unknown `X-Hermes-Key-Id`

```python
def test_v07_unknown_key_id_returns_401(client, agent_with_key):
    agent_id, _, _ = agent_with_key
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id="key-does-not-exist",
        secret=b"doesn't matter",  # never reaches verification
    )
    response = client.get(f"/api/agents/{agent_id}/status", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "UNKNOWN_KEY_ID"
```

### T7 — Key_id binds to a different agent

```python
def test_v07_key_agent_mismatch_returns_403(client, agent_with_key):
    """The X-Hermes-Key-Id maps to agent A, but the URL agent_id is B.
    Expect 403 KEY_AGENT_MISMATCH."""
    agent_a = ("win-test-1", "key-a", os.urandom(32))
    agent_b = ("win-test-2", "key-b", os.urandom(32))
    setup_test_agent(client, *agent_a)
    setup_test_agent(client, *agent_b)

    # Sign with A's key but request B's URL
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_b[0]}/status", b"",
        key_id=agent_a[1], secret=agent_a[2],
    )
    response = client.get(f"/api/agents/{agent_b[0]}/status", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"] == "KEY_AGENT_MISMATCH"
```

### T8 — Body hash mismatch

```python
def test_v07_body_hash_mismatch_returns_401(client, agent_with_key):
    """Sign with body=A but send body=B; expect 401 BODY_HASH_MISMATCH."""
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request(
        "POST", f"/api/agents/{agent_id}/heartbeat",
        body=b'{"force": 1}',  # signed with this body
        key_id=key_id, secret=secret,
    )
    # Send a different body
    response = client.post(
        f"/api/agents/{agent_id}/heartbeat",
        headers=headers,
        json={"different": "body"},
    )
    assert response.status_code == 401
    assert response.json()["error"] == "BODY_HASH_MISMATCH"
```

### T9 — Signature mismatch

```python
def test_v07_signature_mismatch_returns_401(client, agent_with_key):
    """Sign with one secret, verify with a different one."""
    agent_id, key_id, _ = agent_with_key
    wrong_secret = os.urandom(32)
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id=key_id, secret=wrong_secret,
    )
    response = client.get(f"/api/agents/{agent_id}/status", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "INVALID_SIGNATURE"
```

### T10 — Nonce replay

```python
def test_v07_nonce_replay_returns_401(client, agent_with_key, nonce_store):
    """Send the same nonce twice within the window; second
    request should fail."""
    agent_id, key_id, secret = agent_with_key
    headers1 = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status", b"",
        key_id=key_id, secret=secret,
    )
    response1 = client.get(f"/api/agents/{agent_id}/status", headers=headers1)
    assert response1.status_code == 200

    # Replay the same headers
    response2 = client.get(f"/api/agents/{agent_id}/status", headers=headers1)
    assert response2.status_code == 401
    assert response2.json()["error"] == "NONCE_REPLAY"
```

### T11 — Query string on signed endpoint

```python
def test_v07_query_string_rejected(client, agent_with_key):
    """v0.7 §1.4 forbids query strings on signed endpoints."""
    agent_id, key_id, secret = agent_with_key
    headers = sign_v07_request(
        "GET", f"/api/agents/{agent_id}/status?foo=bar", b"",
        key_id=key_id, secret=secret,
    )
    response = client.get(f"/api/agents/{agent_id}/status?foo=bar", headers=headers)
    assert response.status_code == 400
    assert response.json()["error"] == "MALFORMED_HEADERS"
```

### T12 — Path normalization

```python
@pytest.mark.parametrize("path_variant,should_pass", [
    ("/api/agents/win-test-1/status", True),     # canonical
    ("/api/agents//win-test-1/status", False),  # double slash
    ("/API/AGENTS/WIN-TEST-1/STATUS", False),    # uppercase
    ("/api/agents/win-test-1/status/", True),    # trailing slash
])
def test_v07_path_normalization(client, agent_with_key, path_variant, should_pass):
    """The server normalizes paths per the same rule the client uses.
    Exact canonical form passes; deviations fail."""
    # ... similar structure
```

### T13 — Dual-format: v1.6 request on heartbeat

```python
def test_v06_heartbeat_still_works_during_dual_format(client, agent_with_v06):
    """v1.6 request (X-Agent-Id) on POST /heartbeat must still work
    during the transition (Option B migration)."""
    # Uses the existing v1.6 signing helper from tests/helpers/hmac_v06.py
    # (the existing one if any; create if needed)
    response = client.post(
        f"/api/agents/{agent_id}/heartbeat",
        headers=v06_signed_headers,
        json={"force": 1},
    )
    assert response.status_code == 200
```

### T14 — Dual-format: v0.7 request on heartbeat

```python
def test_v07_heartbeat_accepts_v07_format(client, agent_with_key):
    """v0.7 request (X-Hermes-*) on POST /heartbeat works in
    Option B migration."""
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
    assert response.status_code == 200
```

### T15 — Bootstrapper Wait-ForEnrollment end-to-end

```python
def test_v07_enrollment_poll_end_to_end(client, agent_with_key, freezetime):
    """The full bootstrapper flow: 5s polls for 60s, returns
    verified on success."""
    # This is more of an integration test than a unit test.
    # Mark as @pytest.mark.integration; runs in the integration
    # test suite, not the unit suite.
    agent_id, key_id, secret = agent_with_key
    # ... poll loop, asserts verified within 60s
```

### T16 — Cert mismatch (bootstrapper's TLS pin)

The cert-mismatch test is at the bootstrapper layer (the bootstrapper
rejects the orch's cert before sending the HMAC request), not the
orch server. The orch server sees a normal request. This is covered
by the bootstrapper's own test (per
`installer/bootstrapper/install-orch-client.ps1` `Wait-ForEnrollment`).
The orch-side test that does apply: T7 (cert presented by orch
server in TLS handshake, not in HMAC headers).

---

## 4. Implementation sequence (TDD red-green-refactor)

| Step | Cycle | What | File(s) | Test count |
|---|---|---|---|---|
| 1 | Red | Write tests T1-T16 with no implementation | `tests/test_hmac_v07_auth.py` + helpers | 16 (all failing) |
| 2 | Green | Implement `require_hmac_auth_v07` (the FastAPI dependency) | `src/hermes_orch/auth/hmac_v07.py` | T1-T12, T15, T16 pass; T13, T14 (dual-format) fail |
| 3 | Green | Add `lookup_agent_by_key_id` + data model migration | `src/hermes_orch/api/agents.py` (alter agents table) | T6, T7 pass |
| 4 | Green | Add `GET /api/agents/{id}/status` endpoint | `src/hermes_orch/api/agent_status.py` | T1, T15 pass end-to-end |
| 5 | Refactor | Extract `InMemoryNonceStore` to production-ready `RedisNonceStore` (interface) | `src/hermes_orch/auth/nonce_store.py` | All pass; production-ready store |
| 6 | Green | Add dual-format path on `require_hmac_auth` (T13 + T14) | `src/hermes_orch/auth/hmac.py` (extend, not replace) | T13, T14 pass |
| 7 | Green | Add `POST /api/enrollment/v07` HMAC-signed variant | `src/hermes_orch/api/enrollment.py` (extend) | (new tests added separately) |
| 8 | Refactor | Move v0.7 + v1.6 into a unified `verify_hmac` dispatcher keyed on header set | `src/hermes_orch/auth/hmac.py` (consolidate) | All 16 pass; code is DRY |
| 9 | Document | Update `docs/security/agent-endpoint-auth-hotfix-design.md` with v0.7 alignment | (doc-only) | n/a |

The total work for steps 1-9 is approximately 3-5 working days for a
developer familiar with the codebase. The spec from PR #5 is the
specification; this plan is the work breakdown.

---

## 5. New files / modules

| File | Lines (est) | Purpose |
|---|---|---|
| `src/hermes_orch/auth/hmac_v07.py` | ~150 | v0.7 §1.4 verification (8-step), `string_to_sign_v07`, `compute_signature_v07`, `verify_signature_v07`, `require_hmac_auth_v07` FastAPI dependency |
| `src/hermes_orch/auth/nonce_store.py` | ~80 | `NonceStore` protocol + `InMemoryNonceStore` (production-ready skeleton, swap to Redis later) |
| `src/hermes_orch/api/agent_status.py` | ~30 | `GET /api/agents/{id}/status` endpoint |
| `tests/test_hmac_v07_auth.py` | ~400 | 14 unit tests (T1-T12 + T15, T16) |
| `tests/test_hmac_v06_compat.py` | ~80 | 2 dual-format tests (T13, T14) |
| `tests/helpers/hmac_v07.py` | ~50 | `sign_v07_request` helper (mirrors bootstrapper's `Wait-ForEnrollment`) |
| `tests/helpers/nonce_store.py` | ~30 | `InMemoryNonceStore` test double |
| DB migration script | ~10 | `ALTER TABLE agents ADD COLUMN hmac_key_id TEXT; CREATE UNIQUE INDEX ...` |

Total: ~830 lines new code + ~120 lines test helpers.

---

## 6. Coverage matrix

| Test | Test file | Test function | Implementation | Implementation function |
|---|---|---|---|---|
| T1 | `test_hmac_v07_auth.py` | `test_v07_happy_path_returns_200` | `hmac_v07.py` | `require_hmac_auth_v07` |
| T2 | `test_hmac_v07_auth.py` | `test_v07_missing_method_header_returns_401` | `hmac_v07.py` | `require_hmac_auth_v07` |
| T3 | `test_hmac_v07_auth.py` | `test_v07_missing_signature_header_returns_401` | `hmac_v07.py` | `require_hmac_auth_v07` |
| T4 | `test_hmac_v07_auth.py` | `test_v07_old_timestamp_returns_401` | `hmac_v07.py` | `require_hmac_auth_v07` |
| T5 | `test_hmac_v07_auth.py` | `test_v07_future_timestamp_returns_401` | `hmac_v07.py` | `require_hmac_auth_v07` |
| T6 | `test_hmac_v07_auth.py` | `test_v07_unknown_key_id_returns_401` | `agents.py` | `lookup_agent_by_key_id` |
| T7 | `test_hmac_v07_auth.py` | `test_v07_key_agent_mismatch_returns_403` | `hmac_v07.py` | `require_hmac_auth_v07` |
| T8 | `test_hmac_v07_auth.py` | `test_v07_body_hash_mismatch_returns_401` | `hmac_v07.py` | `require_hmac_auth_v07` |
| T9 | `test_hmac_v07_auth.py` | `test_v07_signature_mismatch_returns_401` | `hmac_v07.py` | `verify_signature_v07` |
| T10 | `test_hmac_v07_auth.py` | `test_v07_nonce_replay_returns_401` | `nonce_store.py` | `InMemoryNonceStore.check_and_record` |
| T11 | `test_hmac_v07_auth.py` | `test_v07_query_string_rejected` | `hmac_v07.py` | `require_hmac_auth_v07` |
| T12 | `test_hmac_v07_auth.py` | `test_v07_path_normalization` (4 parametrized cases) | `hmac_v07.py` | `require_hmac_auth_v07` |
| T13 | `test_hmac_v06_compat.py` | `test_v06_heartbeat_still_works_during_dual_format` | `hmac.py` (extend) | `require_hmac_auth` (dual-format dispatcher) |
| T14 | `test_hmac_v06_compat.py` | `test_v07_heartbeat_accepts_v07_format` | `hmac.py` (extend) | `require_hmac_auth` (dual-format dispatcher) |
| T15 | `test_hmac_v07_auth.py` | `test_v07_enrollment_poll_end_to_end` (integration) | `agent_status.py` | `get_agent_status` |
| T16 | (covered at bootstrapper layer) | n/a | n/a | n/a (orch-side, not bootstrapper) |

---

## 7. Out of scope (deferred)

- **Production nonce store** (Redis or DB-backed) — `InMemoryNonceStore`
  is the implementation; the protocol allows a future Redis-backed
  store without changing the consumer code
- **`/api/enrollment/v07` HMAC-signed variant** — separate step 7 in
  the implementation sequence; this plan covers T1-T15, the enrollment
  variant is its own design step
- **Path canonicalization policy** (spec §8 open question) — T12
  parametrized test covers the basic cases; the production policy
  is decided during step 8 (refactor)
- **`hmac_secret` encryption at rest** (B11, separate track) — out of
  scope; the `hmac.py:42-48` "Threat model: plaintext in DB"
  comment is unchanged
- **Spec §8 open questions** (5 items) — T8 (body hash rejection)
  is decided per this plan (reject); others (nonce store TTL,
  path canonicalization policy, enrollment dual-path) are answered
  during step 8

---

## 8. Operator action requested

This plan is a design artifact. The next step is the operator (the
same person who approved the v0.7.1 bootstrapper and the v0.7 §1.4
spec) to:

1. **Review this plan** (focus on §3 implementation sequence, §5 new
   files, §6 coverage matrix)
2. **Approve a new branch** `feature/orch-server-hmac-v07` (or
   similar) for the actual implementation
3. **Schedule the implementation** per the v0.7 §12 operator-binding
   prerequisites (build host + signing cert + clean VM test
   environment + agent_id bound)
4. **Decide spec §3 migration option** (recommended: Option B
   dual-format with `HERMES_HMAC_ACCEPT_V06` env var)

This plan doc does NOT trigger any code changes, branch creation,
or production state mutations. It is a design artifact for the
next operator-binding phase.
