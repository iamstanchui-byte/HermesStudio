# coding: utf-8
"""Canonical public-origin startup validation (security hotfix 2026-08-11).

The server's canonical public origin is configured once at startup
(`HERMES_ORCH_PUBLIC_ORIGIN` env var or `server.public_origin` in
`config.yaml`). It is the origin that the browser-issued dashboard
uses, and it is the allowlist used by the CSRF helper in
`csrf.py::require_same_origin`.

Validation contract (per
`docs/security/agent-endpoint-auth-hotfix-design.md` §6.1, R13):

    validate_public_origin(value: str) -> str

  - Returns the canonical (no trailing slash) form on success.
  - Raises `ValueError` with a human-readable reason on any failure.

Valid:

    "http://192.168.2.152:8765"
    "http://localhost:8765"
    "https://orchestrator.example.com:443"

Rejected (each with its own ValueError message):

    None / ""                → unset
    "not-a-url"              → no scheme
    "ftp://host:8765"        → wrong scheme (only http / https)
    "ws://host:8765"         → wrong scheme
    "http://host/dashboard"  → has path
    "http://host:8765/"      → trailing slash (which is a path "/")
    "http://host:8765?x=1"   → has query
    "http://host:8765#frag"  → has fragment
    "http://user:pass@host"  → has userinfo
    "http://host"            → no explicit port
    "http://host:not-a-port" → unparseable port
    "http://"                → no hostname
    "http://host:99999"      → port out of range (1..65535)

Startup hook:

    main.py's `lifespan()` calls `validate_public_origin` BEFORE
    binding to a port. If validation fails, the lifespan raises and
    the server never starts. This is the FAIL-CLOSED design.

Why startup-time, not request-time:

    - Server misconfiguration is a deployment-time concern, not a
      request-time concern.
    - request-time failure (e.g. 500 on a missing origin) gives a
      worse operator experience than refusing to start at all.
    - Defense-in-depth: `csrf.py::require_same_origin` STILL has a
      defensive `try/except ValueError` around `parsed.port` to
      avoid leaking a 500 if the underlying URL parser hits an edge
      case — but in production that path is unreachable.
"""
from __future__ import annotations

from urllib.parse import urlparse


def validate_public_origin(value: str | None) -> str:
    """Validate the canonical public origin for CSRF / session security.

    Returns the canonical form (no trailing slash) on success.
    Raises `ValueError` on any failure with a clear reason.
    """
    if value is None:
        raise ValueError(
            "HERMES_ORCH_PUBLIC_ORIGIN / server.public_origin is not set. "
            "Set it in config.yaml (server.public_origin) or as the env var "
            "HERMES_ORCH_PUBLIC_ORIGIN to your dashboard's public origin "
            "(e.g. 'http://192.168.2.152:8765')."
        )
    if not isinstance(value, str):
        raise ValueError(
            f"public_origin must be a string, got {type(value).__name__}"
        )
    stripped = value.strip()
    if not stripped:
        raise ValueError(
            "public_origin is empty. Set it to a non-empty absolute URL "
            "(e.g. 'http://192.168.2.152:8765')."
        )

    # urlparse is forgiving — it doesn't reject 'http://' as a scheme
    # problem, it just gives back an empty netloc. We layer our own
    # checks on top.
    parsed = urlparse(stripped)

    # Scheme: must be http or https (no ftp, ws, file, data, javascript, etc.).
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"public_origin scheme must be 'http' or 'https', got "
            f"{parsed.scheme!r} (from value {value!r})."
        )

    # Hostname: must be present and non-empty.
    if not parsed.hostname:
        raise ValueError(
            f"public_origin must include a hostname, got {value!r}."
        )

    # Port: must be present and parseable. urlparse does not raise
    # for unparseable ports — it stores the raw value. We re-parse
    # explicitly and reject anything that's not 1..65535.
    if parsed.port is None:
        raise ValueError(
            f"public_origin must include an explicit port, got {value!r} "
            f"(scheme={parsed.scheme!r}, hostname={parsed.hostname!r}, "
            f"port=None). Add the port to your origin (e.g. ':8765')."
        )
    if not (1 <= parsed.port <= 65535):
        raise ValueError(
            f"public_origin port {parsed.port} is out of range 1..65535 "
            f"(from value {value!r})."
        )

    # Path: must be empty (bare origin contract). urlparse normalizes
    # "http://host:8765/" to path="/" — we reject that too.
    if parsed.path not in ("", "/"):
        raise ValueError(
            f"public_origin must not include a path, got path={parsed.path!r} "
            f"(from value {value!r}). Use the bare origin like "
            f"'{parsed.scheme}://{parsed.hostname}:{parsed.port}'."
        )
    # Be even stricter: "/"" is also rejected. The bare-origin contract
    # is empty path.
    if parsed.path == "/":
        raise ValueError(
            f"public_origin must not include a trailing '/', got "
            f"{value!r}. Use the bare origin like "
            f"'{parsed.scheme}://{parsed.hostname}:{parsed.port}'."
        )

    # Query: must be empty.
    if parsed.query:
        raise ValueError(
            f"public_origin must not include a query string, got "
            f"query={parsed.query!r} (from value {value!r})."
        )

    # Fragment: must be empty.
    if parsed.fragment:
        raise ValueError(
            f"public_origin must not include a fragment, got "
            f"fragment={parsed.fragment!r} (from value {value!r})."
        )

    # Userinfo: must be absent. urlparse populates .username / .password
    # independently — both must be None.
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            f"public_origin must not include userinfo (user:pass@), got "
            f"value {value!r}."
        )

    # All checks passed. Return the canonical form (no trailing slash).
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
