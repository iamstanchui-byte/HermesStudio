# coding: utf-8
"""v1.0.1 (new-user-activation) core/restart.py process-mode tests.

`detect_process_mode()` returns one of:
  - "supervised"     (HERMES_SUPERVISED=systemd|nssm env var; supervisor restarts us)
  - "direct"         (normal Python process; safe to os.execv in-place)
  - "undetectable"   (frozen / embedded / parent launcher doesn't respawn;
                      cannot safely restart)

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
    _parent_process_name,
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


# ===== Parent-launcher detection (v1.0.1) =====
#
# The PyInstaller `hermes-orch.exe` launcher starts python as a child
# but does NOT respawn it on exit. An in-place `os.execv` on the worker
# in that scenario leaves the operator with a dead server. We must
# return `undetectable` so the API returns 501 with a clear
# `restart-server.ps1` instruction.


def test_detect_undetectable_when_parent_is_hermes_orch_launcher(monkeypatch):
    """Parent process is hermes-orch.exe -> undetectable, even if exe/argv
    look like normal Python.

    This is the exact scenario from the user setup:
    `hermes-orch.exe serve --reload` launches `python.exe -m
    hermes_orch.cli serve --reload`. The python child looks like a
    "direct" process by the exe/argv heuristic, but the parent launcher
    doesn't respawn it. The parent-launcher check fires first.
    """
    monkeypatch.delenv("HERMES_SUPERVISED", raising=False)
    # Simulate the python child: looks like a normal Python process
    monkeypatch.setattr(sys, "executable", "C:/path/to/python.exe")
    monkeypatch.setattr(sys, "argv", ["-m", "hermes_orch.cli", "serve", "--reload"])
    # But the parent is the launcher
    monkeypatch.setattr(
        "hermes_orch.core.restart._parent_process_name",
        lambda: "hermes-orch.exe",
    )
    assert detect_process_mode() == "undetectable"


def test_detect_undetectable_when_parent_is_hermes_orch_no_ext(monkeypatch):
    """Parent process is `hermes-orch` (no .exe, Linux/macOS) -> undetectable."""
    monkeypatch.delenv("HERMES_SUPERVISED", raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(sys, "argv", ["-m", "hermes_orch.cli", "serve"])
    monkeypatch.setattr(
        "hermes_orch.core.restart._parent_process_name",
        lambda: "hermes-orch",
    )
    assert detect_process_mode() == "undetectable"


def test_detect_direct_when_parent_is_normal_python_parent(monkeypatch):
    """Parent is `python.exe` (e.g. pytest -> python -m pytest) -> direct.

    The parent-launcher check is a whitelist; a normal python parent
    (e.g. the pytest runner, a test harness, a wrapper script) does
    not block the direct-mode heuristic.
    """
    monkeypatch.delenv("HERMES_SUPERVISED", raising=False)
    monkeypatch.setattr(
        "hermes_orch.core.restart._parent_process_name",
        lambda: "python.exe",
    )
    # The "direct" path requires exe + argv0 to NOT look frozen. Mock
    # sys.executable + sys.argv to look like a normal Python process.
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(sys, "argv", ["/usr/bin/python3", "some_script.py"])
    assert detect_process_mode() == "direct"


def test_parent_process_name_handles_missing_parent(monkeypatch):
    """_parent_process_name returns None on any error (defensive).

    We test the failure path: if `os.getppid()` returns 0 (no parent
    on Windows for Session 0 service? no, even then), the function
    returns None and the rest of the detection continues.
    """
    monkeypatch.setattr(os, "getppid", lambda: 0)
    assert _parent_process_name() is None


def test_parent_process_name_handles_tasklist_failure(monkeypatch):
    """tasklist returns non-zero (parent already dead) -> None."""
    import subprocess

    def fake_run(*args, **kwargs):
        class R:
            returncode = 1
            stdout = ""
        return R()

    monkeypatch.setattr(os, "getppid", lambda: 12345)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _parent_process_name() is None


def test_perform_restart_under_launcher_suggests_restart_script(monkeypatch):
    """perform_restart() under the hermes-orch.exe launcher mentions
    `restart-server.ps1` in its message (not the generic "manually").
    """
    monkeypatch.delenv("HERMES_SUPERVISED", raising=False)
    monkeypatch.setattr(sys, "executable", "/path/to/python.exe")
    monkeypatch.setattr(sys, "argv", ["-m", "hermes_orch.cli", "serve"])
    monkeypatch.setattr(
        "hermes_orch.core.restart._parent_process_name",
        lambda: "hermes-orch.exe",
    )
    mode, message = perform_restart()
    assert mode == "undetectable"
    assert "restart-server.ps1" in message, (
        f"expected 'restart-server.ps1' in message, got: {message!r}"
    )
    assert "hermes-orch.exe" in message, (
        f"expected launcher name in message, got: {message!r}"
    )


# ===== Ancestor chain detection (v1.0.1 Phase 1.2) =====
#
# In the production setup (`restart-server.ps1` -> `hermes-orch.exe` ->
# python -> uvicorn -> worker), the worker's IMMEDIATE parent is
# python.exe (uvicorn), not hermes-orch.exe. The launcher is several
# levels up. The ancestor-chain check catches that case.


def test_detect_undetectable_when_grandparent_is_launcher(monkeypatch):
    """uvicorn is the immediate parent, hermes-orch.exe is the grandparent.

    This is the actual user setup after `restart-server.ps1`:
        worker (request handler) -> python (uvicorn master) -> python
        (uvicorn wrapper) -> hermes-orch.exe (PyInstaller launcher)
    The immediate parent is python.exe so the simple check misses it;
    the ancestor walk must find hermes-orch.exe.
    """
    monkeypatch.delenv("HERMES_SUPERVISED", raising=False)
    monkeypatch.setattr(sys, "executable", "/path/to/python.exe")
    monkeypatch.setattr(sys, "argv", ["-m", "hermes_orch.cli", "serve"])
    # Immediate parent is uvicorn, NOT the launcher
    monkeypatch.setattr(
        "hermes_orch.core.restart._parent_process_name",
        lambda: "python.exe",
    )
    # But the ancestor chain (up to 6 levels) has hermes-orch.exe
    monkeypatch.setattr(
        "hermes_orch.core.restart._get_process_ancestry",
        lambda: [
            (4408, "python.exe"),       # uvicorn master
            (16280, "python.exe"),      # uvicorn wrapper
            (2152, "hermes-orch.exe"),  # PyInstaller launcher
            (10836, "powershell.exe"),  # the script that started it all
        ],
    )
    assert detect_process_mode() == "undetectable"


def test_detect_direct_when_ancestor_chain_is_clean_python(monkeypatch):
    """No launcher anywhere in the chain -> direct still works.

    A pure-python dev setup (pytest, test harness, plain
    `python -m hermes_orch.cli serve`) has python.exe (or pytest) all
    the way up. Must NOT be misclassified as undetectable.
    """
    monkeypatch.delenv("HERMES_SUPERVISED", raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(sys, "argv", ["/usr/bin/python3", "run.py"])
    monkeypatch.setattr(
        "hermes_orch.core.restart._parent_process_name",
        lambda: "python.exe",
    )
    monkeypatch.setattr(
        "hermes_orch.core.restart._get_process_ancestry",
        lambda: [
            (1234, "python.exe"),
            (5678, "python.exe"),
        ],
    )
    assert detect_process_mode() == "direct"


def test_get_process_ancestry_returns_empty_on_non_windows(monkeypatch):
    """Non-Windows platforms return an empty chain (we only have Windows PowerShell)."""
    monkeypatch.setattr(sys, "platform", "linux")
    from hermes_orch.core.restart import _get_process_ancestry
    assert _get_process_ancestry() == []


def test_get_process_ancestry_handles_powershell_failure(monkeypatch):
    """PowerShell missing / failure -> empty chain -> falls through to next rule.

    We use PowerShell's `Get-CimInstance Win32_Process` (not `wmic`,
    which was removed in Windows 11 24H2) to read the process table.
    If the subprocess fails for any reason, we MUST return [] so the
    caller can fall through to the next detection rule.
    """
    import subprocess

    def fake_run(*args, **kwargs):
        raise OSError("powershell not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    from hermes_orch.core.restart import _get_process_ancestry
    assert _get_process_ancestry() == []


def test_get_process_ancestry_handles_powershell_nonzero_exit(monkeypatch):
    """PowerShell returns non-zero -> empty chain (no exception, just no data)."""
    import subprocess

    def fake_run(*args, **kwargs):
        class R:
            returncode = 1
            stdout = ""
            stderr = "fake error"
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    from hermes_orch.core.restart import _get_process_ancestry
    assert _get_process_ancestry() == []


def test_get_process_ancestry_handles_malformed_json(monkeypatch):
    """PowerShell returns non-JSON -> empty chain (defensive parse)."""
    import subprocess

    def fake_run(*args, **kwargs):
        class R:
            returncode = 0
            stdout = "not json at all"
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    from hermes_orch.core.restart import _get_process_ancestry
    assert _get_process_ancestry() == []


def test_has_non_respawning_launcher_ancestor_handles_empty_chain(monkeypatch):
    """Empty ancestry (e.g. wmic failed) -> no launcher found -> False."""
    monkeypatch.setattr(sys, "platform", "linux")
    from hermes_orch.core.restart import _has_non_respawning_launcher_ancestor
    assert _has_non_respawning_launcher_ancestor() is False
