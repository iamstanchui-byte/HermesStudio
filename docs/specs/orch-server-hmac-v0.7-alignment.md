# Orch Server HMAC v0.7 Alignment Spec

**Date:** 2026-08-13
**Author:** Mavis (operator-approved direction: design server-side follow-up to v0.7 §1.4 client format)
**Status:** PROPOSAL for review
**Scope:** Align the orch server's HMAC verification with the v0.7 §1.4
bound-metadata model so the v0.7.1 bootstrapper (Draft 4) can enroll
and verify. Affects `/src/hermes_orch/auth/hmac.py` and a new
`/api/agents/{id}/status` endpoint.

---

## 0. Why this spec exists

The v0.7.1 bootstrapper (`installer/bootstrapper/install-orch-client.ps1`
Draft 4, commit `8cc85d7`) uses the v0.7 §1.4 bound-metadata HMAC
format to call `/api/agents/{id}/status` and verify enrollment. The
**orch server currently uses the v1.6 HMAC format** (per
`src/hermes_orch/auth/hmac.py` line 1-49, "v1.6, 2026-07-29") for the
2 agent-self routes (`POST /{id}/heartbeat` and `GET /{id}`). The v1.6
format and the v0.7 §1.4 format are **incompatible** — different
header names, different signature inputs, different encodings, different
authorization rules.

Without this alignment, the v0.7.1 bootstrapper cannot complete
enrollment. The orch server must be updated to:
1. Accept the v0.7 §1.4 format on the new `/api/agents/{id}/status`
   endpoint
2. (Optional) accept the v0.7 §1.4 format on the existing 2
   agent-self routes, replacing v1.6

This spec proposes the server-side changes. It does NOT implement them.

---

## 1. Authoritative specification: v0.7 §1.4

Per `docs/proposals/orch-client-build-impl-plan-v0.7.md` §1.4:

### 1.1 Request headers (7 headers)

| Header | Value |
|---|---|
| `X-Hermes-Method` | Uppercase HTTP method (`GET`, `POST`, ...) — **MUST equal the actual request method** (see §1.8) |
| `X-Hermes-Path` | Canonical path, **no query string, byte-exact match to request URL path** (per §1.7 + §1.8; v0.7 §1.4 "no query strings on signed endpoints") |
| `X-Hermes-Body-SHA256` | Lower-case hex SHA-256 of the raw request body bytes; `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` for GET (empty body) |
| `X-Hermes-Key-Id` | Operator-assigned HMAC key id (e.g. `key-2026-08-13-win-b-02`); server uses this to look up the agent |
| `X-Hermes-Timestamp` | Unix epoch seconds, decimal string |
| `X-Hermes-Nonce` | Per-request UUID hex (32 chars, `N` format); prevents replay |
| `X-Hermes-Signature` | Base64 of HMAC-SHA256 (NOT hex) |

### 1.2 Canonical string-to-sign

```
<METHOD>\n<PATH>\n<BODY_SHA256_HEX>\n<TIMESTAMP>\n<NONCE>
```

5 fields joined by `\n` (newline). PATH has no query string.

### 1.3 Signature

```
signature_b64 = base64( HMAC-SHA256(key=agent.hmac_secret, msg=canonical_input_bytes) )
```

The server recomputes this and compares with `X-Hermes-Signature` via
`hmac.compare_digest` (constant-time).

### 1.4 Server verification steps

1. All 7 headers present (else 401 with `MISSING_AUTH_HEADERS`)
   (handled by the dispatcher; see §1.9)
1a. **(Hardening Phase 1)** `X-Hermes-Method` MUST equal
   `request.method` (case-insensitive); `X-Hermes-Path` MUST equal
   `request.url.path` byte-exact (see §1.8). Mismatch → 401 with
   `MALFORMED_HEADERS`.
2. `X-Hermes-Timestamp` parses as int, `|now - ts| <= HMAC_WINDOW_SEC`
   (default 300s, same as v1.6; else 401 with `TIMESTAMP_OUT_OF_WINDOW`)
3. Look up agent by `X-Hermes-Key-Id` (NOT by URL `agent_id`); if not
   found, 401 with `UNKNOWN_KEY_ID`
4. Reject if URL `agent_id` does not match the agent bound to this
   `key_id` (per v0.7 §1.4 key-id-to-agent rule; else 403 with
   `KEY_AGENT_MISMATCH`)
5. Read the raw request body BEFORE Pydantic parses it; compute its
   SHA-256; reject if not equal to `X-Hermes-Body-SHA256` (else 401
   with `BODY_HASH_MISMATCH`)
6. Recompute the canonical input + signature; constant-time compare
   with `X-Hermes-Signature` (else 401 with `INVALID_SIGNATURE`)
7. Check the nonce has not been seen recently (in-memory LRU with TTL
   matching the timestamp window; else 401 with `NONCE_REPLAY`)
8. On success, return the `agent_id` (from the DB row, not the URL)

(All steps before "8" return 401; step 4 specifically returns 403
per spec §1.4 KEY_AGENT_MISMATCH rule.)

### 1.5 New endpoint: `GET /api/agents/{id}/status`

Per the v0.7.1 bootstrapper's `Wait-ForEnrollment`, the server needs a
new HMAC-authed endpoint that returns the agent's enrollment state:

```json
{ "status": "verified" | "pending" | "rejected" | "expired" }
```

The bootstrapper polls this every 5s for up to 60s. The HMAC
verification on this endpoint follows §1.1-1.4 above.

