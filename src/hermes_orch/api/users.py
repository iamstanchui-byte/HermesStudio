# coding: utf-8
"""Dashboard user management API (v3.5.0, 2026-07-31).

Admin-only CRUD endpoints for managing the human users that can sign
into the dashboard. Lives separately from `auth.py` because:
  - It's a different concern (admin tooling, not authentication flow).
  - It needs the admin role check on every endpoint — keeping it in
    one file makes the "admin only" pattern obvious to future readers.

Endpoints (all require cookie auth + role=admin):
  GET    /api/users                       — list all users
  POST   /api/users                       — create new user
  POST   /api/users/{username}/password   — admin reset (no old password)
  POST   /api/users/{username}/disable    — set disabled=1
  POST   /api/users/{username}/enable     — set disabled=0

Out of scope for v3.5.0 (CLI only):
  - delete (intentionally — disable is the soft-delete semantic)
  - role change (intentionally — admin demoting self could lock out)
  - "change my own password" (already exists at /api/auth/password)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from hermes_orch.auth.cookie import (
    ROLE_ADMIN,
    ROLE_USER,
    create_user,
    get_user_by_username,
    list_users,
    set_user_disabled,
    set_user_password,
)
from hermes_orch.core.audit import audit_log


router = APIRouter()  # mounted at /api/users in main.py


# ===== Pydantic models =====

class CreateUserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=512)
    # Optional admin flag. If True, the new user gets the admin role.
    # If False (default), gets the regular user role.
    is_admin: bool = Field(default=False)


class ResetPasswordIn(BaseModel):
    new_password: str = Field(min_length=8, max_length=512)
    confirm_password: str = Field(min_length=8, max_length=512)


# ===== Admin guard =====

async def require_admin(request: Request) -> dict[str, Any]:
    """FastAPI dependency: must be a logged-in admin user.

    Returns the full user row so endpoints can audit who did what
    without a second DB roundtrip. 403 (not 401) for non-admins so the
    client can distinguish "you're not signed in" from "you're signed
    in but not allowed".
    """
    # Re-use the same cookie resolution as the rest of the auth flow.
    from hermes_orch.auth.cookie import current_user_id  # local import to avoid cycle
    user_id = await current_user_id(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    db = request.app.state.db
    user = await db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(401, "Not authenticated")
    if user.get("disabled"):
        raise HTTPException(401, "Account disabled")
    if user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "Admin role required")
    return user


# ===== Endpoints =====

@router.get("")
async def list_all_users(request: Request, admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Return all dashboard users. The current user is included so
    admins can see themselves in the list (helps when checking "who
    am I logged in as").
    """
    db = request.app.state.db
    rows = await list_users(db)
    # Shape the response. Don't leak password_hash directly — derive
    # a `has_password` boolean from it (NULL = no password yet).
    # We need to fetch the column separately because list_users() does
    # not select it (it's a public helper, not all callers want the
    # hash in their result set).
    password_status: dict[str, bool] = {}
    if rows:
        # Build a CASE expression result: 1 if password_hash IS NOT NULL
        # (any non-empty bcrypt hash), 0 otherwise. We don't need the
        # actual hash value on the wire.
        placeholders = ",".join("?" for _ in rows)
        param_rows = await db.fetchall(
            f"SELECT username, password_hash IS NOT NULL AS has_pw "
            f"FROM users WHERE id IN ({placeholders})",
            [r["id"] for r in rows],
        )
        password_status = {r["username"]: bool(r["has_pw"]) for r in param_rows}
    users = [
        {
            "username": r["username"],
            "role": r["role"],
            "disabled": bool(r.get("disabled")),
            "is_bootstrap_admin": bool(r.get("is_bootstrap_admin")),
            "created_at": r["created_at"],
            "last_login_at": r.get("last_login_at"),
            "has_password": password_status.get(r["username"], False),
        }
        for r in rows
    ]
    return {"users": users}


@router.post("", status_code=201)
async def create_new_user(
    payload: CreateUserIn,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Create a new dashboard user. Admin-only.

    Username is unique (case-insensitive, enforced by the COLLATE NOCASE
    on the users table). If the username already exists, returns 409.
    """
    db = request.app.state.db
    existing = await get_user_by_username(db, payload.username)
    if existing:
        raise HTTPException(409, f"Username '{payload.username}' already exists")

    role = ROLE_ADMIN if payload.is_admin else ROLE_USER
    user_id = await create_user(
        db,
        username=payload.username,
        password=payload.password,
        role=role,
        is_bootstrap_admin=False,
    )
    try:
        await audit_log(
            db,
            "user.created",
            actor=admin["username"],
            payload={"new_username": payload.username, "role": role},
        )
    except Exception:
        pass
    return {
        "ok": True,
        "username": payload.username,
        "role": role,
    }


@router.post("/{username}/password")
async def admin_reset_password(
    username: str,
    payload: ResetPasswordIn,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Admin reset a user's password. Does NOT require the old password
    (that's the point — for when a user is locked out).

    Admins can reset their own password too via this endpoint, but the
    normal /api/auth/password (with old-password check) is preferred
    for self-service.
    """
    if payload.new_password != payload.confirm_password:
        raise HTTPException(400, "Passwords do not match")
    db = request.app.state.db
    user = await get_user_by_username(db, username)
    if not user:
        raise HTTPException(404, f"User '{username}' not found")
    await set_user_password(db, user["id"], payload.new_password)
    try:
        await audit_log(
            db,
            "user.password_reset",
            actor=admin["username"],
            payload={"target_username": username},
        )
    except Exception:
        pass
    return {"ok": True, "username": username}


@router.post("/{username}/disable")
async def disable_user(
    username: str,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Disable a user. They can't log in or use existing sessions
    (next request gets 401). Their data is preserved.

    Special case: an admin cannot disable themselves (would lock them
    out of the dashboard). 400 in that case.
    """
    if username == admin["username"]:
        raise HTTPException(400, "You cannot disable yourself. Ask another admin.")
    db = request.app.state.db
    user = await get_user_by_username(db, username)
    if not user:
        raise HTTPException(404, f"User '{username}' not found")
    await set_user_disabled(db, user["id"], True)
    try:
        await audit_log(
            db,
            "user.disabled",
            actor=admin["username"],
            payload={"target_username": username},
        )
    except Exception:
        pass
    return {"ok": True, "username": username, "disabled": True}


@router.post("/{username}/enable")
async def enable_user(
    username: str,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Re-enable a previously disabled user. They can log in again
    immediately; existing sessions remain revoked (they'd need to
    log in fresh).
    """
    db = request.app.state.db
    user = await get_user_by_username(db, username)
    if not user:
        raise HTTPException(404, f"User '{username}' not found")
    await set_user_disabled(db, user["id"], False)
    try:
        await audit_log(
            db,
            "user.enabled",
            actor=admin["username"],
            payload={"target_username": username},
        )
    except Exception:
        pass
    return {"ok": True, "username": username, "disabled": False}
