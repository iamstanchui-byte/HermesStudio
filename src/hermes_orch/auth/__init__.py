# coding: utf-8
"""Auth package (v1.6+): HMAC agent auth + user cookie auth (v3.4)."""
from hermes_orch.auth.hmac import (
    DEFAULT_HMAC_WINDOW_SEC,
    compute_signature,
    hmac_required,
    require_hmac_auth,
    string_to_sign,
    verify_signature,
)
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
    generate_user_id,
    get_user_by_username,
    hash_password,
    list_users,
    parse_session_cookie_value,
    require_user,
    set_session_cookie,
    set_user_disabled,
    set_user_password,
    touch_last_login,
    verify_password,
)
from hermes_orch.auth.admin_guard import require_admin
from hermes_orch.auth.csrf import require_same_origin
from hermes_orch.auth.origin_validation import validate_public_origin

__all__ = [
    # hmac (v1.6)
    "DEFAULT_HMAC_WINDOW_SEC",
    "compute_signature",
    "hmac_required",
    "require_hmac_auth",
    "string_to_sign",
    "verify_signature",
    # cookie (v3.4)
    "BOOTSTRAP_ADMIN_USERNAME",
    "COOKIE_NAME",
    "DEFAULT_MAX_AGE_SEC",
    "ROLE_ADMIN",
    "ROLE_USER",
    "clear_session_cookie",
    "constant_time_eq",
    "create_user",
    "current_user",
    "current_user_id",
    "generate_user_id",
    "get_user_by_username",
    "hash_password",
    "list_users",
    "parse_session_cookie_value",
    "require_user",
    "set_session_cookie",
    "set_user_disabled",
    "set_user_password",
    "touch_last_login",
    "verify_password",
    # admin guard (security hotfix 2026-08-11, B12)
    "require_admin",
    # CSRF (security hotfix 2026-08-11, B12 §6.1)
    "require_same_origin",
    # origin validation (security hotfix 2026-08-11, B12 §6.1 R13)
    "validate_public_origin",
]
