"""Auth package (v1.6+): HMAC agent auth and future auth schemes."""
from hermes_orch.auth.hmac import (
    DEFAULT_HMAC_WINDOW_SEC,
    compute_signature,
    hmac_required,
    require_hmac_auth,
    string_to_sign,
    verify_signature,
)

__all__ = [
    "DEFAULT_HMAC_WINDOW_SEC",
    "compute_signature",
    "hmac_required",
    "require_hmac_auth",
    "string_to_sign",
    "verify_signature",
]
