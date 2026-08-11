# coding: utf-8
"""Admin authentication gate (security hotfix 2026-08-11, B12).

Single FastAPI dependency that resolves the current user from the
session cookie and verifies the user has the admin role.

Contract (per `docs/security/agent-endpoint-auth-hotfix-design.md` §4):

    async def require_admin(request: Request) -> dict:
        user = await current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        if user.get("role") != ROLE_ADMIN:
            raise HTTPException(403, "Admin role required")
        return user

`user` is the full row from the `users` table (id, username, role,
disabled, ...). Endpoints use `user['username']` for the audit actor.

Why a separate module (instead of co-locating with the existing
`require_admin` in `api/users.py`):

  - Keeps the admin gate next to the other auth concerns (cookie, hmac)
    so future endpoints that need admin auth have an obvious import.
  - `api/users.py`'s `require_admin` is fine and is kept; this module
    is the canonical dependency for the 7 B12 admin-mutation routes in
    `api/agents.py` and any future admin-gated route.

Distinguishing 401 vs 403:
  - 401: no session, expired session, disabled user, or deleted user.
    "I don't know who you are" — the middleware would also return 401
    in that case but `require_admin` is called AFTER the middleware, so
    endpoints can rely on `require_admin` for a single source of truth.
  - 403: known user, but not admin. "I know who you are, you can't do
    this". Lets the client distinguish "log in" from "you don't have
    permission".
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from hermes_orch.auth.cookie import ROLE_ADMIN, current_user


async def require_admin(request: Request) -> dict:
    """FastAPI dependency: must be a logged-in admin user.

    Returns the full user row (dict). The endpoint can use
    `user['username']` for audit `actor=f"admin:{user['username']}"`.

    Raises:
        HTTPException(401): no session / expired / disabled / not found.
        HTTPException(403): logged in but not admin.
    """
    user = await current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "Admin role required")
    return user
