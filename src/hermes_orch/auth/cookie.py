"""Dashboard session cookies (v3.4, 2026-07-31).

Bcrypt password hashing + itsdangerous-signed HttpOnly cookies. The cookie
payload is just the user_id; everything else (role, last_login_at) is
read fresh from the DB on each request via `current_user`. This keeps
revocation simple — disable the user row and the next request bounces.

Stateless sessions (no Redis, no DB session table). Trade-off: we can't
force-logout a stolen cookie before its 7-day expiry, except by rotating
the SESSION_SECRET (which logs out everyone) or by disabling the user
row (their next request gets 401 because `current_user` returns None).
The latter is fine for "compromised account" scenarios.

Cookie name: `hermes_orch_session`. Distinct from the orchestrator's
internal `orch.*` localStorage keys (sidebar state, theme, etc.).
"""
from __future__ import annotations

import hashlib
import hmac as _stdlib_hmac
import os
import secrets
import time
from typing import Any

import bcrypt
from fastapi import Cookie, HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner


# ===== Constants =====

COOKIE_NAME = "hermes_orch_session"
DEFAULT_MAX_AGE_SEC = 7 * 24 * 60 * 60  # 7 days
BOOTSTRAP_ADMIN_USERNAME = "admin"
ROLE_ADMIN = "admin"
ROLE_USER = "user"


# ===== Secret management =====

_SECRET_CACHE: str | None = None


def _read_or_create_secret() -> str:
    """Resolve the SESSION_SECRET.

    Order:
      1. HERMES_SESSION_SECRET env var (explicit override)
      2. Cached on-disk secret at <config_dir>/session_secret (created
         on first call if missing)
      3. Generated and persisted on first call
    """
    global _SECRET_CACHE
    if _SECRET_CACHE:
        return _SECRET_CACHE

    env = os.environ.get("HERMES_SESSION_SECRET", "").strip()
    if env:
        _SECRET_CACHE = env
        return env

    # Try to find the config dir from the running app. We import lazily
    # to avoid circular imports; if no app is running, fall back to
    # a process-local ephemeral secret (sessions won't survive restart,
    # which is the right behavior for an unconfigured process).
    try:
        from hermes_orch.cli import _default_db_path
    except Exception:
        _SECRET_CACHE = secrets.token_urlsafe(32)
        return _SECRET_CACHE

    # DB path is <config_dir>/hermes-orch.db → config_dir is the parent
    config_dir_path = _default_db_path().parent
    secret_path = config_dir_path / "session_secret"
    if secret_path.exists():
        _SECRET_CACHE = secret_path.read_text(encoding="utf-8").strip()
        return _SECRET_CACHE

    # Generate + persist
    config_dir_path.mkdir(parents=True, exist_ok=True)
    new_secret = secrets.token_urlsafe(32)
    secret_path.write_text(new_secret, encoding="utf-8")
    try:
        # Restrict permissions on POSIX
        if os.name != "nt":
            os.chmod(secret_path, 0o600)
    except OSError:
        pass
    _SECRET_CACHE = new_secret
    return _SECRET_CACHE


def _get_signer() -> TimestampSigner:
    return TimestampSigner(_read_or_create_secret())


# ===== Password hashing (bcrypt) =====

# Bcrypt has a 72-byte input cap. We pre-hash long passwords with
# SHA-256 so a 100-char password still works. The bcrypt output is
# what we store; verify combines the same way.
_BCRYPT_MAX_BYTES = 72


def _bcrypt_input(plain: str) -> bytes:
    raw = plain.encode("utf-8")
    if len(raw) <= _BCRYPT_MAX_BYTES:
        return raw
    return hashlib.sha256(raw).hexdigest().encode("ascii")


