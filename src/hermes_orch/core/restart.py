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
import subprocess
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


# Names of launchers that do NOT auto-respawn their child process.
# When the orchestrator is started by one of these, an in-place `os.execv`
# on the worker is unsafe: the launcher exits when the worker exits, and
# the operator is left with a dead server. We must return
# `undetectable` so the API can return 501 with a manual-restart
# instruction.
_NON_RESPAWNING_LAUNCHERS = frozenset({
    "hermes-orch.exe",
    "hermes-orch",
})


def _parent_process_name() -> str | None:
    """Return the name of the parent process on Windows, or None on error.

    Uses `tasklist` (always present on Windows) rather than psutil, so
    the restart path has zero pip dependencies. The function is called
    only on restart, so the ~50ms tasklist cost is acceptable.

    Returns the bare image name (e.g. "hermes-orch.exe", "python.exe",
    "powershell.exe"). Returns None if the lookup fails for any reason
    (non-Windows, tasklist missing, parent already dead, etc.).
    """
    if sys.platform != "win32":
        return None
    ppid = os.getppid()
    if ppid <= 0:
        return None
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH", "/FI", f"PID eq {ppid}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    first_line = result.stdout.strip().splitlines()[0]
    # CSV format: "Image Name","PID","Session Name","Session#","Mem Usage"
    first_col = first_line.split(",", 1)[0].strip()
    return first_col.strip('"') or None


def _get_process_ancestry(max_depth: int = 6) -> list[tuple[int, str]]:
    """Return the parent-process chain for the current process, root first omitted.

    Uses PowerShell's `Get-CimInstance Win32_Process` to read the full
    process table, then walks up from `os.getppid()`. We use PowerShell
    (not `wmic`) because `wmic` was removed in Windows 11 24H2 and
    later. PowerShell ships with every supported Windows install.

    Each tuple is `(pid, name)`. The first element is the immediate
    parent; the last is the highest ancestor found (typically a system
    process or the original shell).

    The chain is bounded by `max_depth` (default 6) to avoid runaway
    walks if the process table is malformed. A `set` of seen PIDs also
    breaks the chain on cycles.

    Returns [] on any error (non-Windows, PowerShell missing, timeout,
    etc.) so the caller can fall through to the next detection rule.
    The function is only called on restart, so the ~500ms PowerShell
    startup cost is acceptable.
    """
    if sys.platform != "win32":
        return []
    # One PowerShell call: output JSON array of {ProcessId, ParentProcessId, Name}.
    # -NoProfile to skip loading the user profile (faster + more deterministic).
    # -Compressed JSON keeps output small for parsing.
    ps_cmd = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId, ParentProcessId, Name | "
        "ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    import json

    try:
        data = json.loads(result.stdout)
    except ValueError:
        return []
    # PowerShell may return a single object (not wrapped in array) when
    # there's only one process. Normalize to a list.
    if isinstance(data, dict):
        data = [data]

    table: dict[int, tuple[str, int]] = {}
    for entry in data:
        try:
            pid = int(entry.get("ProcessId") or 0)
            parent = int(entry.get("ParentProcessId") or 0)
            name = str(entry.get("Name") or "")
        except (TypeError, ValueError):
            continue
        if pid <= 0 or not name:
            continue
        table[pid] = (name, parent)

    chain: list[tuple[int, str]] = []
    current = os.getppid()
    seen: set[int] = set()
    for _ in range(max_depth):
        if current <= 0 or current in seen:
            break
        seen.add(current)
        info = table.get(current)
        if info is None:
            break
        name, parent = info
        chain.append((current, name))
        current = parent
    return chain


def _has_non_respawning_launcher_ancestor() -> bool:
    """True if any ancestor (up to 6 levels up) is a non-respawning launcher.

    Used as a follow-up to `_parent_process_name()` because under the
    hermes-orch.exe launcher the actual worker process has python.exe
    (uvicorn) as its immediate parent, with hermes-orch.exe several
    levels up. The immediate-parent check alone misses the launcher;
    walking the chain catches it.
    """
    if sys.platform != "win32":
        return False
    for _pid, name in _get_process_ancestry():
        if name.lower() in _NON_RESPAWNING_LAUNCHERS:
            return True
    return False


def detect_process_mode() -> str:
    """Detect how the orchestrator process was started.

    Returns one of:
      - "supervised"     (HERMES_SUPERVISED=systemd|nssm env var; supervisor
                          will restart us on sys.exit(0))
      - "direct"         (no env var, but a normal Python process; safe to
                          os.execv in-place to re-exec ourselves)
      - "undetectable"   (frozen / embedded / parent launcher doesn't
                          respawn / no reliable argv; we cannot safely
                          restart ourselves; tell the operator to restart
                          manually)

    Detection order (first match wins):
      1. HERMES_SUPERVISED env var → supervised
      2. Parent process is a known non-respawning launcher (e.g.
         hermes-orch.exe PyInstaller wrapper) → undetectable
      3. Looks like a normal Python process (sys.executable + sys.argv[0]
         looks like a script, not a frozen binary) → direct
      4. Otherwise → undetectable (frozen / embedded / weird)
    """
    # 1. Supervised mode: explicit env var (set by install scripts)
    supervised = os.environ.get("HERMES_SUPERVISED", "").strip().lower()
    if supervised in ("systemd", "nssm", "supervised", "true", "1", "yes"):
        return PROCESS_MODE_SUPERVISED

    # 2. Non-respawning parent launcher (hermes-orch.exe PyInstaller
    #    wrapper). The launcher starts python as a child, but does not
    #    respawn it on exit. An in-place os.execv on the worker would
    #    leave the operator with a dead server. Force undetectable.
    #
    #    Two checks:
    #    a. immediate parent — catches the case where the worker is
    #       launched directly by hermes-orch.exe
    #    b. any ancestor up the chain — catches the common production
    #       case where uvicorn sits between the worker and the launcher
    parent = _parent_process_name()
    if parent and parent.lower() in _NON_RESPAWNING_LAUNCHERS:
        return PROCESS_MODE_UNDETECTABLE
    if _has_non_respawning_launcher_ancestor():
        return PROCESS_MODE_UNDETECTABLE

    # 3. Direct / dev mode: normal Python process we can re-exec
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

    # 4. Undetectable: no env var, looks frozen / embedded / weird
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
    # Undetectable: caller maps to 501. Try to give the operator a
    # specific, copy-pasteable command. If the parent is the
    # hermes-orch.exe launcher, point them at the restart script
    # (the standard way to bounce the server in this setup).
    parent = _parent_process_name()
    if parent and parent.lower() in _NON_RESPAWNING_LAUNCHERS:
        message = (
            f"Server is running under the {parent} launcher, which does not "
            f"auto-respawn its child. Please run `restart-server.ps1` "
            f"(in the project root) to apply the new bind_host."
        )
    else:
        message = (
            "Cannot restart automatically in this environment. "
            "Please restart the server manually (Ctrl+C and re-run `hermes-orch serve`)."
        )
    return mode, message
