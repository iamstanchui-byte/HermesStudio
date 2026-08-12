# Agent Endpoint Auth Hotfix — Design (B12 + B10 disposition)

**Status:** Design only. **No branch yet, no code yet, no firewall change.**
**Branch target (after sign-off):** `security/agent-endpoint-auth-hotfix`
**Track:** Emergency security hotfix, **independent** of `feature/agent-onboarding-0.10.1` and `security/agent-secret-at-rest`.

---

## 1. Executive summary

The v1.0.2 server exposes multiple agent-management HTTP endpoints with **no authentication at all** (no `current_user`, no HMAC, no `Depends`). Any caller on the LAN/VPN that can reach TCP 8765 can call these endpoints and:

- **Delete any agent** by ID (CASCADE removes profiles, configs, in-flight tasks) — **proven via `DELETE /api/agents/agent-f3l6vj5616s6` returning 204 with no auth** (this design doc's own orphan-cleanup predecessor).
- **Modify any agent** metadata (IP, OS, max_concurrent_tasks).
- **Rotate any agent's key** without knowing the current secret (returns new_secret to caller, agent then fails to HMAC-auth).
- **Add/remove/modify any agent's profiles**.
- **Issue, list, revoke enrollment tokens** — these three ARE already admin-gated, but the rest of `agents.py` is not.

This is a **denial-of-enrollment + identity-takeover + complete-resource-destruction** class vulnerability. The only reason it has not been exploited is that the server is on a private LAN and the user is the sole operator.

This hotfix adds a single dependency (`user: dict = Depends(current_user)` + `role == ROLE_ADMIN` check) to all admin-mutation routes in `agents.py`, removes the anonymous legacy secret-bootstrap path, and tightens audit identity. It is a code-only change; no schema migration.

---

## 2. Scope

### In scope (this hotfix)

- **B12** — add an `admin`-authentication gate to **7** agent-management mutation routes (see §4 for the exact list). The 7 routes are the routes that change agent state and currently have no auth.
- **B10** — disable **1 separate** anonymous legacy secret-bootstrap route: `POST /api/agents/{id}/secret` returns `410 Gone` for **all** callers (unauth, non-admin, admin — everyone). It is **NOT** added to the 7 admin-gated routes; it does not get `Depends(require_admin)`. Legacy recovery is deferred to a separate `security/agent-secret-at-rest` design (not part of B12 hotfix).
- **Audit actor** — replace hardcoded `"operator"` with `f"admin:{user['username']}"`; include `remote_addr` + `route` in audit payload. (B10 stub does not emit audit; the route is permanently 410.)
- **CSRF / browser defense** — add Origin/Referer allowlist validation for cookie-authed browser mutations.
- **Test matrix** — unauth=401, non-admin=403, admin=allowed for the 7 admin-gated routes; B10 stub returns 410 for all callers; HMAC-authed agent routes unchanged.
- **Documentation** — mark HTTP-only deployment as `internal-LAN-only`; HTTPS is the formal production prerequisite (enforced via config gate, not in this hotfix).

> **Implementation trap to avoid**: the B10 `/secret` route must NOT receive `Depends(require_admin)`. It must remain an unauthenticated route that always returns 410. If the implementer accidentally adds `require_admin`, the test matrix fails (admin caller expects 410, not 200/201), and the B10 contract is broken. The 7 admin-gated routes and the 1 B10 stubbed route are separate concerns with separate test contracts.

### Out of scope (separate branches / future work)

- **B1** release tag / dashboard install ref → `feature/agent-onboarding-0.10.1`
- **B2/B3** client config behaviour → `feature/agent-onboarding-0.10.1`
- **B4** versioning cleanup → `feature/agent-onboarding-0.10.1`
- **B6** dashboard clipboard UX → `feature/agent-onboarding-0.10.1`
- **B8** wheel validation in clean venv → `feature/agent-onboarding-0.10.1`
- **B9** token masking in support logs / dashboard re-display prevention → **non-blocking follow-up** (see §10). Listed in roadmap; this hotfix does not depend on it but does not remove it.
- **B11** HMAC secret at-rest (encrypted envelope / mTLS / re-key migration) → `security/agent-secret-at-rest` (separate design track, cannot silently reduce to "drop the column").
- **B13** HTTP enrollment transport (LAN/VPN passive observer may capture enrollment material). New enrollment / new client rollout remains frozen until HTTPS is enabled or a separately approved secure enrollment transport is implemented. See §6.4.

---

## 3. Threat model (current state)

### 3.1 Server posture (verified read-only 2026-08-11)

| Item | Value | Source |
|---|---|---|
| Bind | `0.0.0.0:8765` (all interfaces) | `Get-NetTCPConnection -LocalPort 8765 -State Listen` |
| Server LAN IP | `192.168.2.152` (Ethernet) | `Get-NetIPAddress` |
| Server VPN IP | `169.254.158.106` (OpenVPN link-local) | `Get-NetIPAddress` |
| Default gateway | `192.168.2.2` | `Get-NetRoute 0.0.0.0/0` |
| Process | `python.exe` PID 2812 | `Get-CimInstance Win32_Process` |
| **Windows Firewall rules for TCP 8765** | **NONE** | `Get-NetFirewallRule \| Where name matches 8765/hermes/orchestrator` → 0 rows |
| Active TCP peer (sample) | `192.168.2.153` (admin browser, ephemeral source ports) | `Get-NetTCPConnection -LocalPort 8765` |
| `win-local-1` registered IP | `192.168.2.152` (same as server — likely loopback/NAT from same host) | SQLite read-only |
| `linux-a-01` registered IP | `192.168.2.161` (separate LAN host) | SQLite read-only |
| Admin source IP | `192.168.2.153` (from active TCP peer) | `Get-NetTCPConnection` |
| DHCP server | **NOT** on this host (DHCP runs on router `192.168.2.1`) | `Get-Service DHCPServer` |

### 3.2 What an attacker can do today

| Attack | Pre-conditions | Impact |
|---|---|---|
| `DELETE /api/agents/{id}` | know agent_id | agent row gone; CASCADE profiles/configs; in-flight tasks marked failed; audit logs `actor="operator"` (no caller identity) |
| `POST /api/agents/{id}/rotate-key` | know agent_id | server returns new secret to attacker; legitimate agent's HMAC fails; legitimate agent cannot recover without admin `rotate-key` again |
| `POST /api/agents/{id}/secret` (B10) | know agent_id, secret IS NULL (legacy/migration agent) | attacker binds identity permanently; new-flow agents unaffected (secret already set at enroll) |
| `PUT /api/agents/{id}` | know agent_id | modify IP / max_concurrent_tasks; can re-route tasks or starve the agent |
| `POST /api/agents/{id}/profiles` | know agent_id | inject attacker-controlled profile (e.g., `developer` role on someone else's agent) |
| `DELETE /api/agents/{id}/profiles/{name}` | know agent_id | remove profile; agent's heartbeat continues but skill apply loop fails |

### 3.3 What's protected today (no change needed)

| Endpoint | Auth | Reason |
|---|---|---|
| `POST /api/agents/{id}/heartbeat` | HMAC (agent's own `hmac_secret`) | already correct |
| `GET /api/agents/{id}` | HMAC (agent's own `hmac_secret`) | already correct |
| `POST /api/agents/enroll` | anonymous-by-design (uses single-use enrollment token; atomic consume via `used_at` guard) | not exploitable; replay 410 |
| `POST /api/enrollment-tokens` | admin (current_user + role==ROLE_ADMIN) | already correct |
| `GET /api/enrollment-tokens` | admin | already correct |
| `DELETE /api/enrollment-tokens/{id}` | admin | already correct |

### 3.4 Cookie / session posture

| Aspect | Status | Source |
|---|---|---|
| `HttpOnly` | ✓ YES | `cookie.py:185` |
| `SameSite=Lax` | ✓ YES (CSRF mitigation for top-level nav) | `cookie.py:186` |
| `Secure` flag | Conditional on HTTPS scheme | `cookie.py:180,188` (current: HTTP → Secure=False) |
| `path="/"` | ✓ YES | `cookie.py:187` |
| **CSRF token** | **✗ NONE** | grep: 0 matches for `csrf` / `Origin` header / `Referer` |
| `require_hmac_auth` on agents | Used by `GET /{id}` only | `agents.py:678` |

**Implication for this hotfix**: `SameSite=Lax` blocks most browser cross-site POSTs but is not a complete API control. An attacker on the same LAN (e.g., 192.168.2.153) can directly `curl` the API without a browser — `SameSite` does not help against non-browser attacks. **Endpoint auth is the primary defense; CSRF defense-in-depth is for browser-issued mutations only.**

---

## 4. Endpoint auth matrix (target state)

**Scope summary (per operator 2026-08-11 sign-off):**
- **7 routes** in `agents.py` receive `Depends(require_admin)` (admin gate, no schema change).
- **1 route** in `agents.py` (`POST /api/agents/{id}/secret`) is disabled with `410 Gone` for all callers (B10 disposition; see §5).
- **HMAC-authed agent routes** (`heartbeat`, `GET /{id}`) are unchanged. HMAC does NOT grant admin.
- **Enrollment routes** (`/api/agents/enroll`, `/api/enrollment-tokens/*`) are unchanged.

| Endpoint | Method | Unauth | Non-admin cookie | Admin cookie | HMAC | Post-fix note |
|---|---|:---:|:---:|:---:|:---:|---|
| `/api/agents/` | `POST` | 401 | 403 | 201 | n/a | legacy `register_agent`; keep for backward compat, gate to admin |
| `/api/agents/{id}` | `PUT` | 401 | 403 | 200 | n/a | metadata update; admin-only |
| `/api/agents/{id}` | `DELETE` | 401 | 403 | 204 | n/a | **B12 highest priority** |
| `/api/agents/{id}/rotate-key` | `POST` | 401 | 403 | 200 | n/a | admin rotates; returns new_secret only to admin caller |
| `/api/agents/{id}/profiles` | `POST` | 401 | 403 | 201 | n/a | profile add |
| `/api/agents/{id}/profiles/{name}` | `DELETE` | 401 | 403 | 204 | n/a | profile remove |
| `/api/agents/{id}/profiles/{name}` | `PATCH` | 401 | 403 | 200 | n/a | profile update |
| `/api/agents/{id}/heartbeat` | `POST` | 401 | n/a | n/a | **YES (kept)** | agent self — HMAC; do NOT change to admin |
| `/api/agents/{id}` | `GET` | 401 | n/a | n/a | **YES (kept)** | agent self — HMAC; do NOT change to admin |
| `/api/agents/{id}/secret` | `POST` | **410 Gone** | **410 Gone** | **410 Gone** | n/a | **B10**: see §5; legacy compatibility check §9.3 prerequisite |
| `/api/agents/enroll` | `POST` | by token | by token | by token | n/a | anonymous-by-design with `used_at` atomic guard |
| `/api/enrollment-tokens` | `POST` | 401 | 403 | 200 | n/a | already correct |
| `/api/enrollment-tokens` | `GET` | 401 | 403 | 200 | n/a | already correct |
| `/api/enrollment-tokens/{id}` | `DELETE` | 401 | 403 | 200 | n/a | already correct |

**Standard guard helper** (new module `src/hermes_orch/auth/admin_guard.py`):

```python
from fastapi import Depends, HTTPException, Request
from hermes_orch.auth.cookie import current_user, ROLE_ADMIN

async def require_admin(request: Request) -> dict:
    """Standard admin gate for state-changing routes.
    Raises 401 for unauthenticated, 403 for non-admin.
    Returns the user dict (with id, username, role).
    """
    user = await current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "Admin role required")
    return user
```

Apply to each route:

```python
@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    request: Request,
    user: dict = Depends(require_admin),     # ADD THIS
) -> Response:
    ...
```

---

## 5. B10 disposition

### 5.1 This hotfix

`POST /api/agents/{id}/secret` will return **`410 Gone`** unconditionally. The endpoint will continue to exist (HTTP route) but every code path returns 410. The `set_agent_secret` function in `agents.py:571-643` becomes a stub:

```python
@router.post("/{agent_id}/secret", status_code=410)
async def set_agent_secret(agent_id: str, request: Request) -> dict:
    """B10 disposition (2026-08-11): anonymous legacy secret-bootstrap removed.

    New-flow agents (post 2026-08-11, via /api/agents/enroll) have their
    hmac_secret written atomically in the enroll transaction; this endpoint
    is unnecessary for normal flow. Legacy agents (pre-enroll) that lost
    their hmac_secret should be handled via the admin-authenticated
    recovery flow tracked under security/agent-secret-at-rest (B11).

    The endpoint returns 410 Gone to make the deprecation loud and visible
    to existing clients that may have been relying on it.
    """
    raise HTTPException(
        410,
        "POST /api/agents/{id}/secret is deprecated. New-flow agents have "
        "their HMAC secret set at enroll time. For legacy recovery, use "
        "the admin-authenticated recovery flow (tracked in B11).",
    )
```

### 5.2 Deferred (B11 / `security/agent-secret-at-rest`)

If a real legacy migration is required, design a separate admin-authenticated recovery path:

```
POST /api/admin/agent-secret-recovery/{id}
```

Required conditions (full design in B11 track, not here):

- `Depends(require_admin)` — admin auth, not anonymous.
- Check `agents.hmac_secret IS NULL` (server-side gate).
- Server generates single-use, short-expiry **bootstrap nonce** (e.g., 256-bit, 15-min TTL).
- Return `{agent_id, bootstrap_nonce, expires_at}` to admin caller only.
- Agent host presents `{agent_id, bootstrap_nonce, new_secret}` to a separate recovery endpoint to set secret.
- After successful recovery, mark legacy flag cleared; subsequent calls re-check state.
- Audit `actor="admin:<username>"`, `actor_kind="legacy_recovery"`, `remote_addr`, `route`, `expires_at`.
- Nonce single-use; replay rejected.

**This hotfix does NOT implement legacy recovery.** The endpoint returns 410 unconditionally. If a real legacy agent ever needs recovery, the operator will need to manually re-enroll it (delete + create new) or wait for B11.

---

## 6. CSRF / session / HTTP assumptions

### 6.1 CSRF defense for browser mutations

`SameSite=Lax` remains. For browser-issued state-changing requests (cookie auth in play), add **Origin allowlist check**:

```python
# New helper: src/hermes_orch/auth/csrf.py
from fastapi import HTTPException, Request
from urllib.parse import urlparse


def _origin_match(actual_url, expected_origin: str) -> bool:
    """Compare parsed (scheme, hostname, port) of two origins.

    Ignores path / query / fragment / userinfo on `actual_url`. Those
    are separately checked by the caller when relevant (Origin header
    contract forbids them; Referer allows path).
    """
    a = actual_url
    e = urlparse(expected_origin)
    try:
        actual_port = a.port
        expected_port = e.port
    except ValueError:
        return False
    return (
        a.scheme == e.scheme
        and a.hostname == e.hostname
        and actual_port == expected_port
    )


def require_same_origin(request: Request, expected_origin: str) -> None:
    """Reject cross-origin state-changing requests.

    Browser-issued mutations must come from the dashboard, not a third-party
    site. We compare the request's `Origin` (or, if absent, `Referer`) against
    the canonical configured public origin (`HERMES_ORCH_PUBLIC_ORIGIN`).

    Security notes (per operator 2026-08-11 review + final Origin/Referer
    distinction revision):

    - `request.base_url` is derived from the `Host` header, which the client
      controls. DO NOT use it as the allowlist source. Use the explicit
      `expected_origin` (from `HERMES_ORCH_PUBLIC_ORIGIN` config).

    - Do NOT use `startswith` for the comparison — a malicious origin like
      `http://expected.attacker.example` would bypass a prefix check on
      `http://expected`. Use exact (scheme, host, port) comparison.

    - Do NOT combine `Origin` and `Referer` via `headers.get("origin") or
      headers.get("referer")`. They have **different contracts**:
        * `Origin` (when present) MUST be a bare origin: `scheme://host:port`
          with **no path / query / fragment / userinfo**. Browsers always
          send `Origin` for cross-origin requests, and `Origin` does not
          include a path. If `Origin` has a path, it is attacker-supplied
          (a browser would never produce that) — reject it.
        * `Referer` (when present, in lieu of `Origin`) MAY include a path
          (e.g., `http://host:8765/dashboard`). Compare its parsed origin
          (scheme/host/port) against `expected_origin`, but accept any path
          on the same host. Still reject userinfo and malformed ports.

    - HMAC-authed agent requests don't carry cookies; they go through a
      different path (X-Agent-Id + X-Signature) and are not subject to this check.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return  # safe methods

    origin_header = request.headers.get("origin")
    if origin_header:
        # === Origin present: require a bare origin (no path/query/fragment/userinfo) ===
        a = urlparse(origin_header)
        # Empty path is the bare-origin contract. urlparse normalizes "/" to "" for
        # some inputs but not all; explicitly require empty path.
        if a.path not in ("", "/"):
            raise HTTPException(403, f"Origin must be a bare origin (no path): {origin_header}")
        if a.query:
            raise HTTPException(403, f"Origin must not have a query string: {origin_header}")
        if a.fragment:
            raise HTTPException(403, f"Origin must not have a fragment: {origin_header}")
        if a.username or a.password:
            raise HTTPException(403, f"Origin must not contain userinfo: {origin_header}")
        if not _origin_match(a, expected_origin):
            raise HTTPException(
                403,
                f"Cross-origin request rejected (origin={origin_header}, expected={expected_origin})",
            )
        return

    # === Origin absent, fall back to Referer (if any) ===
    referer_header = request.headers.get("referer")
    if not referer_header:
        raise HTTPException(403, "Missing Origin/Referer for state-changing request")
    r = urlparse(referer_header)
    if r.username or r.password:
        raise HTTPException(403, f"Referer must not contain userinfo: {referer_header}")
    # Referer MAY have a path/query/fragment; we only check the origin tuple.
    if not _origin_match(r, expected_origin):
        raise HTTPException(
            403,
            f"Cross-origin request rejected (referer={referer_header}, expected={expected_origin})",
        )
```

**Canonical origin config** (required for `require_same_origin`):

```yaml
# config.yaml
server:
  public_origin: "http://192.168.2.152:8765"   # exact match, no trailing slash, no path
```

Or env var `HERMES_ORCH_PUBLIC_ORIGIN=http://192.168.2.152:8765`. The hotfix fails fast at startup if this is unset or invalid (refuse to start rather than silently allow all or defer the check to request-time).

**`HERMES_ORCH_PUBLIC_ORIGIN` config contract (startup validation)** — per operator 2026-08-11 final revision:

> `server.public_origin` / `HERMES_ORCH_PUBLIC_ORIGIN` must be an absolute URL with `http` or `https` scheme, hostname, explicit port, and **no path, query, fragment, username, or password**. Invalid configuration **prevents startup** before the service binds to a port.

Concretely, `validate_public_origin(value: str) -> str` returns the canonical form on success or raises `ValueError` (which the startup hook translates to a hard fail with a clear error message). The validation function rejects:

- `None` or empty string
- Strings without `http://` or `https://` scheme
- Strings with a path component (`http://host:8765/dashboard` rejected; `http://host:8765` accepted)
- Strings with a query or fragment (`http://host:8765?x=1` rejected; `http://host:8765` accepted)
- Strings with `userinfo` (username / password)
- Strings with no port or with an unparseable port (the URL parser decides this; we double-check that the resulting `port` is a positive integer ≤ 65535)
- Strings where `urlparse(value).hostname` returns `None` or empty

`require_same_origin()` at request-time **only handles untrusted request Origin / Referer malformed values**, returning 403. It does NOT process server misconfiguration (that path is unreachable when startup validation is in place; we still defensively catch any leftover `ValueError` from `e.port` to avoid leaking a 500, but this is a defense-in-depth backstop, not a real failure mode).

**CSRF test matrix (per operator 2026-08-11 review + 2026-08-11 port-parsing-error revision + final config-validation revision)**:

**Runtime request-time tests (12)**:

| Test | Setup | Expected |
|---|---|---|
| `test_csrf_missing_origin_rejected` | admin cookie, no Origin, no Referer | 403 |
| `test_csrf_malformed_origin_rejected` | admin cookie, `Origin: not-a-url` | 403 |
| `test_csrf_prefix_confusion_origin_rejected` | admin cookie, `Origin: http://192.168.2.152:8765.attacker.example` | 403 (prefix confusion — this was the exact attack the operator flagged) |
| `test_csrf_different_port_rejected` | admin cookie, `Origin: http://192.168.2.152:9999` | 403 (port mismatch) |
| `test_csrf_different_scheme_rejected` | admin cookie, `Origin: https://192.168.2.152:8765` | 403 (scheme mismatch — when server is HTTP) |
| `test_csrf_exact_canonical_origin_accepted` | admin cookie, `Origin: http://192.168.2.152:8765` | 2xx |
| `test_csrf_referer_fallback_accepted` | admin cookie, no Origin, `Referer: http://192.168.2.152:8765/dashboard` | 2xx (port 8765 matches; path ignored) |
| `test_hmac_path_skips_csrf` | HMAC headers only, no Origin | 2xx (HMAC is not browser-issued) |
| `test_get_method_skips_csrf` | admin cookie, no Origin, `GET /api/agents/agent-X` | 2xx (safe method) |
| `test_csrf_invalid_port_origin_rejected` | admin cookie, `Origin: http://192.168.2.152:not-a-port` | **403 (NOT 500)** — `urlparse(...).port` raises `ValueError`; helper must catch and return 403. This is the regression the operator flagged in the 2026-08-11 port-parsing review. |
| `test_csrf_structurally_broken_origin_rejected` | admin cookie, `Origin: ://broken` | 403 (structurally invalid URL; helper must not propagate as 500) |
| `test_csrf_invalid_port_referer_rejected` | admin cookie, no Origin, `Referer: http://192.168.2.152:bad-port/dashboard` | 403 (`urlparse(...).port` ValueError on Referer fallback path) |
| `test_csrf_origin_with_path_rejected` | admin cookie, `Origin: http://192.168.2.152:8765/attacker-path` | **403 (NOT allow)** — Origin must be a bare origin; this is the **Origin vs Referer allowlist bypass** the operator flagged in the 2026-08-11 final revision. `urlparse(...).path` is non-empty, helper rejects. |
| `test_csrf_origin_with_query_rejected` | admin cookie, `Origin: http://192.168.2.152:8765?x=1` | 403 (Origin must not have a query string) |
| `test_csrf_origin_with_fragment_rejected` | admin cookie, `Origin: http://192.168.2.152:8765#frag` | 403 (Origin must not have a fragment) |
| `test_csrf_origin_with_userinfo_rejected` | admin cookie, `Origin: http://user:pass@192.168.2.152:8765` | 403 (Origin must not contain userinfo) |
| `test_csrf_referer_with_valid_dashboard_path_accepted` | admin cookie, no Origin, `Referer: http://192.168.2.152:8765/dashboard` | 2xx (Referer MAY have a path; origin tuple matches canonical) |
| `test_csrf_referer_with_userinfo_rejected` | admin cookie, no Origin, `Referer: http://user:pass@192.168.2.152:8765/dashboard` | 403 (Referer must not contain userinfo) |

**Startup config-validation tests (5)** — these run **before the service binds to a port**, fail closed at startup, and prevent the request-time 500 path from being reachable in production:

| Test | Setup | Expected |
|---|---|---|
| `test_public_origin_unset_prevents_startup` | `HERMES_ORCH_PUBLIC_ORIGIN` unset (or empty) | startup hook raises; server refuses to bind |
| `test_public_origin_invalid_port_prevents_startup` | `HERMES_ORCH_PUBLIC_ORIGIN=http://192.168.2.152:not-a-port` | startup hook raises; server refuses to bind |
| `test_public_origin_has_path_prevents_startup` | `HERMES_ORCH_PUBLIC_ORIGIN=http://192.168.2.152:8765/dashboard` | startup hook raises; server refuses to bind (path is not allowed; canonicalize by stripping trailing `/` if present) |
| `test_public_origin_has_query_or_fragment_prevents_startup` | `HERMES_ORCH_PUBLIC_ORIGIN=http://192.168.2.152:8765?x=1` or `...8765#frag` | startup hook raises; server refuses to bind |
| `test_public_origin_invalid_scheme_prevents_startup` | `HERMES_ORCH_PUBLIC_ORIGIN=ftp://host` or `ws://host:8765` | startup hook raises; server refuses to bind (only `http` / `https` allowed) |

> **Note on `test_csrf_server_misconfig_returns_500`**: this test is **REMOVED** per operator 2026-08-11 final revision. Server misconfiguration is no longer a request-time failure mode — it is a startup-time failure mode covered by the 5 tests above. The request-time `require_same_origin` helper keeps a defensive `try/except ValueError` around `e.port` purely as a backstop, but in production that path is unreachable.

Apply to **all** admin-mutation routes (the 7 in §4; `set_agent_secret` returning 410 doesn't need CSRF check). HMAC-authed agent routes (`heartbeat`, `GET /{id}`) do NOT need this check.

### 6.2 HTTPS deployment gate

- HTTP-only deployment is **internal-LAN-only temporary**. Document this in `docs/install-spec.md` (update after this hotfix merges).
- HTTPS is the formal production prerequisite. The cookie `Secure` flag is already set when `request.url.scheme == "https"` — no code change needed here.
- A future PR will add a config gate: refuse to start the server in non-LAN bind (`0.0.0.0` or non-RFC1918) without HTTPS enabled. That gate is **not** part of this hotfix.

### 6.3 Session policy (already correct, just confirming)

- `HttpOnly=True`, `SameSite=Lax`, `Secure` conditional on HTTPS, `path="/"` — all already set.
- No change to session policy in this hotfix.

### 6.4 B13 — HTTP transport exposure (operator 2026-08-11, expanded 2026-08-11)

**Original B13 scope** (initial draft): enrollment token + initial HMAC secret over HTTP. **Incomplete** per operator review.

**Expanded B13 scope** (this revision): HTTP transport exposes **multiple authentication / authorization surfaces**, not just enrollment material:

| Surface | HTTP exposure | Risk |
|---|---|---|
| Dashboard session cookie (`hermes_session`) | Travels on every browser-issued request to the dashboard, `/api/agents/*`, `/api/enrollment-tokens/*`, etc. | Passive observer on LAN/VPN captures cookie, replays it as the admin user against the B12-hardened destructive endpoints. **B12 alone does not stop this** — B12 enforces who can call, not who has the cookie. |
| Admin login flow (`POST /api/auth/login`) | Carries password over HTTP | Passive observer captures plaintext password; same blast radius as captured cookie (offline crack + replay). |
| Enrollment token plaintext (`POST /api/enrollment-tokens` response, `POST /api/agents/enroll` request) | One-time material, but if captured before use, attacker consumes it and becomes the agent. | Agent identity takeover at enrollment time. |
| Initial HMAC secret plaintext (in `POST /api/agents/enroll` response) | Same as above — captured before agent host uses it. | Permanent identity binding for the attacker. |
| Subsequent HMAC-signed requests (`POST /api/agents/{id}/heartbeat`, etc.) | Headers carry `X-Agent-Id`, `X-Timestamp`, `X-Signature`, `X-Body-Sha256` | Replay risk depends on protocol details (see below). |

**HMAC protocol replay assessment** (operator review concern):

The current HMAC scheme signs `(method, path, body_sha256, timestamp)`. **Replayability depends on server-side controls**, which this design does NOT verify but flags for separate review:

- Is `X-Timestamp` enforced server-side (reject if skew > N seconds)? **Unknown — requires source check** (out of scope for B12 hotfix; B13 follow-up).
- Is the timestamp + body_sha256 + signature tuple single-use (replay cache)? **Unknown — requires source check.**
- Does the server bind the signature to a nonce (e.g., server-issued challenge)? **Unknown — requires source check.**
- Does HTTPS-on-the-wire change any of the above? **No** — replay controls are independent of transport encryption.

Until the source check confirms these, **B13 must also flag "potentially replayable agent HMAC requests on LAN/VPN"** even though the agents themselves don't transmit the HMAC secret in the clear.

**Why existing agents are not "auto-protected by enrollment"**: an existing agent (`linux-a-01`, `win-local-1`) has already exchanged its HMAC secret under HTTP. The secret is no longer transmitted in cleartext on the wire. **However**:
- The historical HTTP capture window may have leaked the secret to a passive observer.
- The replayability of the HMAC scheme (§ above) is independent of historical exchange.
- The session cookie for the operator's dashboard continues to traverse HTTP on every request.

**Formal B13 statement (canonical)**:

> **B13 — HTTP transport exposes browser session / authentication / enrollment material / and potentially replayable agent HMAC requests on LAN/VPN.**
>
> **B12 blocks anonymous direct mutation. B12 does not protect an attacker who has captured an admin session cookie over HTTP.**

**Decisions (operator 2026-08-11)**:

- **B12 hotfix can deploy** (per operator). Auth + audit is correct and necessary regardless of transport.
- **New enrollment / new client rollout remains FROZEN** until one of:
  - HTTPS is enabled on the server (TLS terminates in front of port 8765; `Secure` cookie flag activates; session cookie + login password + enrollment material are encrypted on the wire).
  - A separately approved secure enrollment transport is implemented (e.g., out-of-band token delivery, mTLS, signed enrollment URL).
- **Dashboard admin usage + production security sign-off cannot claim "safe" while transport is HTTP.** HTTPS is the formal production prerequisite.
- **HMAC replay controls** (`X-Timestamp` enforcement, replay cache, nonce binding) are **flagged for B13 follow-up**. They are NOT in B12 hotfix scope. If source check reveals they are missing, that is a separate P0 finding (proposed: **B14**).

**B13 is tracked separately** (not in this hotfix's PR). It does not block B12 merge, but it does block B8 pilot on a real production enrollment, and it blocks any "production ready" security claim.

### 6.5 New enrollment freeze (operational policy)

Until B13 is closed:

- Operator will not click `+ Add agent host` in the dashboard.
- Operator will not run `hermes-orch-agent enroll` from any new client host.
- Operator will not run B8 (clean-host wheel test) using a real production enrollment token.
- Existing agents continue to heartbeat and process tasks; this is not a freeze on existing production workload.

---

## 7. Audit actor model

### 7.1 Actor format

For every admin-gated mutation in §4, replace hardcoded `"operator"` with the authenticated user identity:

```python
await audit_log(
    db, "agent.deleted",
    actor=f"admin:{user['username']}",   # was: actor="operator"
    agent_id=agent_id,
    payload={
        "remote_addr": request.client.host if request.client else None,
        "route": "DELETE /api/agents/{id}",
        # Event-specific extras (e.g., "new_secret_returned": True on rotate-key)
    },
)
```

### 7.2 Actor format for HMAC-authed agent routes

For `heartbeat` (HMAC-authed) and other agent self routes, use:

```python
actor=f"agent:{agent_id}"
```

This is already the convention in some places (e.g., `actor=f"bootstrap:{caller_agent_id or 'unknown'}"` in `set_agent_secret` — but that endpoint is being removed). For consistency, prefer `agent:{agent_id}`.

### 7.3 No new schema field

`actor_kind` is **not added** to the `audit_log` table in this hotfix. Adding a column would force a schema migration, which is out of scope (blast radius, B11 territory). The kind can be inferred from `actor` prefix (`admin:`, `agent:`, `system:`, `bootstrap:`) until B11 redesigns the audit schema.

### 7.4 Payload fields added by this hotfix

Every admin-mutation route's audit call adds:

| Field | Source | Purpose |
|---|---|---|
| `remote_addr` | `request.client.host` | caller IP; helps identify if attack is from LAN or external |
| `route` | hardcoded string per endpoint | grep-friendly route identifier |

No new DB columns; the `payload` column already exists as `TEXT` (JSON-encoded).

---

## 8. Test matrix

### 8.1 Per-route tests (pytest)

For each of the **7 admin-mutation routes** in §4 (`set_agent_secret` returns 410 unconditionally, so it has no admin-success path to test):

| Test | Setup | Expected |
|---|---|---|
| `test_<route>_unauthenticated_401` | no cookie, no HMAC | 401 |
| `test_<route>_nonadmin_403` | valid cookie for non-admin user | 403 |
| `test_<route>_admin_allowed` | valid cookie for admin user | 2xx + audit `actor="admin:<u>"`, payload has `remote_addr`+`route` |
| `test_<route>_csrf_origin_reject` | admin cookie, cross-origin Origin header | 403 |
| `test_<route>_csrf_origin_accept` | admin cookie, same-origin Origin | 2xx |
| `test_<route>_hmac_path_does_not_grant_admin` | HMAC headers only (no cookie) | 401 (HMAC does not grant admin) |

### 8.2 Heartbeat / agent-self routes (regression — must still work)

| Test | Expected |
|---|---|
| `test_heartbeat_no_hmac_401` | 401 |
| `test_heartbeat_valid_hmac_200` | 200 |
| `test_get_agent_self_no_hmac_401` | 401 |
| `test_get_agent_self_valid_hmac_200` | 200 |

### 8.3 B10 (legacy secret bootstrap)

| Test | Expected |
|---|---|
| `test_set_agent_secret_any_caller_410` | 410 (unauth, non-admin, admin all return 410) |
| `test_enroll_then_heartbeat_works` | end-to-end: enroll → save secret → heartbeat 200 |

### 8.4 Cross-cutting

| Test | Expected |
|---|---|
| `test_audit_actor_admin_format` | every admin-mutation audit has `actor` matching `^admin:.+$` |
| `test_audit_payload_has_remote_addr_and_route` | every admin-mutation audit payload has both keys |
| `test_audit_no_hardcoded_operator_string` | grep test: no audit call uses literal `"operator"` (except where intentional, e.g., background scheduled tasks) |
| `test_health_endpoint_unaffected` | `/api/health` (or similar) still 200 without auth |

### 8.5 Negative / regression

| Test | Expected |
|---|---|
| `test_no_actor_kind_column_added` | DB schema unchanged (`audit_log` columns identical to pre-hotfix) |
| `test_no_firewall_management_in_source` | **Source-grep assertion** (per operator 2026-08-11 revision 3, scope-tightened per revision 10): the test scans **only changed executable production source paths**, NOT tests, NOT docs, NOT the design markdown, NOT the test file itself. The test asserts this hotfix introduces no firewall-management command invocation into production runtime or deployment code. **Scope of scan** (allowlist):<br>• `src/hermes_orch/**` (production runtime)<br>• `scripts/**` (deployment / ops scripts)<br>**Scope of scan** (denylist — excluded):<br>• `tests/**` (test files contain literal forbidden tokens for negative-test purposes; the test file itself is excluded to prevent self-trigger)<br>• `docs/**` (design / spec docs may reference forbidden tokens for documentation)<br>• `*.md` files (anywhere)<br>• changelog / release notes<br>Forbidden tokens: `New-NetFirewallRule`, `Set-NetFirewallRule`, `Remove-NetFirewallRule`, `netsh advfirewall`, `iptables`, `ip6tables`, `nft`, `ufw allow`, or equivalent firewall-management commands. **Implementation strategy** (preferred): use `git diff --name-only <base>...HEAD` to enumerate changed files; intersect with the executable-source allowlist; scan only those. Fallback: scan the allowlist tree directly. |
| `test_health_endpoint_unaffected` | `/api/health` (or similar) still 200 without auth |
| `test_enrollment_410_returns_correct_body` | `POST /api/agents/{id}/secret` returns 410 with body matching §5.1 stub (regardless of caller) |

---

## 9. Migration / rollback

### 9.1 Migration

- **Code-only change.** No DB migration. No schema change. No data loss risk.
- Deployment: pull branch → install (no new deps) → restart server (NSSM or systemd).
- Pre-deploy checklist: see §11.1.

### 9.2 Rollback

- `git revert` + redeploy.
- Or: `git reset --hard <last-good-commit>` + redeploy.
- Rollback does NOT lose data. After rollback, agents that have not been re-enrolled (post-B10) will still need the legacy `set_agent_secret` path — but as of this hotfix, no such agent exists in the fleet (the orphan was deleted; remaining agents are HMAC-authenticated).

### 9.3 Pre-deploy verification

Before applying this hotfix, run these read-only checks:

1. **Confirm `agents.hmac_secret` is non-NULL for every production agent** (read-only SQLite; `linux-a-01`, `win-local-1`). If any agent has NULL secret, do NOT apply this hotfix until that agent is re-enrolled.
2. **B10 compatibility check (operator 2026-08-11 requirement)**: confirm that the existing fleet does NOT call `POST /api/agents/{id}/secret` in any normal startup / recovery flow. Search:
   - **Source of deployed agent package** (e.g., `wrapper-config.json` directory, installed wheel source): grep for `/secret` and any hardcoded URL containing `secret`. Also check `agent_cli.py` for any fallback path that calls `/secret`.
   - **Live agent logs** (e.g., `~/.hermes-orchestrator/wrapper.log` or system event log): grep for `/secret` HTTP calls or `state_io_error: secret` failures.
   - **Controlled restart of a non-production equivalent** if available (preferred when test agent exists).
   - Only after confirming **zero callers** of the `/secret` route in the existing fleet may this hotfix disable it with `410`.
3. **Outstanding enrollment tokens check** (per operator 2026-08-11 revision 2): query the `enrollment_tokens` table for tokens that are:
   - `used_at IS NULL` (not consumed), AND
   - `expires_at > now` (not yet expired)

   If any such rows exist, **revoke them through the admin-authenticated enrollment-token API** (not the dashboard — the dashboard currently has no revoke-token control):
   ```
   DELETE /api/enrollment-tokens/{id}    # admin cookie required (post-B12)
   ```
   Or wait for natural expiry (15 min from issue time). Note: B13 enrollment freeze means **no new tokens should be issued until HTTPS / secure transport is enabled**, so any outstanding token here is residue from the testing that produced the orphan `agent-f3l6vj5616s6`.
4. **HERMES_ORCH_PUBLIC_ORIGIN** is set in `config.yaml` or env, matching the actual public origin the dashboard uses (e.g., `http://192.168.2.152:8765`).
5. (Removed `test_no_firewall_rule_added_by_hotfix` per operator 2026-08-11 revision 3; this is enforced as a deployment constraint in §9.7, not as a runtime check.)

### 9.4 Post-deploy verification

**NEVER delete a production agent for auth verification.** Per operator 2026-08-11 directive: production agents (`win-local-1`, `linux-a-01`) carry state (profiles, configs, in-flight tasks, possibly production workloads). Treating them as auth test fixtures is destructive even with backup/re-enroll, because:

- Re-enrollment invalidates HMAC secret; in-flight tasks may fail.
- Profile configs CASCADE-delete on agent delete; some configs may be one-shot uploads.
- Production data is not a test fixture.

Post-deploy verification (read-only, non-destructive):

1. **Unauthenticated DELETE** against an existing production agent ID (e.g., `agent-win-local-1`) without cookie → expect `401`.
2. **Non-admin DELETE** against an existing production agent ID (using a non-admin user's cookie) → expect `403`.
3. **Existing production agent heartbeats continue normally** — verified per operator 2026-08-11 revision 4 as a **configured-interval comparison** (NOT a hardcoded 30 s):

   ```text
   interval = <read from server config, e.g. config.yaml["agent"]["heartbeat_interval_sec"]>
   tolerance = <operator-chosen, e.g. 2 * interval>  # accept up to 2 missed cycles
   T1 = read last_heartbeat_at for both production agents
   wait (2 * interval + tolerance)
   T2 = re-read last_heartbeat_at
   assert T2 > T1 for both agents
   assert (T2 - T1) >= (2 * interval) and (T2 - T1) <= (2 * interval + tolerance)
   ```

   If the server config does not expose a readable heartbeat interval, fall back to: read `T1` and `T2` separated by a wall-clock wait of `wait_seconds` (operator-chosen, e.g. 120 s for 30 s baseline + tolerance), then assert `T2 > T1`. **Do not hardcode "30 s" anywhere in the verification script.**

4. **B10 410 check**: `POST /api/agents/{id}/secret` (any agent) without cookie → expect `410`. With admin cookie → still `410` (B10 returns 410 for everyone).

**NO production disposable-agent test** (per operator 2026-08-11 revision 11 — B13 conflict): §6.4 freezes new enrollment over HTTP, so a disposable-agent flow that issues an enrollment token + enrolls + deletes in production post-deploy violates the B13 freeze. The two statements would directly contradict each other: "no new enrollment over HTTP" and "issue token, enroll, delete as post-deploy check".

**Admin-success behaviour is verified in CI / isolated test DB only**, not in production post-deploy. Specifically, `tests/test_endpoint_auth.py` §8.1 covers the admin-success path (admin cookie → 2xx) against a CI fixture. **A disposable-agent admin-success production verification is forbidden while B13 is open and the server transport is HTTP.** It may be performed only after:

1. HTTPS is deployed (B13 transport closed), OR
2. an isolated TLS-enabled staging environment is used (not the production server).

If operator specifically needs a live admin-success verification after B12 deploy and cannot wait for B13, the only acceptable approach is a **fully isolated disposable agent test in a separate test environment** (different server, different DB, different TLS endpoint) — never against the production server.

### 9.7 Deployment constraint — no firewall-management code (per operator 2026-08-11 revision 3, scope-tightened per revision 10)

**This hotfix contains no firewall-management code**. Specifically, the hotfix MUST NOT introduce any of the following into the executable production source tree (i.e., the files that actually run on the server or in deployment automation):

- PowerShell: `New-NetFirewallRule`, `Set-NetFirewallRule`, `Remove-NetFirewallRule`
- Windows: `netsh advfirewall`
- Linux: `iptables`, `ip6tables`, `nft`
- Ubuntu: `ufw allow`
- macOS / BSD: any equivalent firewall-management command

**Enforcement** (per operator 2026-08-11 revision 10 — the test must NOT self-trigger):

- **Source-grep scope** (allowlist — these are scanned): `src/hermes_orch/**` and `scripts/**` only.
- **Source-grep scope** (denylist — these are NOT scanned, to prevent self-trigger):
  - `tests/**` (test files contain literal forbidden tokens for negative-test purposes; the test file itself is excluded)
  - `docs/**` (design / spec docs may reference forbidden tokens for documentation)
  - `*.md` files (anywhere)
  - changelog / release notes
- **Implementation strategy** (preferred): use `git diff --name-only <base>...HEAD` to enumerate changed files; intersect with the executable-source allowlist (`src/hermes_orch/**` and `scripts/**`); scan only those. Fallback: scan the allowlist tree directly.
- **CI / pre-merge**: the test is in `tests/test_no_firewall_management.py`. The test file itself contains the forbidden tokens as literal strings for `re.search` matching; the test excludes itself from the scan via the path filter above.
- **Code review**: the PR description must include a checklist item: "No firewall-management commands added to `src/hermes_orch/**` or `scripts/**`. Source-grep test green."
- **NOT enforced**: runtime firewall state. CI / dev / production host firewall baselines are intentionally different. Asserting specific firewall state in pytest is the wrong layer.

**Why not in §8.5**: per operator, the firewall-state test is environment-dependent and would break the moment a future firewall-hardening PR is merged. The source-grep test does not have that failure mode, **but** the source-grep test must be carefully scoped to executable production source only — otherwise it self-triggers on its own literal forbidden tokens, or on the design markdown that documents them.

---

## 10. B9 follow-up (non-blocking, NOT removed from roadmap)

B9 is not in this hotfix's blast radius but must remain visible:

- Dashboard `Add agent host` modal must **never re-display raw token after issuance**. (The current UI shows it once in `<pre id="enroll-cmd">`; verify no other UI surfaces the plaintext after the modal closes.)
- Server logs and support output must **mask enrollment tokens** (e.g., `etok-ab...xyz` rather than full). Currently the token is only in the `POST /api/enrollment-tokens` response body; check `loguru` / `logging` config doesn't accidentally log the request body.
- The install command (returned in the issue response) should warn that the token is single-use and must be treated as a secret.
- HTTP `Cache-Control: no-store, no-cache, must-revalidate, private` should be set on the issue endpoint's response (so browser back/forward doesn't leak the token via cache).

These are independent of B12 and tracked separately. They do not block the B12 hotfix merge.

---

## 11. Open questions (need operator input before merge)

### 11.1 Server bind address

The server is currently bound to `0.0.0.0:8765` with zero firewall rules. After this hotfix:

- Admin calls still work from any source IP (because admin is verified by cookie, not by IP).
- But the server is still LAN-exposed. This hotfix does NOT change bind address. Future work (separate PR): add a `bind_host` config option that defaults to `127.0.0.1` for `internal` deployment and `0.0.0.0` only if `tls.enabled=true` AND operator explicitly opts in.

### 11.2 Admin source IP — **CONFIRMED by operator 2026-08-11**

Active TCP peer observed during read-only sampling: `192.168.2.153` (admin browser, ephemeral source ports). Heartbeats from `192.168.2.161` (linux-a-01) and `192.168.2.152` (win-local-1, same as server).

**Status per operator 2026-08-11**: **`192.168.2.153` is the operator's dashboard workstation** (testing environment on the same machine as `minimax code`, for debugging convenience). Confirmed by operator.

**Implication for this hotfix**: cookie-authed admin requests from the operator's browser are expected from `192.168.2.153`. The CSRF check in §6.1 does not depend on this IP (canonical origin is config-driven). This confirmation is useful for future firewall / allowlist work, but the hotfix itself does not require it.

### 11.3 `win-local-1` source IP — **CONFIRMED by operator 2026-08-11**

`win-local-1` registered IP = `192.168.2.152` = server's own LAN IP.

**Status per operator 2026-08-11**: **confirmed same host** as the server (testing environment on the same machine as `minimax code`, for debugging convenience). The registered IP being equal to the server's own LAN IP is now confirmed to be by-design (same host, possibly loopback or NAT).

**Implication for this hotfix**: HMAC auth is host-agnostic; no impact on B12 scope. Future firewall work can treat `192.168.2.152` as the server's own IP (no inbound firewall rule needed from this source).

### 11.4 B11 (secret at-rest) timing

**Decision per operator 2026-08-11**: B11 design track (`security/agent-secret-at-rest`) starts **after B12 hotfix deploy + 7 days of stable observation**, AND **only after existing production agents (`linux-a-01`, `win-local-1`) demonstrate normal heartbeat / profile / task lifecycle throughout that 7-day window**. If any anomaly (e.g., HMAC mismatch, agent drop-off, profile apply failure) is observed, the 7-day clock resets.

B11 is more severe than B12 (plaintext secrets in DB at rest, vulnerable to backup exfiltration, dev/test DB dumps). But B11 is a longer design + migration track; rushing it would risk breaking live agents. The 7-day observation window is the safety margin.

### 11.5 B12 + B10 + B13 + B11 sequencing (operator 2026-08-11)

1. **B12 hotfix** (this design) — code-only auth gate + audit. Merge after sign-off, deploy, observe.
2. **7-day observation window** — confirm fleet stability.
3. **B11 design** — `security/agent-secret-at-rest` track, design doc, migration plan.
4. **B11 implementation** — schema migration + key store + re-key flow. Requires backout plan.
5. **B13 transport** — separate track (HTTPS enablement or out-of-band enrollment). Blocks B8 pilot but not B11.
6. **B8 clean-host wheel test** — only after B13 closed. Use TLS-enabled staging server or isolated network; never real production enrollment over HTTP.
7. **Feature/agent-onboarding-0.10.1** — B1/B2/B3/B4/B6/B8/B9. After B12 hotfix deploy, but B8 depends on B13.

This sequencing is the operator-approved order. Each step is gated by the previous step's success.

---

## 12. Files that will be touched (after sign-off)

This is the planned change set. **Nothing is changed yet.**

- `src/hermes_orch/auth/admin_guard.py` — NEW. `require_admin` dependency (§4).
- `src/hermes_orch/auth/csrf.py` — NEW. `require_same_origin` with `urlparse` exact-compare against `expected_origin` (§6.1).
- `src/hermes_orch/api/agents.py` — add `Depends(require_admin)` to **7 admin-mutation routes** (§4). Stub `set_agent_secret` (`POST /api/agents/{id}/secret`) to return `410 Gone` unconditionally (§5). Update audit calls to use `f"admin:{user['username']}"` + `payload={"remote_addr": ..., "route": ...}` (§7).
- `src/hermes_orch/config.py` (or `config.yaml`) — add `server.public_origin` (or env `HERMES_ORCH_PUBLIC_ORIGIN`) for CSRF allowlist source. Refuse to start if unset (§6.1).
- `tests/test_endpoint_auth.py` — NEW. Pytest covering the test matrix in §8.1, §8.2, §8.3, §8.4, §8.5, §8.6 (CSRF).
- `scripts/pre_deploy_check.sh` (or `.ps1`) — pre-deploy compatibility check (B10 /secret dependency search; read-only SQLite NULL-secret audit; outstanding-token check; FW rules; `HERMES_ORCH_PUBLIC_ORIGIN` set). Output: pass/fail per item, blocks deploy if any fail (§9.3).
- `docs/install-spec.md` — update HTTP-only deployment note (after hotfix merges, not in this PR).
- **No schema migration. No firewall rule. No bind address change.**

---

## 13. Sign-off checklist

Before this design becomes a PR, all 5 revisions from operator 2026-08-11 review + 4 revisions from second-round review must be accepted:

**Round 1 (5 revisions)**:

- [ ] **Revision 1 (§9.4)**: production agent delete is REMOVED from post-deploy verification. Admin-success path is verified in CI / isolated test DB (`tests/test_endpoint_auth.py` §8.1), NOT via a production disposable-agent test. (Per Revision 11: disposable-agent production verification is **forbidden** while B13 is open and the server transport is HTTP.)
- [ ] **Revision 2 (§2 + §4)**: route count stated as **7 admin-mutation routes + 1 disabled B10 route** (not 8). All 8 paths covered.
- [ ] **Revision 3 (§6.1)**: CSRF helper uses `urlparse` exact-compare against canonical `HERMES_ORCH_PUBLIC_ORIGIN`; no `startswith`. Test matrix includes prefix-confusion case. `Host`-header trust removed.
- [ ] **Revision 4 (B13)**: HTTP enrollment transport risk documented; new enrollment frozen until HTTPS or approved secure transport. B8 pilot may validate package but not real production enrollment over HTTP.
- [ ] **Revision 5 (§9.3)**: B10 410 compatibility check documented (search deployed agent source + live logs + controlled restart). Only after confirming zero callers in existing fleet may `/secret` be hard-set to 410.

**Round 2 (4 revisions)**:

- [ ] **Revision 6 (B13 expansion, §6.4)**: B13 now covers session cookie + admin password + login flow + HMAC replay risk in addition to enrollment material. Formal statement: "B12 blocks anonymous direct mutation. B12 does not protect an attacker who has captured an admin session cookie over HTTP." HMAC protocol controls (timestamp enforcement, replay cache, nonce binding) flagged for B13 follow-up; if source check reveals missing controls, propose B14.
- [ ] **Revision 7 (§9.3 token check)**: pre-deploy outstanding-token check uses admin-authenticated `DELETE /api/enrollment-tokens/{id}` API (not the dashboard, which has no revoke UI). B13 enrollment freeze means no new tokens should be issued; any outstanding token is residue from prior testing.
- [ ] **Revision 8 (§8.5 firewall test)**: removed `test_no_firewall_rule_added_by_hotfix`. Replaced with source-grep test (`test_no_firewall_management_in_source`) + deployment constraint in §9.7. Code-review checklist item: "No firewall-management commands added."
- [ ] **Revision 9 (§9.4 heartbeat check)**: heartbeat verification uses **configured interval + tolerance** (read from server config), not hardcoded 30 s. Fallback: T1 + wait + T2 comparison if config has no readable interval. **Do not hardcode "30 s" anywhere in the verification script.**
- [ ] **Revision 10 (final)**: (a) §2 in-scope bullet states "**7 admin-mutation routes**" + "**1 separate B10 /secret route returns 410**", NOT "8 admin-gated routes". (b) Firewall source-grep test scopes to `src/hermes_orch/**` + `scripts/**` only; excludes `tests/**`, `docs/**`, `*.md`, changelog. Implementation uses `git diff --name-only` to enumerate changed files, then scans only those matching the allowlist. Prevents self-trigger from literal forbidden tokens in test file and design doc.
- [ ] **Revision 11 (B13 conflict)**: §9.4 production disposable-agent test **REMOVED**. B13 freezes new enrollment over HTTP, so issuing a token + enrolling + deleting in production post-deploy directly contradicts §6.4. Admin-success verification is CI / isolated test DB only (`tests/test_endpoint_auth.py` §8.1). Disposable-agent production verification is **forbidden** while B13 is open and the server transport is HTTP; only acceptable after HTTPS or in an isolated TLS-enabled staging environment.
- [ ] **Revision 12 (CSRF port-parsing-error)**: §6.1 helper MUST `try/except ValueError` around `parsed.port` calls (both for `actual_port` and `expected_port`). Malformed Origin/Referer returns 403 (not 500). Server-side misconfig (`HERMES_ORCH_PUBLIC_ORIGIN` invalid) returns 500. Three new test cases added: `test_csrf_invalid_port_origin_rejected`, `test_csrf_structurally_broken_origin_rejected`, `test_csrf_invalid_port_referer_rejected`, plus `test_csrf_server_misconfig_returns_500`.
- [ ] **Revision 13 (config validation at startup, not request-time)**: (a) `test_csrf_server_misconfig_returns_500` is **REMOVED**. Server misconfiguration is no longer a request-time failure mode. (b) `HERMES_ORCH_PUBLIC_ORIGIN` (typo fix from `HERMES_ORCH_PUBLIC_ORIGEN`) is validated at startup with strict contract: absolute URL, `http`/`https` only, hostname, explicit port, **no path/query/fragment/userinfo**. Invalid config **prevents startup** before the service binds to a port. (c) Five new startup-validation tests: `test_public_origin_unset_prevents_startup`, `test_public_origin_invalid_port_prevents_startup`, `test_public_origin_has_path_prevents_startup`, `test_public_origin_has_query_or_fragment_prevents_startup`, `test_public_origin_invalid_scheme_prevents_startup`. (d) `require_same_origin()` at request-time keeps a defensive `try/except` around `e.port` (backstop), but in production that path is unreachable when startup validation is in place.
- [ ] **Revision 14 (Origin vs Referer distinction, allowlist bypass fix)**: `require_same_origin()` does **NOT** combine `Origin` and `Referer` via `headers.get("origin") or headers.get("referer")`. The two headers have different contracts: `Origin` (when present) MUST be a bare origin (no path / query / fragment / userinfo); `Referer` (when used as fallback) MAY have a path but must still reject userinfo and malformed ports. Helper extracts `Origin` first; if present, asserts bare-origin contract then compares scheme/host/port. Only if `Origin` is absent does the helper look at `Referer`, comparing only the parsed origin tuple (scheme/host/port) while allowing a Referer path. Six new test cases: `test_csrf_origin_with_path_rejected`, `test_csrf_origin_with_query_rejected`, `test_csrf_origin_with_fragment_rejected`, `test_csrf_origin_with_userinfo_rejected`, `test_csrf_referer_with_valid_dashboard_path_accepted`, `test_csrf_referer_with_userinfo_rejected`.

**Open questions (§11) — operator status**:

- [x] **Q1**: `192.168.2.153` = dashboard workstation? **CONFIRMED yes** (operator 2026-08-11).
- [x] **Q2**: `win-local-1` same host as server? **CONFIRMED yes** (operator 2026-08-11, testing environment on same machine).
- [x] **Q3**: B11 start timing = **7 days after B12 hotfix deploy + stable observation** (operator 2026-08-11).
- [x] **Q4**: server bind address = **defer to separate PR** (operator 2026-08-11).

**Structural sign-offs**:

- [ ] Operator confirms scope (§2) is acceptable.
- [ ] Operator confirms B10 disposition (§5) is acceptable (no legacy recovery in this hotfix).
- [ ] Operator confirms CSRF approach (§6.1) is acceptable (urlparse + canonical origin).
- [ ] Operator confirms expanded B13 (§6.4) is acceptable (transport risk documented; new-enrollment freeze; HMAC replay flagged for B14).
- [ ] Operator confirms audit actor model (§7) is acceptable.
- [ ] Operator confirms test matrix (§8) is acceptable, including 9 CSRF cases + 1 source-grep firewall test.
- [ ] B13 transport freeze + new-enrollment freeze is acknowledged.
- [ ] `security/agent-endpoint-auth-hotfix` branch is created.
- [ ] Implementation follows this design (7 admin gates + 1 B10 stub + csrf + audit + canonical origin + new tests + source-grep firewall).
- [ ] Tests pass (see §8, including 9 CSRF cases + source-grep test).
- [ ] Pre-deploy verification (§9.3) passes — operator runs the compat check script; confirms zero callers of `/secret` in fleet; revokes any outstanding tokens via `DELETE /api/enrollment-tokens/{id}`.
- [ ] Post-deploy verification (§9.4) passes — operator confirms production agents' heartbeats are unaffected via **configured-interval comparison**; admin-success verified only via disposable test agent or CI test DB.

**After sign-off, this doc is frozen** (any change requires a new design doc).
