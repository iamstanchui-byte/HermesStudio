# coding: utf-8
"""Enrollment tokens (v1.0.1 new-user-activation §3.3).

Chicken-and-egg solved by **workspace-scoped, agent-name-agnostic tokens**:
  1. Operator (dashboard user) issues a token via POST /api/enrollment-tokens
  2. Plaintext token + install command shown ONCE in the UI
  3. Operator pastes the install command on the agent host
  4. `agent_cli enroll` POSTs the plaintext to /api/agents/enroll
  5. Server hashes the plaintext, looks up the hash, validates
     (not expired, not used), then creates the agent row +
     marks the token used

Plaintext tokens are NEVER stored — only the SHA-256 hash. If the DB
leaks, tokens cannot be replayed (hash alone is useless to a
network attacker).

Per §3.3.2: the agent's self-declared `agent_name` from the request
body ALWAYS wins over the operator's `requested_agent_name` hint
at issue time. If they differ, no error and no silent coercion —
the agent record is named after the agent's self-declared name.
"""
from __future__ import annotations

import hashlib
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


# Token lifetime per spec §3.3 (15 minutes default). Make it
# configurable later if operators need a longer window; for now
# the spec is explicit and 15 min is the blast-radius-minimizing
# default.
DEFAULT_TTL_MINUTES = 15


def _utcnow_iso() -> str:
    """ISO 8601 UTC with explicit +00:00 suffix (avoid Z vs +00:00 confusion)."""
    return datetime.now(timezone.utc).isoformat()


def _new_token_id() -> str:
    """Generate a token id (different from the plaintext token)."""
    # 16 chars alphanumeric is plenty for an internal DB id
    alphabet = string.ascii_lowercase + string.digits
    return "etok-" + "".join(secrets.choice(alphabet) for _ in range(16))


def _new_token_plaintext() -> str:
    """Generate the plaintext token. 256 bits of entropy, URL-safe.

    `secrets.token_urlsafe(32)` returns ~43 chars of base64url-encoded
    data, which represents 32 bytes = 256 bits of entropy. We prefix
    with `etok-` so the token is recognisable in logs / shell
    history as an enrollment token (vs. some other secret).
    """
    return "etok-" + secrets.token_urlsafe(32)


def hash_token(plaintext: str) -> str:
    """SHA-256 of the plaintext token, hex-encoded.

    This is the ONLY thing we store. The plaintext is shown to the
    operator ONCE at issue time and never persisted.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedToken:
    """Result of `issue_enrollment_token()`. Returned to the dashboard.

    `plaintext` is the ONLY time the server knows the plaintext. The
    caller is expected to surface it in the UI immediately, then
    forget it. The DB row stores only `token_hash`.
    """

    id: str
    plaintext: str
    token_hash: str
    expires_at: str  # ISO 8601 UTC
    label: str
    requested_agent_name: str


def issue_enrollment_token(
    *,
    label: str = "",
    requested_agent_name: str = "",
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> IssuedToken:
    """Generate a new enrollment token.

    Pure function — no DB I/O. The caller (api/enrollment.py) writes
    the row and returns the IssuedToken to the UI.
    """
    plaintext = _new_token_plaintext()
    return IssuedToken(
        id=_new_token_id(),
        plaintext=plaintext,
        token_hash=hash_token(plaintext),
        expires_at=(
            datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        ).isoformat(),
        label=label or "",
        requested_agent_name=requested_agent_name or "",
    )


# ===== Consume result codes =====
#
# The consume endpoint needs to distinguish between four outcomes:
#   - ok            (token + agent row created)
#   - not_found     (no row with this hash — 404)
#   - expired       (expires_at <= now — 410)
#   - already_used  (used_at IS NOT NULL — 410)
#
# We use sentinel strings rather than exceptions so the API layer
# can map them to HTTP status codes cleanly. The exception path
# (genuine server errors: DB locked, etc.) is still a 500.

CONSUME_OK = "ok"
CONSUME_NOT_FOUND = "not_found"
CONSUME_EXPIRED = "expired"
CONSUME_ALREADY_USED = "already_used"


@dataclass(frozen=True)
class ConsumeResult:
    """Result of attempting to consume an enrollment token.

    On `CONSUME_OK`, `agent_id` and `hmac_secret` are set (both shown
    to the agent host ONCE).
    """

    outcome: str  # one of CONSUME_*
    agent_id: str = ""
    agent_name: str = ""
    hmac_secret: str = ""  # set on ok
    requested_name_used: bool = False  # True if requested_agent_name won
                                     # (i.e. agent_name was empty/missing)


def is_consume_ok(result: ConsumeResult) -> bool:
    return result.outcome == CONSUME_OK
