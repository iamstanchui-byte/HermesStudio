# coding: utf-8
"""v1.0.1 (new-user-activation) core/restart.py process-mode tests.

`detect_process_mode()` returns one of:
  - "supervised"     (HERMES_SUPERVISED=systemd|nssm env var; supervisor restarts us)
  - "direct"         (normal Python process; safe to os.execv in-place)
  - "undetectable"   (frozen / embedded; cannot safely restart)

`perform_restart()` is harder to test in-process because supervised +
direct both terminate the test runner. We test the classifier and
contract instead, plus a separate test that exercises the
undetectable path (the only one that returns normally).
"""
from __future__ import annotations

import os
import sys

import pytest

from hermes_orch.core.restart import (
    PROCESS_MODE_DIRECT,
    PROCESS_MODE_UNDETECTABLE,
    detect_process_mode,
    perform_restart,
)


def test_detect_supervised_from_env(monkeypatch):
    """HERMES_SUPERVISED=systemd (or nssm, supervised, true) -> supervised."""
    monkeypatch.setenv("HERMES_SUPERVISED", "systemd")
    assert detect_process_mode() == "supervised"
    monkeypatch.setenv("HERMES_SUPERVISED", "nssm")
    assert detect_process_mode() == "supervised"
    monkeypatch.setenv("HERMES_SUPERVISED", "supervised")
    assert detect_process_mode() == "supervised"
    monkeypatch.setenv("HERMES_SUPERVISED", "true")
    assert detect_process_mode() == "supervised"


def test_detect_direct_when_normal_python(monkeypatch):
    """No env var, sys.executable + sys.argv[0] = python + .py script -> direct.

    In the test runner this is the case (pytest is invoked via a Python
    interpreter; argv[0] is the test module's path).
    """
    monkeypatch.delenv("HERMES_SUPERVISED", raising=False)
    # We don't need to mock sys.executable or sys.argv; in the test
    # process they already match the "normal Python" heuristic.
    # If running under a frozen binary, this test would correctly
    # return "undetectable" instead — pytest can detect that case.
    if sys.executable.endswith(".exe") and (
        not sys.argv or sys.argv[0].endswith(".exe")
    ):
        pytest.skip("Test running under a frozen binary; direct path not applicable")
    assert detect_process_mode() == "direct"


def test_detect_undetectable_when_frozen(monkeypatch):
    """Frozen-style (exe + argv0 = exe, no .py) -> undetectable."""
    monkeypatch.delenv("HERMES_SUPERVISED", raising=False)
    monkeypatch.setattr(sys, "executable", "/path/to/frozen.exe")
    monkeypatch.setattr(sys, "argv", ["/path/to/frozen.exe", "arg1"])
    assert detect_process_mode() == "undetectable"


def test_detect_undetectable_when_no_argv(monkeypatch):
    """Empty argv + no env var -> undetectable (defensive default)."""
    monkeypatch.delenv("HERMES_SUPERVISED", raising=False)
    monkeypatch.setattr(sys, "argv", [])
    assert detect_process_mode() == "undetectable"


def test_env_var_is_case_insensitive(monkeypatch):
    """HERMES_SUPERVISED=SYSTEMD (uppercase) still matches."""
    monkeypatch.setenv("HERMES_SUPERVISED", "SYSTEMD")
    assert detect_process_mode() == "supervised"


def test_env_var_trims_whitespace(monkeypatch):
    """HERMES_SUPERVISED=' systemd ' (with spaces) still matches."""
    monkeypatch.setenv("HERMES_SUPERVISED", " systemd ")
    assert detect_process_mode() == "supervised"


def test_perform_restart_returns_undetectable_without_exiting(monkeypatch):
    """perform_restart in undetectable mode returns ('undetectable', msg).

    The supervised + direct paths terminate the process, so we can only
    unit-test the undetectable path.
    """
    monkeypatch.delenv("HERMES_SUPERVISED", raising=False)
    monkeypatch.setattr(sys, "executable", "/path/to/frozen.exe")
    monkeypatch.setattr(sys, "argv", ["/path/to/frozen.exe"])
    mode, message = perform_restart()
    assert mode == "undetectable"
    assert "manually" in message.lower() or "restart" in message.lower()
