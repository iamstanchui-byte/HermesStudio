# coding: utf-8
"""Unified error JSON contract helpers (Hardening Phase 5, 2026-08-15).

Per `docs/specs/orch-server-hmac-v0.7-alignment.md` §1.12, all
4xx + 5xx responses from the v0.7 endpoints use the unified shape:

    {
      "error": "ERROR_CODE",
      "message": "human readable",
      "request_id": "uuid"
    }

The legacy `detail` field is preserved for backward compat with
the pre-Phase-5 bootstrapper and dashboard parsing.

This module is the single source of truth for:
- `parse_error_detail(detail)` — split "CODE: message" -> (code, message)
- `make_error_response(code, message, request_id, status_code)` — build
  the unified JSONResponse
- The fallback `HTTP_ERROR` code when the detail string has no
  `": "` separator (e.g. an HTTPException raised by a 3rd-party
  FastAPI dependency with a plain string)

The exception handler in `main.py` is the only call site. Tests
should use `make_error_response` directly to assert the wire format
without going through HTTPException (so the test is independent of
the FastAPI dependency machinery).
"""
from __future__ import annotations

import uuid
from typing import Tuple

from fastapi.responses import JSONResponse


# Fallback code when an HTTPException's detail is a plain string
# without the "CODE: message" separator. 3rd-party FastAPI
# dependencies (e.g. Pydantic validation) often raise HTTPException
# with a plain string detail; we wrap them in this generic code
# so the unified contract still produces a parseable `error` field.
GENERIC_HTTP_ERROR_CODE = "HTTP_ERROR"


def parse_error_detail(detail) -> Tuple[str, str]:
    """Parse the HTTPException detail into (code, message).

    The pre-Phase-5 convention is to raise
    `HTTPException(status, "ERROR_CODE: human message")` so the
    string can be split on the first `": "` to get the code and
    the human message separately.

    Args:
        detail: the value passed to `HTTPException(status, detail)`.
                May be a string ("CODE: message") or any other
                value (dict, list, etc.). Strings without a `": "`
                separator return `(GENERIC_HTTP_ERROR_CODE, str(detail))`.

    Returns:
        A tuple `(code, message)`. The code is uppercase snake_case
        (matching the spec §1.12 error code registry); the message
        is the human-readable text after the first `": "` (or the
        full detail if no separator).
    """
    if isinstance(detail, str):
        # Split on the FIRST `": "` only — split with maxsplit=1.
        # A human message can contain `": "` (e.g. "Body hash
        # mismatch: actual=X, provided=Y") and we want the second
        # half intact.
        if ": " in detail:
            code, _, message = detail.partition(": ")
            return (code, message)
        # No separator: use the generic code
        return (GENERIC_HTTP_ERROR_CODE, detail)
    # Non-string detail: use the generic code
    return (GENERIC_HTTP_ERROR_CODE, str(detail))


def make_error_response(
    code: str,
    message: str,
    request_id: str,
    status_code: int,
    *,
    include_legacy_detail: bool = True,
) -> JSONResponse:
    """Build the unified error JSONResponse.

    Args:
        code: the error code (e.g. "NONCE_REPLAY")
        message: the human-readable message
        request_id: the UUID4 for this request (also set as
                    the X-Request-Id response header)
        status_code: the HTTP status code (400, 401, etc.)
        include_legacy_detail: if True (default), also include
                    the legacy `detail` field with the original
                    "CODE: message" string. The pre-Phase-5
                    bootstrapper and dashboard use this field;
                    it is preserved for backward compat and
                    may be removed in a future major version.

    Returns:
        A FastAPI JSONResponse with the unified shape. The
        X-Request-Id response header carries the request_id.
    """
    body = {
        "error": code,
        "message": message,
        "request_id": request_id,
    }
    if include_legacy_detail:
        # Reconstruct the original "CODE: message" string so
        # pre-Phase-5 clients that parse `detail` keep working.
        body["detail"] = f"{code}: {message}"
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers={"X-Request-Id": request_id},
    )


def new_request_id() -> str:
    """Generate a new UUID4 string for request correlation."""
    return str(uuid.uuid4())
