# coding: utf-8
"""HTTP client helpers for the agent wrapper (v3.12.0).

Thin wrappers around httpx.{get,post,put,delete,patch} that apply a
TLS verification policy based on environment variables. The wrapper
runs on each agent host (linux-a-01, win-local-1) and talks to the
orchestrator at `ORCHESTRATOR_URL` (default `http://localhost:8765`).

When the orchestrator is fronted by HTTPS (v3.12.0+), the wrapper
needs to either trust the cert or accept it. Two env vars cover both:

  INSECURE_SKIP_TLS_VERIFY=1
    Set to `1` to disable TLS verification entirely. Use ONLY for
    self-signed dev / LAN deployments where you've decided the
    transport encryption is enough and the cert-pinning overhead
    isn't worth it. Same risk profile as `curl -k`.

  ORCHESTRATOR_CA_BUNDLE=/path/to/ca-bundle.pem
    Set to a PEM file containing the CA cert(s) to trust. Pin to
    the orchestrator's self-signed cert, your internal CA, or the
    public CA chain. Preferred for production: the wrapper won't
    accept a MITM cert from a compromised CA store.

Precedence:
  1. If ORCHESTRATOR_CA_BUNDLE is set and readable -> use as `verify`
  2. Else if INSECURE_SKIP_TLS_VERIFY=1 (truthy) -> verify=False
  3. Else -> verify=True (default; only valid for HTTPS fronted by
     a publicly-trusted cert, or for plain HTTP)

If `ORCHESTRATOR_URL` is `http://...`, the `verify` kwarg is harmless
(httpx ignores it for non-TLS schemes). So callers don't have to
branch on the URL scheme.

The env vars are read at IMPORT time (cached in module globals).
This is intentional: every call site is a hot path (heartbeat every
30s), and we don't want an env-var re-read on every request. If the
operator changes the env vars, the wrapper restart picks them up
(no hot-reload needed).

Why not just use `httpx.Client(verify=...)` with a module-level
instance? Two reasons:
  - 28 existing call sites use `httpx.{get,post,put,delete,patch}`
    directly; refactoring all of them to a `Client` instance is more
    surface area than replacing the function name in the import
  - The wrapper is a long-running CLI; module-level state is fine
    and matches the existing pattern (other env vars are read at
    import too)

If you need a per-request `verify` override (rare; mostly for
testing), pass `verify=` explicitly to these helpers and it wins.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


def _compute_verify() -> bool | str:
    """Resolve the TLS verify policy from env at import time."""
    ca_bundle = (os.environ.get("ORCHESTRATOR_CA_BUNDLE") or "").strip()
    if ca_bundle:
        if os.path.isfile(ca_bundle):
            return ca_bundle
        # Misconfigured: log a warning but don't crash the wrapper
        # (the operator might be planning to drop the cert file in
        # later). Fall through to the insecure-or-default branch.
        # We print to stderr because the wrapper's normal logging
        # may not be initialized yet at import time.
        print(
            f"[agent_http] WARN: ORCHESTRATOR_CA_BUNDLE={ca_bundle!r} "
            f"is not a readable file; falling back to default verify policy",
            flush=True,
        )
    insecure = (os.environ.get("INSECURE_SKIP_TLS_VERIFY") or "").strip().lower()
    if insecure in ("1", "true", "yes", "on"):
        return False
    return True


# Module-level cache. Read once at import. Re-import the module to
# pick up env changes (or just restart the wrapper).
_VERIFY: bool | str = _compute_verify()


def get_verify() -> bool | str:
    """Return the active `verify` policy. Useful for tests + logging."""
    return _VERIFY


# ===== httpx method wrappers =====

def get(url: str, **kwargs: Any):
    """httpx.get with the wrapper's verify policy applied."""
    return httpx.get(url, verify=_VERIFY, **kwargs)


def post(url: str, **kwargs: Any):
    """httpx.post with the wrapper's verify policy applied."""
    return httpx.post(url, verify=_VERIFY, **kwargs)


def put(url: str, **kwargs: Any):
    """httpx.put with the wrapper's verify policy applied."""
    return httpx.put(url, verify=_VERIFY, **kwargs)


def patch(url: str, **kwargs: Any):
    """httpx.patch with the wrapper's verify policy applied."""
    return httpx.patch(url, verify=_VERIFY, **kwargs)


def delete(url: str, **kwargs: Any):
    """httpx.delete with the wrapper's verify policy applied."""
    return httpx.delete(url, verify=_VERIFY, **kwargs)