### 1.6 Key-id-to-agent authorization rule (v0.7 §1.4 specific)

The v1.6 server looks up agents by `X-Agent-Id` directly. v0.7 §1.4
changes this: the server looks up by `X-Hermes-Key-Id` (the key
points to the agent), then validates that the URL `agent_id` matches
the agent bound to that key. This prevents a compromised or
mis-provisioned key from impersonating a different agent.

The data model needs a new column on the `agents` table (or a
separate `agent_keys` table) that maps `key_id` → `agent_id`. For
first release, this can be a single optional column
`agents.hmac_key_id` (UNIQUE constraint). Multiple keys per agent
can be added later if rotation is needed.

### 1.7 Path canonicalization policy (added step 9, 2026-08-15)

The `X-Hermes-Path` header is the source of truth for the canonical
request path. Per the T12 acceptance test, deviations from the
canonical form are rejected (400 with `MALFORMED_HEADERS`):

| Form | Verdict | Reason |
|---|---|---|
| `/api/agents/{id}/status` | **accept** | Canonical form |
| `/api/agents/{id}/status/` | **reject** | Trailing slash; spec §1.1 forbids (root `/` is the only exception) |
| `/api/agents//{id}/status` | **reject** | Double slash; multiple consecutive separators not canonical |
| `/API/AGENTS/{ID}/STATUS` | **reject** | Case-sensitive; the URL path is byte-exact |
| `/api/agents/{id}/status?foo=bar` | **reject** | §1.4 forbids query strings on signed endpoints (400 `MALFORMED_HEADERS`) |

Implementation: the verifier checks the `X-Hermes-Path` value
*before* computing the signature. If the value is non-canonical,
the verifier returns 400 `MALFORMED_HEADERS` immediately. The
signature is never computed against a non-canonical path.

Cross-language invariant: the bootstrapper's `Wait-ForEnrollment`
MUST send the canonical form (no trailing slash, case-correct).
Verified by the cross-language compat test
(`tests/golden/hmac_v07_golden.json`).

### 1.8 Header-to-request binding (added hardening Phase 1, 2026-08-15)

The `X-Hermes-Method` and `X-Hermes-Path` headers are bound to the
actual HTTP request. The verifier checks them against `request.method`
and `request.url.path` respectively, byte-for-byte (with case-
insensitive comparison on the method per RFC 7230 §3.1.1).

Without this binding, an attacker could sign `POST /api/agents/A/heartbeat`
and send `GET /api/agents/B/status` with those headers — the
signature would still match (the X-Hermes-* headers are internally
consistent) but the request would be bound to a different agent
+ endpoint than the one the client actually sent. This defeats
the binding the canonical input is supposed to provide.

**Verdict table**:

| X-Hermes-Method vs request.method | X-Hermes-Path vs request.url.path | Verdict | Error code |
|---|---|---|---|
| match (case-insensitive) | match (byte-exact) | continue to next check | — |
| mismatch | any | **reject** | 401 `MALFORMED_HEADERS` |
| any | mismatch | **reject** | 401 `MALFORMED_HEADERS` |

Implementation: the verifier compares the headers against
`request.method` and `request.url.path` *before* the signature
compare (step 1b of the verifier flow, after the missing-header
check at step 1).

### 1.9 Mixed / partial header set rejection (added hardening Phase 1, 2026-08-15)

The dispatcher (`auth/dispatch.py`) rejects any request that has
EITHER:
- a v0.6 header AND a v0.7 header (mixed protocol)
- a partial v0.7 header set (1-6 of 7 X-Hermes-* headers)
- a partial v0.6 header set (1-2 of 3 X-Agent-Id / X-Timestamp /
  X-Signature headers)

