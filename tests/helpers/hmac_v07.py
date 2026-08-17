"""Re-export of the v0.7 §1.4 client-side signer.

Canonical implementation lives in `hermes_orch.auth.hmac_v07.sign_v07_request`
(added 2026-08-16). This shim preserves the existing import path for any
test that imports from `tests.helpers.hmac_v07` — they get the same function
as the production wrapper (single source of truth, no risk of drift).

The PowerShell bootstrapper's `Wait-ForEnrollment` at
`installer/bootstrapper/install-orch-client.ps1` (line ~285) is the
PowerShell counterpart and MUST stay byte-for-byte in sync — both
produce the same canonical input + signature. The cross-language compat
test on 2026-08-13 byte-equal-verified these.
"""
from __future__ import annotations

from hermes_orch.auth.hmac_v07 import sign_v07_request

__all__ = ["sign_v07_request"]
