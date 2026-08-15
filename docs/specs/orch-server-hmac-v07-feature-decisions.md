# Orch Server HMAC v0.7 Implementation — Feature Decisions & Operator Binding

**Branch:** `feature/orch-server-hmac-v07` (forked from `main` @ `c9f0301`)
**Authoritative spec:** [`orch-server-hmac-v0.7-alignment.md`](orch-server-hmac-v0.7-alignment.md) (on `proposal/orch-server-hmac-v0.7-alignment`, PR #5, OPEN)
**Implementation plan:** [`orch-server-hmac-v0.7-impl-plan.md`](orch-server-hmac-v0.7-impl-plan.md) (same PR #5)
**Status (2026-08-15):** SCAFFOLD ONLY — 0 of 9 impl-plan steps done. No code in `src/hermes_orch/`.
**Production impact:** NONE — new branch, no deploy. Per memory, every production mutation requires explicit operator go.

---

## 0. Why this branch exists

The orch server's v0.7 §1.4 HMAC verification (7 `X-Hermes-*` headers, base64 signature, key-id-to-agent authorization rule, nonce for replay protection) is required for the v0.7.1 bootstrapper's `Wait-ForEnrollment` poll to succeed. The orch server currently uses the v1.6 HMAC format (`src/hermes_orch/auth/hmac.py`, 3 headers `X-Agent-Id` / `X-Timestamp` / `X-Signature`, hex encoding, 4-field string-to-sign, no nonce, lookup by `X-Agent-Id` not by key_id).

**Without this implementation, the bootstrapper's enrollment poll gets 401** — `Wait-ForEnrollment` produces a v0.7-format signed request, the orch only knows v1.6, the v1.6 verifier looks for `X-Agent-Id` and never finds it, the request is rejected.

The v0.7.1 / v0.7.2 / v0.7.3 bootstrapper patches (commits `1f4705c`, `c046593`, `804f5aa` on `proposal/orch-client-build-impl-plan-v0.1`, PR #4) are already merged-ready. The orch server side is the missing piece.

---

## 1. Design decisions (operator sign-off requested)

Per the impl plan §1-8 and the spec §1-7, the following decisions are made by Mavis (Mavis agent) on the operator's behalf ("我不懂 HMAC 的 setup 你拿个主意吧" — 2026-08-15). Operator sign-off is requested before implementation begins (i.e. before step 2 of the 9-step plan).

| # | Decision | Recommendation | Alternative | Reference |
|---|---|---|---|---|
| 1 | Migration option | **Option B: dual-format with `HERMES_HMAC_ACCEPT_V06` env var** (default `true` during transition; flip to `false` at Day 30; remove v1.6 code at Day 60) | A: hard cutover; C: format negotiation | spec §3, impl plan §4 step 6 |
| 2 | Data model | New column `agents.hmac_key_id` UNIQUE (alongside existing `hmac_secret`) | Separate `agent_keys` table | spec §4, impl plan §5 |
| 3 | Enrollment endpoint | New `POST /api/enrollment/v07` (HMAC-signed variant) | Extend existing endpoint | spec §4, impl plan §5 |
| 4 | Status endpoint | New `GET /api/agents/{id}/status` (returns `{status, last_seen, ...}`) | Reuse existing `GET /api/agents/{id}` | spec §4, impl plan §5 |
| 5 | Nonce store | In-process `InMemoryNonceStore` (TTL-based eviction) for Day 5+ impl; production Redis later | Skip nonce (replay window only) | spec §4, impl plan §5 |
| 6 | Body hash policy | Server rejects request if `X-Hermes-Body-SHA256` mismatches actual body (v0.7.1 trust model) | Trust client-provided hash | spec §1, impl plan §3 T8 |
| 7 | Query strings on signed endpoints | Server rejects (v0.7 §1.4 forbids) | Allow (with canonical query string in signing input) | spec §1, impl plan §3 T11 |
| 8 | Path canonicalization | Server uses raw path as-is (no normalization) | URL-decode + lowercase + collapse slashes | spec §8 (open question); impl plan §7 |
| 9 | Deprecation timeline | Day 0 (merge) → Day 0-30 (both formats, `HERMES_HMAC_ACCEPT_V06=true`) → Day 30 (flip env to `false`) → Day 60 (remove v1.6 code) | Shorter (7-day) or longer (90-day) window | spec §7, impl plan §4 step 6-8 |
| 10 | Test build agent_id | `bootstrapper-test-01` (default; operator can override at step 1) | Random uuid | this doc §2 |

**Operator sign-off needed for: ALL 10 decisions above**, especially #1 (migration option), #2 (data model), #4 (new endpoint), #9 (deprecation timeline).

---

## 2. Operator binding (per the rollout-approval contract)

Per the memory: "Plan approval ≠ rollout approval — future rollout approval must bind: exact target hostname, exact installer SHA-256, exact agent_id, exact endpoint, exact token handling, exact firewall action, exact verification/rollback commands." The values below are Mavis's defaults; operator can override any of them.

| Field | Value (Mavis default) | Notes |
|---|---|---|
| Target hostname | `192.168.2.152:8765` | Production orch (HTTPS now enabled per A on 2026-08-15) |
| agent_id (test) | `bootstrapper-test-01` | Distinct from production agents (`win-local-1`, `linux-a-01`) |
| key_id (test) | `key-bootstrapper-test-01` | Distinct from production key_ids |
| HMAC secret (test) | `(operator-generated 32 random bytes, base64-encoded; NOT stored in repo)` | Operator generates at step 1 of impl; never committed |
| Cert fingerprint (pinned) | `9eda254f18ddfc6335349f23b21868a1ecafab0fc9e784316b1e8c5b66472d42` | SHA-256 of orch's `server.crt` DER bytes; verified 2026-08-15 |
| Endpoint (existing, dual-format) | `POST /api/heartbeat` + `GET /api/agents/{id}` (v1.6 format, both kept working) | spec §3 Option B |
| Endpoint (new, v0.7 format) | `GET /api/agents/{id}/status` + `POST /api/enrollment/v07` | spec §4 |
| Token handling | `agents.enrollment_token` (existing column, v1.6 path) + `agents.hmac_key_id` (new column, v0.7 path) | spec §4 |
| Firewall action | NONE (existing firewall rule from A on 2026-08-15 covers HTTPS outbound; no change) | Already enabled |
| Verification commands | `curl -k https://192.168.2.152:8765/api/health` (HTTP 200); `curl -k https://192.168.2.152:8765/api/agents/{id}/status` (HMAC-signed, 200) | Per memory, PS 5.1 `Invoke-WebRequest` is broken for TLS 1.2; use `curl.exe` |
| Rollback command | `git revert <merge-sha>` + restart `HermesOrchServer` (NSSM); spec §3 Option B default keeps v1.6 working, so rollback is a 1-commit revert | N/A — pre-merge is on a feature branch |

---

## 3. Production state at scaffold time (2026-08-15)

Read-only verification, not modified by this scaffold:

| Component | State |
|---|---|
| `HermesOrchServer` NSSM service | Running (PID changed to 46440 after A) |
| `HermesOrchServer-Watchdog` | state=Ready, healthy |
| HTTPS | Enabled (`https.enabled: true`, `public_origin: 'https://...'`) |
| Server cert | Self-signed, RSA 2048, 365-day (expires 2027-08-03, 352 days left) |
| HMAC format | v1.6 (3 headers, hex, no nonce) |
| Existing HMAC endpoints | `POST /api/heartbeat`, `GET /api/agents/{id}` (both v1.6) |
| B12 hotfix | Merged (`c9f0301`); admin-gated routes protected |
| R7-C DB-path | Merged; DB at `C:\ProgramData\HermesOrchestrator\config\hermes-orch.db` |
| Bootstrapper (PR #4) | v0.7.3 ready (Draft 6: real TLS cert fingerprint pinning); 18 commits |

---

## 4. 9-step impl plan (from impl plan §4)

Per the impl plan, the 9-step TDD red-green-refactor sequence is:

| # | Step | Status | ETA | Notes |
|---|---|---|---|---|
| 0 | Branch scaffold (this file) | ✅ DONE (2026-08-15) | — | This document; no code |
| 1 | Operator binding: agent_id, key_id, HMAC secret, cert fingerprint | ⏳ PENDING operator action | 5 min | Operator generates HMAC secret (NOT in repo); provides agent_id / key_id |
| 2 | TDD red: turn 16 TDD test fixtures from `NotImplementedError` to real pytest fixtures | ⏳ NEXT | 2-3 hr | Touches only `tests/` |
| 3 | TDD red: add step-3-specific test cases (e.g. nonce store, dual-format path, enrollment v07) | ⏳ LATER | 1-2 hr | Touches only `tests/` |
| 4 | Implement `src/hermes_orch/auth/hmac_v07.py` (verifier + nonce store interface) | ⏳ LATER | 4-6 hr | First real code in `src/hermes_orch/` |
| 5 | DB migration: add `agents.hmac_key_id` UNIQUE column | ⏳ LATER | 1 hr | DB schema change; needs migration script |
| 6 | Implement `GET /api/agents/{id}/status` endpoint | ⏳ LATER | 1-2 hr | New API route |
| 7 | Add dual-format dispatcher (`HERMES_HMAC_ACCEPT_V06` toggle) | ⏳ LATER | 2-3 hr | spec §3 Option B |
| 8 | Add `POST /api/enrollment/v07` HMAC-signed variant | ⏳ LATER | 2-3 hr | spec §4 |
| 9 | Consolidate + docs + deprecation timeline entry | ⏳ LATER | 1-2 hr | spec §7 |

**Total impl effort: 14-22 hours** (multi-day, can't be one shot). Scaffold (step 0) is the only thing done today.

---

## 5. What this scaffold does NOT do

- ❌ No code change in `src/hermes_orch/`
- ❌ No DB migration
- ❌ No config change
- ❌ No service restart
- ❌ No firewall change
- ❌ No agent enrollment
- ❌ No production deploy
- ❌ No new dependency
- ❌ No secret committed to repo (HMAC secret to be generated by operator, NEVER in repo)

This scaffold is **operator-readable design intent + plan**, nothing more. The 9-step impl begins on operator sign-off (step 1) and runs over multiple days.

---

## 6. Open questions for operator

| # | Question | Default if no answer |
|---|---|---|
| Q1 | Confirm migration Option B (dual-format) over A (hard cutover) or C (format negotiation)? | B (per spec recommendation) |
| Q2 | Confirm test agent_id `bootstrapper-test-01` (vs. your preferred naming)? | Mavis's default |
| Q3 | Confirm cert fingerprint `9eda254f...42` (vs. operator-regenerated)? | Current cert's fingerprint (production) |
| Q4 | When to start step 2 (TDD red phase)? | Tomorrow |
| Q5 | Do you want a draft PR opened at step 1 (operator binding done) or at step 2 (first code change)? | Draft PR at step 1 |
| Q6 | Branch name: keep `feature/orch-server-hmac-v07`, or rename? | Keep |

---

## 7. Companion documents (on other branches)

- **Spec:** `proposal/orch-server-hmac-v0.7-alignment` branch → `docs/specs/orch-server-hmac-v0.7-alignment.md` (PR #5, OPEN)
- **Impl plan:** same branch → `docs/specs/orch-server-hmac-v0.7-impl-plan.md`
- **TDD drafts (T1-T16):** same branch → `tests/test_hmac_v07_auth.py`, `tests/test_hmac_v06_compat.py` (DRAFT, fixtures raise `NotImplementedError`)
- **Helpers (golden test, nonce store):** same branch → `tests/helpers/hmac_v07.py`, `tests/helpers/nonce_store.py`
- **Golden test (cross-language HMAC contract):** same branch → `tests/golden/hmac_v07_golden.json`, `tests/test_hmac_v07_golden.py` (4 passing tests, locked)
- **Bootstrapper (client-side, v0.7.3):** `proposal/orch-client-build-impl-plan-v0.1` branch → `installer/bootstrapper/install-orch-client.ps1` (PR #4, OPEN, 18 commits)
- **Production HTTPS enable (A on 2026-08-15):** committed to `C:\ProgramData\HermesOrchestrator\config\config.yaml` (production config, NOT in git)
- **Firewall auto-add (v0.7.2 patch):** `proposal/orch-client-build-impl-plan-v0.1` branch → `installer/bootstrapper/install-orch-client.ps1` (commit `c046593`)

---

## 8. What the next session will do

Assuming operator sign-off on this scaffold (Q1-Q6 above):

1. Operator answers Q1-Q6 (5 min, possibly deferred to tomorrow morning)
2. Operator generates HMAC secret for `bootstrapper-test-01` (out-of-band; not in repo)
3. Step 2: TDD red phase — make the 16 TDD test fixtures real pytest fixtures, watch them fail (red)
4. Step 4: Implement `hmac_v07.py` verifier, watch tests pass (green)
5. Step 5-8: DB migration, dual-format, status endpoint, enrollment v07
6. Step 9: Consolidate, open PR, operator reviews, deploy (with explicit go)

Each step ends with a git commit on this branch. Step 0 (this scaffold) is the only commit so far.
