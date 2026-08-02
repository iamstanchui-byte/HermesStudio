# coding: utf-8
"""Auth endpoints (v3.4 dashboard user auth, 2026-07-31).

Two layers of auth co-exist in the orchestrator:

  1. Agent HMAC auth (`/api/agents/*/heartbeat` etc.) — the agent on
     the host proves it's a known wrapper by signing the request.
     Lives in `hermes_orch.auth.hmac` and is mounted with
     `Depends(require_hmac_auth)` per-route.

  2. Dashboard user cookie auth (this file) — the human at the
     browser proves they're allowed to see the dashboard by holding
     a signed session cookie. Set by /api/auth/login, cleared by
     /api/auth/logout. Gated by middleware in main.py.

Endpoints:
  POST /api/auth/login          — username + password → cookie
  POST /api/auth/setup          — bootstrap admin sets initial password
  POST /api/auth/logout         — clear cookie
  GET  /api/auth/me             — current user info (or 401)
  POST /api/auth/password       — change own password (logged in)
  GET  /login                   — login page (HTML)
  GET  /setup-password          — bootstrap setup page (HTML)
  POST /logout                  — logout (form action, redirects to /login)

The bootstrap flow:
  1. `hermes-orch init` creates a user with username="admin" and
     password_hash=NULL. is_bootstrap_admin=1.
  2. User opens /login, enters "admin" + any password.
  3. Server sees password_hash IS NULL → returns 401 with
     {detail: "must_set_password", username: "admin"}.
  4. Frontend redirects to /setup-password?username=admin.
  5. User enters new password twice → POST /api/auth/setup.
  6. Server hashes + sets cookie + redirects to /.

After setup, subsequent logins use the normal /api/auth/login flow.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from hermes_orch.auth.cookie import (
    BOOTSTRAP_ADMIN_USERNAME,
    COOKIE_NAME,
    DEFAULT_MAX_AGE_SEC,
    ROLE_ADMIN,
    ROLE_USER,
    clear_session_cookie,
    constant_time_eq,
    create_user,
    current_user,
    current_user_id,
    get_user_by_username,
    hash_password,
    parse_session_cookie_value,
    require_user,
    set_session_cookie,
    set_user_password,
    touch_last_login,
    verify_password,
)
from hermes_orch.core.audit import audit_log


router = APIRouter()  # JSON API, mounted at /api/auth
page_router = APIRouter()  # HTML pages, mounted at / (no prefix)

# Templates (separate from dashboard.py's instance so /login + /setup-password
# don't need the full base context). The login page is fully standalone — no
# sidebar, no nav, just a centered card. Keeps the unauthenticated UX clean
# and avoids leaking any layout details to a non-logged-in user.
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_auth_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
_auth_templates.env.auto_reload = True


# ===== Pydantic request models =====

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=0, max_length=512)


class SetupIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    new_password: str = Field(min_length=8, max_length=512)
    confirm_password: str = Field(min_length=8, max_length=512)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=0, max_length=512)
    new_password: str = Field(min_length=8, max_length=512)
    confirm_password: str = Field(min_length=8, max_length=512)


# ===== Endpoints =====

@router.post("/login")
async def login(payload: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    """Username + password → signed session cookie.

    Special case: if the user exists and is the bootstrap admin with
    no password set yet, returns 401 with `must_set_password=true` so
    the frontend can route them to the /setup-password flow.
    """
    db = request.app.state.db
    user = await get_user_by_username(db, payload.username)

    if not user:
        # Run a dummy bcrypt to keep response time similar to a real
        # verify (defense against username-enumeration via timing).
        verify_password("decoy", "$2b$12$" + "x" * 53)
        raise HTTPException(401, "Invalid username or password")

    if user.get("disabled"):
        # Don't reveal whether the account exists; same response.
        raise HTTPException(401, "Invalid username or password")

    # Bootstrap admin with no password set → must go through /setup.
    if user.get("password_hash") is None:
        if user.get("is_bootstrap_admin"):
            raise HTTPException(
                status_code=401,
                detail={
                    "message": "must_set_password",
                    "must_set_password": True,
                    "username": user["username"],
                },
            )
        # Non-bootstrap user with NULL password is a config error.
        # Treat as disabled rather than 500.
        raise HTTPException(401, "Invalid username or password")

    if not verify_password(payload.password, user["password_hash"]):
        try:
            await audit_log(
                db,
                "auth.login_failed",
                actor="user",
                payload={"username": payload.username, "reason": "bad_password"},
            )
        except Exception:
            pass
        raise HTTPException(401, "Invalid username or password")

    # Success: set cookie + touch last_login + audit.
    set_session_cookie(response, user["id"], request=request)
    await touch_last_login(db, user["id"])
    try:
        await audit_log(
            db,
            "auth.login",
            actor="user",
            user_id=user["id"],
            payload={"username": user["username"]},
        )
    except Exception:
        pass

    return {
        "ok": True,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user.get("role", ROLE_USER),
        },
    }


@router.post("/setup")
async def setup_password(payload: SetupIn, request: Request, response: Response) -> dict[str, Any]:
    """Bootstrap flow: set the initial password for a user.

    Only allowed for the bootstrap admin (is_bootstrap_admin=1) and
    only when password_hash IS NULL. After setup, password_hash is
    set and a session cookie is returned (logs the user in
    immediately so they don't have to type the password twice).
    """
    if payload.new_password != payload.confirm_password:
        raise HTTPException(400, "Passwords do not match")
    if len(payload.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    db = request.app.state.db
    user = await get_user_by_username(db, payload.username)
    if not user or not user.get("is_bootstrap_admin") or user.get("password_hash") is not None:
        # Don't reveal which condition failed.
        raise HTTPException(400, "Setup not allowed for this user")

    await set_user_password(db, user["id"], payload.new_password)
    set_session_cookie(response, user["id"], request=request)
    await touch_last_login(db, user["id"])
    try:
        await audit_log(
            db,
            "auth.bootstrap_setup",
            actor="user",
            user_id=user["id"],
            payload={"username": user["username"]},
        )
    except Exception:
        pass

    return {
        "ok": True,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user.get("role", ROLE_ADMIN),
        },
    }


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    user_id: str = Depends(current_user_id),
) -> dict[str, bool]:
    """Clear the session cookie. Idempotent — works even if no cookie
    is set, since the response is just `Set-Cookie: ...; Max-Age=0`.
    """
    clear_session_cookie(response)
    if user_id:
        try:
            db = request.app.state.db
            await audit_log(
                db,
                "auth.logout",
                actor="user",
                user_id=user_id,
            )
        except Exception:
            pass
    return {"ok": True}


@router.get("/me")
async def me(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    """Return the current user row, or 401. Used by the frontend
    `currentUser` fetch on page load to decide which header to show
    (Sign-in link vs user pill).
    """
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user.get("role", ROLE_USER),
        "is_bootstrap_admin": bool(user.get("is_bootstrap_admin", 0)),
        "last_login_at": user.get("last_login_at"),
    }


@router.post("/password")
async def change_password(
    payload: ChangePasswordIn,
    request: Request,
    user_id: str = Depends(require_user),
) -> dict[str, bool]:
    """Change the logged-in user's password. Requires the old
    password (anti-hijack: stolen cookie alone can't escalate).
    """
    if payload.new_password != payload.confirm_password:
        raise HTTPException(400, "Passwords do not match")
    if len(payload.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    db = request.app.state.db
    from hermes_orch.auth.cookie import get_user_by_username
    # We need the user row to verify the old password. current_user_id
    # is enough; look up the row.
    user = await db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(401, "User not found")

    if not verify_password(payload.old_password, user.get("password_hash")):
        raise HTTPException(401, "Old password is incorrect")

    await set_user_password(db, user_id, payload.new_password)
    try:
        await audit_log(
            db,
            "auth.password_changed",
            actor="user",
            user_id=user_id,
        )
    except Exception:
        pass
    return {"ok": True}


# ===== HTML page routes =====

@page_router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Login page. Standalone HTML (no sidebar / nav) — unauthenticated
    users shouldn't see the dashboard chrome. After successful login,
    the page POSTs to /api/auth/login and is redirected to `next` (or
    /agents by default).
    """
    # If the user is already logged in, bounce to the dashboard.
    user_id = await current_user_id(request)
    if user_id:
        return RedirectResponse(url=next or "/agents", status_code=302)

    return _auth_templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "next": next or "/agents",
            "error": error or "",
        },
    )


@page_router.get("/setup-password", response_class=HTMLResponse)
async def setup_password_page(
    request: Request,
    username: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Bootstrap password setup page. Only meaningful when the named
    user is the bootstrap admin with no password set. The frontend
    double-checks via /api/auth/me (which would 200 if logged in)
    and via the form's server-side validation.
    """
    return _auth_templates.TemplateResponse(
        request=request,
        name="setup_password.html",
        context={
            "username": username or BOOTSTRAP_ADMIN_USERNAME,
            "error": error or "",
        },
    )


@page_router.post("/logout")
async def logout_form(request: Request) -> RedirectResponse:
    """Form-based logout (for the topbar POST). Clears the cookie
    and redirects to /login. Always redirects — idempotent.
    """
    response = RedirectResponse(url="/login", status_code=302)
    clear_session_cookie(response)
    user_id = await current_user_id(request)
    if user_id:
        try:
            db = request.app.state.db
            await audit_log(db, "auth.logout", actor="user", user_id=user_id)
        except Exception:
            pass
    return response