All three cases are rejected with **401 `MIXED_HEADERS`** (the
single error code for "header set is not strictly v0.7-or-v0.6
and complete"). The previous dispatcher routed partial v0.7 sets
to the v0.7 verifier (which failed with `MISSING_AUTH_HEADERS` on
the missing headers), but this leaked the v0.7 header presence
to the v0.7 path and let an attacker fingerprint the server by
including a v0.6 header (which was silently ignored by the v0.7
verifier). Strict reject eliminates this attack surface.

**Verdict table**:

| v0.6 headers present | v0.7 headers present | Verdict | Error code |
|---|---|---|---|
| 0 | 0 | reject | 401 `MISSING_AUTH_HEADERS` |
| 0 | 7 | route to v0.7 verifier | — |
| 0 | 1-6 | **reject** | 401 `MIXED_HEADERS` |
| 1-3 | 0 | route to v1.6 verifier | — |
| 1-3 | 1-7 | **reject** | 401 `MIXED_HEADERS` |

Implementation: the dispatcher enumerates the present headers
from each format *before* routing. If both are present, or if
either format has a partial set, the dispatcher raises
`MIXED_HEADERS` immediately. The verifiers (v0.7 and v1.6) are
then guaranteed to receive a complete header set for their
format only.

### 1.10 Enrollment v07 state machine (added hardening Phase 3, 2026-08-15)

The `POST /api/enrollment/v07` endpoint is a **pre-provision
activation** — the operator pre-issues a row in the `agents`
table with `hmac_key_id` + `hmac_secret` + `status='verifying'`,
and the v0.7 endpoint transitions the row from `verifying` to
`verified` when the agent host proves possession of the secret
via the 7-header HMAC signature. The endpoint does NOT create
new agent rows (that path is the legacy `/api/agents/enroll`
which uses an enrollment token, NOT the v0.7 HMAC path).

**State machine** (the only allowed transitions):

```
  (pre-provision via operator)
              │
              ▼
         ┌──────────┐    POST /api/enrollment/v07     ┌──────────┐
         │ verifying│ ─────────────────────────────▶ │ verified │
         └──────────┘   200 + {status: verified}     └──────────┘
              │                                          │
              │ (operator action via admin UI /          │ (operator action)
              │  script, out of scope for v0.7)           ▼
              ▼                                       ┌──────────┐
         ┌──────────┐                                  │ blocked  │
         │ blocked  │                                  └──────────┘
         └──────────┘                                       │
              │ (operator action)                            ▼
              ▼                                       ┌──────────┐
         ┌──────────┐                                  │suspended │
         │suspended │                                  └──────────┘
         └──────────┘
```

**Allowed values of `agents.status`** (the canonical enum):

| Value | Meaning |
|---|---|
| `verifying` | Pre-provisioned; awaiting the v0.7 endpoint call to activate |
| `verified` | Active; the v0.7 endpoint has confirmed the secret |
| `blocked` | Operator-blocked; the row is preserved for audit but the agent cannot heartbeat or re-enroll |
| `suspended` | Operator-suspended; same as `blocked` but reversible (e.g. temporary maintenance) |

**Verdict table for `POST /api/enrollment/v07`** (the only allowed
starting state is `verifying`):

| Current `status` | Endpoint verdict | Error code |
|---|---|---|
| `verifying` | 200 + `{status: verified}` | — |
| `verified` | **reject** (already activated; idempotent re-enroll is not allowed; the bootstrapper should stop polling) | 409 `ENROLLMENT_STATE_CONFLICT` |
| `blocked` | **reject** | 409 `ENROLLMENT_STATE_CONFLICT` |
| `suspended` | **reject** | 409 `ENROLLMENT_STATE_CONFLICT` |
| (no row found by `hmac_key_id`) | **reject** | 401 `UNKNOWN_KEY_ID` (verifier step 5) |
| (any other value, including typos) | **reject** | 409 `ENROLLMENT_STATE_CONFLICT` |

**Implementation contract**:

1. The endpoint runs the v0.7 verifier (7 X-Hermes-* headers,
   `hmac_key_id` → `agent_id` lookup, signature verify, etc.) —
   the verifier runs **before** the state check, so an
   unauthenticated or replayed request never sees the status.

2. After the verifier returns the `auth_agent_id`, the endpoint
   runs a single atomic UPDATE:
   ```sql
   UPDATE agents
   SET status = 'verified', last_heartbeat_at = ?, ...
   WHERE id = ? AND status = 'verifying'
   ```
   If `rowcount == 0` (no row updated), the row was NOT in
   `verifying` state — return 409 `ENROLLMENT_STATE_CONFLICT`.
   The atomic UPDATE prevents two concurrent enrollments from
   both transitioning the same row.

3. The endpoint also validates the `status` enum at READ time
   (e.g. when populating the response): any non-enum value in
   the DB returns 500 `INVALID_AGENT_STATUS` (operator should
   fix the DB; the spec does not silently coerce).

4. The GET `/api/agents/{id}/status` endpoint validates the
   enum the same way and returns the `status` field unchanged.
   The bootstrapper's Wait-ForEnrollment poll stops on
   `status: verified`. Operators can observe `blocked` /
   `suspended` via the dashboard.

**Rationale for explicit `verifying` start state**: the
pre-Phase-3 endpoint accepted ANY status, including
`verified`. This meant a malicious or buggy agent could
re-enroll an already-verified row, resetting its
`last_heartbeat_at` and changing the `os_type` / `hostname`
fields. The `verifying`-only start state makes enrollment
single-shot per pre-provisioned row, and `ENROLLMENT_STATE_CONFLICT`
gives the operator a clear signal when something tries to
re-enroll.

**Backward compatibility**: existing agents (the 2 known
production agents `win-local-1` + `linux-a-01` on the v1.6
HMAC path) are NOT affected. They never call the v0.7
enrollment endpoint. New agents via the bootstrapper follow
the new flow: operator pre-provisions with `status=verifying`,
agent calls the v0.7 endpoint, row transitions to `verified`.

### 1.11 AgentStatus enum + polling contract (added hardening Phase 6, 2026-08-15)

The `agents.status` field has a fixed 4-value enum. The
`GET /api/agents/{id}/status` endpoint and the v0.7 enrollment
endpoint both validate the value against this enum at READ
time; any value not in the enum returns 500 `INVALID_AGENT_STATUS`
(operator must fix the DB row; the server does not silently
coerce).

**The enum** (canonical, used in Python `Literal` and as the
source of truth for the v0.7 verifier + status endpoint):

```python
AgentStatus = Literal["verifying", "verified", "blocked", "suspended"]
```

| Value | When set | Bootstrapper behavior |
|---|---|---|
| `verifying` | Operator pre-provision, before v0.7 endpoint call | Keep polling (5s × up to 60s) |
| `verified` | v0.7 endpoint call succeeded | **Stop polling, exit 0** |
| `blocked` | Operator action (admin UI / script) | **Stop polling, exit non-zero with BLOCKED status** |
| `suspended` | Operator action (admin UI / script) | **Stop polling, exit non-zero with SUSPENDED status** |

**Polling contract** (the bootstrapper's `Wait-ForEnrollment`
loop):

- **Endpoint**: `GET /api/agents/{id}/status`
- **Poll interval**: 5 seconds (fixed; do not exponential-backoff
  — the spec deliberately keeps this simple so the bootstrapper
  can complete in 60s worst case)
- **Total timeout**: 60 seconds (12 polls × 5s)
- **Auth**: v0.7 §1.4 HMAC, full 7 headers
- **Response shape**: `{"agent_id": str, "status": "verifying"|"verified"|"blocked"|"suspended", "last_heartbeat_at": str|null}`
- **On `status=verified`**: stop polling, exit 0
- **On `status=blocked` or `status=suspended`**: stop polling,
  exit non-zero with a human-readable error. The bootstrapper
  surfaces this to the operator; the agent host is not enrolled
  and the operator must take action (re-provision or unblock)
- **On any other `status` value (e.g. `pending` from legacy
  schemas, or a typo)**: the server returns **500
  `INVALID_AGENT_STATUS`** with a generic message. The
  bootstrapper treats this as a server-side bug and exits
  non-zero. The operator should file a bug; the spec does
  not allow the server to silently coerce unknown values to
  `verifying` or `unknown`
- **On network error / TLS error / HMAC error**: the
  bootstrapper retries (the 60s window is generous enough to
  cover 1-2 transient errors)
- **On `ENROLLMENT_STATE_CONFLICT` (409)**: this is impossible
  in the polling flow (the bootstrapper doesn't POST to
  `/api/enrollment/v07` directly — the operator pre-provisions
  the row with `status=verifying`, and the v0.7 endpoint
  transitions it). If seen, treat as a server-side bug

**Server-side validation rules**:

1. `GET /api/agents/{id}/status` reads the `status` field
   from the DB. If the value is not one of the 4 enum values
   (case-sensitive exact match), the endpoint returns **500
   `INVALID_AGENT_STATUS`** — NOT `200` with a coerced value,
   NOT `404`. The 500 is fail-closed: the bootstrapper must
   not treat unknown statuses as `verified`.

2. The v0.7 enrollment endpoint's `ENROLLMENT_STATE_CONFLICT`
   check (spec §1.10) uses the same enum. Any non-enum value
   in the row at enrollment time also returns 409 (the row
   must be `verifying` to transition; anything else is
   conflict).

3. The `agents.status` DB column is `TEXT NOT NULL DEFAULT
   'verifying'`. The DB does NOT enforce the enum (SQLite
   ALTER TABLE ADD CHECK is unsupported; the spec relies on
   app-layer validation per the existing convention in
   `db.py`). The app-layer validation lives in
   `hermes_orch.core.agent_status.validate_agent_status()`.

**Why the 500 fail-closed**: a typo'd status in the DB (e.g.
`verfid` from a manual SQL update) would otherwise leak
through as `200 + {status: "verfid"}`. The bootstrapper
strict-compares against `"verified"`, so the typo would
cause the bootstrapper to keep polling past 60s and time out
— which is the *less* dangerous failure mode. But the
operator wouldn't know there's a DB bug; the 500 surfaces
the issue immediately with a structured error code.

