"""Tests for wrapper cleanup-ack session_id sanitization (v1.0.2 hotfix).

Background:
  In production, the server's heartbeat response occasionally surfaced
  a `cleanup_session_ids` entry that contained an ANSI escape byte (0x1B)
  inside the session id. The wrapper used that string verbatim in
  `f"/api/agents/{agent_id}/sessions/{sid}/cleanup-ack"`, and httpx
  refused the request with:

    InvalidURL: Invalid non-printable ASCII character in URL, '\x1b' at position 77.

  The wrapper caught the exception in the per-sid except block and printed
  "cleanup-ack failed for ..." but the same bad sid keeps coming back on
  every heartbeat, so the wrapper effectively loops on the same error
  forever (and the row stays in `pending_cleanup`).

Fix:
  Add `_is_safe_session_id(sid)` helper in `hermes_orch.agent_cli` that
  returns True only for URL-safe ids (alnum + dash + underscore). The
  nested `_cleanup_local_sessions` skips cleanup-ack (and the local
  hermes delete) for any sid the helper rejects, and logs a single
  warning per bad id. The server's row is left for operator review;
  we no longer generate httpx noise for it.

The tests below cover the helper directly (unit). The wiring into the
nested function is a single if-continue at the top of the for loop;
exercising it would require a full DaemonContext stub which is out of
scope for this hotfix (the helper is the only logic worth testing in
isolation -- the rest is the existing control flow with one extra guard).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Add the project root to sys.path so `import hermes_orch.agent_cli` works
# even when pytest is run from a different cwd.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ----------------------------------------------------------------------
# 1) helper exists and is exported at module level
# ----------------------------------------------------------------------

def test_is_safe_session_id_helper_exists():
    """The helper must be importable from hermes_orch.agent_cli."""
    from hermes_orch.agent_cli import _is_safe_session_id
    assert callable(_is_safe_session_id)


# ----------------------------------------------------------------------
# 2) classification -- the real-world case
# ----------------------------------------------------------------------

@pytest.mark.parametrize("sid,expected", [
    # normal ids (the real-world case) -> safe
    ("20260722_222004_bf90f2", True),
    ("abcDEF-0123_xyz", True),
    ("single", True),
    ("a", True),
    ("0", True),
    # bad: contains ANSI escape (0x1B) -- the original bug
    ("20260722_222004_bf90f2\x1b", False),
    ("\x1b[31mid", False),
    # bad: other non-printable / control chars
    ("id_with_\x00_null", False),
    ("id_with_\n_newline", False),
    ("id_with_\r_cr", False),
    ("id_with_\ttab", False),
    # bad: whitespace
    ("id with space", False),
    ("id\twith\ttab", False),
    # bad: URL-meaningful chars (would break the path even if printable)
    ("id/with/slash", False),
    ("id?with?query", False),
    ("id#with#fragment", False),
    ("id%with%encoded", False),
    # bad: empty
    ("", False),
])
def test_is_safe_session_id_classification(sid, expected):
    from hermes_orch.agent_cli import _is_safe_session_id
    assert _is_safe_session_id(sid) is expected, (
        f"_is_safe_session_id({sid!r}) returned "
        f"{not expected}, expected {expected}"
    )


# ----------------------------------------------------------------------
# 3) defensive: must not raise on non-string inputs
# ----------------------------------------------------------------------

@pytest.mark.parametrize("bad_input", [
    None,
    123,
    12.5,
    b"bytes-not-str",
    [],
    {},
    True,
    False,
])
def test_is_safe_session_id_rejects_non_strings(bad_input):
    """A malformed heartbeat payload (None / bytes / int) must not raise
    in the helper -- it must just return False so the loop skips it."""
    from hermes_orch.agent_cli import _is_safe_session_id
    assert _is_safe_session_id(bad_input) is False


# ----------------------------------------------------------------------
# 4) the helper is pure: calling it twice returns the same answer
# ----------------------------------------------------------------------

def test_is_safe_session_id_is_pure():
    from hermes_orch.agent_cli import _is_safe_session_id
    # The original buggy id from the production log
    sid = "20260722_222004_bf90f2\x1b[31m"
    assert _is_safe_session_id(sid) is False
    # Calling again must yield the same answer (no global state mutation)
    assert _is_safe_session_id(sid) is False
    # And a known-good id must still be safe after we rejected a bad one
    assert _is_safe_session_id("20260816_150000_good01") is True
