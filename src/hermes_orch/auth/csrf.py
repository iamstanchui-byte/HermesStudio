# coding: utf-8
"""CSRF defense-in-depth for browser-issued state-changing requests
(security hotfix 2026-08-11, B12).

`SameSite=Lax` on the session cookie is the primary CSRF defense.
This helper is defense-in-depth: it confirms the request's
`Origin` (or, if absent, `Referer`) matches the canonical
configured public origin (`HERMES_ORCH_PUBLIC_ORIGIN`).

Contract (per
`docs/security/agent-endpoint-auth-hotfix-design.md` §6.1, R14):

    require_same_origin(request: Request) -> None

  - GET / HEAD / OPTIONS → return (safe methods).
  - Origin header present:
      - MUST be a BARE origin: no path / query / fragment / userinfo.
        A single "/" is also rejected.
      - If bare-origin contract holds, compare (scheme, hostname, port)
        against the canonical public origin.
      - If match → return; else → 403.
  - Origin header absent, Referer header present:
      - Reject userinfo; reject malformed port (ValueError → 403).
      - Compare (scheme, hostname, port) of Referer against the
        canonical public origin. Referer MAY have a path/query/fragment.
      - If match → return; else → 403.
  - Both absent → 403 (no way to verify origin).

Why Origin and Referer are NOT combined with `or`:

    They have DIFFERENT contracts. `Origin` (when present) is a
    bare origin (no path). `Referer` (when used as fallback) is a
    full URL (path allowed). Combining them via
    `headers.get("origin") or headers.get("referer")` would let an
    attacker use a Referer-shaped Origin to bypass the bare-origin
    check. See R14 in the design doc for the exact allowlist bypass
    this fix prevents.

HMAC-authed agent requests don't carry cookies; they go through a
different path (X-Agent-Id + X-Signature) and are NOT subject to
this check. They are excluded by route-level Depends wiring, not
by this helper.
"""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import HTTPException, Request


def _origin_match(actual_url, expected_origin: str) -> bool:
    """Compare parsed (scheme, hostname, port) of two origins.

    `actual_url` is a `urllib.parse.ParseResult` from
    `urlparse(header_value)`. `expected_origin` is the canonical
    bare-origin string (e.g. `http://192.168.2.152:8765`).

    Returns True iff scheme + hostname + port all match.

    This helper deliberately ignores path / query / fragment /
    userinfo on `actual_url` — those are separately checked by the
    caller when relevant (Origin header contract forbids them;
    Referer allows path).

    Defense-in-depth: the caller already wrapped `expected_origin`
    in a `try/except ValueError` at startup; this helper adds the
    same backstop for the request-time value.
    """
    expected = urlparse(expected_origin)
    try:
        actual_port = actual_url.port
        expected_port = expected.port
    except ValueError:
        # Malformed port in the request-time value (e.g.
        # `Origin: http://host:not-a-port`). Caller treats 403.
        return False
    if expected_port is None:
        # Should be unreachable because startup validation enforces
        # an explicit port, but be defensive: a non-canonical
        # expected_origin would let requests through.
        return False
    return (
        actual_url.scheme == expected.scheme
        and actual_url.hostname == expected.hostname
        and actual_port == expected_port
    )


def require_same_origin(request: Request) -> None:
    """Reject cross-origin state-changing requests.

    Reads the canonical public origin from `request.app.state.public_origin`
    (set by `main.py::lifespan()` from `HERMES_ORCH_PUBLIC_ORIGIN` after
    startup-time validation in `origin_validation.validate_public_origin`).

    Raises HTTPException(403) on any of:
      - Origin / Referer both absent
      - Origin has a path, query, fragment, or userinfo
      - Origin / Referer do not match the canonical origin
      - Origin / Referer is structurally invalid (unparseable port, etc.)
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return  # safe methods are not subject to CSRF checks

    expected_origin: str | None = getattr(
        request.app.state, "public_origin", None
    )
    if not expected_origin:
        # If this is reachable, the startup hook missed setting the
        # canonical origin. Fail-closed: refuse the request rather
        # than silently allow it. In production, the lifespan's
        # `validate_public_origin` raises before this code path is
        # reachable, so this is defense-in-depth.
        raise HTTPException(
            503,
            "Server is not configured with a public origin. "
            "Set HERMES_ORCH_PUBLIC_ORIGIN / server.public_origin.",
        )

    origin_header = request.headers.get("origin")
    if origin_header:
        # === Origin present: require a bare origin (no path / query / fragment / userinfo) ===
        a = urlparse(origin_header)

        # The bare-origin contract: path MUST be empty. urlparse
        # normalizes the trailing-slash input "http://host:8765/" to
        # path="/", which we reject. Anything non-empty (including "/")
        # is an attacker-supplied header (a real browser would never
        # put a path in Origin).
        if a.path != "":
            raise HTTPException(
                403,
                f"Origin must be a bare origin (no path): {origin_header!r}",
            )
        if a.query:
            raise HTTPException(
                403,
                f"Origin must not have a query string: {origin_header!r}",
            )
        if a.fragment:
            raise HTTPException(
                403,
                f"Origin must not have a fragment: {origin_header!r}",
            )
        if a.username is not None or a.password is not None:
            raise HTTPException(
                403,
                f"Origin must not contain userinfo: {origin_header!r}",
            )
        # Defense-in-depth: parsed.port raises ValueError on
        # unparseable ports (e.g. `http://host:not-a-port`).
        try:
            _ = a.port  # may raise ValueError
        except ValueError:
            raise HTTPException(
                403,
                f"Origin has an unparseable port: {origin_header!r}",
            )

        if not _origin_match(a, expected_origin):
            raise HTTPException(
                403,
                f"Cross-origin request rejected "
                f"(origin={origin_header!r}, expected={expected_origin!r})",
            )
        return

    # === Origin absent, fall back to Referer (if any) ===
    referer_header = request.headers.get("referer")
    if not referer_header:
        raise HTTPException(
            403,
            "Missing Origin/Referer for state-changing request",
        )
    r = urlparse(referer_header)
    if r.username is not None or r.password is not None:
        raise HTTPException(
            403,
            f"Referer must not contain userinfo: {referer_header!r}",
        )
    # Defense-in-depth: parsed.port raises ValueError on
    # unparseable ports.
    try:
        _ = r.port  # may raise ValueError
    except ValueError:
        raise HTTPException(
            403,
            f"Referer has an unparseable port: {referer_header!r}",
        )

    # Referer MAY have a path / query / fragment; we only check the
    # origin tuple (scheme, hostname, port).
    if not _origin_match(r, expected_origin):
        raise HTTPException(
            403,
            f"Cross-origin request rejected "
            f"(referer={referer_header!r}, expected={expected_origin!r})",
        )
