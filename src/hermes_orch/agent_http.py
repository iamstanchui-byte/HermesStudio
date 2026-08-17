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

import json
import os
from typing import Any
from urllib.parse import urlparse

import httpx

from hermes_orch.auth.hmac_v07 import sign_v07_request


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
# pick up env changes (or just restart the wrapper). Send SIGHUP to
# the running wrapper to call `reload_verify()` (see below) without
# a full restart.
_VERIFY: bool | str = _compute_verify()


def get_verify() -> bool | str:
    """Return the active `verify` policy. Useful for tests + logging."""
    return _VERIFY


def reload_verify() -> tuple[bool | str, bool | str]:
    """Re-read the TLS verify policy from env vars and update the cache.

    Why this exists
    ---------------
    The verify policy is computed once at import time (`_VERIFY` above)
    and cached in the module global, because the wrapper's heartbeat
    hot path calls `httpx.<method>(url, verify=_VERIFY, ...)` for every
    tick (~5s) and we don't want an env-var re-read on every request.

    The downside: changing `INSECURE_SKIP_TLS_VERIFY` or
    `ORCHESTRATOR_CA_BUNDLE` at runtime has no effect until restart.

    The fix: this function re-runs `_compute_verify()` and updates the
    module global. The wrapper's start function installs a SIGHUP
    handler that calls this -- on Unix, `kill -HUP <pid>` re-reads the
    env without a restart. On Windows (no real SIGHUP), operators
    should use the service manager: `nssm restart <svc>` or
    `sc stop <svc> && sc start <svc>`.

    Returns:
        (old_verify, new_verify) -- useful for the operator to see
        the change took effect in the log.

    See docs/wrapper-runbook.md (TBD) for the operational pattern.
    """
    global _VERIFY
    old = _VERIFY
    _VERIFY = _compute_verify()
    return old, _VERIFY


# === HMAC v0.7 client-side signing (2026-08-16) ===
#
# When the wrapper has an HMAC credential configured, ALL outgoing
# requests to the orchestrator get signed (7 X-Hermes-* headers).
# When no credential is configured, requests go out unsigned (v0.6
# X-Agent-Id is added by the caller separately if needed).
#
# The credential is set ONCE on wrapper startup via
# `set_hmac_credential()`; the wrapper reads it from
# `wrapper-config.json` (`hmac_key_id` + `hmac_secret_hex` fields).
# See docs/proposals/orch-client-build-impl-plan-v0.7.md §1.4.
#
# Why module-level: avoids threading the credential through 23 call
# sites in agent_cli.py. The 7 headers are computed once per request
# (~0.1ms) so this is free in practice.

_HMAC_KEY_ID: str | None = None
_HMAC_SECRET: bytes | None = None


def set_hmac_credential(key_id: str, secret_hex: str) -> None:
    """Configure the wrapper's HMAC v0.7 credential.

    After this call, all outgoing requests are signed with the 7
    X-Hermes-* headers. To disable, call `set_hmac_credential("", "")`
    or restart the wrapper.

    The secret is stored as bytes (decoded from hex). The hex form is
    the on-disk / config-file representation because binary secrets
    don't survive JSON round-trips cleanly.

    Raises:
        ValueError: if `secret_hex` is not valid hex.
    """
    global _HMAC_KEY_ID, _HMAC_SECRET
    _HMAC_KEY_ID = key_id or None
    if secret_hex:
        _HMAC_SECRET = bytes.fromhex(secret_hex)
    else:
        _HMAC_SECRET = None


def get_hmac_credential() -> tuple[str | None, bytes | None]:
    """Return the active HMAC credential (key_id, secret_bytes) for tests."""
    return _HMAC_KEY_ID, _HMAC_SECRET


def has_hmac_credential() -> bool:
    """True iff an HMAC credential is configured. Cheap, used per request."""
    return _HMAC_KEY_ID is not None and _HMAC_SECRET is not None


