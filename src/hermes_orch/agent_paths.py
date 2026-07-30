# coding: utf-8
"""Path resolution for hermes profile directories.

Used by the agent CLI (register, apply-configs, start) to find where hermes
keeps its profile data, without hard-coding per-OS paths in user config.

Resolution order:
  1. Env var: HERMES_PROFILES_DIR (highest priority, user override)
  2. Run `hermes` CLI and ask it (e.g. `hermes --profiles-dir` or `hermes config`)
  3. Common defaults by OS:
       - Windows: %LOCALAPPDATA%/hermes/profiles (typically C:/Users/<u>/AppData/Local/hermes/profiles)
       - macOS:   ~/Library/Application Support/hermes/profiles
       - Linux:   ~/.local/share/hermes/profiles

If nothing found, return None and let the caller decide (ask user or error).
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path


def detect_hermes_profiles_dir() -> Path | None:
    """Find hermes profiles dir using env var, hermes CLI, then OS defaults."""
    # 1. env var
    env_dir = os.environ.get("HERMES_PROFILES_DIR", "").strip()
    if env_dir:
        p = Path(env_dir).expanduser()
        if p.exists():
            return p
        # env var set but path missing -> still respect it; user knows what they want
        return p

    # 2. try `hermes` CLI
    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        for args in (
            ["--profiles-dir"],
            ["config", "get", "profiles_dir"],
            ["config", "profiles_dir"],
        ):
            try:
                r = subprocess.run(
                    [hermes_bin, *args],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if r.returncode == 0 and r.stdout.strip():
                    p = Path(r.stdout.strip()).expanduser()
                    if p.exists():
                        return p
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

    # 3. OS default
    sysname = platform.system().lower()
    if sysname.startswith("win"):
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        candidates = [
            Path(local) / "hermes" / "profiles",
            Path.home() / "AppData" / "Local" / "hermes" / "profiles",
        ]
    elif sysname == "darwin":
        candidates = [
            Path.home() / "Library" / "Application Support" / "hermes" / "profiles",
            Path.home() / ".hermes" / "profiles",
        ]
    else:  # linux and other unix
        candidates = [
            Path.home() / ".local" / "share" / "hermes" / "profiles",
            Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
            / "hermes" / "profiles",
            Path.home() / ".hermes" / "profiles",
        ]
    for c in candidates:
        if c.exists():
            return c
    return None


def resolve_profile_root(
    declared_root: str,
    role: str,
    *,
    profiles_dir: Path | None = None,
) -> Path:
    """Resolve a profile root from wrapper-config.json.

    - `~` and env vars are expanded.
    - If declared_root is a template like "<profiles_dir>/<role>", substitute.
    - If it doesn't exist, try to fall back to detected profiles_dir + role.
    """
    # Expand ~ and env vars
    expanded = Path(os.path.expandvars(os.path.expanduser(declared_root)))

    if expanded.exists():
        return expanded

    # Template substitution
    if "<profiles_dir>" in declared_root or "${profiles_dir}" in declared_root:
        base = profiles_dir or detect_hermes_profiles_dir()
        if base is None:
            raise FileNotFoundError(
                f"profile root template {declared_root!r} for role {role!r} but no "
                f"hermes profiles dir found. Set HERMES_PROFILES_DIR env var or "
                f"edit wrapper-config.json to an absolute path."
            )
        return base / role

    # Fallback: try detected dir + role
    base = profiles_dir or detect_hermes_profiles_dir()
    if base is not None:
        candidate = base / role
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        f"profile root {declared_root!r} (resolved to {expanded}) for role "
        f"{role!r} does not exist. Set HERMES_PROFILES_DIR or fix the path in "
        f"wrapper-config.json."
    )