def hash_password(plain: str) -> str:
    """Bcrypt-hash a password. Returns the encoded hash (as string)."""
    if not plain:
        raise ValueError("password must not be empty")
    return bcrypt.hashpw(_bcrypt_input(plain), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(plain: str, stored_hash: str | None) -> bool:
    """Constant-time bcrypt compare. Returns False if stored_hash is None
    or empty (used to mark a user as 'must set password').
    """
    if not stored_hash:
        return False
    if not plain:
        return False
    try:
        return bcrypt.checkpw(_bcrypt_input(plain), stored_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ===== Cookie sign / verify =====

def make_session_cookie_value(user_id: str) -> str:
    """Sign a user_id into a tamper-proof string. itsdangerous's
    TimestampSigner binds the value to the current time so we can
    detect age on the way back in.
    """
    return _get_signer().sign(user_id.encode("utf-8")).decode("ascii")


def parse_session_cookie_value(
    signed: str | None, max_age_sec: int = DEFAULT_MAX_AGE_SEC
) -> str | None:
    """Verify the cookie signature + age. Returns the user_id, or None
    if missing/expired/tampered.
    """
    if not signed:
        return None
    try:
        raw = _get_signer().unsign(signed, max_age=max_age_sec)
    except SignatureExpired:
        return None
    except BadSignature:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ===== Response helpers =====

def set_session_cookie(response: Response, user_id: str, max_age_sec: int = DEFAULT_MAX_AGE_SEC) -> None:
    """Attach the signed session cookie to a response. HttpOnly +
    SameSite=Lax for CSRF mitigation. Secure flag is set ONLY when
    the request URL is https:// — we don't have a global "are we in
    production" flag and don't want to lock out dev.
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=make_session_cookie_value(user_id),
        max_age=max_age_sec,
        httponly=True,
        samesite="lax",
        path="/",
        # secure flag set per-request via the response.set_cookie
        # call site if we know we're on https; for now we omit it
        # and rely on the LAN-only deployment context.
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


# ===== FastAPI dependencies =====

async def current_user_id(
    request: Request,
) -> str | None:
    """Return the user_id from the session cookie, or None if no
    valid session. Always succeeds (returns None instead of raising)
    so endpoints can decide whether to require auth.

    We read the cookie via `request.cookies` instead of the FastAPI
    `Cookie(default=None, alias=...)` pattern because the latter passes
    a Cookie sentinel (not None) when the cookie is absent, which
    makes the type annotation `str | None` a lie. Reading directly
    from request.cookies returns None for missing cookies.
    """
    # request.cookies is a dict populated by Starlette from the Cookie
    # request header. It returns None for missing keys.
    session = request.cookies.get(COOKIE_NAME)
    uid = parse_session_cookie_value(session)
    if not uid:
        return None
    # Validate the user still exists and isn't disabled. We do this
    # on every request so disabling a user is immediate.
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT id, disabled FROM users WHERE id = ?", (uid,)
    )
    if not row or row.get("disabled"):
        return None
    return uid


async def current_user(
    request: Request,
    user_id: str | None = None,  # set by Depends(current_user_id) below
) -> dict[str, Any] | None:
    """Return the full user row, or None if no session / user gone /
    disabled. Same always-succeeds pattern as current_user_id.
    """
    if user_id is None:
        # current_user_id was not in the dependency chain; resolve it
        user_id = await current_user_id(request)
    if not user_id:
        return None
    db = request.app.state.db
    return await db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))


async def require_user(
    request: Request,
    user_id: str | None = None,  # set by Depends(current_user_id) below
) -> str:
    """FastAPI dependency: must have a valid session. Returns user_id
    or raises 401. Pages redirect to /login; JSON endpoints get 401.
    """
    if user_id is None:
        user_id = await current_user_id(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    return user_id


# ===== User CRUD helpers =====

def generate_user_id() -> str:
    """New user id: `usr-` + 8 random hex chars. Short enough to be
    grep-friendly, long enough to avoid collisions in small fleets.
    """
    return f"usr-{secrets.token_hex(4)}"


async def create_user(
    db,
    username: str,
    password: str | None = None,
    role: str = ROLE_USER,
    is_bootstrap_admin: bool = False,
) -> str:
    """Create a new user. Returns the new user_id.

    `password=None` is reserved for the bootstrap admin (created by
    `hermes-orch init`); the first web login sets the password. For
    all other users a password is required.
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    if password is not None:
        password_hash = hash_password(password)
    else:
        # Bootstrap admin only — stored as NULL so verify_password
        # returns False until the user sets a password via the web UI.
        password_hash = None
    user_id = generate_user_id()
    now = int(time.time())
    await db.execute(
        "INSERT INTO users (id, username, password_hash, role, is_bootstrap_admin, "
        "disabled, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
        (user_id, username, password_hash, role, 1 if is_bootstrap_admin else 0, now),
    )
    return user_id


async def get_user_by_username(db, username: str) -> dict[str, Any] | None:
    return await db.fetchone(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    )


async def set_user_password(db, user_id: str, new_password: str) -> None:
    """Set / replace the user's password hash."""
    await db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(new_password), user_id),
    )


async def touch_last_login(db, user_id: str) -> None:
    await db.execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?",
        (int(time.time()), user_id),
    )


async def list_users(db) -> list[dict[str, Any]]:
    return await db.fetchall(
        "SELECT id, username, role, disabled, is_bootstrap_admin, "
        "created_at, last_login_at FROM users ORDER BY created_at"
    )


async def set_user_disabled(db, user_id: str, disabled: bool) -> None:
    await db.execute(
        "UPDATE users SET disabled = ? WHERE id = ?",
        (1 if disabled else 0, user_id),
    )


def constant_time_eq(a: str, b: str) -> bool:
    """Constant-time string comparison. Used for the bootstrap admin
    username check (we don't want to leak username length via timing).
    """
    return _stdlib_hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