### 1.12 Unified error JSON contract (added hardening Phase 5, 2026-08-15)

All 4xx + 5xx responses from the v0.7 endpoints use a unified
JSON shape:

```json
{
  "error": "ERROR_CODE",
  "message": "human readable message",
  "request_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

The `X-Request-Id` response header carries the same UUID as
`request_id` for cross-system log correlation.

**Legacy `detail` field (preserved for backward compat)**:

The pre-Phase-5 format was `{"detail": "ERROR_CODE: human message"}`
in a single string. The legacy `detail` field is preserved
with the same content for backward compatibility with
existing clients (the bootstrapper's `Wait-ForEnrollment`
and the dashboard's existing error parsing). New code
should use the `error` + `message` pair; the legacy
`detail` field is deprecated and may be removed in a
future major version (>=2.0).

**Why a unified contract**:

- The previous design used `{"detail": "ERROR_CODE: human message"}`
  and required clients to `detail.split(": ")[0]` to extract
  the error code. This is fragile: the split breaks if a `:`
  appears in the human message (e.g. an IPv6 address in the
  message body), and the `detail` field is overloaded (both
  the error code and the human message in one string).
- The new contract separates the code (`error`) from the
  human message (`message`), adds a `request_id` for
  cross-system log correlation, and uses the standard
  FastAPI exception handler pattern.
- The split-on-`": "` pattern is preserved as a fallback
  for clients that haven't migrated to the new field yet.

**Wire format by status code**:

| Status | Body shape |
|---|---|
| 200 | data fields only (e.g. `{"agent_id": "...", "status": "verified"}`) |
| 400 / 401 / 403 / 404 / 409 | `{"error": "CODE", "message": "...", "request_id": "uuid"}` + legacy `{"detail": "CODE: ..."}` |
| 500 | `{"error": "INTERNAL_SERVER_ERROR" or "INVALID_AGENT_STATUS", "message": "...", "request_id": "uuid"}` + legacy `{"detail": "..."}` |

**Error code registry** (single source of truth, all
HTTPException-raised codes use the `ERROR_CODE: message`
detail format which the exception handler parses):

| Code | Where raised |
|---|---|
| `MISSING_AUTH_HEADERS` | v0.7 verifier step 1 (header missing) |
| `MALFORMED_HEADERS` | v0.7 verifier steps 1b/3 (method/path/query) |
| `TIMESTAMP_OUT_OF_WINDOW` | v0.7 verifier step 2 |
| `UNKNOWN_KEY_ID` | v0.7 verifier step 5 |
| `INVALID_SIGNATURE` | v0.7 verifier step 6 |
| `NONCE_REPLAY` | v0.7 verifier step 7 (atomic add_if_absent) |
| `KEY_AGENT_MISMATCH` | v0.7 status endpoint (defense in depth) |
| `BODY_HASH_MISMATCH` | v0.7 verifier step 4 |
| `MIXED_HEADERS` | dispatcher (Phase 1) |
| `ENROLLMENT_STATE_CONFLICT` | v0.7 enrollment endpoint (Phase 3) |
| `INVALID_AGENT_STATUS` | v0.7 status endpoint (Phase 6) |
| `HTTP_ERROR` | generic fallback when detail has no `:` separator |

**Implementation**:

- `hermes_orch.core.error_contract.parse_error_detail(detail)`
  parses the `"CODE: message"` string into `(code, message)`
- `hermes_orch.core.error_contract.make_error_response(
    code, message, request_id, status_code)` builds the
  JSON response with both new fields and the legacy `detail`
- `hermes_orch.main.request_id_middleware` generates a UUID4
  per request, attaches it to `request.state.request_id` and
  the `X-Request-Id` response header
- `hermes_orch.main.custom_http_exception_handler` registered
  via `app.add_exception_handler(HTTPException, ...)` parses
  the detail string and returns the unified shape

### 1.13 HERMES_HMAC_ACCEPT_V06 flag + deprecation mechanism (added hardening Phase 4, 2026-08-15)

The dispatcher (`auth/dispatch.py`) accepts BOTH v0.6 and v0.7
HMAC formats on the 2 dual-format routes (heartbeat, GET /{id})
during the migration window (per spec §3 Option B). After the
operator has fully migrated all agents to v0.7, the v0.6 path
should be disabled. This section defines the deprecation
mechanism.

**Two env vars** (both read at startup; not per-request):

| Env var | Type | Default | Meaning |
|---|---|---|---|
| `HERMES_HMAC_ACCEPT_V06` | bool (`true` / `false` / `1` / `0` / `yes` / `no`) | `true` | If `true`, dispatcher accepts v0.6 HMAC format. If `false`, v0.6 requests are rejected with 401 `V0_6_DEPRECATED`. |
| `HERMES_HMAC_DEPRECATION_DATE` | ISO 8601 date (`YYYY-MM-DD`) | unset | If set and the date is in the past, the server logs a deprecation warning on startup and tags every v0.6 request log with a `[DEPRECATION]` marker. |

**Verdict table for v0.6 (X-Agent-Id) requests on dual-format routes**:

| `HERMES_HMAC_ACCEPT_V06` | Verdict | Response |
|---|---|---|
| `true` (default) | route to v1.6 verifier | 200 + normal response |
| `false` | reject | 401 `V0_6_DEPRECATED: v0.6 HMAC format is disabled; use v0.7 (X-Hermes-* headers) — see docs/specs/orch-server-hmac-v0.7-alignment.md` |

**Cutover plan** (per the operator's day-30/60/90 deprecation window):

1. **Day 0**: this branch lands; `HERMES_HMAC_ACCEPT_V06` defaults to `true`. All v0.6 + v0.7 requests work.
2. **Day 30** (or operator's chosen date): operator sets `HERMES_HMAC_DEPRECATION_DATE=YYYY-MM-DD` 30 days in the future. Server starts logging a deprecation warning on startup; each v0.6 request is tagged with `[DEPRECATION]`. The operator uses these logs to identify any remaining v0.6 clients.
3. **Day 60**: operator sets `HERMES_HMAC_ACCEPT_V06=false`. v0.6 requests now return 401 `V0_6_DEPRECATED`. The operator monitors logs to ensure no production traffic is using v0.6; if any is, the operator coordinates with the agent host to upgrade.
4. **Day 90**: operator removes the v0.6 verifier from `auth/hmac.py` and the dispatcher from `auth/dispatch.py` (separate PR; one final v0.7-only cutover). At this point the migration is complete.

**Why a flag, not just removing v0.6 code**:

- Removing v0.6 support in one PR is too disruptive — any
  agent still using v0.6 would 401 immediately. The flag
  gives the operator a soft cutover with monitoring.
- The flag is the standard 12-factor pattern: behavior
  changes via env var, no code change required to flip
  the behavior. Easy to roll back (set `true` again).

**Why a deprecation date env var**:

- Forces the operator to commit to a cutover date (not
  "someday"). The server logs the warning on startup so
  the operator sees it daily in their log review.
- Tags each v0.6 request so the operator can count them
  via log analysis. If the count drops to 0 before the
  flag flips, the operator can safely proceed.

**Implementation**:

- `hermes_orch.auth.hmac._read_accept_v06()` reads the env
  var at startup (and on each request — env var can change
  without restart in a development context; in production,
  a process restart is fine since the v0.6 cutover is a
  planned event).
- `hermes_orch.auth.hmac._read_deprecation_date()` reads
  the ISO date and returns a `datetime.date` (or None).
- `hermes_orch.main.lifespan()` logs a startup warning if
  the deprecation date is set and in the past.
- `hermes_orch.auth.dispatch.dispatch_hmac_auth` wraps
  the v0.6 path in a flag check; on `false`, raise
  `HTTPException(401, "V0_6_DEPRECATED: ...")`.

**Test coverage**:

- Flag=`true` (default): v0.6 request works on the
  dual-format routes (heartbeat, GET /{id})
- Flag=`false`: v0.6 request returns 401 `V0_6_DEPRECATED`
- Flag state does NOT affect v0.7 requests (v0.7 always
  works)
- Deprecation date in the past: startup log emits a
  deprecation warning (verifiable via caplog)

**Production safety**:

- Default `HERMES_HMAC_ACCEPT_V06=true` preserves the
  pre-Phase-4 behavior — no production change unless the
  operator flips the flag.
- The deprecation date env var is informational; the
  flag is the actual cutover control.
- The 401 `V0_6_DEPRECATED` error code is documented in
  spec §1.12 and surfaces in the unified error contract.

---

## 2. Current v1.6 implementation (what changes)

`src/hermes_orch/auth/hmac.py` lines 1-265 implement the v1.6 format:

| Aspect | v1.6 (current) | v0.7 §1.4 (target) | Change scope |
|---|---|---|---|
| Header count | 3 (X-Agent-Id, X-Timestamp, X-Signature) | 7 (X-Hermes-*) | new headers added |
| Agent lookup | by `X-Agent-Id` | by `X-Hermes-Key-Id` + URL match | new data model column |
| Signature input fields | 4 (method, path, body_hash, timestamp) | 5 (method, path, body_hash, timestamp, nonce) | +nonce field |
| Signature encoding | hex | base64 | encoding change |
| Path | includes query string | NO query string | path normalization |
| Nonce | none (timestamp window only) | required, replay-protected | new LRU + TTL store |
| Body hash verification | implicit (server signs with the raw body) | explicit (`X-Hermes-Body-SHA256` header checked) | new check |
| `hmac_secret` storage | plaintext in DB (per `hmac.py:42-48` "Threat model: plaintext in DB") | unchanged (out of scope for this spec) | no change here |
| Legacy mode | `HERMES_HMAC_REQUIRED=false` allows no-signature auth | TBD; could be removed or repurposed | needs operator decision |

### 2.1 What does NOT change

- `hmac_secret` storage format (plaintext in DB) — out of scope; tracked
  in `security/agent-secret-at-rest` (B11) as a separate design track
- 7 admin-gated routes (B12 hotfix; not affected by HMAC refactor)
- The 410 Gone for B10 (`rotate-key`)
- HMAC window default (300s)
- The enrollment endpoint (`/api/enrollment`) which is anonymous
  and does not use HMAC

---

## 3. Migration options

The operator (per the red lines) MUST decide between 3 options. This
spec recommends **Option B** (dual-format with version detection) for
the v1.6 → v0.7 transition, then cutover to v0.7-only after a
deprecation window.

| Option | Behavior | Pros | Cons | Recommendation |
|---|---|---|---|---|
| **A. Hard cutover** | Server accepts v0.7 only; v1.6 requests get 401 | Simple code; one path | Breaks any existing agent using v1.6; requires re-bootstrapping all agents | NO — too disruptive for the 2 known agents (`win-local-1`, `linux-a-01`) |
| **B. Dual-format (version detection)** | Server detects v0.7 by presence of `X-Hermes-Method` header; v1.6 requests (no `X-Hermes-*` headers) still work; v0.7 requests on the new endpoint work | Both formats work side-by-side; the 2 existing agents stay alive; new agents use the bootstrapper; clean cutover at a later date | Slightly more code; v0.7 must be reachable on the new endpoint; v1.6 stays on the old 2 routes | **YES** — preserves production |
| **C. Format negotiation** | Client sends `Accept-Signature: v0.7` header; server picks the right verifier | Future-proof; supports incremental rollout of v0.8 etc. | Overkill for 2 known clients; the v1.6 → v0.7 transition is small enough that dual-format suffices | NO — over-engineered for the current scale |

**Recommended migration (Option B)**:
1. Server implements v0.7 verification alongside v1.6
2. The new `/api/agents/{id}/status` endpoint is **v0.7 only**
3. The 2 existing routes (`heartbeat`, `GET /{id}`) accept BOTH
   v1.6 and v0.7 (per the dual-format path)
4. Operator can deprecate v1.6 later by setting a flag
   `HERMES_HMAC_REQUIRE_V07=true`; v1.6 requests then get 401
5. After a deprecation window (e.g. 30 days of stable v0.7), remove
   v1.6 support entirely (one PR)

The dual-format path is gated by a new env var
`HERMES_HMAC_ACCEPT_V06` (default `true` during the transition; the
operator flips to `false` after deprecation).

---

## 4. New HMAC implementation (server-side, high-level)

This section describes what the new `hmac_v07.py` module should
contain. It mirrors the v0.7.1 bootstrapper's `Wait-ForEnrollment`.

### 4.1 Module: `src/hermes_orch/auth/hmac_v07.py`

```python
# coding: utf-8
"""HMAC-SHA256 v0.7 §1.4 agent authentication (bound-metadata model).

Companion to the v1.6 implementation in hmac.py. v0.7 §1.4 specifies:
  - 7 headers (X-Hermes-Method, X-Hermes-Path, X-Hermes-Body-SHA256,
    X-Hermes-Key-Id, X-Hermes-Timestamp, X-Hermes-Nonce, X-Hermes-Signature)
  - canonical input: METHOD\nPATH\nBODY_SHA256_HEX\nTIMESTAMP\nNONCE
  - signature: base64(HMAC-SHA256(secret, canonical_input))
  - agent lookup: by X-Hermes-Key-Id + URL agent_id match
  - path excludes query string (v0.7 §1.4 "no query strings on signed endpoints")
  - nonce is replay-protected via in-memory LRU with TTL
"""
```

Key functions (per the bootstrapper's contract):
- `string_to_sign_v07(method, path, body_sha256_hex, timestamp, nonce) -> str`
- `compute_signature_v07(secret, ...) -> str` (base64-encoded)
- `verify_signature_v07(secret, ..., provided_signature) -> bool`
- `require_hmac_auth_v07(request) -> str` (FastAPI dependency; returns agent_id)
- `check_nonce_replay(nonce, ttl_seconds) -> bool` (LRU-backed; default TTL 300s)
- `lookup_agent_by_key_id(key_id) -> Optional[AgentRow]`

### 4.2 Module: `src/hermes_orch/api/agent_status.py`

The new endpoint:
```python
@router.get("/api/agents/{agent_id}/status", dependencies=[Depends(require_hmac_auth_v07)])
async def get_agent_status(agent_id: str, request: Request) -> dict:
    """Return the agent's enrollment state for the bootstrapper's
    Wait-ForEnrollment poll. Returns {"status": "verified"|"pending"|...}.
    HMAC verification is done by the dependency; this handler just
    queries the DB."""
    row = await request.app.state.db.fetchone(
        "SELECT enrollment_status FROM agents WHERE id = ?", (agent_id,)
    )
    if not row:
        raise HTTPException(404, f"Agent {agent_id} not found")
    return {"status": row["enrollment_status"]}
