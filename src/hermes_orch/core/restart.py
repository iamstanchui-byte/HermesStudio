# coding: utf-8
"""Restart-required flag + process mode detection (v1.0.1 new-user-activation §3.1.2).

The orchestrator cannot rebind a live socket listener without a restart.
Network-bind changes (LAN enable / disable) therefore require the operator
to explicitly trigger a restart. The contract:

    1. POST /api/settings/bind-host  writes desired bind + sets the
       restart-required flag.
    2. UI tells the operator "Restart required to apply network-access
       changes."
    3. POST /api/server/restart  reads the flag and either:
       a. `supervised` mode  -> sys.exit(0) (supervisor restarts us)
       b. `direct` dev mode   -> os.execv(sys.executable, ...) in-place
       c. `undetectable` mode -> 501 + manual restart instructions
    4. The flag is cleared on successful startup of the new process.

`detect_process_mode()` is intentionally a heuristic. The orchestrator
does not run as a true Windows service here; the supervised mode
relies on an env var (`HERMES_SUPERVISED=systemd|nssm`) set by the
install script. Without it, we assume direct mode and let the OS
in-place re-exec do its job; if that's not viable (frozen binary,
embedded), the user gets 501 and a copy-pasteable command.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _flag_path() -> Path:
    """Path to the restart-required flag file.

    Lives in the config dir (~/.hermes-orchestrator/restart-required.flag).
    Computed via `find_config_path()` if available, else defaults to
    `~/.hermes-orchestrator/`. The flag is intentionally NOT a
    config key — it's transient state that only exists between
    "operator changed bind" and "next server start".
    """
    try:
        from hermes_orch.config import find_config_path

        cfg_path = find_config_path()
        if cfg_path:
            return cfg_path.parent / "restart-required.flag"
    except Exception:
        pass
    return Path.home() / ".hermes-orchestrator" / "restart-required.flag"


@dataclass(frozen=True)
class RestartInfo:
    """Public result of `is_restart_required()`.

    `reason` is whatever the API endpoint wrote. Operators can see
    this in the dashboard to know WHY a restart is required
    (e.g. 'bind_host changed from 127.0.0.1 to 0.0.0.0').
    """

    required: bool
    reason: str = ""


def write_restart_required(reason: str) -> Path:
    """Mark a restart as required. Idempotent (overwrites previous reason)."""
    path = _flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(reason, encoding="utf-8")
    return path


def is_restart_required() -> RestartInfo:
    """Read the restart flag. Returns RestartInfo(required=False) if no flag."""
    path = _flag_path()
    if not path.exists():
        return RestartInfo(required=False)
    try:
        return RestartInfo(required=True, reason=path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        # Flag is unreadable (corrupt binary content, or filesystem
        # error). Still mark `required=True` so the operator sees the
        # restart option in the dashboard — they can either restart
        # (clears the flag) or manually delete the file.
        return RestartInfo(required=True, reason="(reason unreadable)")


def clear_restart_required() -> bool:
    """Clear the restart flag. Returns True if a flag was cleared."""
    path = _flag_path()
    if path.exists():
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return False


# ----- process mode detection (§3.1.2) -----


PROCESS_MODE_SUPERVISED = "supervised"
PROCESS_MODE_DIRECT = "direct"
PROCESS_MODE_UNDETECTABLE = "undetectable"


def detect_process_mode() -> str:
    """Detect how the orchestrator process was started.

    Returns one of:
      - "supervised"     (HERMES_SUPERVISED=systemd|nssm env var; supervisor
                          will restart us on sys.exit(0))
      - "direct"         (no env var, but a normal Python process; safe to
                          os.execv in-place to re-exec ourselves)
      - "undetectable"   (frozen / embedded / no reliable argv; we cannot
                          safely restart ourselves; tell the operator to
                          restart manually)

    The supervised env var is the canonical signal. Without it we fall
    back to a heuristic check: if `sys.executable` looks like a real
    Python interpreter and `sys.argv[0]` is a Python script (not a
    frozen exe), assume `direct`. Otherwise `undetectable`.

    The heuristic is intentionally conservative — a false `direct` on a
    frozen binary would cause an in-place exec that breaks the process.
    Better to 501 with manual instructions than to crash a frozen
    deployment.
    """
    # 1. Supervised mode: explicit env var (set by install scripts)
    supervised = os.environ.get("HERMES_SUPERVISED", "").strip().lower()
    if supervised in ("systemd", "nssm", "supervised", "true", "1", "yes"):
        return PROCESS_MODE_SUPERVISED

    # 2. Direct / dev mode: normal Python process we can re-exec
    exe = sys.executable or ""
    argv0 = sys.argv[0] if sys.argv else ""
    # A frozen binary (PyInstaller) has exe ending in .exe AND argv0 == exe
    # (no .py script). A normal Python process has argv0 ending in .py.
    is_frozen = (
        exe.lower().endswith(".exe")
        and (not argv0 or argv0.lower().endswith(".exe"))
    )
    if not is_frozen and exe and argv0:
        return PROCESS_MODE_DIRECT

    # 3. Undetectable: no env var, looks frozen / embedded
    return PROCESS_MODE_UNDETECTABLE


def perform_restart() -> tuple[str, str]:
    """Actually restart the process. Returns (mode, message).

    The HTTP handler calls this when /api/server/restart is invoked.
    The function NEVER returns in `supervised` or `direct` mode (the
    process is gone). In `undetectable` mode, it returns normally and
    the handler maps the result to 501.
    """
    mode = detect_process_mode()
    if mode == PROCESS_MODE_SUPERVISED:
        # sys.exit(0) returns control to the supervisor. The HTTP response
        # has already been flushed by the caller (see api/server.py).
        sys.exit(0)
    if mode == PROCESS_MODE_DIRECT:
        # os.execv replaces the current process image. argv is preserved
        # (so subsequent boots see the same --config-dir / port flags).
        os.execv(sys.executable, [sys.executable] + sys.argv)
        # execv does not return on success.
        raise RuntimeError("os.execv returned unexpectedly")
    # Undetectable: caller maps to 501.
    return mode, "Please restart the server manually (Ctrl+C and re-run `hermes-orch serve`)."
