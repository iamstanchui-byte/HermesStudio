# HMAC Agent Authentication (v1.6, 2026-07-29)

## Why

Before v1.6, the wrapper → server auth was a placeholder:
`X-Signature: SHA-256(secret)`. This stamped the secret hash but
didn't bind the signature to the request. An attacker on the local
network who intercepted any signed request could:

1. **Replay it forever** (no timestamp binding)
2. **Replay it against any wrapper endpoint** (no path binding)
3. **Mutate the body and re-sign** (no body binding — they have the
   hash of the secret but never needed it to forge)

v1.6 ships real HMAC-SHA256 that binds the signature to
(method, path, body, timestamp), and a server-side verifier that
rejects anything outside a 5-minute window.

## Wire format

Request headers (every wrapper-side request):

| Header | Value |
|---|---|
| `X-Agent-Id` | Agent id (e.g. `win-local-1`) |
| `X-Timestamp` | Unix epoch seconds, decimal |
| `X-Signature` | Hex HMAC-SHA256 |

Signature input (the "string-to-sign"):

```
<METHOD>\n<PATH>\n<SHA256_HEX(body_bytes)>\n<TIMESTAMP>
```

Where:
- `METHOD`: uppercase HTTP method (`GET`, `POST`, ...)
- `PATH`: full request path including query string
  (e.g. `/api/agents/win-local-1/heartbeat`)
- `body_bytes`: raw request body bytes (empty bytes for GET)
- `TIMESTAMP`: same value as the `X-Timestamp` header

Algorithm:
```
sig = HMAC-SHA256(key=secret, msg=string-to-sign).hexdigest()
```

## Server validation

`require_hmac_auth` (FastAPI dependency in `src/hermes_orch/auth/hmac.py`):

1. All 3 headers present (else 401)
2. `X-Timestamp` parses as int, `|now - ts| <= 300` (5-min window, env
   `HERMES_HMAC_WINDOW_SEC` to tune)
3. Look up `agents.hmac_secret` by `X-Agent-Id`
4. If `NULL`: legacy mode. Allow if `HERMES_HMAC_REQUIRED` is unset;
   fail with 401 if set.
5. Recompute signature, compare with `hmac.compare_digest` (constant-time)

## Endpoints that require HMAC

All wrapper-side endpoints (anything a wrapper daemon calls):

- `POST /api/agents/{id}/heartbeat`
- `POST /api/agents/{id}/sessions/{sid}/cleanup-ack`
- `POST /api/agents/{id}/profiles/{name}/configs/{cid}/ack`
- `GET  /api/agents/{id}/profiles/{name}/configs/pending`
- `GET  /api/agents/{id}/profiles/{name}/skills` (and .../skills/{name})
- `POST /api/agents/{id}/profiles/{name}/skills`
- `DELETE /api/agents/{id}/profiles/{name}/skills/{name}`
- `POST /api/agents/{id}/profiles/{name}/skills/{name}/copy`
- `GET  /api/agents/{id}` (sync_config)
- `POST /api/tasks/{id}/start`
- `POST /api/tasks/{id}/poll`
- `POST /api/tasks/{id}/result`
- `POST /api/projects/{id}/tasks/{tid}/output-chunk`
- `POST /api/projects/{id}/tasks/{tid}/tool-call`
- `POST /api/projects/{id}/session`
- `GET  /api/projects/{id}/session?role={role}`
- `GET  /api/projects/{id}/files/{path}`
- `PUT  /api/projects/{id}/files/{path}`
- `GET  /api/projects/{id}/memory/state`
- `GET  /api/projects/{id}/memory/facts`
- `GET  /api/projects/{id}/memory/trace`
- `GET  /api/projects/memory/recent`

Dashboard reads (browser → server, in-process or fetch) are NOT
HMAC-protected — the operator UI doesn't have the secret. The
project-scope guard (e.g. `WHERE project_id = ?`) prevents IDOR.

## Bootstrap

The wrapper's `start` command calls `_bootstrap_hmac_secret` on
every startup. This POSTs the local secret to
`POST /api/agents/{id}/secret`:

- **201** `{"status": "set"}` — first call, secret stored
- **200** `{"status": "already_set", "match": true}` — same secret, no-op
- **409** `{"status": "conflict"}` — different secret, **hard fail** in the wrapper

This means: the wrapper self-heals after a server DB wipe, and the
operator can manually push a new secret if needed.

For one-time migration of existing agents that haven't been
restarted, run `scripts/migrate_hmac_secrets.py`.

## Threat model

In-scope (what v1.6 protects against):

- A local-network attacker replaying captured wrapper requests
- A local-network attacker impersonating another wrapper (without
  the secret, they can't forge signatures)
- A stale request from >5 min ago being replayed

Out-of-scope (deliberate):

- DB compromise: the secret is stored plaintext. An attacker with
  DB read can impersonate any wrapper. Defense: encrypt at rest
  (KMS), or move to Ed25519 asymmetric signing. Both deferred.
- Compromised wrapper host: if an attacker can read
  `~/.hermes-orchestrator/.secret-<id>`, they can sign as that agent.
  Defense: filesystem permissions (we `chmod 600` on register).
- MITM between wrapper and orchestrator: HMAC doesn't add
  confidentiality. Defense: TLS (currently local network, no TLS).

## Operational notes

- **Rotating a secret** (v1.6.1+): `POST /api/agents/{id}/rotate-key`
  issues a new secret, sets `old_secret_hash` with grace expiry.
  During the grace window, the old secret is accepted too. Not in
  v1.6 — manual DB update for now.
- **Re-registering**: if you delete the agent row in the DB and
  re-run `hermes-orch-agent register`, the new secret overrides the
  old (one-shot, see `/secret` endpoint).
- **Forensics**: failed HMAC attempts are audited as
  `agent.hmac_auth_failed` with `path`, `method`, and timestamp
  drift. Monitor these for spikes (could indicate misconfigured
  wrapper or active attack).
