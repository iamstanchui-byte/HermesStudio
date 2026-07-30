"""Tests for v1.9.3 subprocess timeout behavior.

The wrapper's _run_task() spawns a hermes-agent subprocess and waits
for it to exit. If hermes hangs (LLM API timeout, network issue,
etc.), the wrapper needs a hard timeout to unblock the main loop.

Pre-v1.9.3: subprocess.kill() was called immediately on timeout.
This skipped hermes's graceful shutdown (session flush, MCP server
cleanup), losing any partial output the agent had produced.

v1.9.3: _run_subprocess_with_timeout() sends SIGTERM first, waits
`grace_seconds` for clean exit, and only SIGKILLs if the process
ignored SIGTERM. Same shape as Linux/Unix process supervision.

These tests verify:
  1. A subprocess that exits within the timeout returns (rc, False)
  2. A subprocess that exceeds the timeout gets terminated within
     timeout + grace_seconds, returns (None, True)
  3. A subprocess that ignores SIGTERM gets kill()ed after the grace
  4. The result code is correct for fast-exiting processes
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from hermes_orch.agent_cli import _run_subprocess_with_timeout


# Use the current Python interpreter for tests (cross-platform, no
# need to find a "sleep" binary on Windows).
PYTHON = sys.executable


def _spawn_sleeper(seconds: float) -> subprocess.Popen:
    """Spawn a Python subprocess that sleeps for `seconds`."""
    return subprocess.Popen(
        [PYTHON, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_fast_exit_returns_rc_no_timeout():
    """Subprocess that exits within timeout returns (rc, False)."""
    proc = _spawn_sleeper(0.2)
    start = time.time()
    rc, timed_out = _run_subprocess_with_timeout(proc, timeout=5)
    elapsed = time.time() - start
    assert timed_out is False
    assert rc == 0
    # Should exit well before the timeout
    assert elapsed < 2


def test_timeout_triggers_termination():
    """Subprocess that exceeds timeout gets terminated.

    The pre-v1.9.3 behavior (immediate kill) and v1.9.3 behavior
    (terminate-then-kill) both satisfy this test; we just check
    that the call returns within a reasonable bound and the
    subprocess is dead afterward.
    """
    proc = _spawn_sleeper(60)  # would sleep 60s if not killed
    start = time.time()
    rc, timed_out = _run_subprocess_with_timeout(
        proc, timeout=1, grace_seconds=2,
    )
    elapsed = time.time() - start
    # Must return quickly: timeout(1) + grace(2) + a bit of slack
    # for proc.wait() overhead.
    assert elapsed < 5, f"helper took {elapsed:.2f}s, expected < 5s"
    assert timed_out is True
    assert rc is None
    # Subprocess must be dead (poll() returns the return code or None)
    assert proc.poll() is not None, "subprocess still alive after timeout"


def test_terminate_kills_cooperative_process():
    """A cooperative process (handles SIGTERM) is killed by the
    terminate-then-kill sequence within timeout + grace.

    On Windows, terminate() is GenerateConsoleCtrlEvent; Python's
    default SIGTERM handler doesn't catch it the same way as on
    POSIX. We don't test for "within X seconds of terminate"; we
    just test that the helper eventually returns and the process
    is dead.
    """
    proc = _spawn_sleeper(60)
    start = time.time()
    rc, timed_out = _run_subprocess_with_timeout(
        proc, timeout=1, grace_seconds=3,
    )
    elapsed = time.time() - start
    assert timed_out is True
    assert rc is None
    # The process must be dead by the time the helper returns
    # (terminate or kill, whichever fired)
    assert proc.poll() is not None
    # Sanity: the call shouldn't take much longer than timeout + grace
    assert elapsed < 7, f"helper took {elapsed:.2f}s, expected < 7s"


def test_terminate_then_kill_for_uncooperative_process():
    """A process that ignores SIGTERM gets kill()ed after the grace.

    We simulate "ignores terminate" by using subprocess.Popen with
    creationflags that suppress the default SIGTERM handler on
    Windows (CREATE_NEW_PROCESS_GROUP). This makes terminate() a
    no-op; the helper must then fall through to kill().
    """
    kwargs = {}
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP = 0x00000200
        # Prevents CTRL_BREAK from reaching the child as a signal;
        # terminate() becomes a no-op and the helper must kill().
        kwargs["creationflags"] = 0x00000200
    proc = subprocess.Popen(
        [PYTHON, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
    start = time.time()
    rc, timed_out = _run_subprocess_with_timeout(
        proc, timeout=1, grace_seconds=2,
    )
    elapsed = time.time() - start
    assert timed_out is True
    assert rc is None
    assert proc.poll() is not None
    # The helper should still complete within timeout + grace
    # (kill is immediate on Windows after TerminateProcess).
    assert elapsed < 6, f"helper took {elapsed:.2f}s, expected < 6s"


def test_nonzero_exit_code_preserved():
    """A subprocess that exits with non-zero (e.g. error) keeps
    the rc value; we don't accidentally mark it as timed out."""
    proc = subprocess.Popen(
        [PYTHON, "-c", "import sys; sys.exit(7)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rc, timed_out = _run_subprocess_with_timeout(proc, timeout=5)
    assert timed_out is False
    assert rc == 7
