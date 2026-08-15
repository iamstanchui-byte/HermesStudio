# coding: utf-8
"""AgentStatus enum + validation (Hardening Phase 6, 2026-08-15).

The `agents.status` field has a fixed 4-value enum per
`docs/specs/orch-server-hmac-v0.7-alignment.md` §1.11:

    verifying  - pre-provisioned; awaiting v0.7 endpoint call
    verified   - active; the v0.7 endpoint has confirmed the secret
    blocked    - operator-blocked; the row is preserved for audit
    suspended  - operator-suspended; same as blocked but reversible

The DB does NOT enforce the enum (SQLite ALTER TABLE ADD CHECK
is unsupported; the spec relies on app-layer validation per the
existing convention in `db.py`). The app-layer validation lives
here so the status endpoint + enrollment endpoint + any future
reader share one source of truth.

Usage:

    from hermes_orch.core.agent_status import (
        AgentStatus,
        validate_agent_status,
    )

    # In the status endpoint:
    raw = row.get("status")
    status = validate_agent_status(raw)  # raises ValueError on typo
    return {"status": status, ...}

The v0.7 enrollment endpoint's state machine (spec §1.10) uses
the same enum: only `verifying` is the allowed start state for
the `verifying` -> `verified` transition.
"""
from __future__ import annotations

from typing import Literal


# Canonical enum. Case-sensitive exact match. Source of truth for
# the v0.7 status endpoint + enrollment state machine + any
# future reader.
AgentStatus = Literal["verifying", "verified", "blocked", "suspended"]


# Tuple form for membership checks (Literal types are not directly
# iterable in a useful way for `in` checks; the tuple is the
# single source of truth that `AgentStatus` documents).
_AGENT_STATUS_VALUES: tuple[AgentStatus, ...] = (
    "verifying",
    "verified",
    "blocked",
    "suspended",
)


def is_valid_agent_status(value: str | None) -> bool:
    """True iff the value is one of the 4 canonical AgentStatus
    enum values. False for None, empty string, typo'd values,
    or case-mismatched values ('Verified' is not valid; the
    enum is case-sensitive).
    """
    if value is None:
        return False
    return value in _AGENT_STATUS_VALUES


def validate_agent_status(value: str | None) -> AgentStatus:
    """Validate the value is a canonical AgentStatus enum value
    and return it (typed). Raises ValueError on any non-enum value
    so the caller can surface a 500 INVALID_AGENT_STATUS error
    with the bad value in the detail.

    The caller is responsible for the HTTP error wrapping. This
    function is a pure data validation helper with no I/O.
    """
    if not is_valid_agent_status(value):
        raise ValueError(
            f"invalid agent status {value!r}; expected one of "
            f"{list(_AGENT_STATUS_VALUES)}"
        )
    # is_valid_agent_status narrowed the type, but the type checker
    # doesn't know that; cast is safe here.
    return value  # type: ignore[return-value]
