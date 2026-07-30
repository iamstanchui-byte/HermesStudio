# coding: utf-8
"""Cross-platform OS helpers.

The orchestrator runs on Windows (LocalSystem / NSSM) and Linux
(systemd user service).  A handful of UI-driven actions ("open folder",
"open in file manager") need to launch the host's file manager from the
server process.  This module centralises the dispatch so the API
endpoints and (eventually) the CLI share one implementation.

Design goals:
  * Single source of truth for "what file manager opens a path on this OS".
  * Graceful fallback on Linux — xdg-open isn't always installed
    (minimal containers, WSL without GUI, headless servers).
  * Windows-correct path handling — explorer.exe wants backslashes for
    some UNC / mounted-drive paths.
  * UI hint string — `file_manager_label()` so templates can say
    "Opened in Files" / "Finder" / "Explorer" without branching in JS.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

# ===== Platform detection =====

PLATFORM = platform.system().lower()
IS_WINDOWS = PLATFORM.startswith("win")
IS_DARWIN = PLATFORM == "darwin"
IS_LINUX = PLATFORM == "linux"


# Ordered by preference: most generic first, then desktop-specific.
# Each entry must accept a single path arg and launch the file manager.
# `xdg-open` is the freedesktop standard and is what most Linux distros ship.
# The fallbacks cover the cases where the user has KDE/GNOME but not
# xdg-utils installed.
_LINUX_FILE_MANAGERS = (
    "xdg-open",
    "gio",
    "kde-open5",
    "kde-open",
    "gnome-open",
    "nautilus",
    "dolphin",
    "thunar",
    "pcmanfm",
)


# ===== UI hints =====

_FILE_MANAGER_LABELS = {
    "win": "Explorer",
    "darwin": "Finder",
    "linux": "Files",
}


def file_manager_label() -> str:
    """Human-readable name of the default file manager on this OS.

    Used by the UI to render success messages like
    "Opened in {label}" without hardcoding platform strings in JS.
    """
    if IS_WINDOWS:
        return _FILE_MANAGER_LABELS["win"]
    if IS_DARWIN:
        return _FILE_MANAGER_LABELS["darwin"]
    if IS_LINUX:
        return _FILE_MANAGER_LABELS["linux"]
    return "file manager"


def platform_name() -> str:
    """Lowercased platform string for the API (`platform` field in responses).

    Returns one of: "windows", "darwin", "linux", or the raw `platform.system()`.
    """
    if IS_WINDOWS:
        return "windows"
    if IS_DARWIN:
        return "darwin"
    if IS_LINUX:
        return "linux"
    return PLATFORM


# ===== Actions =====


def _resolve_linux_file_manager() -> str | None:
    """Find the best available Linux file manager. None if none installed."""
    for cmd in _LINUX_FILE_MANAGERS:
        if shutil.which(cmd):
            return cmd
    return None


def open_path(path: str | os.PathLike[str]) -> tuple[bool, str | None]:
    """Open a local path in the OS file manager.

    Returns `(ok, error)`. On success `error` is None.
    On failure, `error` is a human-readable string suitable for showing
    in a UI status message (NOT a stack trace).

    Behaviour by platform:
      * Windows: `explorer.exe <path>` (path normalised to backslashes
        because explorer.exe is picky about some UNC / mount paths).
      * macOS:   `open <path>`.
      * Linux:   tries `xdg-open` first, then `gio`, `kde-open5`,
        `kde-open`, `gnome-open`, `nautilus`, `dolphin`, `thunar`,
        `pcmanfm` in that order. If none is found, returns
        `(False, "no Linux file manager found (install xdg-utils)")`.

    Note: the subprocess is fire-and-forget (Popen, no wait). The
    file manager is a GUI process; we don't want to block the
    server waiting for the user to close it.
    """
    p = Path(path)
    if not p.exists():
        return False, f"path does not exist: {p}"

    p_str = str(p)

    try:
        if IS_WINDOWS:
            # explorer.exe wants backslashes for some UNC / mount paths
            win_path = p_str.replace("/", "\\")
            subprocess.Popen(["explorer", win_path])
        elif IS_DARWIN:
            subprocess.Popen(["open", p_str])
        else:
            # Linux / other Unix — try the fallback chain
            cmd = _resolve_linux_file_manager()
            if cmd is None:
                return (
                    False,
                    "no Linux file manager found "
                    "(install xdg-utils, or one of: gio, kde-open, nautilus)",
                )
            subprocess.Popen([cmd, p_str])
        return True, None
    except FileNotFoundError as e:
        # The chosen executable was on PATH at lookup time but
        # disappeared by the time we tried to exec (rare race).
        return False, f"file manager not found: {e}"
    except OSError as e:
        # Permission denied, exec format error, etc.
        return False, f"{type(e).__name__}: {e}"
    except Exception as e:  # last-resort safety net
        return False, f"{type(e).__name__}: {e}"


# ===== Install / supervisor detection =====


def service_manager_name() -> str:
    """Human-readable name of the OS service manager used to host the orchestrator.

    Used in install docs and in the CLI's stop/help text. Returns:
      * Windows: "NSSM (Windows Service)"
      * Linux:   "systemd"
      * macOS:   "launchd" (not currently supported; orchestrator on macOS
                 is run as a foreground process for now)
    """
    if IS_WINDOWS:
        return "NSSM (Windows Service)"
    if IS_LINUX:
        return "systemd"
    if IS_DARWIN:
        return "launchd"
    return "OS service manager"


def service_manager_install_hint() -> str:
    """One-liner pointing to the install command for this platform.

    Surfaced in CLI help text and the install spec.
    """
    if IS_WINDOWS:
        return "Run watchdog\\register-system.ps1 (admin PowerShell)"
    if IS_LINUX:
        return "Run watchdog/install-systemd.sh"
    return "Service install is not automated for this platform"