```

### 4.3 Data model change

A new column on the `agents` table:
```sql
ALTER TABLE agents ADD COLUMN hmac_key_id TEXT;  -- NULL for v1.6 agents
CREATE UNIQUE INDEX idx_agents_hmac_key_id ON agents(hmac_key_id);
```

For backward compat, `hmac_key_id` is NULL for existing agents; the
dual-format path serves them via v1.6 (`X-Agent-Id` lookup). For new
agents, the bootstrapper's Wait-ForEnrollment includes a step that
sends the key_id during enroll (the existing anonymous enroll endpoint
needs a new v0.7 variant that takes key_id).

### 4.4 Enrollment integration

The existing `/api/enrollment` endpoint is anonymous. The v0.7
bootstrapper's flow is:
1. Bootstrapper sends `POST /api/enrollment` with `enrollment_token`
   + `agent_id` + `key_id` + HMAC signature
2. Server validates the token, sets `agents.hmac_key_id` to the
   provided `key_id`, marks the agent as enrolled, returns success
3. Bootstrapper then polls `GET /api/agents/{id}/status` with HMAC
   signature to confirm `verified`

The existing enrollment endpoint needs a v0.7 variant that takes
`key_id` and signs the request. For first release, the simplest is:
- Add a new endpoint `POST /api/enrollment/v07` that accepts the
  same fields as the existing one plus `key_id`, with HMAC headers
- The bootstrapper uses the v0.7 variant; old agents use the v1.6
  variant

(Alternative: extend the existing enrollment endpoint to optionally
take HMAC headers; HMAC present → v0.7 path; absent → anonymous v1.6
path. Simpler for the client; same complexity on the server.)

---

## 5. Error responses

All v0.7 HMAC errors return a 4xx with a JSON body. The bootstrapper
maps these to plain-English messages (per `installer/bootstrapper/
install-orch-client.ps1` §3 plain-English error table).

| Status | Body | Bootstrapper plain-English error |
|---|---|---|
| 400 | `{"error": "MALFORMED_HEADERS", "detail": "..."}` | (rare; bad header format) |
| 401 | `{"error": "MISSING_AUTH_HEADERS", "detail": "Missing X-Hermes-* headers"}` | "The orchestrator rejected the request..." |
| 401 | `{"error": "INVALID_TIMESTAMP", "detail": "..."}` | "The orchestrator rejected the request..." |
| 401 | `{"error": "TIMESTAMP_OUT_OF_WINDOW", "detail": "..."}` | "..." |
| 401 | `{"error": "UNKNOWN_KEY_ID", "detail": "..."}` | "The orchestrator rejected the request..." |
| 401 | `{"error": "BODY_HASH_MISMATCH", "detail": "..."}` | "..." |
| 401 | `{"error": "INVALID_SIGNATURE", "detail": "..."}` | "..." |
| 401 | `{"error": "NONCE_REPLAY", "detail": "..."}` | "..." |
| 403 | `{"error": "KEY_AGENT_MISMATCH", "detail": "URL agent_id does not match the agent bound to this key"}` | "..." |
| 404 | `{"error": "AGENT_NOT_FOUND", "detail": "..."}` | "..." |
| 500 | `{"error": "INTERNAL", "detail": "..."}` | "..." |

The bootstrapper doesn't currently distinguish between these 4xx codes
(per the v0.7.1 §0.af-bootstrap error table, all 4xx map to plain-
English "orchestrator rejected" or specific user-action messages). For
first release, this granularity is sufficient.

---

## 6. Test cases (acceptance criteria)

The implementation must pass these on a clean Windows 10/11 target
+ clean Python 3.14 venv + a local orchestrator. Test cases
per the v0.7.1 §9 row O matrix.

| ID | Scenario | Expected |
|---|---|---|
| T1 | Happy path: bootstrapper signs a GET `/api/agents/win-local-1/status` with valid key_id, valid secret, valid timestamp + nonce, valid path | 200 + `{"status": "verified"}` |
| T2 | Missing `X-Hermes-Method` header | 401 MISSING_AUTH_HEADERS |
| T3 | Missing `X-Hermes-Signature` header | 401 MISSING_AUTH_HEADERS |
| T4 | Timestamp 600s in the past | 401 TIMESTAMP_OUT_OF_WINDOW |
| T5 | Timestamp 600s in the future | 401 TIMESTAMP_OUT_OF_WINDOW |
| T6 | Unknown `X-Hermes-Key-Id` | 401 UNKNOWN_KEY_ID |
| T7 | `X-Hermes-Key-Id` exists but URL `agent_id` doesn't match the bound agent | 403 KEY_AGENT_MISMATCH |
| T8 | Body hash mismatch (sign with one body, send another) | 401 BODY_HASH_MISMATCH |
| T9 | Signature mismatch (sign with one secret, verify with another) | 401 INVALID_SIGNATURE |
| T10 | Nonce replay (send same nonce twice within the window) | 401 NONCE_REPLAY on the second request |
| T11 | Query string on signed endpoint | 400 MALFORMED_HEADERS or rejected (v0.7 §1.4 forbids) |
| T12 | Path normalization: extra slashes, case differences, URL encoding | server normalizes per the same rule the client uses |
| T13 | Dual-format: v1.6 request (X-Agent-Id) on `POST /heartbeat` | works (per Option B migration) |
| T14 | Dual-format: v0.7 request (X-Hermes-*) on `POST /heartbeat` | works (per Option B migration) |
| T15 | Bootstrapper Wait-ForEnrollment against a real orch | enrollment poll returns `status=verified` within 60s |
| T16 | Cert mismatch (bootstrapper's TLS pin rejects the orch's cert) | bootstrapper throws CERT_MISMATCH (orch never sees the request) |

---

## 7. Backward compatibility / deprecation timeline

| Day | Action | Backward compat |
|---|---|---|
| Day 0 (merge) | Server implements v0.7 (alongside v1.6); `POST /api/agents/{id}/status` is v0.7-only; `heartbeat` and `GET /{id}` accept both | v1.6 still works (default `HERMES_HMAC_ACCEPT_V06=true`) |
| Day 0-30 | v0.7 in use by the bootstrapper on new agents; v1.6 still in use by the 2 existing agents | both formats work |
| Day 30 | Operator sets `HERMES_HMAC_ACCEPT_V06=false`; v1.6 requests get 401 with `DEPRECATED_V06` | v1.6 still works for 30 days then stops |
| Day 60 | v1.6 code path removed from `hmac.py`; `HERMES_HMAC_ACCEPT_V06` env var is a no-op | v0.7 only |

---

## 8. Open questions / out of scope

1. **Body hash verification** — the v0.7 §1.4 spec says the body is
   included in the signature, but for GETs the body is empty. v0.7
   also adds the `X-Hermes-Body-SHA256` header for explicit
   body-hash binding. Does the server reject requests where
   `X-Hermes-Body-SHA256` ≠ SHA-256(body), or is it informational?
   **Proposed**: reject (T8 above).
2. **Nonce store TTL** — the LRU is in-memory; if the server
   restarts, all nonces are forgotten. An attacker could replay a
   request whose nonce was seen before the restart, if the timestamp
   is still within the window. **Proposed**: the LRU is in-memory
   for first release; the operator can add a Redis-backed store
   later. The risk is bounded by the 5-minute timestamp window.
3. **hmac_secret storage** — the v1.6 comment says "plaintext in DB"
   (line 42-48). B11 (`security/agent-secret-at-rest`) is the
   separate design track for encrypted-at-rest. **Out of scope here.**
4. **Path canonicalization** — what does the server do with
   `/api/agents//win-local-1/status` (double slash), or
   `/API/AGENTS/WIN-LOCAL-1/STATUS` (uppercase)? The v0.7 spec is
   silent. **Proposed**: server rejects paths that don't match the
   exact canonical form the client signed; the client is expected
   to send the canonical form. (T12 above.)
