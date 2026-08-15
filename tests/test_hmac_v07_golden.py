"""Regression test: sign_v07_request output matches the v0.7 §1.4 golden.

Locks the 7 X-Hermes-* headers that tests/helpers/hmac_v07.py::sign_v07_request
produces for a specific set of FIXED inputs. If the function output diverges
from tests/golden/hmac_v07_golden.json, this test fails — protecting against
unintended changes to the canonical input format, signature scheme, or
header names.

The golden file was cross-validated on 2026-08-13 against the PowerShell
implementation at installer/bootstrapper/install-orch-client.ps1::Wait-ForEnrollment
(compat test in C:\\Users\\stanley\\AppData\\Local\\Temp\\compat_{python,powershell}.{py,ps1}).
Both implementations produce byte-identical 7 headers with these inputs.

When to update the golden file:
  1. The v0.7 §1.4 spec itself changes (canonical format, header names, etc.)
  2. The PowerShell bootstrapper implementation is intentionally updated to
     match a new spec

How to regenerate after a legitimate change:
  1. Update the spec at docs/proposals/orch-client-build-impl-plan-v0.7.md §1.4
  2. Update tests/helpers/hmac_v07.py to match
  3. Update installer/bootstrapper/install-orch-client.ps1::Wait-ForEnrollment
  4. Re-run the cross-language compat test (compat_python.py + compat_powershell.ps1)
  5. If both sides agree, update the golden file with the new expected values
  6. Commit with a clear "spec change v0.7.x -> v0.7.y" message
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.helpers.hmac_v07 import sign_v07_request


# === FIXED inputs (locked — these are the "test vector") ===
# These inputs are part of the contract; do NOT change them.
# If you need to test other inputs, write a separate test function.
FIXED_INPUTS = {
    "method": "GET",
    "path": "/api/agents/test-agent/status",
    "body": b"",                                      # empty body
    "key_id": "key-test",
    "secret": b"0123456789abcdef",                    # 16 bytes
    "timestamp": 1700000000,                          # fixed; no auto-fill
    "nonce": "0123456789abcdef0123456789abcdef",      # 32 hex; no uuid fill
}


# === Load the golden file ===
GOLDEN_PATH = Path(__file__).parent / "golden" / "hmac_v07_golden.json"


def _load_golden_expected() -> dict:
    """Load the expected_headers block from the golden file."""
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return data["expected_headers"]


def test_sign_v07_request_matches_golden_v07_section_1_4():
    """sign_v07_request output MUST equal the v0.7 §1.4 golden for fixed inputs.

    If this test fails:
      - DO NOT just update the golden file
      - First, find out WHY the function output changed:
        1. Did someone modify tests/helpers/hmac_v07.py?
        2. Did someone modify the canonical format in the v0.7 spec?
        3. Did someone update the bootstrapper's Wait-ForEnrollment to a new spec?
      - Cross-check with the PowerShell implementation:
        re-run compat_powershell.ps1 with the same fixed inputs
        (it's in C:\\Users\\stanley\\AppData\\Local\\Temp\\)
      - If BOTH sides changed in the same way AND the spec changed, update the golden.
      - Otherwise, REVERT the unintended change.
    """
    expected = _load_golden_expected()
    actual = sign_v07_request(**FIXED_INPUTS)

    assert actual == expected, (
        f"sign_v07_request output diverged from the v0.7 §1.4 golden.\n"
        f"Inputs: {FIXED_INPUTS!r}\n"
        f"  expected: {json.dumps(expected, indent=2, sort_keys=True)}\n"
        f"  actual  : {json.dumps(actual, indent=2, sort_keys=True)}\n"
        f"Diff (per-header):\n"
        + "\n".join(
            f"    {k}: expected={expected.get(k)!r} actual={actual.get(k)!r} "
            f"{'OK' if expected.get(k) == actual.get(k) else 'MISMATCH'}"
            for k in sorted(set(expected) | set(actual))
        )
    )


def test_golden_file_has_all_seven_headers():
    """Sanity check: the golden file is complete (no missing headers).

    v0.7 §1.4 specifies exactly 7 X-Hermes-* headers. If a new header is
    added to the spec, this test will catch a forgotten golden file update.
    """
    expected = _load_golden_expected()
    expected_keys = {
        "X-Hermes-Method",
        "X-Hermes-Path",
        "X-Hermes-Body-SHA256",
        "X-Hermes-Key-Id",
        "X-Hermes-Timestamp",
        "X-Hermes-Nonce",
        "X-Hermes-Signature",
    }
    assert set(expected.keys()) == expected_keys, (
        f"Golden file has wrong key set.\n"
        f"  expected: {sorted(expected_keys)}\n"
        f"  actual  : {sorted(expected.keys())}\n"
        f"  missing : {sorted(expected_keys - set(expected.keys()))}\n"
        f"  extra   : {sorted(set(expected.keys()) - expected_keys)}\n"
    )


def test_signature_is_well_formed_base64():
    """Sanity check: X-Hermes-Signature decodes to 32 bytes (SHA-256 length)."""
    import base64

    expected = _load_golden_expected()
    sig_b64 = expected["X-Hermes-Signature"]
    try:
        sig_bytes = base64.b64decode(sig_b64, validate=True)
    except Exception as e:
        raise AssertionError(f"X-Hermes-Signature is not valid base64: {sig_b64!r} ({e})")
    assert len(sig_bytes) == 32, (
        f"X-Hermes-Signature decodes to {len(sig_bytes)} bytes, expected 32 (SHA-256). "
        f"value={sig_b64!r}"
    )


def test_body_sha256_is_empty_body_constant():
    """Sanity check: X-Hermes-Body-SHA256 matches the well-known empty-body SHA-256."""
    # Empty body SHA-256 is a well-known constant:
    #   sha256(b"") = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
    EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    expected = _load_golden_expected()
    assert expected["X-Hermes-Body-SHA256"] == EMPTY_SHA256, (
        f"X-Hermes-Body-SHA256 should match the empty-body SHA-256 constant.\n"
        f"  expected: {EMPTY_SHA256}\n"
        f"  actual  : {expected['X-Hermes-Body-SHA256']}\n"
        f"This may indicate a body-hash bug, or the body was non-empty in the test vector."
    )