def _body_bytes_for_hmac(kwargs: dict[str, Any]) -> bytes | None:
    """Extract the body bytes that httpx will send, for HMAC body-SHA256.

    Returns None if the body shape is not amenable to signing (form data,
    streaming, etc.) -- the caller should skip signing in that case and
    let the server's middleware reject the request (no silent corruption).

    Matches httpx's encoding for the kwargs we use in agent_cli:
      - `content=<bytes/str>` -> as-is (or .encode("utf-8") if str)
      - `json=<dict>` -> json.dumps(json, separators=(",", ":")).encode("utf-8")
        (the default compact form; httpx uses this)
      - `data=<...>` -> form data, NOT signed (HMAC is for JSON requests)
      - none of the above -> b"" (empty body)

    Note: the EXACT bytes httpx sends depend on its internal serializer.
    We use the same default JSON encoder to keep parity. If a future
    httpx change diverges, the server will reject with BODY_HASH_MISMATCH
    and the operator will see clear logs.
    """
    content = kwargs.get("content")
    if content is not None:
        if isinstance(content, str):
            return content.encode("utf-8")
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)
        return None
    json_data = kwargs.get("json")
    if json_data is not None:
        # Default httpx JSON encoding: separators=(",", ":") (compact).
        # json.dumps default separators are (', ', ': ') -- NOT compact.
        # Match httpx explicitly to avoid body-hash mismatch on the server.
        try:
            return json.dumps(
                json_data,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            return None
    # `data=` is form-encoded; HMAC §1.4 expects JSON. Skip signing.
    if "data" in kwargs:
        return None
    # Nothing in the kwargs -> empty body. Sign as b"" (the well-known
    # empty-body SHA-256 is a valid value per v0.7 §1.4).
    return b""


def _signed_headers(method: str, url: str, kwargs: dict[str, Any]) -> dict[str, str]:
    """Compute the 7 X-Hermes-* headers for this request, or {} if not configured."""
    if not has_hmac_credential():
        return {}
    body = _body_bytes_for_hmac(kwargs)
    if body is None:
        # Form data or unencodable payload. Don't sign -- server will
        # reject with a clear 401 if the path requires HMAC.
        return {}
    # Path-only: v0.7 §1.4 forbids query strings on signed paths.
    # urlparse is cheap (~0.01ms) and avoids accidentally including
    # the orchestrator host or query in the signed material.
    path = urlparse(url).path
    return sign_v07_request(
        method=method,
        path=path,
        body=body,
        key_id=_HMAC_KEY_ID,  # type: ignore[arg-type]
        secret=_HMAC_SECRET,  # type: ignore[arg-type]
    )


def _merge_headers(kwargs: dict[str, Any], signed: dict[str, str]) -> dict[str, str]:
    """Merge signed headers into the request's `headers=` kwarg.

    Existing headers (e.g. the v0.6 `X-Agent-Id` fallback) are
    preserved -- signing is additive. The signed headers always win
    on collision (defense: caller-supplied X-Hermes-* could be
    malicious; the module's signing is the source of truth).
    """
    if not signed:
        return kwargs.get("headers") or {}
    existing = dict(kwargs.get("headers") or {})
    existing.update(signed)
    return existing


# ===== httpx method wrappers =====
#
# Each wrapper:
#   1. Resolves TLS verify policy (existing behavior)
#   2. If HMAC credential is configured, injects the 7 X-Hermes-* headers
#      (new in 2026-08-16; signing is opt-in via set_hmac_credential)
#   3. Forwards to httpx.<method>

def get(url: str, **kwargs: Any):
    """httpx.get with the wrapper's verify policy + (optional) HMAC headers applied."""
    signed = _signed_headers("GET", url, kwargs)
    if signed:
        kwargs = {**kwargs, "headers": _merge_headers(kwargs, signed)}
    return httpx.get(url, verify=_VERIFY, **kwargs)


def post(url: str, **kwargs: Any):
    """httpx.post with the wrapper's verify policy + (optional) HMAC headers applied."""
    signed = _signed_headers("POST", url, kwargs)
    if signed:
        kwargs = {**kwargs, "headers": _merge_headers(kwargs, signed)}
    return httpx.post(url, verify=_VERIFY, **kwargs)


def put(url: str, **kwargs: Any):
    """httpx.put with the wrapper's verify policy + (optional) HMAC headers applied."""
    signed = _signed_headers("PUT", url, kwargs)
    if signed:
        kwargs = {**kwargs, "headers": _merge_headers(kwargs, signed)}
    return httpx.put(url, verify=_VERIFY, **kwargs)


def patch(url: str, **kwargs: Any):
    """httpx.patch with the wrapper's verify policy + (optional) HMAC headers applied."""
    signed = _signed_headers("PATCH", url, kwargs)
    if signed:
        kwargs = {**kwargs, "headers": _merge_headers(kwargs, signed)}
    return httpx.patch(url, verify=_VERIFY, **kwargs)


def delete(url: str, **kwargs: Any):
    """httpx.delete with the wrapper's verify policy + (optional) HMAC headers applied."""
    signed = _signed_headers("DELETE", url, kwargs)
    if signed:
        kwargs = {**kwargs, "headers": _merge_headers(kwargs, signed)}
    return httpx.delete(url, verify=_VERIFY, **kwargs)


# ===== Scheme-fallback helpers (2026-08-16 wrapper self-heal) =====
#
# Production story:
#   1. Operator flipped the server from HTTP to HTTPS on 2026-08-15.
#   2. Every wrapper with `orchestrator_url: "http://..."` in
#      wrapper-config.json broke. They couldn't reach the server any
#      more (port 8765 is HTTPS-only now).
#   3. We manually SSH'd into each agent host and edited the JSON
#      to flip the URL to https. Worked for 2 hosts, but doesn't
#      scale.
#
# Design:
#   When a wrapper's heartbeat / config-poll / ack call gets a
#   TCP-level failure (connection refused, timeout, "server
#   disconnected"), the wrapper now retries with the other scheme
#   (http <-> https). On success the wrapper re-reads `/api/server/info`
#   to learn the canonical URL, then writes it to
#   `wrapper-config.json` so the next restart uses the new URL.
#
#   The retry is bounded to ONE attempt with the other scheme. If
#   both fail, the LAST error propagates so the existing logging +
#   back-off behaviour continues to work. The retry is suppressed
#   for non-connection failures (e.g. invalid URL, HTTP 5xx) because
#   swapping scheme would not help and could mask real bugs.
#
# Security:
#   With `INSECURE_SKIP_TLS_VERIFY=1` (test mode, self-signed cert)
#   an MITM on either scheme is possible -- fallback is no worse
#   than the current state. With cert verification on (certifi or
#   ORCHESTRATOR_CA_BUNDLE) the wrapper already rejects MITM certs;
#   fallback is safe. Long-term the v0.7.3 cert fingerprint pin
#   eliminates the concern entirely.


def _swap_scheme(url: str) -> str:
    """Return `url` with its scheme toggled http<->https. Path, query,
    fragment, userinfo, and port are preserved verbatim.

    The function is intentionally narrow: it only handles `http` and
    `https`. Other schemes (e.g. `ws://`, `ftp://`) pass through
    unchanged so we don't accidentally rewrite a non-HTTP URL the
    caller is using for a different purpose. Non-strings raise.
    """
    if not isinstance(url, str):
        raise TypeError(f"_swap_scheme requires str, got {type(url).__name__}")
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    # unknown / non-http scheme: pass through. Callers should pre-validate.
    return url


def _classify_failure(exc: BaseException) -> str:
    """Classify a httpx exception for the fallback decision.

    Returns one of:
      - "connection"  : swap scheme and retry (TCP / TLS / wire errors)
      - "other"       : propagate as-is (invalid URL, HTTP status, etc.)

    'connection' covers the cases where the server MIGHT be on the
    other scheme (e.g. an HTTPS server returns nothing over plain
    HTTP, which the kernel reports as a connection failure). 'other'
    covers everything else, where swapping scheme would just turn
    one failure into another.
    """
    import httpx as _httpx
    # ConnectError = ConnectError (TCP refused / no route / etc.)
    # ConnectTimeout / ReadTimeout / PoolTimeout = transient network
    # RemoteProtocolError = wire-level garbage (e.g. plain HTTP
    #   against HTTPS) -- exactly the symptom of a server-flip
    if isinstance(exc, (
        _httpx.ConnectError,
        _httpx.ConnectTimeout,
        _httpx.ReadTimeout,
        _httpx.PoolTimeout,
        _httpx.RemoteProtocolError,
    )):
        return "connection"
    return "other"


def request_with_fallback(method: str, url: str, **kwargs: Any):
    """Call `httpx.<method>(url, ...)` with the wrapper's verify policy
    applied. On a classified "connection" failure, retry once with the
    other scheme. Return `(response, actual_url)`.

    On a non-connection failure, the exception propagates immediately
    (no retry). If both attempts fail, the LAST exception propagates
    so the caller's existing error handling / back-off still fires.

    The caller is expected to:
      1. Use this for periodic checks (heartbeat, config poll, ack).
      2. After a successful call, fetch `/api/server/info` to learn
         the canonical URL.
      3. If the canonical URL differs from the configured URL, write
         it to wrapper-config.json. Next restart uses the new URL.
    """
    # Lazy import to avoid a circular import: agent_cli imports
    # agent_http at module load, so importing httpx here would
    # potentially re-enter agent_cli.
    import httpx as _httpx

    # Map method name to httpx.<method> callable.
    fn_name = method.lower()
    fn = getattr(_httpx, fn_name, None)
    if fn is None:
        raise ValueError(f"unsupported HTTP method: {method!r}")

    try:
        # Inject HMAC v0.7 headers if a credential is configured.
        # Sign once for the primary URL; the alt URL reuses the same
        # headers (path doesn't change between http<->https swap).
        signed = _signed_headers(method, url, kwargs)
        call_kwargs = kwargs if not signed else {**kwargs, "headers": _merge_headers(kwargs, signed)}
        resp = fn(url, verify=_VERIFY, **call_kwargs)
        return resp, url
    except BaseException as exc:
        kind = _classify_failure(exc)
        if kind != "connection":
            raise
        # Try the other scheme. If THAT also fails with a connection
        # error, surface the new error (which is more recent and
        # likely more informative -- e.g. "no HTTPS server on 8765"
        # beats the original "no HTTP server on 8765" when the
        # server actually moved to HTTPS).
        alt_url = _swap_scheme(url)
        resp = fn(alt_url, verify=_VERIFY, **call_kwargs)
        return resp, alt_url


# ===== Client class (v0.7-aware connection pool) =====
#
# Long-running daemon loops (the wrapper's config-poll, skills-sync, etc.)
# want a connection pool instead of one TCP+TLS handshake per request.
# `httpx.Client(...)` is the natural fit, but the raw `httpx.Client`
# bypasses this module's HMAC-injection logic. This `Client` class is
# a drop-in replacement: same constructor, same `get/post/put/patch/delete`
# surface, but every outbound request gets the 7 X-Hermes-* headers
# injected (when a credential is configured) and the wrapper's
# `verify` policy applied.
#
# Usage:
#     with agent_http.Client(timeout=10) as client:
#         r = client.get(url)
#         r = client.post(url, json=...)
#
# Why not subclass httpx.Client and only override .request()? Because
# httpx's surface is large; subclassing risks subtle signature drift.
# Wrapping and exposing only the methods the wrapper actually uses
# (get/post/put/patch/delete + request) keeps the API small and
# audited. If you need other httpx methods, add them here.
class Client:
    """v0.7-aware httpx.Client wrapper. See module docstring for rationale.

    The constructor accepts the same kwargs as `httpx.Client` (most
    commonly `timeout=`, `verify=`). `verify=` defaults to this module's
    cached policy if not passed (or if `None` is passed).
    """

    def __init__(self, **kwargs: Any) -> None:
        # Honor the module's TLS verify policy when caller doesn't
        # explicitly pass verify=... (or passes verify=None).
        if "verify" not in kwargs or kwargs.get("verify") is None:
            kwargs["verify"] = _VERIFY
        self._client = httpx.Client(**kwargs)

    # ---- passthrough context manager ----
    def __enter__(self) -> "Client":
        self._client.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._client.__exit__(*args)

    def close(self) -> None:
        self._client.close()

    # ---- low-level: every method funnels through here ----
    def request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        data: Any = None,
        files: Any = None,
        json: Any = None,
        params: Any = None,
        headers: dict[str, str] | None = None,
        cookies: Any = None,
        auth: Any = None,
        follow_redirects: bool = False,
        timeout: Any = None,
        extensions: Any = None,
    ) -> Any:
        """Issue an HTTP request, auto-injecting v0.7 HMAC headers if a
        credential is configured. The signature matches the subset of
        `httpx.Client.request` that the wrapper actually uses.
        """
        kwargs: dict[str, Any] = {
            "content": content,
            "data": data,
            "files": files,
            "json": json,
            "params": params,
            "cookies": cookies,
            "auth": auth,
            "follow_redirects": follow_redirects,
            "timeout": timeout,
            "extensions": extensions,
        }
        # Compute v0.7 signed headers (returns {} if no credential).
        signed = _signed_headers(method, url, kwargs)
        if signed:
            headers = _merge_headers({"headers": headers}, signed)["headers"]
        return self._client.request(method, url, headers=headers, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> Any:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self.request("DELETE", url, **kwargs)