5. **Backward compat for `/api/enrollment`** — does the existing
   anonymous enrollment endpoint stay, or does it become HMAC-only?
   **Proposed**: keep the anonymous endpoint for v1.6 agents; add a
   new HMAC-signed v0.7 variant for bootstrapper-enrolled agents.
   (Section 4.4 above.)

---

## 9. Operator action requested

This spec is a design doc, NOT an implementation. The next step is
the operator (the same person who approved the v0.7.1 bootstrapper)
to:
1. **Review this spec** (focus on §3 migration options, §4 new
   implementation, §5 error responses, §6 test cases)
2. **Decide on §3 migration option** (recommended: Option B
   dual-format, with `HERMES_HMAC_ACCEPT_V06` flag)
3. **Approve a new branch** (`feature/orch-server-hmac-v07` or
   similar) for the server-side implementation
4. **Schedule implementation** per the v0.7 §12 operator-binding
   prerequisites (build host + signing cert + clean VM test
   environment + agent_id bound)

The v0.7.1 bootstrapper (Draft 4, commit `8cc85d7`) is **frozen**
and will not change. The server-side work is a separate branch +
PR + VM test matrix.

This spec doc does NOT trigger any code changes, branch creation,
or production state mutations. It is a design artifact for the next
operator-binding phase.
