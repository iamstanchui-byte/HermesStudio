# coding: utf-8
"""Agent-side CLI (hermes-orch-agent command).

Commands (per REVIEW.md Ã‚Â§8.1):
- register            Register with orchestrator (get secret, write to .secret file)
- start               Start the wrapper daemon (heartbeat + ready for tasks)
- stop                Stop the daemon
- status              Show current status
- apply-configs       One-shot: poll orchestrator for pending profile configs and apply
- apply-configs-loop  Daemon: keep polling and applying

Implementation note: this is a SEPARATE process from the orchestrator.
On Windows: registered as Windows Service via NSSM. Use
            `watchdog/register-system.ps1` (admin PowerShell) to install.
On Linux:   registered as a systemd user service. Use
            `watchdog/install-systemd.sh` to install.
The orchestrator itself also has a watchdog for monitoring + auto-restart
on both platforms (see `watchdog/` for the per-platform scripts).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import platform
import re
import subprocess
import time
import fnmatch
from pathlib import Path

import click
import httpx
# v3.12.0: HTTPS support. httpx.get/post/etc are wrapped by agent_http
# so the wrapper auto-applies the right `verify` policy (env-driven,
# see agent_http.py for the env var contract). All call sites below
# use `from hermes_orch.agent_http import get/post/...` so swapping
# the import is enough Ã¢â‚¬â€ no per-call verify= plumbing.
from hermes_orch.agent_http import (  # noqa: E402  (import after httpx on purpose)
    delete as _httpx_delete,
    get as _httpx_get,
    get_verify as _agent_http_verify,
    patch as _httpx_patch,
    post as _httpx_post,
    put as _httpx_put,
)

import shutil

# Force UTF-8 on stdout/stderr for service-manager log streams.
# On Windows + NSSM, the log is a cp1252 pipe Ã¢â‚¬â€ any Unicode in
# click.echo / prompt content (CJK, em-dash, smart quotes) blows up with
# UnicodeEncodeError on that stream. That failure propagates OUT of
# _run_task and the supervisor marks the task failed Ã¢â‚¬â€ even though the
# actual work (hermes subprocess) never even started.
# On Linux + systemd, the journal captures bytes verbatim, but the
# same reconfigure keeps click.echo safe under locale-odd environments
# (e.g. LANG=C, no UTF-8 charmap). Cheap insurance either way.
# Bug seen 2026-07-23 on proj-1a4a2962 ("Create a skill to get Ã©Â¦â„¢Ã¦Â¸Â¯Ã¥Â¤Â©Ã¦Â°Â£...").
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass  # already closed, or non-standard stream

from hermes_orch.agent_paths import detect_hermes_profiles_dir


# ===== Helpers =====


def _resolve_hermes_bin() -> str | None:
    """Find the hermes CLI binary.

    Order:
      1. HERMES_BIN env var (user override, takes absolute path)
      2. shutil.which("hermes") Ã¢â‚¬â€ uses current PATH
      3. Common user-local locations (systemd/services don't load .bashrc
         and Windows services run as LocalSystem with a different %USERPROFILE%):
         - Windows: %LOCALAPPDATA%/hermes/hermes-agent/venv/Scripts/hermes.exe
                    %LOCALAPPDATA%/hermes/bin/hermes.exe
                    %USERPROFILE%/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe
                    ~/AppData/Roaming/Python/Scripts/hermes.exe   (pip --user default)
         - Linux:   $HOME/.local/bin/hermes                       (pip --user default)
                    $HOME/.local/share/hermes/bin/hermes
                    $HOME/bin/hermes
      4. Absolute fallbacks on Linux: /usr/local/bin/hermes, /usr/bin/hermes

    Returns absolute path string, or None if not found.
    """
    # 1. explicit override
    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        p = Path(env_bin).expanduser()
        if p.exists() and p.is_file() and os.access(p, os.X_OK):
            return str(p)
    # 2. PATH
    found = shutil.which("hermes")
    if found:
        return found
    # 3. fallback locations
    home = Path.home()
    sysname = platform.system().lower()
    if sysname.startswith("win"):
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        roaming = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        candidates = [
            Path(local) / "hermes" / "hermes-agent" / "venv" / "Scripts" / "hermes.exe",
            Path(local) / "hermes" / "bin" / "hermes.exe",
            Path(local) / "hermes" / "hermes-agent" / "hermes.exe",
            home / "AppData" / "Local" / "hermes" / "hermes-agent" / "venv" / "Scripts" / "hermes.exe",
            home / "AppData" / "Local" / "hermes" / "bin" / "hermes.exe",
            Path(roaming) / "Python" / "Scripts" / "hermes.exe",
            home / "AppData" / "Roaming" / "Python" / "Scripts" / "hermes.exe",
        ]
    else:
        candidates = [
            home / ".local" / "bin" / "hermes",
            home / ".local" / "share" / "hermes" / "bin" / "hermes",
            home / "bin" / "hermes",
            Path("/usr/local/bin/hermes"),
            Path("/usr/bin/hermes"),
        ]
    for c in candidates:
        if c.exists() and c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def _read_secret(secret_path: Path) -> str:
    """Read shared secret from disk (one-time setup writes it)."""
    if not secret_path.exists():
        raise click.ClickException(
            f"Secret file not found: {secret_path}\n"
            f"Run 'hermes-orch-agent register' first."
        )
    return secret_path.read_text(encoding="utf-8-sig").strip()


# Cap on the cleaned task summary stored in the DB. 32KB is enough for
# long research / backtest tasks; a "Show full" button on the dashboard
# lets the user read the whole thing in a scrollable block.
MAX_SUMMARY_CHARS = 32_000


# Module-level cache of per-profile storage_refs. Populated by
# _heartbeat (the server returns it in the heartbeat response),
# read by _run_task to build the [AVAILABLE STORAGE] block in
# the task prompt. Cleared on every heartbeat tick (so operator
# edits to storage_refs in the dashboard propagate within 5s).
_storage_refs_cache: dict[str, list[dict]] = {}

# v3.6.0: per-agent concurrent task cap. Sourced from the heartbeat
# response (server's `agents.max_concurrent_tasks` column) and used
# to size the ThreadPoolExecutor in the daemon loop. Default 1 =
# backward compatible (sequential, one task at a time per process).
# Read by the daemon loop at the top of every tick; size changes
# take effect on the next tick (the pool is rebuilt per tick so we
# don't need to resize an active pool).
_max_concurrent_tasks_cache: int = 1


# v3.12.1 follow-up #6: server-side default for the per-task
# conversation-history window (read from
# /api/agents/{id}/max_history_config each tick). Cached in
# a module-level var so the main loop's per-tick poll can write
# it and `_run_task` can read it without passing through
# function args. Default 6 matches the server's
# config.supervisor.default_max_history_turns; the per-task
# value in task.params["_max_history_turns"] still wins when
# the wrapper is mid-dispatch (set by orchestrator at
# dispatch time, server-resolved across the 4-tier priority
# chain). The cache is only the FALLBACK default.
_max_history_turns_cache: int = 6


def _render_storage_block(role: str) -> str:
    """Render the [AVAILABLE STORAGE] block for the task prompt.

    Pulls from `_storage_refs_cache` (populated by heartbeat) for
    the given profile role. If empty, returns "" (block omitted).
    Each entry is rendered with its `name` alias first (so the agent
    can see `stanley` instead of the long URL), then the kind, then
    the full ref, then the description.

    The 15MB orch cap means the agent must write large outputs
    to one of these paths directly. Per orch-as-coordinator
    principle (2026-07-22): orch stores metadata, agent stores
    data.

    Output format (per entry):
      - [name] kind  ref  -- description
    If `name` is missing, falls back to `[kind] ref  -- description`.
    """
    refs = _storage_refs_cache.get(role) or []
    if not refs:
        return ""
    lines = [
        "--- AVAILABLE STORAGE (for outputs > 15MB; orch cap below) ---",
        "Per orch policy, the orchestrator's project share folder is for",
        "metadata + small files (cap 15MB per file). For LARGE outputs,",
        "write directly to one of these paths and only store a reference",
        "in your task result. Choose by name (preferred) or kind:",
    ]
    for s in refs:
        kind = s.get("kind", "?")
        ref = s.get("ref", "?")
        desc = (s.get("description") or "").strip()
        name = (s.get("name") or "").strip()
        # Format: - [name] kind  ref  -- description
        # If no name: - [kind] ref  -- description
        if name:
            line = f"  - [{name}] {kind}  {ref}"
        else:
            line = f"  - [{kind}] {ref}"
        if desc:
            line += f"  -- {desc}"
        lines.append(line)
    lines.append("--- END AVAILABLE STORAGE ---")
    return "\n".join(lines)


def _resolve_storage_hint(role: str, output_to: str) -> str:
    """Resolve `params.output_to` (an alias name OR a full ref) to a
    [STORAGE HINT] block for the task prompt.

    The supervisor (LLM planner) sets `params.output_to` on tasks that
    need a specific storage destination. The value can be either:
      - an alias `name` (preferred; e.g. "stanley")
      - the full `ref` (fallback if no alias matches; e.g. the long URL)

    Returns the hint block string, or "" if `output_to` doesn't match
    any configured entry. Empty return signals the agent to fall
    back to the [AVAILABLE STORAGE] context.

    Used by `_run_task` to prepend a small "[STORAGE HINT for this task]"
    block right before [AVAILABLE STORAGE] so the agent doesn't have
    to guess which entry to use when there are multiple.
    """
    if not output_to:
        return ""
    refs = _storage_refs_cache.get(role) or []
    if not refs:
        return ""
    target = output_to.strip()
    # Try name alias first, then full ref
    matched = None
    matched_via = ""
    for s in refs:
        name = (s.get("name") or "").strip()
        ref = (s.get("ref") or "").strip()
        if name and target == name:
            matched = s
            matched_via = "alias name"
            break
        if ref and target == ref:
            matched = s
            matched_via = "full ref"
            break
    if not matched:
        # Not found Ã¢â‚¬â€ return a soft warning so the agent knows
        return (
            "--- STORAGE HINT (output_to unresolved) ---\n"
            f"output_to: {target}\n"
            f"WARNING: no entry in [AVAILABLE STORAGE] matches this value.\n"
            "Falling back to [AVAILABLE STORAGE] context Ã¢â‚¬â€ pick the best\n"
            "entry by hand, or fix the task's output_to and re-dispatch.\n"
            "--- END STORAGE HINT ---"
        )
    name = (matched.get("name") or "").strip()
    kind = matched.get("kind", "?")
    ref = matched.get("ref", "?")
    desc = (matched.get("description") or "").strip()
    display = f"[{name}] {kind}  {ref}" if name else f"[{kind}] {ref}"
    lines = [
        "--- STORAGE HINT (for this task) ---",
        f"output_to: {target}  (resolved via {matched_via})",
        f"Target entry: {display}",
    ]
    if desc:
        lines.append(f"Description: {desc}")
    lines.append("Use THIS specific entry from [AVAILABLE STORAGE] for large outputs.")
    lines.append("--- END STORAGE HINT ---")
    return "\n".join(lines)


def _render_workflow_skill_block(
    profile_root: "Path", skill_name: str, params: dict | None = None
) -> str:
    """Render the [SKILL: <name>] block for a workflow-run task.

    Stage 1.5 (2026-07-23): when a workflow step has a `skill`
    reference, the run endpoint puts `{"_workflow_skill": "<name>"}`
    in the task's params. The wrapper reads this and injects the
    skill's body into the task prompt so the agent has the procedure
    (URLs, API patterns, completion criteria) without having to
    re-discover the data source on every run.

    Reads from `<profile_root>/skills/<name>/SKILL.md` (the
    hermes 0.17+ folder layout). If the skill doesn't exist
    locally, returns "" + logs a warning (the agent will have to
    figure it out from the goal).

    The body is truncated to 100KB to keep the prompt bounded.
    """
    if not skill_name:
        return ""
    skill_path = profile_root / "skills" / skill_name / "SKILL.md"
    if not skill_path.exists():
        click.echo(
            f"[daemon] WARNING: workflow-skill {skill_name!r} not found at "
            f"{skill_path}; task will run without skill body"
        )
        return ""
    try:
        body = skill_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        click.echo(f"[daemon] failed to read skill {skill_name!r}: {e}")
        return ""
    # Truncate to keep prompt bounded. 100KB is enough for any
    # realistic skill (most are 5-30KB). The LLM gets the procedure
    # without unbounded token growth.
    max_bytes = 100_000
    if len(body.encode("utf-8")) > max_bytes:
        body = body.encode("utf-8")[:max_bytes].decode("utf-8", errors="replace")
        body += "\n\n[... truncated to 100KB ...]"
    return (
        f"--- SKILL: {skill_name} (workflow reference, body prepended) ---\n"
        f"{body}\n"
        f"--- END SKILL: {skill_name} ---\n\n"
        f"This skill was referenced by a workflow step. Follow its procedure "
        f"to execute this task. After reading, you may use any tool you have "
        f"access to.\n"
        f"--- END SKILL HINT ---"
    )


def _render_output_format_block() -> str:
    """Render the [OUTPUT FORMAT] block for the task prompt.

    Convention (2026-07-22, user-stated):
    - .md  : for normal text output (human-readable deliverable)
    - .json: ONLY for parameter values (machine-readable structured data)
    - DO NOT write `.results.json` to cache_dir as a status file Ã¢â‚¬â€
      task status is reported via the API (status/summary fields),
      not as a separate file. The wrapper auto-upload loop also
      skips any `*.results.json` it finds for this reason.

    Always emitted (no opt-in flag) Ã¢â‚¬â€ this is the project's house
    style and shouldn't change per-profile.
    """
    return (
        "--- OUTPUT FORMAT ---\n"
        "Output file conventions for the orchestrator's project share:\n"
        "- .md  : use for normal text output (human-readable deliverable)\n"
        "- .json: use ONLY for parameter values (machine-readable structured\n"
        "         data the next step will parse). Never as a status file.\n"
        "- DO NOT write a `.results.json` to cache_dir. Your task status is\n"
        "  already reported via the API (status/summary fields on /result).\n"
        "  The wrapper auto-upload also skips any `*.results.json` it sees.\n"
        "--- END OUTPUT FORMAT ---"
    )


def _atomic_write(target: Path, content: str) -> None:
    """Atomic write: write to .tmp then rename. Survives partial writes.

    IMPORTANT: pass ``newline=""`` to write_text. On Windows, the default
    ``newline=None`` translates ``\n`` to ``os.linesep`` (``\r\n``) on every
    write. Combined with the wrapper's self-taught reverse-sync (read disk,
    POST content, apply back), this creates a runaway loop: each round adds
    one more ``\r`` before every ``\n``, so skill files grow by ~N bytes
    per round where N is the line count. Setting ``newline=""`` keeps the
    bytes-on-disk identical to ``content.encode("utf-8")``, so file SHA ==
    desired_sha and the auto-sync dedup holds.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="")
    # On Windows, Path.replace is atomic if both files on same volume.
    tmp.replace(target)


# ===== Project memory fetch (HTTP) =====
#
# The wrapper needs to read the project's L2 (facts.md) and L3 (state.md)
# for prompt injection. We do this via the orchestrator's HTTP API rather
# than reading from disk because the wrapper runs on a different machine
# than the orchestrator (e.g. linux-a-01 vs the Windows server), so its
# local filesystem doesn't have the projects_root path. Going through HTTP
# works regardless of where the wrapper is running.

def _hmac_headers(
    agent_id: str, secret: str, *, method: str = "GET", path: str = "", body: bytes = b""
) -> dict:
    """Build HMAC-signed headers for a wrapper request (v1.6+).

    The signature binds (method, path, sha256(body), timestamp) so
    captured requests can't be replayed against a different endpoint
    or with a different body. The server-side verify is in
    src/hermes_orch/auth/hmac.py (the same `string_to_sign` format).

    Used by:
      - _fetch_project_state_http / _fetch_user_recent_http /
        _fetch_project_facts_http (GET, no body) Ã¢â‚¬â€ these pass defaults
      - heartbeat (POST with optional JSON body) Ã¢â‚¬â€ call site passes
        method="POST" + path + body
      - all other wrapper endpoints Ã¢â‚¬â€ call site passes method/path/body

    Args:
      agent_id: the agent id (goes into X-Agent-Id)
      secret: the shared secret (read from .secret-<id> file)
      method: HTTP method (default GET for backwards compat with
        the 3 fetch helpers that don't pass it)
      path: request path including query string (default "" for
        helpers that don't need it Ã¢â‚¬â€ but the server's verify will
        still pass because empty path matches empty path)
      body: raw request body bytes (default b"")
    """
    import time as _t
    from hermes_orch.auth import compute_signature
    ts = str(int(_t.time()))
    sig = compute_signature(secret, method, path, body, ts)
    return {
        "X-Agent-Id": agent_id,
        "X-Timestamp": ts,
        "X-Signature": sig,
    }


# ===== v1.9.3 subprocess timeout helper =====
#
# Previously, _run_task did `proc.wait(timeout=timeout)` and on
# TimeoutExpired called `proc.kill()`. Two problems:
#
#   1. `timeout` was a CLI-level constant (--timeout default 1800).
#      The task model has its own `timeout_seconds` field (per-task,
#      also defaulting to 1800 in the schema), but the wrapper
#      ignored it. A supervisor-set 1200s task would still get the
#      full 1800s before being killed.
#
#   2. `proc.kill()` is SIGKILL/TerminateProcess Ã¢â‚¬â€ immediate, no
#      chance for hermes to flush its session, write a graceful
#      shutdown, or release file locks. Better practice: SIGTERM
#      first (grace_seconds), then SIGKILL only if it ignored
#      SIGTERM. Same shape as Linux/Unix process supervision.
#
# This helper centralizes both fixes. It returns (rc, timed_out)
# so the caller can build the right failure result message. It
# is module-level (not nested in start()) so it can be unit-tested
# directly with a real subprocess Ã¢â‚¬â€ no daemon required.
DEFAULT_SUBPROCESS_GRACE_SEC = 5
MAX_TASK_TIMEOUT_SEC = 1800  # 30 min hard cap (defense in depth)


def _run_subprocess_with_timeout(
    proc: "subprocess.Popen",
    *,
    timeout: int,
    grace_seconds: int = DEFAULT_SUBPROCESS_GRACE_SEC,
) -> tuple[int | None, bool]:
    """Wait for `proc` to exit, with a hard timeout.

    Behavior:
      - If the process exits within `timeout` seconds, returns
        (returncode, False).
      - If the process is still alive after `timeout` seconds:
          1. Send SIGTERM/terminate() (graceful shutdown)
          2. Wait up to `grace_seconds` for clean exit
          3. If still alive, send SIGKILL/kill() and wait 2s for
             OS to reap
        Returns (None, True). The caller is responsible for the
        "task failed: timeout" result message.

    Why terminate-then-kill (not just kill):
      - hermes-agent on a clean SIGTERM flushes its session.db,
        releases MCP server connections, and writes a partial
        transcript. SIGKILL skips all of that, so the user loses
        whatever output the agent had generated so far.
      - On Windows, terminate() calls GenerateConsoleCtrlEvent
        which hermes handles as a graceful shutdown. Most of the
        time hermes exits in <1s; the 5s grace covers slow cases.
    """
    try:
        rc = proc.wait(timeout=timeout)
        return rc, False
    except subprocess.TimeoutExpired:
        # Phase 1: graceful terminate. On Windows this is
        # GenerateConsoleCtrlEvent; on POSIX it's SIGTERM.
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=grace_seconds)
            return None, True  # exited within grace
        except subprocess.TimeoutExpired:
            pass
        # Phase 2: force kill. Stops ignoring SIGTERM/CTRL_BREAK.
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        return None, True


def _bootstrap_hmac_secret(orchestrator_url: str, agent_id: str, secret: str) -> None:
    """Push the local secret to the orchestrator's hmac_secret column.

    Called from start() on every wrapper start. The endpoint is
    one-shot:
      - 201 {"status": "set"}        -> first call, secret stored
      - 200 {"status": "already_set", "match": true} -> same secret, no-op
      - 409 {"status": "conflict"}   -> different secret, loud error

    We log success quietly and surface 409s as a hard failure
    (the operator must reconcile before the wrapper can do useful
    work; without matching secrets, every signed request 401s).
    """
    import time as _t
    path = f"/api/agents/{agent_id}/secret"
    body = json.dumps({"secret": secret}).encode("utf-8")
    # The bootstrap endpoint isn't HMAC-authed (it IS the bootstrap),
    # so we sign it with a dummy value. But the server doesn't
    # check HMAC on this endpoint Ã¢â‚¬â€ it just reads the body. So we
    # can omit the signature header (the server doesn't require it).
    try:
        r = _httpx_post(
            f"{orchestrator_url}{path}",
            content=body,
            headers={
                "Content-Type": "application/json",
                # X-Agent-Id is required (the endpoint reads it for
                # audit logging, not for auth).
                "X-Agent-Id": agent_id,
            },
            timeout=10,
        )
        if r.status_code in (200, 201):
            click.echo(f"  [hmac] secret synced to orchestrator ({r.json().get('status')})")
        elif r.status_code == 409:
            raise click.ClickException(
                f"HMAC secret conflict: orchestrator has a different hmac_secret for "
                f"agent {agent_id} than the local file. The local .secret-{agent_id} "
                f"was likely rotated without telling the orchestrator. Fix with "
                f"POST /api/agents/{agent_id}/rotate-key (v1.6.1+ Ã¢â‚¬â€ manual DB update "
                f"for now: DELETE from agents where id='{agent_id}' then re-register)."
            )
        else:
            click.echo(
                f"  [hmac] WARNING: bootstrap returned {r.status_code} {r.text[:200]}"
            )
    except httpx.HTTPError as e:
        # Network errors are non-fatal Ã¢â‚¬â€ the wrapper will still try
        # to heartbeat, and if HMAC is required the heartbeat will
        # 401 with a clearer error.
        click.echo(f"  [hmac] bootstrap network error (non-fatal): {e}")


def _stream_throttle_loop(
    path: "Path",
    should_stop: "Callable[[], bool]",
    flush: "Callable[[str], None]",
    *,
    throttle_s: float = 2.0,
    buf_max: int = 8192,
    sleep_fn: "Callable[[float], None] | None" = None,
    time_fn: "Callable[[], float] | None" = None,
) -> None:
    """Tail `path` in a loop, throttling `flush` calls so we don't
    spam the network on every byte of agent output.

    Flushes when EITHER:
      - the buffer has accumulated buf_max bytes, OR
      - throttle_s seconds have elapsed since the last flush.

    Always does a final flush on exit (so we don't lose the tail
    of the transcript when the subprocess finishes).

    Args:
        path: file to tail (the wrapper writes hermes's stdout/stderr
            here).
        should_stop: callable returning True when the loop should
            exit (e.g. threading.Event.is_set).
        flush: callable invoked with the buffered text each time
            the throttle trips. Errors are swallowed by the caller.
        throttle_s: minimum seconds between flushes (default 2s).
        buf_max: maximum bytes before a forced flush (default 8KB).
        sleep_fn: how to sleep between iterations (default
            time.sleep). Pass a threading.Event.wait() to make the
            loop interruptible.
        time_fn: how to read the current time (default time.time).
            Injectable for unit tests that want deterministic timing.

    The function is extracted (not inlined into _tail_stream) so
    unit tests can exercise the throttling algorithm without
    spinning up a real hermes subprocess.
    """
    import time as _t
    if sleep_fn is None:
        sleep_fn = _t.sleep
    if time_fn is None:
        time_fn = _t.time

    pos = 0
    buf = ""
    last_flush = time_fn()
    while not should_stop():
        try:
            with open(path, "rb") as f:
                f.seek(pos)
                new = f.read()
            if new:
                pos += len(new)
                buf += new.decode("utf-8", errors="replace")
        except FileNotFoundError:
            pass
        now = time_fn()
        if buf and (
            len(buf) >= buf_max
            or now - last_flush >= throttle_s
        ):
            flush(buf)
            buf = ""
            last_flush = now
        if should_stop():
            break
        sleep_fn(0.25)
    # Final flush on shutdown so the user sees the last few lines
    # even if the buffer hadn't filled yet
    if buf:
        flush(buf)


def _fetch_project_state_http(
    orchestrator_url: str, agent_id: str, secret: str, project_id: str
) -> str | None:
    """Fetch the project's L3 (state.md) via HTTP API. None if missing.

    Returns the full state.md content (no truncation; we apply 2KB cap
    client-side via simple char count, since L3 is already capped at
    2KB by the synthesis module).
    """
    try:
        r = _httpx_get(
            f"{orchestrator_url}/api/projects/{project_id}/memory/state",
            headers=_hmac_headers(
                agent_id, secret,
                method="GET",
                path=f"/api/projects/{project_id}/memory/state",
            ),
            timeout=10,
        )
    except Exception as e:
        click.echo(f"[daemon] state fetch HTTP error: {e}")
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    if not data.get("exists"):
        return None
    content = data.get("content")
    if not content:
        return None
    # Cap at 2KB client-side
    if len(content.encode("utf-8")) > 2048:
        content = content.encode("utf-8")[:2048].decode("utf-8", errors="replace")
        content += "\n[Ã¢â‚¬Â¦truncatedÃ¢â‚¬Â¦]"
    return content


def _fetch_user_recent_http(
    orchestrator_url: str, agent_id: str, secret: str
) -> str | None:
    """Fetch the user-level L3 (recent.md) via HTTP API. None if missing.

    Returns the recent.md content (4KB cap). Used for cross-project
    context: "what has the user been up to lately".
    """
    try:
        r = _httpx_get(
            f"{orchestrator_url}/api/projects/memory/recent",
            headers=_hmac_headers(
                agent_id, secret,
                method="GET",
                path="/api/projects/memory/recent",
            ),
            timeout=10,
        )
    except Exception as e:
        click.echo(f"[daemon] recent fetch HTTP error: {e}")
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    if not data.get("exists"):
        return None
    content = data.get("content")
    if not content:
        return None
    if len(content.encode("utf-8")) > 2048:
        content = content.encode("utf-8")[:2048].decode("utf-8", errors="replace")
        content += "\n[Ã¢â‚¬Â¦recent truncatedÃ¢â‚¬Â¦]"
    return content


def _fetch_project_facts_http(
    orchestrator_url: str, agent_id: str, secret: str, project_id: str
) -> str | None:
    """Fetch the project's L2 (facts.md) tail via HTTP API. None if missing.

    Returns the last 4KB of facts.md, matching what the LLM will benefit
    from seeing (recent task results > old goal text). Truncation marker
    is prepended if the file is larger.
    """
    try:
        r = _httpx_get(
            f"{orchestrator_url}/api/projects/{project_id}/memory/facts",
            headers=_hmac_headers(
                agent_id, secret,
                method="GET",
                path=f"/api/projects/{project_id}/memory/facts",
            ),
            timeout=10,
        )
    except Exception as e:
        click.echo(f"[daemon] facts fetch HTTP error: {e}")
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    content = data.get("content")
    if not content:
        return None
    if len(content.encode("utf-8")) > 4096:
        tail = content.encode("utf-8")[-4096:].decode("utf-8", errors="replace")
        return "[earlier entries truncated]\n" + tail
    return content


def _clean_hermes_output(stdout: str) -> str:
    """Strip noise from hermes stdout before storing as task summary.

    Hermes prints a lot of UI chrome (box-drawing borders, ANSI color codes,
    "Initializing agent..." / "Session: <id>" / "Resume this session with..."
    / "Duration: 14s" / "Messages: 24..." lines) that is useful for a
    human watching a live terminal but pure noise in the orchestrator's
    task-result summary. The dashboard renders the result block on demand
    (300 char preview by default, "Show full" for the cleaned text), and
    we want the agent's actual conclusion to show, not hermes' session
    metadata.

    We keep the first MAX_SUMMARY_CHARS chars (32KB by default Ã¢â‚¬â€ large
    enough for research / backtest / multi-step tasks; small enough to
    keep the DB row light). The dashboard's "Show full" button + max-h-96
    scroll lets the user read the whole thing.
    """
    import re
    s = stdout[:MAX_SUMMARY_CHARS]
    # Strip ANSI escape codes (color, cursor moves, etc.)
    s = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)
    # Strip the box-drawing borders that wrap "Hermes" tool headers.
    # First: long runs of box-drawing chars (Ã¢â€â‚¬Ã¢â€ÂÃ¢â€ÂÃ¢â€¢Â­Ã¢â€¢Â®Ã¢â€¢Â°Ã¢â€¢Â¯Ã¢â€â€š...).
    s = re.sub(r"[Ã¢â€â‚¬Ã¢â€ÂÃ¢â€¢Â­Ã¢â€¢Â®Ã¢â€¢Â°Ã¢â€¢Â¯Ã¢â€â€šÃ¢â€Å’Ã¢â€ÂÃ¢â€â€Ã¢â€ËœÃ¢â€Å“Ã¢â€Â¤Ã¢â€Â¬Ã¢â€Â´Ã¢â€Â¼]{4,}", "[Ã¢â‚¬Â¦]", s)
    # Then: drop lines that are mostly box-drawing chrome
    # (e.g. individual `Ã¢â€¢Â­Ã¢â€â‚¬ Ã¢Å¡â€¢ Hermes Ã¢â€â‚¬...Ã¢â€¢Â®` lines, or short `Ã¢â€¢Â°Ã¢â€â‚¬...Ã¢â€¢Â¯`).
    s = re.sub(r"^\s*[Ã¢â€¢Â­Ã¢â€¢Â®Ã¢â€¢Â°Ã¢â€¢Â¯Ã¢â€â€šÃ¢â€Å’Ã¢â€ÂÃ¢â€â€Ã¢â€ËœÃ¢â€Å“Ã¢â€Â¤Ã¢â€Â¬Ã¢â€Â´Ã¢â€Â¼Ã¢â€â‚¬Ã¢â€ÂÃ¢â€Æ’Ã¢â€ÂÃ¢â€¢Â¾Ã¢â€¢Â¼]+\s*[^\n]*$", "", s, flags=re.MULTILINE)
    # Drop the standalone "Ã¢Å¡â€¢ Hermes" brand mark on its own line.
    s = re.sub(r"^\s*Ã¢Å¡â€¢\s*Hermes[^\n]*$", "", s, flags=re.MULTILINE)
    # Strip hermes' "tool call" rows like "Ã¢â€Å  Ã°Å¸â€™Â» preparing terminalÃ¢â‚¬Â¦"
    # and "Ã¢â€Å  Ã°Å¸â€™Â» $         curl ... 1.2s". These dominate the output.
    s = re.sub(r"^\s*Ã¢â€Å \s*[^\n]*$", "", s, flags=re.MULTILINE)
    # Strip the trailing session metadata block that hermes prints on
    # exit. Everything from "Resume this session with:" to EOF is noise
    # for a human reading the task summary.
    s = re.sub(
        r"\n*Resume this session with:.*$",
        "\n[Ã¢â‚¬Â¦session metadata strippedÃ¢â‚¬Â¦]",
        s,
        flags=re.DOTALL,
    )
    # Also strip the equivalent "Session: <id>" / "Duration:" / "Messages:"
    # block that sometimes appears at the start of the output.
    s = re.sub(
        r"^Session:\s+\S+.*?Messages:\s+\d+.*$",
        "[Ã¢â‚¬Â¦session metadata strippedÃ¢â‚¬Â¦]",
        s,
        flags=re.MULTILINE | re.DOTALL,
    )
    # Strip the prompt-template echo that hermes prepends to its output
    # (Query: ... --- PROJECT CONTEXT --- ... --- END CONTEXT ---). Without
    # this, L2 (facts.md) Task Results entries get polluted with
    # "Query: create_file(...) LOCAL WORKING DIR: ... SKILL SELF-TEACHING
    # ..." which is the wrapper's own prompt text echoed back.
    s = _strip_prompt_echo(s)
    # Collapse 3+ blank lines into 1.
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# v3.11.0 (2026-08-03): TASK_FAILED marker convention.
#
# The hermes-agent subprocess returns exit code 0 even when the
# agent's reasoning decides the task should fail (e.g. "threshold
# not met, abort"). The orchestrator's task model assumes exit 0 =
# success, so the agent has no clean way to mark a task as failed.
# Without this, feedback_to (which depends on `status='failed'`)
# never fires for LLM-decided failures Ã¢â‚¬â€ the loop-back path only
# triggers for server-side failures (dispatch.mismatch, stuck
# wrapper, missing profile).
#
# The convention: the agent writes a line containing
# `TASK_FAILED: <reason>` to stdout (typically via its last shell
# call, e.g. `echo "TASK_FAILED: total=\$45 < threshold=\$100"`).
# Even with exit code 0, the wrapper detects the marker and
# converts the result to status=failed, with the marker text as
# the `error` field on the task row. The supervisor's loop-back
# then fires (per the existing feedback_to path) and the upstream
# steps are re-dispatched.
#
# Marker rules:
#   - The marker is a literal string `TASK_FAILED:` (case-sensitive,
#     with the colon). Anywhere in stdout.
#   - Only the FIRST occurrence is honored. Multiple markers are
#     ignored; the first is treated as the agent's primary signal.
#   - The reason text is the substring after the first `TASK_FAILED:`
#     on the matched line, trimmed. Multi-line reasons can be put
#     on the same line OR on subsequent lines (we only take the
#     substring after the marker, single line).
#   - Cleanup: `_clean_hermes_output` strips box-drawing / ANSI
#     before this check, so the marker survives the clean step.
#   - If no marker Ã¢â€ â€™ status=completed (unchanged behavior).
_TASK_FAILED_MARKER = "TASK_FAILED:"


def _apply_task_failed_marker(summary: str) -> dict:
    """Convert a hermes-cleaned summary to a result dict, applying
    the TASK_FAILED convention.

    v3.11.0: extracted from `_run_task`'s exit-code-0 branch so the
    marker-detection logic is unit-testable in isolation.

    Args:
        summary: the cleaned hermes stdout (post _clean_hermes_output).

    Returns:
        The result dict to POST to /api/tasks/{id}/result. Either:
          - {"status": "completed", "summary": ..., "skipped_artifacts": []}
            (no marker)
          - {"status": "failed", "error": "TASK_FAILED: <reason>",
             "summary": <full stdout>, "skipped_artifacts": []}
            (marker present, first occurrence wins)
    """
    if _TASK_FAILED_MARKER in summary:
        # Find the first line containing the marker and extract
        # the reason (substring after the marker, trimmed). If
        # somehow the marker appears without a line separator
        # (single-line stdout), we still pick the substring after
        # the first marker.
        for line in summary.splitlines():
            if _TASK_FAILED_MARKER in line:
                reason = line.split(_TASK_FAILED_MARKER, 1)[1].strip()
                return {
                    "status": "failed",
                    "error": f"TASK_FAILED: {reason}",
                    "summary": summary,
                    "skipped_artifacts": [],
                }
        # Marker present in summary but no line matched (defensive Ã¢â‚¬â€
        # could happen if the marker is in a line that was stripped
        # by cleanup). Fall through to completed.
    return {"status": "completed", "summary": summary, "skipped_artifacts": []}


def _strip_prompt_echo(s: str) -> str:
    """Strip the prompt template echo that hermes prepends to its output.

    The wrapper builds the prompt as:
        {action}({params})\n\n[OUTPUT FORMAT]\n\n[STORAGE HINT if params.output_to]\n\n[AVAILABLE STORAGE]\n\n[SKILL: <name> if workflow reference]\n\n--- PROJECT CONTEXT ---\n{context_block}\n--- END CONTEXT ---

    The OUTPUT FORMAT block is always prepended (house style: .md for
    output, .json only for params, no .results.json). STORAGE HINT is
    prepended only if params.output_to is set on the task. AVAILABLE
    STORAGE is prepended only if storage_refs is configured for the
    role. SKILL is prepended only if the task was created from a
    workflow step with a `skill` reference (Stage 1.5, 2026-07-23).
    PROJECT CONTEXT is prepended only if a context_block is built.

    Hermes echoes this prompt at the top of its stdout (because it was the
    system message it received), so without stripping, the orchestrator's
    task summary contains "Query: create_file(...) --- PROJECT CONTEXT ---
    LOCAL WORKING DIR: ... SKILL SELF-TEACHING..." which is just noise
    from the human reader's perspective and pollutes the L2 (facts.md)
    Task Results section that gets injected into future task prompts.

    We strip the LAST prompt-echo closing marker found near the start
    of the output (whichever is deepest: END OUTPUT FORMAT, then
    END STORAGE HINT, then END AVAILABLE STORAGE, then END CONTEXT).
    Order in the prompt: OUTPUT FORMAT first, then STORAGE HINT, then
    AVAILABLE STORAGE, then PROJECT CONTEXT, so the deepest marker is
    the right one to strip up to.

    Strategy: only strip if the markers are near the start (first ~2500
    chars -- bigger to accommodate the storage block) -- if the body of
    the analysis references these strings later in the output, we
    leave them alone.
    """
    head = s[:2500]
    # Find the LAST closing marker in the first 2500 chars. Each marker
    # is associated with a block that the agent shouldn't have echoed
    # in the first place. Strip everything up to and including the
    # deepest closing marker. Order: OUTPUT FORMAT (always) Ã¢â€ â€™ STORAGE
    # HINT (only if params.output_to set) Ã¢â€ â€™ AVAILABLE STORAGE (only if
    # storage_refs configured) Ã¢â€ â€™ PROJECT CONTEXT (only if context_block
    # built). The deepest marker is the right one to strip up to.
    end_markers = [
        r"\s*--- END OUTPUT FORMAT ---\s*\n",
        r"\s*--- END STORAGE HINT ---\s*\n",
        r"\s*--- END AVAILABLE STORAGE ---\s*\n",
        r"\s*--- END SKILL: \S+ ---\s*\n",
        r"\s*--- END SKILL HINT ---\s*\n",
        r"\s*--- END CONTEXT ---\s*\n",
    ]
    last_end = -1
    last_end_pos = 0
    for marker in end_markers:
        m = re.search(marker, head)
        if m and m.end() > last_end_pos:
            last_end_pos = m.end()
    if last_end_pos > 0:
        return s[last_end_pos:].lstrip()
    # If only "Query: ..." is at the start, drop that single line
    s = re.sub(r"^Query:[^\n]*\n\n?", "", s, count=1)
    return s


# Hermes state.db schema versions:
#   v0.17+ (current): sessions table has input_tokens, output_tokens,
#                     cache_read_tokens, cache_write_tokens, reasoning_tokens,
#                     model, billing_provider, billing_base_url,
#                     estimated_cost_usd, started_at, ended_at, cwd
# Earlier versions: schema may differ (no per-session token columns).
# If a column is missing, _capture_session_tokens falls back gracefully
# and returns the partial dict.
_HERMES_TOKEN_COLUMNS_V0_17 = [
    "id",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "model",
    "billing_provider",
    "billing_base_url",
    "estimated_cost_usd",
    "started_at",
    "ended_at",
    "message_count",
    "tool_call_count",
]


# ===== Zombie-session sweep (Plan B for #22 follow-up) =====
#
# Hermes's own `state.db` accumulates rows in the `sessions` table for
# every hermes CLI/TUI/Telegram invocation. Most close cleanly
# (ended_at IS NOT NULL), but a non-trivial number end up with
# ended_at IS NULL when the hermes subprocess is killed mid-flight
# (Ctrl-C, parent wrapper crash, OOM). These zombies are harmless
# individually but they grow the state.db (super profile was 33 MB
# with 49 zombie sessions as of 2026-07-21) and they generate log
# spam because the wrapper's normal cleanup path only touches
# `source='orchestrator'` rows (which the supervisor manages).
#
# The fix: periodically scan each profile's state.db for sessions
# that are (a) still active (ended_at IS NULL) AND (b) NOT
# orchestrator-managed (source != 'orchestrator') AND (c) older than
# the TTL. Call `hermes sessions delete <id> --yes` for each. Safe to
# run because:
#   - We exclude source='orchestrator' (the supervisor owns those)
#   - We require 7+ days old (won't touch any session currently in use)
#   - hermes sessions delete is idempotent (no-op if already gone)
_ZOMBIE_SESSION_TTL_DAYS = 7
_ZOMBIE_SESSION_SWEEP_INTERVAL_S = 24 * 3600  # once a day is plenty
_last_zombie_sweep: dict[str, float] = {}  # profile_name -> ts


def _sweep_zombie_sessions_inline(
    pname: str,
    pcfg: dict,
    *,
    ttl_days: int = _ZOMBIE_SESSION_TTL_DAYS,
    dry_run: bool = False,
    hermes_profiles_dir: "Path | None" = None,
) -> int:
    """Delete hermes-internal zombie sessions from <profile>/state.db.

    Targets:
      - ended_at IS NULL (process never set the close timestamp)
      - source != 'orchestrator' (we don't touch orchestrator-managed
        sessions; those go through the supervisor cleanup-ack flow)
      - started_at < now - ttl_days (defensive: don't touch recent
        sessions in case a long-lived hermes subprocess is still using
        them Ã¢â‚¬â€ e.g. a dashboard or gateway)

    Returns the number of sessions deleted (0 if none found / dry-run).

    Best-effort: failures are logged, not raised. The orchestrator
    can re-run safely on the next tick.
    """
    from hermes_orch.agent_paths import resolve_profile_root
    import sqlite3 as _sqlite3
    import subprocess as _subprocess
    import time as _time_mod

    try:
        root = resolve_profile_root(
            pcfg.get("root", ""),
            pname,
            profiles_dir=hermes_profiles_dir,
        )
    except Exception as e:
        click.echo(f"[daemon] zombie-sweep {pname}: resolve_profile_root failed: {e}")
        return 0
    state_db = Path(root) / "state.db"
    if not state_db.exists():
        return 0

    # Threshold = (now - ttl_days) in unix epoch seconds. Hermes
    # state.db `started_at` is stored as unix seconds (float), based
    # on observed schema.
    threshold = _time_mod.time() - ttl_days * 86400
    try:
        db = _sqlite3.connect(str(state_db))
        try:
            rows = db.execute(
                "SELECT id, source, started_at FROM sessions "
                "WHERE ended_at IS NULL "
                "  AND (source IS NULL OR source != 'orchestrator') "
                "  AND started_at IS NOT NULL AND started_at < ? "
                "ORDER BY started_at ASC LIMIT 200",
                (threshold,),
            ).fetchall()
        finally:
            db.close()
    except Exception as e:
        click.echo(f"[daemon] zombie-sweep {pname}: state.db query failed: {e}")
        return 0

    if not rows:
        return 0

    if dry_run:
        click.echo(
            f"[daemon] zombie-sweep {pname}: dry-run, would delete "
            f"{len(rows)} session(s) older than {ttl_days}d"
        )
        return 0

    # Find the hermes CLI on PATH. We try a couple of common locations;
    # if neither works, we silently skip (the wrapper can't do much
    # without hermes anyway Ã¢â‚¬â€ it'll have been failing earlier already).
    hermes_bin = None
    for cand in (Path(root).parent.parent / "hermes-agent" / "venv" / "bin" / "hermes",
                 Path(root).parent / "hermes-agent" / "venv" / "bin" / "hermes"):
        if cand.exists():
            hermes_bin = str(cand)
            break
    if hermes_bin is None:
        # Fall back to whatever's on PATH
        hermes_bin = "hermes"

    deleted = 0
    for sid, source, started_at in rows:
        # Skip if session is super-stale (> 90d): hermes may have
        # already pruned it; the SQL DELETE is harmless but the
        # subprocess call is wasted. (defensive: cheap skip)
        age_d = (_time_mod.time() - float(started_at)) / 86400 if started_at else 0
        if age_d > 90:
            continue
        try:
            proc = _subprocess.run(
                [hermes_bin, "sessions", "delete", sid, "--yes"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0:
                deleted += 1
            elif "not found" in (proc.stderr or "").lower() or "no such" in (proc.stderr or "").lower():
                # Already gone Ã¢â‚¬â€ count as success (we wanted it gone)
                deleted += 1
            else:
                click.echo(
                    f"[daemon] zombie-sweep {pname}: delete {sid} (age {age_d:.1f}d) "
                    f"failed: rc={proc.returncode} err={proc.stderr.strip()[:200]}"
                )
        except _subprocess.TimeoutExpired:
            click.echo(f"[daemon] zombie-sweep {pname}: delete {sid} timed out (>15s)")
        except Exception as e:
            click.echo(f"[daemon] zombie-sweep {pname}: delete {sid} error: {e}")
    if deleted:
        click.echo(
            f"[daemon] zombie-sweep {pname}: deleted {deleted} zombie session(s) "
            f"(older than {ttl_days}d, source != orchestrator)"
        )
    return deleted


def _read_profile_config(profile_root: Path) -> dict:
    """Read <profile_root>/config.yaml and extract the fields the
    orchestrator's agent page needs to show per-profile metadata:
    LLM model (default / base_url / provider) and MCP server list.

    Returns a dict with keys:
        model_default:   str | None
        model_base_url:  str | None
        model_provider:  str | None
        mcp_servers:     list[dict]  (each: {"name": str, "enabled": bool})

    All fields are optional. On missing file, parse error, or empty
    config, returns a dict with all-None / empty-list defaults. Never
    raises Ã¢â‚¬â€ heartbeat must not break on a single bad config.yaml.

    YAML schema (v0.17+):
        model:
          default: MiniMax-M3
          base_url: https://...
          provider: minimax-oauth
        mcp_servers:
          <name>:
            command: ...
            args: [...]
            enabled: true   # optional, default true
    """
    out: dict = {
        "model_default": None,
        "model_base_url": None,
        "model_provider": None,
        "mcp_servers": [],
    }
    if not profile_root:
        return out
    cfg_path = profile_root / "config.yaml"
    if not cfg_path.exists() or not cfg_path.is_file():
        return out
    try:
        import yaml
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        click.echo(f"  WARN: failed to read {cfg_path}: {e}")
        return out
    if not isinstance(cfg, dict):
        return out
    model = cfg.get("model") or {}
    if isinstance(model, dict):
        out["model_default"] = model.get("default") if isinstance(model.get("default"), str) else None
        out["model_base_url"] = model.get("base_url") if isinstance(model.get("base_url"), str) else None
        out["model_provider"] = model.get("provider") if isinstance(model.get("provider"), str) else None
    mcp_servers = cfg.get("mcp_servers") or {}
    if isinstance(mcp_servers, dict):
        for name, conf in mcp_servers.items():
            if not isinstance(name, str) or not name:
                continue
            enabled = True
            if isinstance(conf, dict) and "enabled" in conf:
                enabled = bool(conf.get("enabled"))
            out["mcp_servers"].append({"name": name, "enabled": enabled})
    return out


def _extract_skills_used_from_transcript(transcript_path: Path) -> list[str]:
    """Parse a hermes transcript for the `Ã°Å¸â€œÅ¡ skill <name>` markers that
    indicate which skills the agent actually loaded during the task.

    The hermes CLI prints a line per skill it loads via the skill_view
    tool, formatted as:
        Ã¢â€Å  Ã°Å¸â€œÅ¡ skill     <name>  <duration>
        Ã¢â€Å  Ã°Å¸â€œÅ¡ skill     <name>  <duration>
    (Note: multiple spaces between "skill" and the name.)

    Stage 1.5 multi-skill awareness (2026-07-23): the wrapper reports
    this list in the /result POST so promote-to-workflow can preserve
    every skill the source used. The orchestrator-side helper does the
    same parse Ã¢â‚¬â€ kept duplicated on purpose (the server can't read the
    agent-side transcript directly).
    """
    import re
    pattern = re.compile(r"Ã°Å¸â€œÅ¡\s+skill\s+(\S+)\s+[\d.]+s")
    out: list[str] = []
    seen: set[str] = set()
    try:
        text = transcript_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    for m in pattern.finditer(text):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _apply_hermes_compaction_config(profile_root: Path, max_history_turns: int) -> bool:
    """Write `compression.protect_last_n` (and `threshold` 0.30) into
    the per-profile hermes config.yaml.

    v3.12.1 follow-up #6: hermes 0.19.1+ ships an opt-in
    per-turn micro-compaction (config block `compression`).
    The wrapper drives it by writing the per-task N value
    into `compression.protect_last_n` so the next hermes
    subprocess picks up the new cap on startup. We also drop
    `compression.threshold` to 0.30 (down from the default
    0.50) so compaction fires earlier — the measured 4x
    prompt growth (commit 20fb097) is already a problem at
    50% context usage, and a savings demo with 30+ iterations
    will routinely exceed 30%.

    Atomic write: write to a temp file in the same directory,
    then rename. Avoids leaving a half-written config.yaml if
    the wrapper is killed mid-write (hermes 0.19.1 reads
    config.yaml at startup, so a torn write would either
    reject hermes or — worse — silently use a partial
    compression block).

    Args:
        profile_root: the profile's root directory
            (e.g. `<profiles_dir>/super/`). The config lives
            at `<profile_root>/config.yaml`.
        max_history_turns: the per-task N value. 0 means "no
            cap" (we write protect_last_n=0; hermes treats 0
            as keep-everything-compressed-when-needed). > 200
            is a typo and we silently fall back to 6.

    Returns:
        True if the write succeeded, False otherwise (file
        didn't exist, YAML parse error, permission denied).
        Failures are non-fatal: hermes will start with the
        previous config, which is safer than a torn write.
    """
    if not profile_root:
        return False
    cfg_path = Path(profile_root) / "config.yaml"
    if not cfg_path.exists():
        return False
    # Defensive clamp.
    n = max(0, min(200, int(max_history_turns)))
    try:
        import yaml as _yaml
        # Read existing config (preserve everything the operator
        # might have set — model, mcp_servers, providers, etc.).
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return False
        # Set/merge the compression block. Hermes 0.19.1
        # treats `compression` as a top-level dict with these
        # sub-keys. We set:
        #   enabled: True (turn it on, regardless of prior value)
        #   threshold: 0.30 (compact earlier than default 0.50)
        #   target_ratio: 0.20 (keep 20% as recent tail — same as
        #     hermes default; explicit so future hermes defaults
        #     don't override us)
        #   protect_last_n: <N> (the cap — this is the value
        #     that drives our "last N turns uncompressed" model)
        #   in_place: True (use hermes's in-place compaction;
        #     don't rotate session_id, see #38763)
        #   protect_first_n: 3 (preserve head messages
        #     verbatim — same as hermes default)
        existing_comp = data.get("compression") or {}
        if not isinstance(existing_comp, dict):
            existing_comp = {}
        existing_comp["enabled"] = True
        existing_comp["threshold"] = 0.30
        existing_comp["target_ratio"] = (
            existing_comp.get("target_ratio") or 0.20
        )
        existing_comp["protect_last_n"] = n
        existing_comp["protect_first_n"] = (
            existing_comp.get("protect_first_n") or 3
        )
        existing_comp["in_place"] = True
        data["compression"] = existing_comp
        # Atomic write: write to a sibling temp file, then
        # os.replace() — POSIX-atomic and Windows-atomic for
        # the same directory.
        tmp_path = cfg_path.with_suffix(".yaml.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            _yaml.safe_dump(
                data, f, default_flow_style=False,
                sort_keys=False, allow_unicode=True,
            )
        os.replace(tmp_path, cfg_path)
        # v3.12.4 instrument: race vs c3e998d atomic-write (diag only).
        import time as _race_t
        click.echo(f"[race-diag-internal {cfg_path}] t={_race_t.time():.6f} os.replace DONE")
        return True
    except Exception as e:
        click.echo(
            f"  WARN: failed to apply hermes compaction config "
            f"to {cfg_path}: {e}"
        )
        # Clean up the temp file if it's still there
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return False


def _capture_session_tokens(profile_root: Path) -> dict | None:
    """Read the most recent hermes session from ``<profile_root>/state.db``.

    Returns a dict with token usage fields (mapped to the orchestrator's
    token_usage schema) or ``None`` if the session can't be read.

    Mapping (hermes state.db -> orchestrator token_usage):
        input_tokens  -> prompt_tokens
        output_tokens -> completion_tokens
        sum of all token columns -> total_tokens
        model         -> model
        billing_provider -> (stored in call_label suffix, not a column)
        billing_base_url -> base_url

    Defensive: if the schema is older than v0.17, the column query may
    fail; we log a warning and return None. Sub-second tasks (sessions
    that end before being committed) also return None.
    """
    if not profile_root:
        return None
    state_db = profile_root / "state.db"
    if not state_db.exists() or not state_db.is_file():
        return None
    try:
        import sqlite3
        # Read-only URI so we never block on a hermes write lock.
        uri = f"file:{state_db}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            c = conn.cursor()
            # Probe columns first: if the schema is older than v0.17, the
            # token columns won't exist. List the actual columns and skip
            # the ones we expect.
            c.execute("PRAGMA table_info(sessions)")
            available = {row[1] for row in c.fetchall()}
            if not available:
                return None
            cols = [col for col in _HERMES_TOKEN_COLUMNS_V0_17 if col in available]
            if "input_tokens" not in cols or "started_at" not in cols:
                # Either pre-v0.17 (no token columns) or some other schema
                # we don't recognize. Skip silently.
                return None
            # v3.12.1 follow-up #6: also pull message_count (per-session
            # turn count) so the wrapper can populate
            # task_dispatch.history_turn_count for the operator.
            # hermes 0.19.1+ has this column; the schema probe
            # above already accounts for older versions (col
            # list is built from `_HERMES_TOKEN_COLUMNS_V0_17`,
            # which doesn't include it; we add it conditionally
            # after probing).
            if "message_count" in available:
                cols.append("message_count")
            col_list = ", ".join(cols)
            # Don't filter by ended_at Ã¢â‚¬â€ most hermes sessions don't commit
            # an end timestamp even when they're functionally done (the
            # process exits but the session row stays NULL on ended_at /
            # end_reason). The most recent session by started_at is what
            # we want, regardless of whether ended_at is set. The wrapper
            # is the only writer for sessions started within the task's
            # hermes subprocess lifetime, so picking the most recent is
            # the right session for our task.
            c.execute(
                f"SELECT {col_list} FROM sessions "
                "ORDER BY started_at DESC LIMIT 1"
            )
            row = c.fetchone()
            if not row:
                return None
            session = dict(zip(cols, row))
        finally:
            conn.close()
    except Exception as e:
        click.echo(f"  WARN: state.db read failed: {e}")
        return None

    in_t = session.get("input_tokens") or 0
    out_t = session.get("output_tokens") or 0
    cache_r = session.get("cache_read_tokens") or 0
    cache_w = session.get("cache_write_tokens") or 0
    reasoning = session.get("reasoning_tokens") or 0
    return {
        "session_id": session.get("id"),
        "model": session.get("model") or "unknown",
        "billing_provider": session.get("billing_provider"),
        "billing_base_url": session.get("billing_base_url"),
        "estimated_cost_usd": session.get("estimated_cost_usd"),
        "started_at": session.get("started_at"),
        "ended_at": session.get("ended_at"),
        "message_count": session.get("message_count"),
        # v3.12.1 follow-up #6: also expose as `history_turn_count`
        # under a clearer alias. The orchestrator's
        # /api/tasks/{id}/result handler reads this from the
        # result dict and persists it into task_dispatch
        # .history_turn_count (so the dashboard / operator can
        # verify the conversation-history growth fix is
        # working). hermes 0.19.1+ has the underlying
        # `state.db.sessions.message_count` column; older
        # versions return NULL.
        "history_turn_count": (
            int(session.get("message_count"))
            if session.get("message_count") is not None
            else None
        ),
        "tool_call_count": session.get("tool_call_count"),
        # The orchestrator's token_usage schema only has 3 token columns
        # (prompt / completion / total). We map input -> prompt,
        # output -> completion, and report total = input + output
        # (cache tokens are reported in the breakdown but not added to
        # the headline total, to keep "BY MODEL" / "BY AGENT" sums
        # comparable across providers that may or may not bill cache).
        "prompt_tokens": in_t,
        "completion_tokens": out_t,
        "total_tokens": in_t + out_t,
        "cache_read_tokens": cache_r,
        "cache_write_tokens": cache_w,
        "reasoning_tokens": reasoning,
    }


def _load_wrapper_config(config_path: Path) -> dict:
    """Load wrapper config JSON. Fields:
    {
      "agent_id": "linux-a-01",
      "orchestrator_url": "http://192.168.1.10:8765",
      "secret_file": "~/.hermes-orchestrator/.secret-linux-a-01",
      "profiles": {
        "data-analyst": {"root": "~/.hermes/profiles/data-analyst"},
        "backtest-runner": {"root": "~/.hermes/profiles/backtest-runner"}
      }
    }
    """
    if not config_path.exists():
        raise click.ClickException(
            f"Wrapper config not found: {config_path}\n"
            f"Create it first (see docs/wrapper-config.example.json)."
        )
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    # Expand ~ in paths
    for pname, pcfg in raw.get("profiles", {}).items():
        if "root" in pcfg:
            pcfg["root"] = str(Path(pcfg["root"]).expanduser())
    raw["secret_file"] = str(Path(raw["secret_file"]).expanduser())
    return raw


# ===== CLI group =====


@click.group()
def cli() -> None:
    """Hermes agent wrapper - runs on each agent machine."""


@cli.command()
@click.option("--orchestrator", required=True, help="Orchestrator URL (e.g. http://192.168.1.10:8765)")
@click.option("--agent-id", required=True, help="Unique agent ID (e.g. linux-a-01)")
@click.option("--roles", default=None, help="Comma-separated roles (e.g. data-analyst,backtest-runner). Optional Ã¢â‚¬â€ defaults to auto-detected hermes profile names if --profiles-dir is set or HERMES_PROFILES_DIR is set.")
@click.option("--ip", default=None, help="Agent IP (default: auto-detect)")
@click.option("--os-type", default=None, type=click.Choice(["windows", "linux"]), help="OS (default: auto-detect)")
@click.option("--secret-file", default=None, help="Where to write the secret (default: ~/.hermes-orchestrator/.secret-<agent_id>)")
@click.option("--config-file", default=None, help="Where to write wrapper-config.json (default: ~/.hermes-orchestrator/wrapper-config.json)")
@click.option("--profiles-dir", default=None, help="Override hermes profiles dir (default: auto-detect via env/CLI/OS)")
def register(
    orchestrator: str,
    agent_id: str,
    roles: str | None,
    ip: str | None,
    os_type: str | None,
    secret_file: str | None,
    config_file: str | None,
    profiles_dir: str | None,
) -> None:
    """Register this agent with the orchestrator (one-time setup).

    1. POST /api/agents/ to create the agent
    2. Receive one-time secret
    3. Write secret to ~/.hermes-orchestrator/.secret-<agent_id> (chmod 600)
    4. Write skeleton wrapper-config.json for the daemon
    """
    import platform
    import socket

    # Auto-detect OS
    if os_type is None:
        plat = platform.system().lower()
        if plat.startswith("win"):
            os_type = "windows"
        elif plat.startswith("linux"):
            os_type = "linux"
        else:
            os_type = plat

    # Auto-detect local IP if not provided
    if not ip:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            ip = "127.0.0.1"

    # Parse roles: explicit --roles, else auto-detect from detected hermes profiles dir
    role_list: list[str] = []
    if roles:
        role_list = [r.strip() for r in roles.split(",") if r.strip()]
    elif profiles_dir or os.environ.get("HERMES_PROFILES_DIR"):
        # Try to enumerate subdirs as role names
        base = Path(profiles_dir).expanduser() if profiles_dir else detect_hermes_profiles_dir()
        if base and base.exists():
            for p in sorted(base.iterdir()):
                if p.is_dir() and not p.name.startswith("."):
                    role_list.append(p.name)
            if role_list:
                click.echo(f"  - auto-detected {len(role_list)} profile(s) from {base}: {role_list}")
    if not role_list:
        click.echo("  - no roles specified and none auto-detected.")
        click.echo("    Pass --roles data-foo,bar-baz, or set HERMES_PROFILES_DIR.")
        click.echo("    You can also add roles later via the dashboard.")

    # Build request body
    body = {
        "agent_id": agent_id,
        "ip": ip,
        "os_type": os_type,
        "roles": role_list,
    }
    click.echo(f"Registering '{agent_id}' (OS={os_type}, IP={ip}, roles={role_list})")
    click.echo(f"  -> POST {orchestrator.rstrip('/')}/api/agents/")

    # POST
    try:
        r = _httpx_post(
            f"{orchestrator.rstrip('/')}/api/agents/",
            json=body,
            timeout=30,
        )
    except httpx.RequestError as e:
        raise click.ClickException(f"Cannot reach orchestrator at {orchestrator}: {e}")

    if r.status_code == 409:
        raise click.ClickException(
            f"Agent '{agent_id}' already exists. Pick a different ID or use the dashboard to manage it."
        )
    if r.status_code != 201:
        try:
            err = r.json()
            detail = err.get("detail") or err
        except Exception:
            detail = r.text
        raise click.ClickException(f"Registration failed: HTTP {r.status_code}: {detail}")

    data = r.json()
    secret = data.get("setup_secret")
    if not secret:
        raise click.ClickException("Orchestrator did not return a setup_secret")

    click.echo(f"  <- registered (agent id: {data['agent']['id']}, profiles: {len(data['agent']['profiles'])})")

    # Write secret
    if not secret_file:
        secret_file = str(Path.home() / ".hermes-orchestrator" / f".secret-{agent_id}")
    secret_path = Path(secret_file)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(secret, encoding="utf-8")
    try:
        os.chmod(secret_path, 0o600)
    except (OSError, AttributeError, NotImplementedError):
        # Windows doesn't fully support chmod 0o600. Best-effort.
        pass

    # Detect hermes profiles dir for the skeleton
    detected_dir = None
    if profiles_dir:
        detected_dir = Path(profiles_dir).expanduser()
        click.echo(f"  - using --profiles-dir: {detected_dir}")
    else:
        detected_dir = detect_hermes_profiles_dir()
        if detected_dir:
            click.echo(f"  - detected hermes profiles dir: {detected_dir}")
        else:
            click.echo("  - WARN: couldn't auto-detect hermes profiles dir.")
            click.echo("    Set HERMES_PROFILES_DIR env var or use --profiles-dir.")
            click.echo("    Skeleton will use '~' templates Ã¢â‚¬â€ edit later.")

    # Write wrapper-config.json skeleton
    if not config_file:
        config_file = str(Path.home() / ".hermes-orchestrator" / "wrapper-config.json")
    cfg_path = Path(config_file)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg_path.exists():
        # Don't clobber an existing config Ã¢â‚¬â€ just inform
        click.echo(f"  - wrapper-config.json already exists at {cfg_path} (left untouched)")
        click.echo(f"    (edit it manually to point to the right hermes profile roots)")
    else:
        # If we have a real detected dir, write absolute paths. Otherwise use
        # a template that the daemon will resolve via env var or detection.
        profiles_section: dict = {}
        if detected_dir:
            for r in role_list:
                # Only include profiles that actually exist in the detected dir.
                # Skip the ones the user hasn't created yet.
                candidate = detected_dir / r
                if candidate.exists():
                    profiles_section[r] = {"root": str(candidate)}
                else:
                    # Use template Ã¢â‚¬â€ daemon will warn if it can't find this role
                    profiles_section[r] = {"root": str(detected_dir / r)}
        else:
            for r in role_list:
                profiles_section[r] = {"root": f"~/.hermes/profiles/{r}"}

        skeleton = {
            "agent_id": agent_id,
            "orchestrator_url": orchestrator,
            "secret_file": str(secret_path),
            "_profiles_dir": str(detected_dir) if detected_dir else None,
            "profiles": profiles_section,
        }
        # Strip None for cleaner JSON
        if skeleton["_profiles_dir"] is None:
            del skeleton["_profiles_dir"]

        cfg_path.write_text(
            json.dumps(skeleton, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        click.echo(f"  - wrote skeleton wrapper-config.json to {cfg_path}")
        if detected_dir:
            # Verify each declared role actually exists
            missing = [r for r in role_list if not (detected_dir / r).exists()]
            if missing:
                click.echo(f"  - WARN: these roles don't have a profile dir yet: {missing}")
                click.echo(f"    Either create them in hermes, or edit wrapper-config.json")

    # Done
    click.echo("")
    click.secho("  [ok] Registration complete", fg="green", bold=True)
    click.echo("")
    click.echo("  Next steps:")
    click.echo(f"    1. (optional) edit {cfg_path} to fix profile root paths")
    click.echo(f"    2. start the daemon:  hermes-orch-agent start")
    click.echo("")
    click.echo("  To test connection right now:")
    click.echo(f"    hermes-orch-agent status")
    click.echo(f"    hermes-orch-agent apply-configs --config {cfg_path}")


@cli.command()
@click.option(
    "--config",
    "config_file",
    default="~/.hermes-orchestrator/wrapper-config.json",
    help="Path to wrapper-config.json",
)
@click.option("--interval", default=5, help="Heartbeat interval seconds")
@click.option("--once", is_flag=True, help="Process one task then exit (for testing)")
@click.option(
    "--timeout",
    default=1800,
    help="Max seconds per task (hermes subprocess timeout)",
)
@click.option(
    "--no-sync",
    is_flag=True,
    help="Skip the initial sync-config call (use existing wrapper-config.json as-is)",
)
def start(
    config_file: str,
    interval: int,
    once: bool,
    timeout: int,
    no_sync: bool,
) -> None:
    """Start the wrapper daemon (heartbeat + task processing loop).

    Reads wrapper-config.json + secret file. Every N seconds:
      1. Heartbeat to orchestrator
      2. For each 'assigned' task: claim it (POST /start), then run hermes
         subprocess, then submit result (POST /result)

    Run with --once to process a single task and exit (for testing).
    """
    import subprocess
    import time as time_mod
    from hermes_orch.agent_paths import detect_hermes_profiles_dir, resolve_profile_root

    cfg_path = Path(config_file).expanduser()
    if not cfg_path.exists():
        raise click.ClickException(f"Config not found: {cfg_path}")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    agent_id = cfg.get("agent_id")
    orchestrator_url = cfg.get("orchestrator_url", "").rstrip("/")
    secret_path = Path(cfg.get("secret_file", "")).expanduser()
    profiles_cfg = cfg.get("profiles") or {}

    if not agent_id:
        raise click.ClickException("wrapper-config.json missing 'agent_id'")
    if not orchestrator_url:
        raise click.ClickException("wrapper-config.json missing 'orchestrator_url'")
    if not secret_path.exists():
        raise click.ClickException(f"Secret file not found: {secret_path}")
    if not profiles_cfg:
        click.echo("WARN: no profiles in wrapper-config.json Ã¢â‚¬â€ wrapper will idle")

    secret = secret_path.read_text(encoding="utf-8-sig").strip()
    if not secret:
        raise click.ClickException(f"Secret file {secret_path} is empty")

    # v1.6 HMAC bootstrap: on every start, push the local secret to
    # the orchestrator so it can verify our signatures. The
    # endpoint is one-shot: first call sets hmac_secret, subsequent
    # calls with the same value are no-ops, mismatched values get
    # 409 (the operator must rotate to change secrets). This means
    # the wrapper self-heals if the orchestrator's DB was wiped.
    _bootstrap_hmac_secret(orchestrator_url, agent_id, secret)

    # Auto-sync config from orchestrator (picks up newly added roles)
    if not no_sync:
        click.echo("[daemon] syncing config from orchestrator...")
        # Reuse the sync-config logic by calling it inline (it's small enough)
        try:
            r = _httpx_get(
                f"{orchestrator_url}/api/agents/{agent_id}",
                headers=_hmac_headers(
                    agent_id, secret,
                    method="GET",
                    path=f"/api/agents/{agent_id}",
                ),
                timeout=10,
            )
            if r.status_code == 200:
                agent = r.json()
                orch_roles = [p["name"] for p in agent.get("profiles", [])]
                existing = cfg.get("profiles") or {}
                added = [r for r in orch_roles if r not in existing]
                if added:
                    # Re-read config in case another process wrote it
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
                    existing = cfg.get("profiles") or {}
                    for r in orch_roles:
                        if r not in existing:
                            existing[r] = {"root": f"<profiles_dir>/{r}"}
                    cfg["profiles"] = existing
                    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
                    click.echo(f"  synced {len(added)} new role(s): {added}")
                    profiles_cfg = existing  # use updated
                else:
                    click.echo("  config up to date")
        except Exception as e:
            click.echo(f"  sync failed (continuing with existing config): {e}")

    # Resolve hermes profiles dir (for fallback path resolution)
    hermes_profiles_dir = detect_hermes_profiles_dir()
    click.echo(f"Wrapper starting:")
    click.echo(f"  agent_id: {agent_id}")
    click.echo(f"  orchestrator: {orchestrator_url}")
    click.echo(f"  profiles: {list(profiles_cfg.keys())}")
    if hermes_profiles_dir:
        click.echo(f"  hermes profiles dir: {hermes_profiles_dir}")
    click.echo(f"  interval: {interval}s")
    click.echo(f"  timeout per task: {timeout}s")
    click.echo("")

    stop_flag = {"stop": False}

    def _handle_sigint(signum, frame):
        click.echo("\n[daemon] SIGINT received, stopping after current task...")
        stop_flag["stop"] = True

    try:
        import signal
        signal.signal(signal.SIGINT, _handle_sigint)
    except (AttributeError, ValueError):
        pass  # Windows quirks

    def _auth_headers(method: str, path: str, body: bytes = b"") -> dict:
        """Sign a request with the agent's HMAC secret (v1.6+).

        The signature binds (method, path, body, timestamp) so captured
        requests can't be replayed against a different endpoint.
        """
        from hermes_orch.auth import compute_signature
        ts = str(int(time_mod.time()))
        sig = compute_signature(secret, method, path, body, ts)
        return {
            "X-Agent-Id": agent_id,
            "X-Timestamp": ts,
            "X-Signature": sig,
        }

    def _heartbeat() -> tuple[list[dict], list[str]]:
        """Heartbeat to orchestrator. Returns (tasks, cleanup_session_ids).

        cleanup_session_ids is a list of hermes session IDs the supervisor
        has aged out (status='pending_cleanup' in project_sessions). The
        wrapper runs `hermes sessions delete <id> --yes` for each and
        POSTs to /sessions/{id}/cleanup-ack to mark the row deleted.

        Per-profile metadata (LLM model + MCP servers) is read from each
        profile's config.yaml and sent in a `profiles: [...]` list. The
        orchestrator stores these in the agent_profiles table so the
        agent page can show "model: MiniMax-M3 (minimax-oauth)" badges
        and the collapsible MCP server list. Reading every cycle (5s)
        is cheap (~few KB YAML files) and ensures the dashboard stays
        in sync with the live config (e.g. user adds a new MCP server,
        it shows up within 5s without a wrapper restart).

        Also pulls per-profile `storage_refs` from the response
        (user-stated 2026-07-22): the orchestrator returns
        `storage_refs_by_profile: {profile_name: [{kind, ref, description}, ...]}`.
        The wrapper caches this in a module-level dict and the
        `_run_task` function injects an [AVAILABLE STORAGE] block into
        the task prompt so the agent knows where to write large
        outputs (bypassing the 15MB per-file orch cap).

        v3.6.0: also caches the per-agent `max_concurrent_tasks` cap
        in module-level `_max_concurrent_tasks_cache`. The daemon
        loop sizes its ThreadPoolExecutor from this value; size changes
        take effect on the next tick (the pool is rebuilt per tick,
        so we don't have to track in-flight tasks and resize the pool
        while workers are running).
        """
        profile_meta: list[dict] = []
        for role, pcfg in (profiles_cfg or {}).items():
            try:
                root = resolve_profile_root(
                    pcfg.get("root", ""),
                    role,
                    profiles_dir=hermes_profiles_dir,
                )
            except Exception:
                continue
            meta = _read_profile_config(root)
            profile_meta.append({
                "name": role,
                "model_default": meta["model_default"],
                "model_base_url": meta["model_base_url"],
                "model_provider": meta["model_provider"],
                "mcp_servers": meta["mcp_servers"],
            })
        try:
            _heartbeat_body = json.dumps({
                "status": "idle",
                "profiles": profile_meta,
            }).encode("utf-8")
            r = _httpx_post(
                f"{orchestrator_url}/api/agents/{agent_id}/heartbeat",
                headers=_auth_headers(
                    "POST",
                    f"/api/agents/{agent_id}/heartbeat",
                    _heartbeat_body,
                ),
                content=_heartbeat_body,
                timeout=10,
            )
            if r.status_code != 200:
                click.echo(f"[daemon] heartbeat {r.status_code}: {r.text[:200]}")
                return [], []
            body = r.json() or {}
            # Cache storage_refs keyed by profile name. _run_task
            # reads from this dict when building the prompt.
            srefs = body.get("storage_refs_by_profile") or {}
            if isinstance(srefs, dict):
                _storage_refs_cache.clear()
                for pname, refs in srefs.items():
                    if isinstance(refs, list):
                        _storage_refs_cache[str(pname)] = refs
            # v3.6.0: cache the per-agent concurrent task cap. Defensive:
            # clamp to 1..32 here too (server validates but a malformed
            # body from a buggy custom client should still not crash the
            # pool with max_workers=0 or -1). Default 1 = backward compat.
            # We use `global` because the cache is a module-level
            # variable (not a closure variable; the daemon loop is a
            # top-level function, not nested).
            global _max_concurrent_tasks_cache
            try:
                cap = int(body.get("max_concurrent_tasks") or 1)
            except (TypeError, ValueError):
                cap = 1
            cap = max(1, min(32, cap))
            _max_concurrent_tasks_cache = cap
            return body.get("tasks", []), body.get("cleanup_session_ids", []) or []
        except httpx.RequestError as e:
            click.echo(f"[daemon] heartbeat failed: {e}")
            return [], []

    def _cleanup_local_sessions(session_ids: list[str]) -> None:
        """Run `hermes sessions delete <id> --yes` for each session ID,
        then POST /cleanup-ack to the orchestrator so the DB row flips
        from pending_cleanup to deleted. Best-effort: a hermes delete
        failure (e.g. session already gone) is non-fatal Ã¢â‚¬â€ we still
        ack so the DB doesn't get stuck in pending_cleanup forever.
        """
        if not session_ids:
            return
        # Locate the hermes CLI. On Linux it's typically at
        # ~/.local/bin/hermes. On Windows it's usually at
        # %LOCALAPPDATA%\hermes\hermes-agent\hermes.exe. We try a few
        # common locations; if all fail, fall back to "hermes" and let
        # the OS resolve via PATH.
        hermes_bin: str | None = None
        candidates: list[Path] = [
            Path.home() / ".local" / "bin" / "hermes",
        ]
        # Add Windows-specific candidates
        if sys.platform == "win32":
            local_app = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            candidates += [
                Path(local_app) / "hermes" / "hermes-agent" / "hermes.exe",
                Path(local_app) / "hermes" / "hermes-agent" / "venv" / "Scripts" / "hermes.exe",
                Path(local_app) / "Programs" / "hermes" / "hermes.exe",
            ]
        else:
            candidates += [
                Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes",
                Path.home() / ".hermes" / "hermes-agent" / "hermes",
            ]
        for cand in candidates:
            if cand.exists() and os.access(str(cand), os.X_OK):
                hermes_bin = str(cand)
                break
        if not hermes_bin:
            hermes_bin = "hermes"
        for sid in session_ids:
            try:
                proc = subprocess.run(
                    [hermes_bin, "sessions", "delete", sid, "--yes"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode == 0:
                    click.echo(f"[daemon] deleted hermes session: {sid}")
                else:
                    # "Session not found" / "no such session" is fine Ã¢â‚¬â€
                    # the session is already gone, which is what we wanted.
                    stderr_low = (proc.stderr or "").lower()
                    if "not found" in stderr_low or "no such" in stderr_low:
                        click.echo(f"[daemon] hermes session already gone: {sid}")
                    else:
                        click.echo(
                            f"[daemon] hermes sessions delete {sid} failed "
                            f"(rc={proc.returncode}): {(proc.stderr or '').strip()[:200]}"
                        )
            except Exception as e:
                click.echo(f"[daemon] hermes sessions delete {sid} error: {e}")
            # Always ack -- even on failure, we don't want the row stuck
            # in pending_cleanup. The audit log retains the failure
            # context via the stdout/stderr we just printed.
            try:
                # v1.9 fix: path was a literal `{}` template (not f-string)
                # which produced a signature bound to the WRONG path --
                # the server would 401 every cleanup-ack. Caught when
                # flipping HERMES_HMAC_REQUIRED=true exposed the
                # hmac_auth_failed events at full volume. Now the path
                # matches the actual URL.
                _ck_path = f"/api/agents/{agent_id}/sessions/{sid}/cleanup-ack"
                _httpx_post(
                    f"{orchestrator_url}{_ck_path}",
                    headers=_auth_headers('POST', _ck_path),
                    timeout=10,
                )
            except Exception as e:
                click.echo(f"[daemon] cleanup-ack failed for {sid}: {e}")

    def _claim(task_id: str) -> bool:
        """Atomically flip task from 'assigned' to 'running'."""
        try:
            # v1.9.1 fix: same class of bug as cleanup-ack (v1.9).
            # The path arg must be the f-string-interpolated actual
            # path, not the literal `{}` template. Server's
            # require_hmac_auth computes the signature over
            # request.url.path; if the wrapper signs a different
            # string, every call 401s.
            _start_path = f"/api/tasks/{task_id}/start"
            r = _httpx_post(
                f"{orchestrator_url}{_start_path}",
                headers=_auth_headers('POST', _start_path),
                timeout=10,
            )
            if r.status_code != 200:
                click.echo(f"[daemon] claim {task_id} failed: {r.status_code} {r.text[:200]}")
                return False
            return True
        except httpx.RequestError as e:
            click.echo(f"[daemon] claim {task_id} network error: {e}")
            return False

    def _submit_result(task_id: str, result: dict) -> bool:
        try:
            # v1.9.3 fix: TWO bugs in this one call. v1.9.1 fixed the
            # path-template bug (extract f-string into _result_path
            # local). This is the deeper bug:
            #
            #   (a) Body-signing bug: the wrapper was using
            #       `json=result` (httpx encodes the dict) but
            #       signing the request with `body=b""` (the
            #       default for _auth_headers). Server's
            #       require_hmac_auth hashes the actual request
            #       body, gets a different SHA256, and 401s.
            #
            #   (b) Content-Type bug: when we switched to
            #       `content=_result_body` (the fix for (a)),
            #       httpx does NOT auto-set Content-Type to
            #       application/json. Without it, the server's
            #       pydantic TaskResult model gets the body as
            #       a string and fails validation with 422
            #       "Input should be a valid dictionary or
            #       object to extract fields from".
            #
            # Pre-fix symptoms: hermes ran fine, the wrapper did
            # all the post-processing (upload artifacts, capture
            # tokens), then `_submit_result` got 401 (a) Ã¢â€ â€™ task
            # stayed in 'running' for 3 minutes Ã¢â€ â€™ supervisor's
            # stuck_wrapper check finally marked it failed. With
            # the v1.9.1 path fix applied, the 401 went away but
            # the 422 surfaced (b). User saw "task timeout failed"
            # but the root cause was a chain of signing + content-
            # type bugs, not a hermes hang.
            #
            # Fix: serialize the body once, sign it, send it as
            # `content=`, and set Content-Type explicitly. Same
            # pattern as output-chunk (line ~2203) and tool-call
            # (line ~2270). The dict is now round-tripped through
            # the same bytes both sides.
            #
            # Regression test: tests/test_submit_result_body.py
            # (added in v1.9.3).
            _result_path = f"/api/tasks/{task_id}/result"
            _result_body = json.dumps(result).encode("utf-8")
            _auth_h = _auth_headers('POST', _result_path, _result_body)
            _auth_h["Content-Type"] = "application/json"
            r = _httpx_post(
                f"{orchestrator_url}{_result_path}",
                headers=_auth_h,
                content=_result_body,
                timeout=10,
            )
            if r.status_code != 200:
                click.echo(f"[daemon] submit result {task_id} failed: {r.status_code} {r.text[:200]}")
                return False
            return True
        except httpx.RequestError as e:
            click.echo(f"[daemon] submit result {task_id} network error: {e}")
            return False

    def _run_task(task: dict) -> dict:
        """Run hermes subprocess for a task. Returns result dict."""
        tid = task["id"]
        role = task.get("agent_role", "")
        action = task.get("action", "")
        params = task.get("params") or {}
        # Mark the start time of THIS task. The auto-upload loop at the
        # end of this function uses it to skip files that pre-date the
        # current task (cached files from previous tasks in the same
        # project). Without this filter, every task re-uploads every
        # file in the project cache, causing duplicate artifact rows
        # and audit_log events. (Bug fixed 2026-07-22: user reported
        # NÃƒâ€”duplicate artifact.registered events for the same file.)
        import time as _time_mod_for_start
        _task_start_ts = _time_mod_for_start.time()
        profile_cfg = profiles_cfg.get(role)
        if not profile_cfg:
            return {
                "status": "failed",
                "error": f"role {role!r} not in wrapper-config.json profiles",
            }
        # Resolve profile root (with ~ + env + auto-detect fallback)
        try:
            profile_root = resolve_profile_root(
                profile_cfg["root"],
                role,
                profiles_dir=hermes_profiles_dir,
            )
        except FileNotFoundError as e:
            return {"status": "failed", "error": str(e)}

        # Hermes CLI flags. Wrapper is autonomous (no human in the loop), so
        # default to bypassing approval prompts. Per-task override via
        # params.yolo (bool) and params.accept_hooks (bool). Profile-level
        # override via profile_cfg["yolo"] / ["accept_hooks"].
        # Top-level default: from wrapper-config.json hermes_options, or True.
        hermes_options = cfg.get("hermes_options") or {}
        profile_yolo = profile_cfg.get("yolo", hermes_options.get("yolo", True))
        profile_accept = profile_cfg.get("accept_hooks", hermes_options.get("accept_hooks", True))
        yolo = bool(params.get("yolo", profile_yolo))
        accept_hooks = bool(params.get("accept_hooks", profile_accept))

        if not profile_root.exists():
            return {
                "status": "failed",
                "error": f"profile root does not exist: {profile_root}",
            }

        # Build prompt for hermes.
        #
        # Cross-host file flow (File API):
        # - Wrapper creates a LOCAL cache dir (under the profile) for this project
        # - Downloads any parent task output files into the cache
        # - Runs hermes with cwd=cache (agent uses normal file tools)
        # - Uploads the output file to the orchestrator via PUT file API
        # - Removes the cache when done
        # This way, the agent never touches the orchestrator's filesystem
        # directly, and cross-host works without shared filesystems.
        project_id = task.get("project_id")
        output_path = task.get("output_path")
        depends_on = task.get("depends_on") or []
        parent_outputs = task.get("parent_outputs") or {}  # injected by heartbeat

        # Local cache: <profile_root>/.orch-cache/<project_id>/
        cache_dir = None
        if project_id:
            cache_dir = Path(profile_root) / ".orch-cache" / project_id
            cache_dir.mkdir(parents=True, exist_ok=True)

        # Download parent outputs into cache
        for parent_id, parent_output in parent_outputs.items():
            if not parent_output:
                continue
            try:
                rel = parent_output.lstrip("/").replace("\\", "/")
                _file_path_p = f"/api/projects/{project_id}/files/{rel}"
                r = _httpx_get(
                    f"{orchestrator_url}{_file_path_p}",
                    headers=_hmac_headers(
                        agent_id, secret,
                        method="GET", path=_file_path_p,
                    ),
                    timeout=30,
                )
                if r.status_code == 200:
                    local_path = cache_dir / rel
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    local_path.write_bytes(r.content)
                    click.echo(f"  cached: {rel} ({len(r.content)} bytes)")
                else:
                    click.echo(f"  WARN: parent {parent_id} output {parent_output} -> HTTP {r.status_code}")
            except Exception as e:
                click.echo(f"  WARN: failed to download {parent_output}: {e}")

        # Build context block for the prompt
        context_lines = []
        if cache_dir:
            context_lines.append(f"LOCAL WORKING DIR: {cache_dir}")
            context_lines.append(
                f"  (use file tools to read/write files here. Other tasks "
                f"in this project share this folder via the orchestrator.)"
            )
        if output_path:
            output_local = cache_dir / output_path if cache_dir else None
            context_lines.append(f"YOUR OUTPUT FILE: {output_local}")
            context_lines.append(
                f"  (when done, write your result to this file. "
                f"Other tasks will read it from the orchestrator.)"
            )
        # Path A (#22): if the supervisor denormalized the project's
        # procedure.md into this task's `procedure_md` column at assignment
        # time, include it in the prompt as the first thing the agent sees.
        # This is the n8n-style "how to do this workflow" doc Ã¢â‚¬â€ the agent
        # reads it BEFORE the LLM-driven action call so it follows the
        # project-specific procedure rather than improvising. The denormalized
        # copy lives in the tasks table (set by Supervisor._assign_task),
        # so the wrapper doesn't need a separate file fetch.
        procedure_text = task.get("procedure_md") or ""
        if procedure_text and procedure_text.strip():
            context_lines.append("--- WORKFLOW PROCEDURE (read this first) ---")
            context_lines.append(procedure_text)
            context_lines.append("--- END WORKFLOW PROCEDURE ---")
        # Optional self-teach hint. The agent can drop a markdown file at
        # `../skills/<name>.md` (relative to the cache dir, which is
        # `<profile>/.orch-cache/<project_id>/`, so `../skills/` lands
        # inside the profile's skills/ dir). The wrapper's periodic
        # reverse-sync picks it up within 30s and pushes it to the
        # orchestrator with `X-Skill-Source: self-taught`. The hint is
        # intentionally permissive ("may") so the agent doesn't try to
        # teach every task; it should reserve self-teaching for genuinely
        # reusable knowledge (an API pattern, a tool integration that
        # worked well, etc.) that isn't already covered by an existing
        # skill in its profile.
        if cache_dir:
            context_lines.append(
                f"SKILL SELF-TEACHING (optional): If during this task you "
                f"discover something genuinely reusable for future tasks "
                f"(an API pattern, a workflow, a tool integration that "
                f"worked well), you may write it as a markdown file under "
                f"this profile's `skills/` directory. ABSOLUTE PATH: "
                f"`{profile_root}\\skills\\<name>.md` (Windows) or "
                f"`{profile_root}/skills/<name>.md` (Unix). The orchestrator "
                f"will auto-register it as a skill within ~30 seconds. Only "
                f"do this when the knowledge is reusable across future tasks "
                f"AND not already covered by an existing skill in your "
                f"profile. Skip for one-off task-specific notes."
            )
        # Inject parent task summaries (existing behaviour)
        if depends_on:
            for parent_id in depends_on:
                try:
                    r = _httpx_get(
                        f"{orchestrator_url}/api/tasks/{parent_id}",
                        headers=_hmac_headers(
                            agent_id, secret,
                            method="GET",
                            path=f"/api/tasks/{parent_id}",
                        ),
                        timeout=10,
                    )
                    if r.status_code == 200:
                        parent = r.json()
                        par_status = parent.get("status", "?")
                        par_name = parent.get("name") or parent.get("action") or "?"
                        par_output = parent.get("output_path") or "-"
                        par_summary = ((parent.get("result") or {}).get("summary") or "")[:200]
                        context_lines.append(
                            f"PREVIOUS TASK {parent_id} ({par_name}): status={par_status}, output={par_output}"
                        )
                        if par_summary:
                            context_lines.append(f"  summary: {par_summary}")
                except Exception as e:
                    context_lines.append(f"PREVIOUS TASK {parent_id}: (could not fetch: {e})")
        context_block = "\n".join(context_lines)

        # Phase 1 of 3-tier memory (docs/design/3-tier-memory.md): inject
        # the project's L2 (facts.md) tail into the prompt so the new
        # task knows what prior work already exists. Read is best-effort;
        # if facts.md is missing or unreadable, fall through to the
        # normal prompt.
        #
        # IMPORTANT: read via HTTP API, NOT via MemoryWriter disk read.
        # The wrapper runs on a different machine (e.g. linux-a-01) than
        # the orchestrator (Windows), so its local filesystem doesn't have
        # the projects_root path. MemoryWriter on the wrapper would fall
        # back to a default path that doesn't exist, silently injecting
        # nothing. Going through the orchestrator's HTTP API works
        # regardless of where the wrapper is running.
        try:
            # Phase 3: also inject user-level L3 (recent.md) ABOVE all
            # project-level context. recent.md is the cross-project 7-day
            # summary so the agent knows what the user has been up to.
            # Falls through silently if missing (first project ever).
            recent_text = _fetch_user_recent_http(
                orchestrator_url, agent_id, secret
            )
            if recent_text:
                context_block = (
                    "--- USER RECENT (L3: recent.md, last 7 days) ---\n"
                    + recent_text
                    + "\n--- END USER RECENT ---\n\n"
                    + context_block
                )
            # Phase 2: also inject L3 (state.md) ABOVE L2. L3 is the
            # LLM-synthesized high-level view; L2 is the cite-able
            # raw facts. Order matters: the agent sees the synthesis
            # first (faster orientation), then the supporting evidence.
            state_text = _fetch_project_state_http(
                orchestrator_url, agent_id, secret, project_id
            )
            if state_text:
                context_block = (
                    "--- PROJECT STATE (L3: state.md) ---\n"
                    + state_text
                    + "\n--- END PROJECT STATE ---\n\n"
                    + context_block
                )
            facts_text = _fetch_project_facts_http(
                orchestrator_url, agent_id, secret, project_id
            )
            if facts_text:
                context_block += "\n\n--- PROJECT MEMORY (L2: facts.md) ---\n" + facts_text + "\n--- END PROJECT MEMORY ---"
        except Exception as e:
            click.echo(f"[daemon] failed to load project memory: {e}")

        # Build prompt for hermes. Prepend the [OUTPUT FORMAT] block
        # (always Ã¢â‚¬â€ house style: .md for output, .json only for params,
        # no .results.json) and the [AVAILABLE STORAGE] block (if any,
        # so the agent knows where to put large outputs BEFORE it
        # starts thinking about how to structure the work). Without
        # this, the LLM might default to "write to cache_dir" and hit
        # the 15MB upload cap, then try to chunk or base64 the data
        # through the orch Ã¢â‚¬â€ neither is what we want.
        params_str = json.dumps(params, ensure_ascii=False) if params else "{}"
        output_format_block = _render_output_format_block()
        storage_block = _render_storage_block(role)
        # If the task declared `params.output_to` (alias or full ref),
        # prepend a [STORAGE HINT] block that resolves the alias to its
        # specific entry. This sits right before [AVAILABLE STORAGE] so
        # the agent sees the target first, then the full list as context
        # for the case where output_to points to an unknown name.
        storage_hint_block = _resolve_storage_hint(role, params.get("output_to") or "")
        # Stage 1.5 (2026-07-23): if the task was created from a workflow
        # step with a `skill` reference, the run endpoint put the skill
        # name in params as `_workflow_skill`. Read it here and inject
        # the skill body as a [SKILL: <name>] block. Sits right after
        # STORAGE blocks so the agent has the data-source procedure
        # before it starts planning the task.
        workflow_skill_name = (params or {}).get("_workflow_skill")
        workflow_skill_block = ""
        if workflow_skill_name:
            workflow_skill_block = _render_workflow_skill_block(
                profile_root, workflow_skill_name, params
            )
        prefix_parts = [output_format_block]
        if storage_hint_block:
            prefix_parts.append(storage_hint_block)
        if storage_block:
            prefix_parts.append(storage_block)
        if workflow_skill_block:
            prefix_parts.append(workflow_skill_block)
        if context_block:
            prefix_parts.append(
                f"--- PROJECT CONTEXT ---\n{context_block}\n--- END CONTEXT ---"
            )
        if prefix_parts:
            prompt = (
                f"{action}({params_str})\n\n"
                + "\n\n".join(prefix_parts)
            )
        else:
            prompt = f"{action}({params_str})"
        # Defensive: even with the UTF-8 reconfigure at module top,
        # click.echo writing to a redirected stream can still fail on
        # odd terminals. We don't want a logging failure to abort the
        # task (the hermes subprocess hasn't even been spawned yet).
        # errors='replace' substitutes ? for unencodable chars.
        try:
            click.echo(f"[daemon] task {tid}: running")
            click.echo(f"  role={role}  profile_root={profile_root}")
            click.echo(f"  cache_dir={cache_dir}")
            click.echo(f"  output_path={output_path}")
            click.echo(f"  prompt={prompt[:200]}")
        except UnicodeEncodeError:
            try:
                # Last-ditch: encode manually with errors='replace' so we
                # never lose a task to a logging issue.
                safe_prompt = prompt[:200].encode("ascii", "replace").decode("ascii")
                click.echo(f"[daemon] task {tid}: running (ascii-safe log)")
                click.echo(f"  prompt={safe_prompt}")
            except Exception:
                click.echo(f"[daemon] task {tid}: running (log print suppressed due to encoding error)")

        # Resolve hermes binary (systemd doesn't load .bashrc, and Windows
        # services run as LocalSystem with a different HOME/PATH. Detection
        # covers common install locations; HERMES_BIN env var overrides all.)
        hermes_bin = _resolve_hermes_bin()
        if not hermes_bin:
            sysname = platform.system().lower()
            if sysname.startswith("win"):
                searched = (
                    "$HERMES_BIN, PATH, %LOCALAPPDATA%\\hermes\\hermes-agent\\venv\\Scripts\\hermes.exe, "
                    "%LOCALAPPDATA%\\hermes\\bin\\hermes.exe, %APPDATA%\\Python\\Scripts\\hermes.exe"
                )
            else:
                searched = (
                    "$HERMES_BIN, PATH, ~/.local/bin/hermes, ~/.local/share/hermes/bin/hermes, "
                    "~/bin/hermes, /usr/local/bin/hermes, /usr/bin/hermes"
                )
            return {
                "status": "failed",
                "error": (
                    f"hermes CLI not found. Searched: {searched}. "
                    f"Install hermes-agent or set HERMES_BIN env var to its absolute path."
                ),
            }

        # Build hermes CLI args. By default we add --yolo (autonomous mode:
        # bypass approval prompts since no human is in the subprocess loop)
        # and --accept-hooks (auto-approve shell hooks). Both can be disabled
        # per-task via params.yolo=false / params.accept_hooks=false.
        hermes_args = [hermes_bin, "-p", role, "chat", "-q", prompt]
        # v3.12.1 follow-up #6: apply the per-task conversation-history
        # window to the hermes config BEFORE invoking hermes. We
        # write `compression.protect_last_n` (and bump `threshold`
        # down to 0.30 to trigger compaction earlier than the
        # default 0.50) into the per-profile config.yaml. Hermes
        # 0.19.1's micro-compaction then keeps the last N turns
        # uncompressed, capping the ~4x prompt growth we measured
        # in commit 20fb097. The per-task value comes from
        # `task.params["_max_history_turns"]` (server-resolved
        # across the 4-tier priority chain in
        # `soul_dispatch._create_dispatched_task`); we fall back
        # to `_max_history_turns_cache` (the server default
        # polled each tick) for older wrappers / pre-Phase-A
        # tasks that don't carry the per-task key.
        _task_mht = None
        if params.get("_max_history_turns") is not None:
            try:
                _task_mht = int(params["_max_history_turns"])
            except (TypeError, ValueError):
                _task_mht = None
        if _task_mht is None:
            _task_mht = _max_history_turns_cache
        # v3.12.4 instrument: race vs c3e998d atomic-write (diag only).
        # Logs the exact microsecond of config-write start/end + hermes
        # start/exit. Off after diagnosis — see v3.12.4 chat.
        import time as _race_t
        _race_t0 = _race_t.time()
        click.echo(f"[race-diag {tid}] t={_race_t0:.6f} _apply_hermes_compaction_config START (role={role}, profile_root={profile_root})")
        _compaction_rc = _apply_hermes_compaction_config(profile_root, _task_mht)
        _race_t1 = _race_t.time()
        click.echo(f"[race-diag {tid}] t={_race_t1:.6f} _apply_hermes_compaction_config DONE (rc={_compaction_rc}, dur={(_race_t1-_race_t0)*1000:.1f}ms)")
        if yolo:
            hermes_args.append("--yolo")
        if accept_hooks:
            hermes_args.append("--accept-hooks")

        # Session resume: query for THIS role's session. Hermes session
        # namespaces are per-profile, so a session created by profile X
        # cannot be resumed by profile Y (hermes returns "Session not
        # found" and the agent echoes the action without doing real
        # work). The orchestrator's per-role map (`current_sessions_json`)
        # ensures each wrapper only ever tries to resume a session that
        # its own profile created.
        if project_id and role:
            try:
                r = _httpx_get(
                    f"{orchestrator_url}/api/projects/{project_id}/session",
                    params={"role": role},
                    headers=_hmac_headers(
                        agent_id, secret,
                        method="GET",
                        path=f"/api/projects/{project_id}/session?role={role}",
                    ),
                    timeout=10,
                )
                if r.status_code == 200:
                    sid = (r.json() or {}).get("current_session_id")
                    if sid:
                        hermes_args += ["--resume", sid]
                        click.echo(f"  resuming session: {sid} (role={role})")
                    else:
                        click.echo(
                            f"  no prior session for role={role}, starting fresh"
                        )
            except Exception as e:
                click.echo(f"  WARN: session lookup failed: {e}")

        # Run hermes with stdout/stderr redirected to FILES, NOT PIPE.
        #
        # Why files (not PIPE):
        #   subprocess.PIPE has a 64KB kernel buffer. When the LLM
        #   streams ~9K+ output tokens (typical for a multi-tool
        #   task like a Google Drive query), the buffer fills and
        #   hermes's stdout write() BLOCKS. Meanwhile the wrapper
        #   is in `proc.communicate()` waiting for hermes to exit
        #   before reading Ã¢â‚¬â€ classic deadlock. The 1800s timeout
        #   kills hermes and we get `error: hermes timeout after
        #   1800s` even though the agent was making real progress.
        #
        # With file redirection, the OS handles the buffering (write
        # goes to disk, hermes never blocks on output). We get the
        # full transcript on disk for review. The liveness poller
        # (separate thread) keeps the server informed of progress
        # regardless of stdout size.
        try:
            # Run hermes in the local cache dir so file tools work there
            # (the agent sees a real filesystem; we sync via API).
            hermes_cwd = str(cache_dir) if cache_dir else str(profile_root)
            # Per-task transcript files. Persisted on disk so the user
            # can review what the agent actually said/did after a
            # timeout (the prior PIPE approach lost all output on
            # deadlock). Rotation by tid keeps names short + unique.
            stdout_log = Path(hermes_cwd) / f"hermes.{tid}.stdout.log"
            stderr_log = Path(hermes_cwd) / f"hermes.{tid}.stderr.log"
            stdout_fh = open(stdout_log, "wb")
            stderr_fh = open(stderr_log, "wb")
            try:
                # v3.12.7 (2026-08-06): set HOME / USERPROFILE /
                # HERMES_HOME in os.environ so the child hermes process
                # inherits them. The wrapper often runs under Task
                # Scheduler / systemd in a non-interactive session
                # with HOME unset. hermes CLI calls Path.home() early;
                # if it returns the system service home (no profiles
                # there), every `hermes -p <profile>` errors with
                # "Profile <x> does not exist" even when the profile
                # IS registered in the orchestrator DB. Defense:
                # fall back to the user the wrapper was configured
                # for (read from WRAPPER_TARGET_USER env, or the
                # hard-coded stanley default). We do this in a
                # separate block BEFORE the Popen call (an if
                # statement cannot appear in the middle of a function
                # call's argument list).
                _wh = str(Path.home())
                if not _wh.startswith(("C:\\Users\\", "/home/", "/Users/")):
                    # Path.home() returned a system service path
                    # (e.g. C:\WINDOWS\system32\config\systemprofile).
                    # Override with the configured user home. The raw
                    # string uses single backslashes (r"\") to produce
                    # an actual Windows path on disk.
                    _wh = r"C:\Users\stanley"
                # Also override LOCALAPPDATA if it's missing or points
                # at the system service profile (same root cause).
                _lad = os.environ.get("LOCALAPPDATA", "")
                if not _lad or _lad.startswith(("C:\\WINDOWS\\", "C:\\Windows\\", "/var/")):
                    _lad = _wh + r"\AppData\Local"
                os.environ["HOME"] = _wh
                os.environ["USERPROFILE"] = _wh
                os.environ["LOCALAPPDATA"] = _lad
                os.environ["HERMES_HOME"] = str(Path(_lad) / "hermes")
                proc = subprocess.Popen(
                    hermes_args,
                    cwd=hermes_cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_fh,
                    stderr=stderr_fh,
                    # env=...: force UTF-8 output from hermes even on Windows
                    env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                )
                # v3.12.4 instrument: race vs c3e998d atomic-write (diag only).
                import time as _race_t
                click.echo(f"[race-diag {tid}] t={_race_t.time():.6f} Popen DONE (pid={proc.pid}, role={role}, cwd={hermes_cwd})")
            except BaseException:
                # If Popen fails, close the file handles we opened
                stdout_fh.close()
                stderr_fh.close()
                raise
        except FileNotFoundError:
            return {
                "status": "failed",
                "error": f"hermes CLI not found at {hermes_bin}. Check installation.",
            }
        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

        # Keep task liveness alive while hermes runs. Without this, the
        # orchestrator's stuck-task detector (180s threshold) marks the
        # task failed even though the wrapper is still actively working.
        # The detector only looks at the task's `last_liveness_at`, not
        # the agent's `last_heartbeat_at`, so a long hermes subprocess
        # (e.g. fetch_weather with multiple sources, or a long tool
        # sequence) trips the detector. Poll /api/tasks/{id}/poll every
        # 30s in a background thread; the orchestrator updates
        # last_liveness_at on each call. If poll ever returns
        # status != "running" (e.g. the user cancelled), kill hermes
        # so the wrapper stops wasting tokens on a dead task.
        import threading
        stop_poll = threading.Event()
        def _poll_liveness() -> None:
            while not stop_poll.is_set():
                try:
                    r = _httpx_post(
                        f"{orchestrator_url}/api/tasks/{tid}/poll",
                        headers=_hmac_headers(
                            agent_id, secret,
                            method="POST",
                            path=f"/api/tasks/{tid}/poll",
                        ),
                        timeout=5,
                    )
                    if r.status_code == 200:
                        body = r.json() or {}
                        if body.get("status") != "running":
                            click.echo(
                                f"  task {tid} no longer running "
                                f"(status={body.get('status')}); killing hermes"
                            )
                            try:
                                proc.kill()
                            except Exception:
                                pass
                            return
                except Exception as e:
                    click.echo(f"  WARN: task poll failed: {e}")
                # 30s sleep, but check stop flag every second so we exit
                # quickly when the subprocess finishes
                for _ in range(30):
                    if stop_poll.is_set():
                        return
                    time_mod.sleep(1)
        poll_thread = threading.Thread(target=_poll_liveness, daemon=True)
        poll_thread.start()

        # ===== Live output streaming (v1.1, 2026-07-29) =====
        # Tail hermes's per-task stdout/stderr files in background
        # threads and POST each chunk to the orchestrator so the
        # dashboard can render the agent's progress in real time.
        #
        # Throttle (matches the Task Progress Monitor v1.1 design):
        #   - 2s timer (so idle output still flushes promptly)
        #   - OR 8KB buffer (so burst output batches up)
        # whichever comes first. Without this, a slow LLM streaming
        # token-by-token would spam one POST per token.
        #
        # Each stream (stdout / stderr) gets its own seq counter
        # starting at 1. The frontend de-dupes by (stream, seq) and
        # renders stderr in a collapsed section (per design).
        stop_stream = threading.Event()
        # Per-stream seq counters (stdout / stderr). Restart at 1
        # for each task so a new task starts fresh.
        _seq = {"stdout": 0, "stderr": 0}
        STREAM_THROTTLE_S = 2
        STREAM_BUF_MAX = 8192

        def _post_chunk(text: str, stream: str) -> None:
            if not text:
                return
            _seq[stream] += 1
            try:
                _output_body = json.dumps({
                    "seq": _seq[stream],
                    "text": text,
                    "stream": stream,
                }).encode("utf-8")
                _httpx_post(
                    f"{orchestrator_url}/api/projects/{project_id}"
                    f"/tasks/{tid}/output-chunk",
                    headers=_auth_headers(
                        "POST",
                        f"/api/projects/{project_id}/tasks/{tid}/output-chunk",
                        _output_body,
                    ),
                    content=_output_body,
                    timeout=5,
                )
            except Exception as e:
                # Don't let a stream POST failure break the main loop.
                # The dashboard will see a gap; the agent keeps running.
                # We log so operators can debug network issues.
                click.echo(f"  WARN: output-chunk POST failed: {e}")

        def _tail_stream(path: Path, stream: str) -> None:
            """Tail `path` in a loop. Buffers bytes; flushes when the
            buffer hits STREAM_BUF_MAX OR STREAM_THROTTLE_S seconds
            have elapsed since the last flush. Always does a final
            flush on exit (so we don't lose the tail of the transcript
            when hermes exits)."""
            def _flush(text: str) -> None:
                _post_chunk(text, stream)
            _stream_throttle_loop(
                path,
                should_stop=lambda: stop_stream.is_set(),
                flush=_flush,
                throttle_s=STREAM_THROTTLE_S,
                buf_max=STREAM_BUF_MAX,
                sleep_fn=lambda s: stop_stream.wait(s),
            )

        stream_threads = []
        for _path, _stream in (
            (stdout_log, "stdout"),
            (stderr_log, "stderr"),
        ):
            t = threading.Thread(
                target=_tail_stream,
                args=(_path, _stream),
                daemon=True,
            )
            t.start()
            stream_threads.append(t)

        # ===== Looping detection (v1.2, v1.7) =====
        # Watch the stdout file for tool-call rows and POST one
        # event per call to the orchestrator. The server's
        # compute_loop_status() then queries these events to flag
        # the task as "looping" if the same (tool, signature)
        # pair fires >= per-tool threshold times in LOOP_WINDOW_S.
        #
        # v1.7 (2026-07-29): expanded from shell-only to cover
        # every tool hermes emits. The pattern list is in
        # hermes_orch.core.tool_call_patterns (sourced from
        # agent/display.py:_get_cute_tool_message in the hermes
        # repo). First match wins. The signature is the args
        # portion with trailing duration stripped, SHA1'd.
        #
        # Per-tool thresholds (server-side) are tuned to natural
        # call rates: shell=5, read=15 (reading 20 files is normal),
        # memory=12 (memory writes are normal), etc. See
        # docs/loop-detection-v1.7.md.
        import hashlib as _hashlib
        from hermes_orch.core.tool_call_patterns import (
            TOOL_PATTERNS as _TOOL_PATTERNS,
            strip_duration_suffix as _strip_duration,
        )
        def _post_tool_call(tool: str, signature: str) -> None:
            try:
                _tool_body = json.dumps(
                    {"tool": tool, "signature": signature}
                ).encode("utf-8")
                _httpx_post(
                    f"{orchestrator_url}/api/projects/{project_id}"
                    f"/tasks/{tid}/tool-call",
                    headers=_auth_headers(
                        "POST",
                        f"/api/projects/{project_id}/tasks/{tid}/tool-call",
                        _tool_body,
                    ),
                    content=_tool_body,
                    timeout=5,
                )
            except Exception as e:
                # Same posture as _post_chunk: best-effort, never
                # crash the main loop on a stream error.
                click.echo(f"  WARN: tool-call POST failed: {e}")
        def _tail_tool_calls() -> None:
            pos = 0
            while not stop_stream.is_set():
                try:
                    with open(stdout_log, "rb") as f:
                        f.seek(pos)
                        new = f.read()
                    if new:
                        pos += len(new)
                        text = new.decode("utf-8", errors="replace")
                        for line in text.splitlines():
                            # First matching tool pattern wins.
                            for pat in _TOOL_PATTERNS:
                                m = pat.regex.search(line)
                                if m:
                                    raw = m.group(pat.sig_group)
                                    body = _strip_duration(raw)
                                    if not body:
                                        # Matched a tool but args is empty
                                        # (e.g. `Ã¢â€Å  Ã°Å¸â€™Â» $  done`). Skip.
                                        break
                                    sig = _hashlib.sha1(
                                        body.encode("utf-8")
                                    ).hexdigest()[:16]
                                    _post_tool_call(pat.tool, sig)
                                    break  # one event per line
                except FileNotFoundError:
                    pass
                stop_stream.wait(0.5)
        tool_call_thread = threading.Thread(
            target=_tail_tool_calls, daemon=True
        )
        tool_call_thread.start()
        stream_threads.append(tool_call_thread)
        try:
            try:
                # Per-task timeout (v1.9.3, 2026-07-30):
                # Use task.timeout_seconds (per-task, set by supervisor
                # or task author) capped at MAX_TASK_TIMEOUT_SEC (30 min
                # hard cap) as a defense-in-depth ceiling. The wrapper's
                # CLI --timeout is now only the FALLBACK when
                # task.timeout_seconds is missing/zero.
                #
                # Pre-v1.9.3: the wrapper used the CLI --timeout value
                # (default 1800) for every task regardless of what the
                # task declared. A supervisor-set 1200s task would still
                # get 1800s before being killed. Worse: a 1ms task would
                # also wait 1800s if hermes hung. The 30-min cap also
                # protects against operator typo (e.g. 999999s).
                task_timeout_field = task.get("timeout_seconds")
                task_timeout = min(
                    int(task_timeout_field) if task_timeout_field else timeout,
                    MAX_TASK_TIMEOUT_SEC,
                )
                if task_timeout_field is None or int(task_timeout_field) == 0:
                    click.echo(
                        f"  task_timeout={task_timeout}s "
                        f"(no task.timeout_seconds; using CLI --timeout={timeout})"
                    )
                elif int(task_timeout_field) > MAX_TASK_TIMEOUT_SEC:
                    click.echo(
                        f"  task_timeout={task_timeout}s "
                        f"(capped from task.timeout_seconds={task_timeout_field}; "
                        f"MAX_TASK_TIMEOUT_SEC={MAX_TASK_TIMEOUT_SEC})"
                    )
                # proc.wait() does NOT touch stdout/stderr (those go to
                # files), so it never deadlocks on the PIPE-buffer issue.
                # _run_subprocess_with_timeout handles the timeout +
                # graceful terminate-then-kill sequence (v1.9.3).
                rc, timed_out = _run_subprocess_with_timeout(
                    proc, timeout=task_timeout,
                )
                # v3.12.4 instrument: race vs c3e998d atomic-write (diag only).
                import time as _race_t
                click.echo(f"[race-diag {tid}] t={_race_t.time():.6f} hermes EXIT (rc={rc}, timed_out={timed_out})")
            except Exception as e:
                # proc.wait() can throw on Windows if the child closes
                # handles abruptly. Bail out via the catch-all below
                # (the except Exception as e at line ~2750 returns
                # {"status": "failed", "error": ...}). Surface the
                # error here for visibility.
                raise
            if timed_out:
                # Subprocess didn't exit in time. Helper already sent
                # SIGTERM (then SIGKILL after grace). We just clean up
                # wrapper-side state and return a clear failure result
                # so the main loop unblocks and picks up the next task.
                # stop the liveness poller before any other handling
                stop_poll.set()
                # Live output streaming (v1.1): stop the tail threads
                # BEFORE closing the file handles so they can do a
                # final flush of any buffered output. join() with a
                # small timeout Ã¢â‚¬â€ if a thread is stuck, the main loop
                # still proceeds (the tail is best-effort).
                stop_stream.set()
                for t in stream_threads:
                    t.join(timeout=3)
                # Close file handles so the buffered output is flushed
                try:
                    stdout_fh.close()
                    stderr_fh.close()
                except Exception:
                    pass
                return {"status": "failed", "error": f"hermes subprocess timeout after {task_timeout}s. See {stdout_log.name} for full transcript."}
            # Live output streaming (v1.1): stop tail threads so they
            # do a final flush of any buffered chunks before we close
            # the file handles and read the full transcript.
            stop_stream.set()
            for t in stream_threads:
                t.join(timeout=3)
            # Close file handles Ã¢â‚¬â€ Python flushes its userspace buffer
            # on close, so we get the complete transcript on disk.
            stdout_fh.close()
            stderr_fh.close()
            # Read transcripts back from disk. Cap at 100KB in memory
            # (the full file is on disk for review). _clean_hermes_output
            # further truncates to MAX_SUMMARY_CHARS (32KB) before
            # storing in the DB. The 100KB ceiling here protects us
            # from OOM on absurd outputs (100MB log, recursive loop, etc.)
            # while still preserving the LLM's actual conclusion at the end.
            MAX_INPROC_READ_BYTES = 100 * 1024  # 100KB cap
            try:
                raw = stdout_log.read_bytes()
                if len(raw) > MAX_INPROC_READ_BYTES:
                    # Take TAIL Ã¢â‚¬â€ that's where the agent's actual conclusion
                    # is. The start is typically the prompt echo (handled by
                    # _strip_prompt_echo) and progress logs. Cleaner keeps
                    # the agent's last messages.
                    stdout_bytes = raw[-MAX_INPROC_READ_BYTES:]
                else:
                    stdout_bytes = raw
            except Exception as e:
                stdout_bytes = f"<read failed: {e}>".encode("utf-8", errors="replace")
            try:
                raw = stderr_log.read_bytes()
                stderr_bytes = raw[-MAX_INPROC_READ_BYTES:] if len(raw) > MAX_INPROC_READ_BYTES else raw
            except Exception as e:
                stderr_bytes = f"<read failed: {e}>".encode("utf-8", errors="replace")
            # Decode bytes as UTF-8, replacing bad chars (Windows consoles can
            # produce mixed encodings in edge cases)
            try:
                stdout = stdout_bytes.decode("utf-8", errors="replace")
            except Exception:
                stdout = stdout_bytes.decode("cp1252", errors="replace")
            try:
                stderr = stderr_bytes.decode("utf-8", errors="replace")
            except Exception:
                stderr = stderr_bytes.decode("cp1252", errors="replace")
            stdout = (stdout or "").strip()
            stderr = (stderr or "").strip()
            click.echo(
                f"  exit={rc} stdout_len={len(stdout_bytes)} (cap {MAX_INPROC_READ_BYTES}) "
                f"stderr_len={len(stderr_bytes)}"
            )
            click.echo(f"  full transcript: {stdout_log}")

            # Save session_id to the project for future resume.
            # Hermes prints "Session: <id>" near the end of its output.
            if rc == 0 and project_id:
                import re
                m = re.search(r"Session:\s+(\S+)", stdout)
                if m:
                    new_sid = m.group(1).rstrip(".,;:")
                    if new_sid and not new_sid.startswith("-"):
                        try:
                            _session_body = json.dumps(
                                {"session_id": new_sid, "role": role}
                            ).encode("utf-8")
                            _httpx_post(
                                f"{orchestrator_url}/api/projects/{project_id}/session",
                                headers=_hmac_headers(
                                    agent_id, secret,
                                    method="POST",
                                    path=f"/api/projects/{project_id}/session",
                                    body=_session_body,
                                ),
                                content=_session_body,
                                timeout=10,
                            )
                            click.echo(f"  saved session: {new_sid}")
                        except Exception as e:
                            click.echo(f"  WARN: session save failed: {e}")

            if rc == 0:
                summary = _clean_hermes_output(stdout) if stdout else "(no output)"
                # v3.11.0 (2026-08-03): TASK_FAILED convention. The
                # agent can signal task-level failure by printing
                # `TASK_FAILED: <reason>` anywhere in stdout, even
                # with exit code 0. The wrapper detects this marker
                # and converts the result to status=failed so the
                # orchestrator's supervisor can run feedback_to
                # loop-back, propagation, etc. Without this, an
                # agent has no clean way to mark a task as failed
                # (exit 0 = implicit success, hermes subprocess
                # itself rarely errors out by exit code). Use case
                # (savings demo on 2026-08-03): agent's check step
                # reads a savings file, decides threshold not met,
                # and needs to signal failure so the orchestrator
                # re-runs the upstream `add_savings` step. The
                # agent's last shell action does:
                #   echo "TASK_FAILED: total=\$45 < threshold=\$100"
                # and exits 0 Ã¢â‚¬â€ the wrapper catches the marker.
                # Only the FIRST occurrence is honored (a
                # defensive default; if multiple are present the
                # first one is the agent's primary signal).
                result = _apply_task_failed_marker(summary)
                # If task declared an output_path: check local cache, upload
                # to orchestrator via PUT file API, then attach artifact meta.
                if cache_dir and output_path:
                    output_local = cache_dir / output_path
                    if output_local.exists() and output_local.is_file():
                        # 15MB per-file cap (matches orch's write_file cap).
                        # If output_path is too large, skip the upload and
                        # record in skipped_artifacts so the dashboard
                        # shows "use share folder" hint.
                        MAX_OUTPUT_PATH_BYTES = 15 * 1024 * 1024
                        try:
                            output_size = output_local.stat().st_size
                        except Exception:
                            output_size = 0
                        if output_size > MAX_OUTPUT_PATH_BYTES:
                            click.echo(
                                f"  SKIP output_path {output_path}: "
                                f"{output_size} bytes exceeds "
                                f"{MAX_OUTPUT_PATH_BYTES // (1024*1024)}MB cap"
                            )
                            result["skipped_artifacts"].append({
                                "path": output_path,
                                "size_bytes": output_size,
                                "reason": (
                                    f"exceeds {MAX_OUTPUT_PATH_BYTES // (1024*1024)}MB cap; "
                                    f"use share folder"
                                ),
                            })
                        else:
                            file_bytes = output_local.read_bytes()
                            file_sha = hashlib.sha256(file_bytes).hexdigest()
                            # Upload to orchestrator (relative path)
                            rel = output_path.lstrip("/").replace("\\", "/")
                            try:
                                _file_path = f"/api/projects/{project_id}/files/{rel}"
                                r = _httpx_put(
                                    f"{orchestrator_url}{_file_path}",
                                    content=file_bytes,
                                    headers=_hmac_headers(
                                        agent_id, secret,
                                        method="PUT", path=_file_path,
                                        body=file_bytes,
                                    ),
                                    timeout=60,
                                )
                                if r.status_code == 200:
                                    click.echo(
                                        f"  uploaded: {rel} "
                                        f"({len(file_bytes)} bytes, sha={file_sha[:12]})"
                                    )
                                    # Send as artifacts list (standard contract).
                                    # The server registers each entry in the artifacts
                                    # table for browsing / downloading.
                                    result.setdefault("artifacts", []).append({
                                        "path": rel,
                                        "size_bytes": len(file_bytes),
                                        "sha256": file_sha,
                                    })
                                else:
                                    click.echo(
                                        f"  WARN: upload {rel} failed: HTTP {r.status_code} {r.text[:200]}"
                                    )
                            except Exception as e:
                                click.echo(f"  WARN: upload {rel} error: {e}")
                    else:
                        click.echo(
                            f"  WARN: task declared output_path={output_path} "
                            f"but file does not exist at {output_local}"
                        )

                # Auto-upload any other files the agent created in the cache
                # (regardless of output_path). This catches the common case
                # where the agent uses a different filename or writes
                # multiple files. Skip the orchestrator's own dirs.
                #
                # Per orch-as-coordinator principle: 15MB per-file cap. Files
                # larger than the cap are recorded in `skipped_artifacts`
                # (returned in TaskResult) so the dashboard can show
                # "N files too large Ã¢â‚¬â€ use share folder". The full path
                # and size are preserved so the operator can locate the file
                # on the agent's local cache.
                if cache_dir and project_id:
                    artifacts_extra = []
                    skipped_extra = []
                    # 15MB cap, matches orch server's write_file endpoint.
                    # Larger files should go to share folder (see
                    # agent_profiles.storage_refs), not through orch.
                    MAX_AUTO_UPLOAD_BYTES = 15 * 1024 * 1024
                    # v3.10.4 (2026-08-02) BUGFIX: skip well-known
                    # dependency / build / VCS directories. Previously the
                    # auto-upload loop did `cache_root.rglob("*")` and
                    # only filtered `__pycache__/` + dotfiles, so an
                    # agent that ran `npm install` to build a docx
                    # uploaded 373 node_modules/* files (each
                    # `artifact.registered` audit event + DB row) in
                    # ~1s, polluting the artifacts list. The wrapper
                    # should only upload deliverables the user cares
                    # about Ã¢â‚¬â€ actual code from `cache_root` is rarely
                    # a deliverable, and a `node_modules/` upload
                    # definitely isn't. Skip if ANY path component
                    # matches one of these names (case-insensitive,
                    # since Windows is case-insensitive and Linux
                    # wrappers can also be on case-sensitive fs).
                    #
                    # Directory names: package managers (npm, pnpm, yarn),
                    # VCS (.git), Python (venv, .venv, __pycache__,
                    # .pytest_cache, .mypy_cache, .ruff_cache), JS/TS
                    # build outputs (dist, build, out), Rust (target),
                    # tox.
                    #
                    # v3.10.4 follow-up (2026-08-02): added glob pattern
                    # support. v3.10.3's exact-name list missed
                    # `.pdfvenv/` (an agent-created venv for PDF build
                    # tools) Ã¢â‚¬â€ 1403 files / 36MB got uploaded as
                    # "artifacts" for proj-e05e89e9's
                    # finalize-hk-view-report step. Whack-a-mole
                    # (adding `.pdfvenv` to the explicit list) is the
                    # wrong fix Ã¢â‚¬â€ agents will create more custom-named
                    # venvs (`.myvenv`, `proj-venv`, `tool_venv`,
                    # `.sandbox-venv`, etc.) forever. The right fix is
                    # to match any dir whose name contains "venv"
                    # (case-insensitive). Added as a glob pattern.
                    # Also added `*.egg-info` and `*.dist-info` for
                    # Python package metadata (pip install creates
                    # these next to every installed package Ã¢â‚¬â€ easy to
                    # miss without the pattern).
                    _SKIP_DIR_PATTERNS = (
                        # Exact names (most common cases)
                        "node_modules",
                        ".git",
                        "__pycache__",
                        ".pytest_cache",
                        ".mypy_cache",
                        ".ruff_cache",
                        "venv",
                        ".venv",
                        "env",
                        ".env",
                        "dist",
                        "build",
                        "out",
                        "target",
                        ".tox",
                        ".nox",
                        ".parcel-cache",
                        ".turbo",
                        ".next",
                        ".nuxt",
                        ".svelte-kit",
                        "coverage",
                        ".nyc_output",
                        # Glob patterns (catches custom-named variants)
                        "*venv*",         # .pdfvenv, my-venv, proj_venv, etc.
                        "*.egg-info",     # Python package metadata
                        "*.dist-info",    # Python package metadata (PEP 376)
                    )
                    try:
                        cache_root = cache_dir.resolve()
                        # Find files modified during this task run. We use a
                        # generous cutoff: 5 min before the task started (so
                        # parent tasks' cached files are also captured) and
                        # now as the upper bound. Compute the cutoff from
                        # _task_start_ts (captured at function entry) so
                        # the filter is deterministic per-task. Without
                        # this filter, every task re-uploads every file
                        # in the cache and the audit_log fills with
                        # duplicate artifact.registered events.
                        from datetime import datetime, timezone, timedelta
                        # Per-task cutoff: any file with mtime before
                        # (_task_start_ts - 300s) is treated as "from
                        # a previous task" and skipped. 5 min cushion
                        # catches any clock skew between cache file
                        # writes and the timestamp the wrapper records.
                        task_cutoff_ts = _task_start_ts - 300
                        for f in cache_root.rglob("*"):
                            if not f.is_file():
                                continue
                            # Skip files not modified during this task.
                            # Use mtime directly (cheaper than stat twice).
                            try:
                                f_mtime = f.stat().st_mtime
                            except OSError:
                                continue
                            if f_mtime < task_cutoff_ts:
                                continue  # leftover from earlier task
                            # Skip our own internal test files and the cache
                            # structure (we use relative paths for the upload)
                            rel = f.relative_to(cache_root).as_posix()
                            if rel in (output_path,):
                                continue  # already uploaded
                            # v3.10.4: skip any file under a known
                            # dependency / build / VCS directory. Check
                            # the PARENT components only (not the leaf
                            # filename), so a file literally named
                            # `build.py` at the cache root is NOT
                            # skipped Ã¢â‚¬â€ only files inside a `build/`
                            # subdirectory are. Example matches:
                            # `node_modules/foo.js`,
                            # `web/node_modules/foo.js`,
                            # `__pycache__/bar.cpython-311.pyc`.
                            #
                            # v3.10.4 follow-up: support glob patterns
                            # in `_SKIP_DIR_PATTERNS`. Exact names are
                            # matched as-is, patterns use `fnmatch`
                            # (case-insensitive). Catches custom-named
                            # variants like `.pdfvenv`, `proj-venv`,
                            # `.sandbox-venv` without a maintenance
                            # burden of adding each to the explicit list.
                            rel_parts = rel.split("/")
                            _skip = False
                            for p in rel_parts[:-1]:
                                pl = p.lower()
                                for pat in _SKIP_DIR_PATTERNS:
                                    if "*" in pat:
                                        if fnmatch.fnmatchcase(pl, pat.lower()):
                                            _skip = True
                                            break
                                    else:
                                        if pl == pat:
                                            _skip = True
                                            break
                                if _skip:
                                    break
                            if _skip:
                                continue
                            if rel.startswith("__pycache__/"):
                                continue
                            if f.name.startswith("."):
                                continue
                            # Skip orchestrator control files. The supervisor /
                            # coordinator use these as signals (e.g. decision.md
                            # drives the iter loop). Auto-uploading from a stale
                            # agent cache re-creates a file the supervisor just
                            # unlinked on replan, causing the iter loop to read
                            # a stale verdict and auto-complete the project.
                            # Same applies to other low-level control files.
                            if f.name in ("decision.md", "decisions.md",
                                          "status.md", "plan.md"):
                                continue
                            # Skip the hermes transcript files we just wrote
                            # (they're debugging artifacts for the user, not
                            # deliverable content). Saves a 64KB+ upload of
                            # mostly-UI-chrome.
                            if rel.startswith("hermes.") and rel.endswith(".log"):
                                continue
                            # Skip the agent's structured status files. Per
                            # house style (2026-07-22): .results.json (or any
                            # `*.results.json` / `*_results.json` agent-written
                            # file) is the task status, not a deliverable.
                            # Real output is .md (human-readable). The agent's
                            # status is already reported via the API on
                            # /result, so uploading the file would just
                            # clutter the artifacts list.
                            if f.name.endswith(".results.json") or f.name.endswith("_results.json"):
                                continue
                            # Size check BEFORE read_bytes() Ã¢â‚¬â€ avoid OOM on
                            # huge files (an LLM could write a 10GB log if
                            # it loops). If too large, record in skipped
                            # and continue.
                            try:
                                file_size = f.stat().st_size
                            except Exception:
                                continue
                            if file_size > MAX_AUTO_UPLOAD_BYTES:
                                click.echo(
                                    f"  SKIP {rel}: {file_size} bytes exceeds "
                                    f"{MAX_AUTO_UPLOAD_BYTES // (1024*1024)}MB cap "
                                    f"(use share folder)"
                                )
                                skipped_extra.append({
                                    "path": rel,
                                    "size_bytes": file_size,
                                    "reason": (
                                        f"exceeds {MAX_AUTO_UPLOAD_BYTES // (1024*1024)}MB "
                                        f"cap; use share folder"
                                    ),
                                })
                                continue
                            try:
                                file_bytes = f.read_bytes()
                            except Exception:
                                continue
                            if not file_bytes:
                                continue
                            file_sha = hashlib.sha256(file_bytes).hexdigest()
                            try:
                                _file_path2 = f"/api/projects/{project_id}/files/{rel}"
                                r2 = _httpx_put(
                                    f"{orchestrator_url}{_file_path2}",
                                    content=file_bytes,
                                    headers=_hmac_headers(
                                        agent_id, secret,
                                        method="PUT", path=_file_path2,
                                        body=file_bytes,
                                    ),
                                    timeout=60,
                                )
                                if r2.status_code == 200:
                                    click.echo(
                                        f"  auto-uploaded: {rel} "
                                        f"({len(file_bytes)} bytes, sha={file_sha[:12]}...)"
                                    )
                                    artifacts_extra.append({
                                        "path": rel,
                                        "size_bytes": len(file_bytes),
                                        "sha256": file_sha,
                                    })
                                else:
                                    click.echo(
                                        f"  WARN: auto-upload {rel} failed: HTTP {r2.status_code}"
                                    )
                            except Exception as e:
                                click.echo(f"  WARN: auto-upload {rel} error: {e}")
                    except Exception as e:
                        click.echo(f"  WARN: auto-upload scan error: {e}")
                    # Merge auto-uploaded artifacts into the result so the
                    # server can register them in the artifacts table. Merge
                    # each list EXACTLY ONCE Ã¢â‚¬â€ extending twice causes duplicate
                    # artifact rows in the DB (caught 2026-07-22: 6 files
                    # produced 12 artifact.registered audit events, server
                    # registered 12 artifact rows for the 6 unique files).
                    if artifacts_extra:
                        result.setdefault("artifacts", []).extend(artifacts_extra)
                    if skipped_extra:
                        result.setdefault("skipped_artifacts", []).extend(skipped_extra)
                # Capture hermes session tokens (input/output/cache/etc) from
                # the profile's state.db so the orchestrator can record real
                # per-task token usage. Best-effort: returns None if the
                # schema is older or the session isn't committed yet.
                token_usage = _capture_session_tokens(profile_root)
                if token_usage:
                    result["token_usage"] = token_usage
                    # v3.12.1 follow-up #6: pop the history_turn_count
                    # field out of token_usage (separate top-level
                    # key) so the orchestrator's /api/tasks/{id}/result
                    # handler can persist it into task_dispatch
                    # .history_turn_count. The token_usage table
                    # itself stays focused on token metrics.
                    htc = token_usage.pop("history_turn_count", None)
                    if htc is not None:
                        result["history_turn_count"] = htc
                        click.echo(
                            f"  history_turn_count: {htc} "
                            f"(hermes session message_count)"
                        )
                    click.echo(
                        f"  tokens: in={token_usage.get('prompt_tokens', 0)} "
                        f"out={token_usage.get('completion_tokens', 0)} "
                        f"cache_r={token_usage.get('cache_read_tokens', 0)} "
                        f"cache_w={token_usage.get('cache_write_tokens', 0)}"
                    )
                # Stage 1.5 multi-skill (2026-07-23): parse the hermes
                # transcript for the `Ã°Å¸â€œÅ¡ skill <name>` markers and report
                # the unique list to the orchestrator. promote-to-workflow
                # uses this to preserve every skill the source used.
                skills_used = _extract_skills_used_from_transcript(stdout_log)
                if skills_used:
                    result["skills_used"] = skills_used
                    click.echo(f"  skills_used: {skills_used}")
                # Stop the liveness poller before returning
                stop_poll.set()
                return result
            failed_result = {
                "status": "failed",
                "error": (stderr or stdout or f"hermes exited {rc}")[:8000],
            }
            # Even on failure the hermes subprocess may have completed
            # enough to commit a session row (with partial token usage).
            # Capture it so we still get a token record for cost analysis.
            token_usage = _capture_session_tokens(profile_root)
            if token_usage:
                failed_result["token_usage"] = token_usage
                # v3.12.1 follow-up #6: same top-level extraction as
                # the success path above — even on a failed hermes
                # run, the partial session's message_count is a
                # useful observability signal.
                htc = token_usage.pop("history_turn_count", None)
                if htc is not None:
                    failed_result["history_turn_count"] = htc
            # Same for skills_used Ã¢â‚¬â€ partial transcript may still show
            # what the agent loaded before failing.
            skills_used = _extract_skills_used_from_transcript(stdout_log)
            if skills_used:
                failed_result["skills_used"] = skills_used
            # Stop the liveness poller before returning
            stop_poll.set()
            return failed_result
        except Exception as e:
            # Catch-all: proc.wait() can throw on Windows if the child
            # process closes handles abruptly. Also try to close our
            # file handles so the partial transcript gets flushed.
            try:
                proc.kill()
            except Exception:
                pass
            try:
                stdout_fh.close()
            except Exception:
                pass
            try:
                stderr_fh.close()
            except Exception:
                pass
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

    # Main loop
    click.echo("[daemon] entering main loop. Press Ctrl-C to stop.")

    # Reverse-sync: scan filesystem for skills/ files and push missing ones
    # to the orchestrator. Used both for the "Sync from disk" button on the
    # dashboard (immediate trigger via __sync_skills__ marker config) and for
    # agent self-taught skills (the agent writes a SKILL.md into
    # skills/<name>/SKILL.md and the next periodic scan picks it up).
    # Hermes 0.17+ only reads the folder layout
    # `skills/<name>/SKILL.md`; flat-file skills/<name>.md is no longer
    # supported (dropped 2026-07-19, commit d5b7c9a).
    # Subfolder layout `skills/<category>/<name>/SKILL.md` is ALSO supported
    # (added 2026-07-24, Option B). The wrapper recursively scans up to
    # 2 levels deep; deeper nesting is unsupported. Names are derived
    # from the full sub-path: `skills/productivity/xlsx/SKILL.md` Ã¢â€ â€™
    # `name="productivity/xlsx"`. Hermes itself still uses the leaf
    # directory name to identify a skill, so two skills with the
    # same leaf name in different subfolders (e.g. `data/xlsx` and
    # `productivity/xlsx`) will COLLIDE at the hermes layer Ã¢â‚¬â€ the
    # operator should avoid this. Our DB happily stores both as
    # distinct records (different file_path, different name).
    _SKILL_FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    # Subfolder regex allows path-separator (slash) up to 1 level of
    # nesting. Reuses the same charset as the flat-name regex to keep
    # the validator simple.
    _SKILL_SUBFOLDER_RE = re.compile(
        r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
    )
    # Per-profile throttle for the periodic auto-sync (don't scan every 5s).
    _SKILL_AUTO_SYNC_INTERVAL = 30  # seconds
    _last_skill_sync: dict[str, float] = {}  # profile_name -> last sync time

    def _sync_one_profile_skills(
        client: httpx.Client,
        pname: str,
        pcfg: dict,
        *,
        log_prefix: str = "[daemon] skills-sync",
    ) -> int:
        """Scan <root>/skills/*.md and push missing/changed ones to the
        orchestrator. Returns the number of skills registered.

        The wrapper is the source of truth for what's on disk Ã¢â‚¬â€ if a skill
        file is on disk but missing from the orchestrator, we POST a
        profile_configs row for it (status=applied, source=self-taught).
        Files that match what's already in the orchestrator (same name,
        same size) are skipped to keep the audit log quiet.
        """
        try:
            root = resolve_profile_root(
                pcfg.get("root", ""),
                pname,
                profiles_dir=hermes_profiles_dir,
            )
        except Exception as e:
            click.echo(f"{log_prefix} ({pname}) resolve error: {e}")
            return 0
        skills_dir = root / "skills"
        if not skills_dir.exists() or not skills_dir.is_dir():
            return 0
        # Fetch current DB view (include_deleted so we know about deletes too,
        # but we'll only ever push new/upsert here Ã¢â‚¬â€ deletes are dashboard-driven)
        try:
            r = client.get(
                f"{orchestrator_url}/api/agents/{agent_id}/profiles/{pname}/skills?include_deleted=1",
                headers=_auth_headers(
                    "GET",
                    f"/api/agents/{agent_id}/profiles/{pname}/skills?include_deleted=1",
                ),
                timeout=10,
            )
            r.raise_for_status()
            db_skills = {s["name"]: s for s in r.json() or []}
        except Exception as e:
            click.echo(f"{log_prefix} ({pname}) DB fetch error: {e}")
            return 0
        registered = 0
        try:
            # Two layouts are supported (2026-07-24: added subfolder):
            #   1. Flat:  skills/<name>/SKILL.md        Ã¢â€ â€™ name = "<name>"
            #   2. Nested: skills/<category>/<name>/SKILL.md  Ã¢â€ â€™ name = "<category>/<name>"
            #      (recursive up to 2 levels; deeper nesting is unsupported
            #       to keep the API simple)
            #
            # The DB stores file_path as the full relative path
            # ("skills/<category>/<name>/SKILL.md") and the `name` is
            # derived from that. The server's _row_to_skill already
            # handles this Ã¢â‚¬â€ see agents.py:1253-1254.
            #
            # Hermes 0.17+ uses the LEAF directory name to identify a
            # skill, so `data/xlsx` and `productivity/xlsx` would COLLIDE
            # at the hermes layer (both register as `xlsx`). Our DB
            # treats them as distinct records. The operator is
            # responsible for avoiding the leaf-name collision when
            # organizing skills into subfolders.
            #
            # Flat-file skills/<name>.md is no longer supported
            # (dropped 2026-07-19, commit d5b7c9a). We only scan for
            # SKILL.md (capital S) inside a subfolder.
            for file_path in sorted(skills_dir.rglob("SKILL.md")):
                if not file_path.is_file():
                    continue
                # file_path = skills_dir / (optional category/) <name> / SKILL.md
                # Derive the skill name from the relative path
                try:
                    rel = file_path.relative_to(skills_dir)  # e.g. "xlsx/SKILL.md" or "productivity/xlsx/SKILL.md"
                except ValueError:
                    continue
                parts = rel.parts  # e.g. ("xlsx", "SKILL.md") or ("productivity", "xlsx", "SKILL.md")
                if len(parts) == 2:
                    # Flat: skills/<name>/SKILL.md
                    name = parts[0]
                    if not _SKILL_FOLDER_RE.match(name):
                        click.echo(f"{log_prefix} ({pname}) skip '{rel}': bad name")
                        continue
                elif len(parts) == 3:
                    # Subfolder: skills/<category>/<name>/SKILL.md
                    name = f"{parts[0]}/{parts[1]}"
                    if not _SKILL_SUBFOLDER_RE.match(name):
                        click.echo(f"{log_prefix} ({pname}) skip '{rel}': bad subfolder name")
                        continue
                else:
                    # Deeper nesting (3+ levels) Ã¢â‚¬â€ unsupported
                    click.echo(
                        f"{log_prefix} ({pname}) skip '{rel}': depth > 2 not supported"
                    )
                    continue
                try:
                    file_bytes = file_path.read_bytes()
                except Exception as e:
                    click.echo(f"{log_prefix} ({pname}/{name}) read error: {e}")
                    continue
                if not file_bytes:
                    continue  # skip empty files
                sha = hashlib.sha256(file_bytes).hexdigest()
                # Change detection: prefer SHA over byte-length. The API
                # returns `sha256` (hex of desired_content bytes). If the
                # file's SHA matches what the DB already has, the file is
                # unchanged Ã¢â‚¬â€ don't re-post. This is content-addressed so
                # it's immune to encoding round-trip bugs (the previous
                # byte-length compare was sensitive to em-dash = 3 bytes
                # and caused an infinite re-apply loop on files with
                # multi-byte chars).
                db_skill = db_skills.get(name)
                if (
                    db_skill
                    and db_skill.get("status") in ("applied", "pending", "applying")
                    and db_skill.get("sha256")
                    and db_skill.get("sha256") == sha
                ):
                    continue
                # New or changed file Ã¢â‚¬â€ push to orchestrator. The orchestrator
                # creates a pending row, we immediately ack as applied (file
                # is already on disk), so the apply-pending loop won't try
                # to write the file again.
                try:
                    content_text = file_bytes.decode("utf-8", errors="replace")
                except Exception:
                    content_text = file_bytes.decode("cp1252", errors="replace")
                _skill_body = json.dumps(
                    {"name": name, "content": content_text}
                ).encode("utf-8")
                # v3.12.1 follow-up #7 (skills-sync Content-Type): when we
                # pass `content=_skill_body` (bytes), httpx does NOT
                # auto-set Content-Type to application/json. Without it,
                # the server's SkillCreate Pydantic model receives the
                # body as a string and FastAPI returns 422
                # "Input should be a valid dictionary or object to
                # extract fields from". Same root cause as the v1.9.3
                # result-submit fix at line ~2044; that one set
                # Content-Type explicitly, this one was missed.
                _skill_headers = {
                    **_auth_headers(
                        "POST",
                        f"/api/agents/{agent_id}/profiles/{pname}/skills",
                        _skill_body,
                    ),
                    "Content-Type": "application/json",
                    "X-Skill-Source": "self-taught",
                }
                r = client.post(
                    f"{orchestrator_url}/api/agents/{agent_id}/profiles/{pname}/skills",
                    headers=_skill_headers,
                    content=_skill_body,
                    timeout=15,
                )
                if r.status_code == 201:
                    cfg_row = r.json()
                    # Immediately ack as applied
                    try:
                        _ack_body = json.dumps(
                            {"status": "applied", "actual_sha256": sha}
                        ).encode("utf-8")
                        client.post(
                            f"{orchestrator_url}/api/agents/{agent_id}/profiles/{pname}/configs/{cfg_row['id']}/ack",
                            headers=_auth_headers(
                                "POST",
                                f"/api/agents/{agent_id}/profiles/{pname}/configs/{cfg_row['id']}/ack",
                                _ack_body,
                            ),
                            content=_ack_body,
                            timeout=10,
                        )
                    except Exception as e:
                        click.echo(f"{log_prefix} ({pname}/{name}) ack error: {e}")
                    click.echo(
                        f"{log_prefix} {pname}/{name} registered "
                        f"({len(file_bytes)} bytes, sha={sha[:12]}...)"
                    )
                    registered += 1
                else:
                    click.echo(
                        f"{log_prefix} ({pname}/{name}) push failed: "
                        f"HTTP {r.status_code} {r.text[:200]}"
                    )
        except Exception as e:
            click.echo(f"{log_prefix} ({pname}) scan error: {e}")
        return registered

    def _poll_max_history_config() -> int:
        """Fetch the server's default max_history_turns and cache it.

        v3.12.1 follow-up #6: the orchestrator exposes
        GET /api/agents/{id}/max_history_config so the wrapper
        can pick up server-side config changes WITHOUT a
        wrapper restart. Operators tune config.yaml (or
        the future settings-page UI) and the new value is
        picked up on the next tick's poll.

        Per-task overrides in task.params["_max_history_turns"]
        still win (server-resolved at dispatch time across the
        4-tier priority chain); this cache is only the FALLBACK
        default. The cache is read in `_run_task`'s
        `compression.protect_last_n` write path.

        Cost: one HMAC-signed GET per tick (~5s). The endpoint
        is cheap (just reads from request.app.state.config),
        so the wrapper-side overhead is bounded by the
        HMAC computation + a small JSON parse.

        Returns: 0 on success (cached value updated), 1 on
        failure (cached value left at the previous setting).
        """
        global _max_history_turns_cache
        try:
            path = f"/api/agents/{agent_id}/max_history_config"
            r = _httpx_get(
                f"{orchestrator_url}{path}",
                headers=_auth_headers("GET", path),
                timeout=5,
            )
            if r.status_code != 200:
                return 1
            body = r.json() or {}
            new_value = int(body.get("value", 6))
            # Defensive clamp: 0..200 matches the Pydantic
            # validator on ProjectPlan.max_history_turns.
            # 0 = "no cap" (allowed). > 200 = typo, fall back.
            if new_value < 0 or new_value > 200:
                return 1
            _max_history_turns_cache = new_value
            return 0
        except Exception as e:
            click.echo(f"[daemon] max_history_config poll failed: {e}")
            return 1

    def _apply_pending_configs_inline() -> int:
        """Drain pending profile_configs from the orchestrator and apply.

        Returns the number of configs applied. Runs cheaply (no file I/O
        when nothing pending) so we can call it every tick.
        """
        if not profiles_cfg:
            return 0
        applied = 0
        try:
            with httpx.Client(timeout=10, verify=_agent_http_verify()) as client:
                for pname, pcfg in profiles_cfg.items():
                    # Resolve the profile root (template like <profiles_dir>/<role>)
                    try:
                        root = resolve_profile_root(
                            pcfg.get("root", ""),
                            pname,
                            profiles_dir=hermes_profiles_dir,
                        )
                    except Exception:
                        continue
                    if not root.exists():
                        continue
                    while True:
                        try:
                            r = client.get(
                                f"{orchestrator_url}/api/agents/{agent_id}"
                                f"/profiles/{pname}/configs/pending",
                                headers=_auth_headers(
                                    "GET",
                                    f"/api/agents/{agent_id}/profiles/{pname}/configs/pending",
                                ),
                            )
                            r.raise_for_status()
                            if not r.content or r.json() is None:
                                break
                            cfg_row = r.json()
                        except Exception as e:
                            click.echo(f"[daemon] config poll error ({pname}): {e}")
                            break
                        target = root / cfg_row["file_path"]
                        try:
                            # Special marker: __sync_skills__ isn't a real file.
                            # It's a "please reverse-sync now" trigger dropped
                            # by the dashboard's "Sync from disk" button. We
                            # run the sync and ack as applied (no file written).
                            if cfg_row["file_path"] == "__sync_skills__":
                                with httpx.Client(timeout=30, verify=_agent_http_verify()) as sync_client:
                                    n = _sync_one_profile_skills(sync_client, pname, pcfg)
                                click.echo(
                                    f"[daemon] sync-skills trigger for {pname}: "
                                    f"{n} new skill(s) registered"
                                )
                                actual_sha = hashlib.sha256(
                                    f"sync:{n}".encode()
                                ).hexdigest()
                                _ack_body2 = json.dumps(
                                    {"status": "applied", "actual_sha256": actual_sha}
                                ).encode("utf-8")
                                ack = client.post(
                                    f"{orchestrator_url}/api/agents/{agent_id}"
                                    f"/profiles/{pname}/configs/{cfg_row['id']}/ack",
                                    headers=_auth_headers(
                                        "POST",
                                        f"/api/agents/{agent_id}/profiles/{pname}/configs/{cfg_row['id']}/ack",
                                        _ack_body2,
                                    ),
                                    content=_ack_body2,
                                    timeout=10,
                                )
                                ack.raise_for_status()
                                applied += 1
                                continue
                            # Safety check: file_path must resolve inside `root`
                            # (defense-in-depth Ã¢â‚¬â€ the API also validates skill
                            # names, but a buggy config shouldn't be able to
                            # escape the profile dir on the agent host).
                            target_resolved = target.resolve()
                            root_resolved = root.resolve()
                            try:
                                target_resolved.relative_to(root_resolved)
                            except ValueError:
                                raise RuntimeError(
                                    f"refusing to write outside profile root: {target} (root={root})"
                                )
                            content = cfg_row["desired_content"] or ""
                            if content == "" and cfg_row["file_path"].startswith("skills/"):
                                # Empty content for a skills/ path = delete
                                # the file on the agent host (used by the
                                # dashboard's "delete skill" flow).
                                #
                                # Folder layout: skills/<name>/SKILL.md Ã¢â€ â€™
                                # rmtree the whole <name> folder
                                # (we only register SKILL.md; aux files
                                # in the folder are owned by the user)
                                skill_folder = target_resolved.parent
                                if skill_folder.exists() and skill_folder.is_dir():
                                    import shutil as _shutil
                                    _shutil.rmtree(skill_folder)
                                    click.echo(
                                        f"[daemon] deleted folder {skill_folder} "
                                        f"on {pname}"
                                    )
                                else:
                                    click.echo(
                                        f"[daemon] delete folder {skill_folder} "
                                        f"(already absent) on {pname}"
                                    )
                                actual_sha = hashlib.sha256(b"").hexdigest()
                            else:
                                _atomic_write(target, content)
                                actual_sha = hashlib.sha256(
                                    content.encode()
                                ).hexdigest()
                                click.echo(
                                    f"[daemon] applied {cfg_row['file_path']} "
                                    f"to {pname} (sha={actual_sha[:12]}...)"
                                )
                            _ack_body3 = json.dumps(
                                {"status": "applied", "actual_sha256": actual_sha}
                            ).encode("utf-8")
                            _ack_headers = _auth_headers(
                                "POST",
                                f"/api/agents/{agent_id}/profiles/{pname}/configs/{cfg_row['id']}/ack",
                                _ack_body3,
                            )
                            # v3.10.2 (2026-08-02) BUGFIX: Content-Type is
                            # required for FastAPI to parse the body as
                            # JSON for the ProfileConfigAck Pydantic model.
                            # Without it, the server returns 422 "Input
                            # should be a valid dictionary" and the wrapper
                            # silently swallows the failure in the try/except
                            # below, leaving the config row stuck in
                            # 'applying' until the supervisor reaper clears it
                            # (60s later). Reproduced across both linux-a-01
                            # and win-local-1 wrappers.
                            _ack_headers["Content-Type"] = "application/json"
                            ack = client.post(
                                f"{orchestrator_url}/api/agents/{agent_id}"
                                f"/profiles/{pname}/configs/{cfg_row['id']}/ack",
                                headers=_ack_headers,
                                content=_ack_body3,
                                timeout=10,
                            )
                            ack.raise_for_status()
                            applied += 1
                        except Exception as e:
                            click.echo(f"[daemon] config apply error: {e}")
                            try:
                                _ack_body4 = json.dumps(
                                    {"status": "failed", "error": str(e)}
                                ).encode("utf-8")
                                _ack_h4 = _auth_headers(
                                    "POST",
                                    f"/api/agents/{agent_id}/profiles/{pname}/configs/{cfg_row['id']}/ack",
                                    _ack_body4,
                                )
                                # See applied-ack path above for the
                                # Content-Type rationale. Both ack POSTs
                                # in this function need it.
                                _ack_h4["Content-Type"] = "application/json"
                                client.post(
                                    f"{orchestrator_url}/api/agents/{agent_id}"
                                    f"/profiles/{pname}/configs/{cfg_row['id']}/ack",
                                    headers=_ack_h4,
                                    content=_ack_body4,
                                    timeout=10,
                                )
                            except Exception:
                                pass
                            break
        except Exception as e:
            click.echo(f"[daemon] config sync outer error: {e}")
        return applied

    try:
        # Record the process start time used by the self-restart
        # watchdog (see the sleep-loop block below). Snapshot once at
        # startup so we compare against a stable reference; using
        # "previous tick" would falsely trigger on .pyc regeneration
        # by our own imports.
        _self_start_ts = time_mod.time()
        # Agent-level heartbeat in a daemon thread (independent of the
        # main loop). Without this, a long-running hermes subprocess
        # (3+ min) blocks the main loop's heartbeat call, so the
        # orchestrator's `stale` threshold (90s) trips and the agent
        # shows as offline even though the wrapper is actively working.
        # Mirrors the per-task liveness poll inside _run_task: we
        # already learned from #22 + the stuck-wrapper diagnosis that
        # long subprocesses need a side-channel for liveness, and the
        # agent-level heartbeat is exactly that side channel.
        # Idempotent: if the thread is already running, skip.
        import threading as _threading
        if "_hb_thread" not in globals() or not _hb_thread.is_alive():
            def _heartbeat_loop():
                while not stop_flag["stop"]:
                    try:
                        _heartbeat()
                    except Exception as e:
                        click.echo(f"[daemon] bg heartbeat error: {e}")
                    # interval/2 so heartbeats overlap with main loop's
                    # slower calls (apply-configs takes a few seconds)
                    time_mod.sleep(max(1, interval // 2))
            _hb_thread = _threading.Thread(
                target=_heartbeat_loop, daemon=True, name="agent-hb-bg"
            )
            _hb_thread.start()
            click.echo(f"[daemon] background heartbeat thread started (every {max(1, interval // 2)}s)")

        while not stop_flag["stop"]:
            tasks, cleanup_ids = _heartbeat()
            # Process any session-cleanup requests the supervisor queued
            # before the rest of the loop work. Cheap when empty.
            if cleanup_ids:
                _cleanup_local_sessions(cleanup_ids)
            assigned = [t for t in tasks if t.get("status") == "assigned"]
            if assigned:
                click.echo(f"[daemon] got {len(assigned)} assigned task(s)")
            # v3.10.4 (2026-08-02) BUGFIX: apply pending profile configs
            # (soul.md) BEFORE the task pool, not after. The previous
            # order (heartbeat -> run tasks -> apply configs) meant a
            # long-running task (3+ min hermes subprocess) blocked the
            # config poll for the entire task duration. The supervisor's
            # 30s dispatch timeout would fire before the wrapper got a
            # chance to claim the pending config, and the new task would
            # fail with `dispatch.soul_apply_failed: SOUL apply timed
            # out (status=pending)` Ã¢â‚¬â€ even though the wrapper was
            # healthy and would have applied the config in 1-7s if it
            # had been polled.
            #
            # Real-world repro: proj-e05e89e9 (analyst 2) on 2026-08-02.
            # Task 365463c4 (research-hk-market-context, super profile)
            # started at 02:23:39 and was running. The super-b config
            # 89449e75 was queued at 02:23:35 Ã¢â‚¬â€ the wrapper was busy
            # with 365463c4, never polled super-b, the 30s dispatch
            # timeout fired at 02:24:05, compare-features failed,
            # finalize-hk-view-report was skipped as a downstream
            # consequence. Two tasks lost to a one-line ordering bug.
            #
            # Safe to call before the task pool: the apply writes to
            # <profile.root>/<file_path> (typically soul.md), and the
            # task reads its profile's soul.md at start time (cached for
            # the task's lifetime). No shared lock needed; concurrent
            # apply + read is benign Ã¢â‚¬â€ the task will see either the
            # old or the new soul.md, and either is a valid working
            # state.
            #
            # Cheap when nothing pending Ã¢â‚¬â€ the inner loop is one HTTP
            # GET per profile that returns `None`, then breaks. So
            # adding this pre-tick cost is ~0ms in the common case.
            _apply_pending_configs_inline()
            # v3.12.1 follow-up #6: poll the server's default
            # max_history_turns. Cheap (one HMAC GET per tick);
            # lets operators tune config.yaml and pick up the
            # change on the next tick, no wrapper restart.
            _poll_max_history_config()
            # v3.6.0: per-agent concurrent task cap. We rebuild the
            # ThreadPoolExecutor on every tick so cap changes (via
            # PUT /api/agents/{id}) take effect within `interval`
            # seconds without a wrapper restart. The pool size is
            # the lower of:
            #   (a) cap from server (1..32, default 1)
            #   (b) len(assigned) (no point spinning up idle workers
            #       when fewer tasks are available)
            # so we never queue workers that have nothing to do.
            #
            # Threading note: the workers each run a hermes subprocess
            # via subprocess.Popen + .communicate(). The GIL is released
            # during that I/O so the workers run in true parallel for
            # IO-bound work (LLM API calls, file uploads). For pure
            # CPU work this would be a bottleneck, but hermes's hot
            # path is dominated by subprocess + network calls.
            #
            # Why ThreadPoolExecutor over fork-N-processes: 1 process
            # keeps shared state (config cache, file descriptors, the
            # in-process daemon heartbeat thread, the L1/L2 profile
            # metadata) reusable across workers. Forking N processes
            # would lose that and require re-reading config per child.
            # The trade-off is no GIL-free parallelism, which is fine
            # here because hermes workers are IO-bound subprocesses.
            cap = _max_concurrent_tasks_cache
            pool_size = max(1, min(cap, len(assigned) if assigned else cap))
            if pool_size > 1 and len(assigned) > 1:
                click.echo(
                    f"[daemon] using ThreadPoolExecutor with "
                    f"{pool_size} workers (cap={cap}, tasks={len(assigned)})"
                )
            # Each worker does the same thing the old sequential loop
            # did: claim, run, submit. We pass the task dict in;
            # everything else is closure-captured (the orchestrator URL,
            # the helper funcs, stop_flag).
            def _process_one(t: dict) -> None:
                if stop_flag["stop"]:
                    return
                if not _claim(t["id"]):
                    return
                result = _run_task(t)
                _submit_result(t["id"], result)
            if assigned:
                # `with` ensures all workers are joined (or the pool
                # is shut down) before we fall through to the
                # post-task maintenance work below (config apply,
                # zombie sweep, skill sync). Without `with`, a
                # `break` in the pool shutdown could leave
                # workers in flight while we move on.
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=pool_size,
                    thread_name_prefix="hermes-task",
                ) as pool:
                    futures = [pool.submit(_process_one, t) for t in assigned]
                    # Wait for all workers. We DON'T short-circuit on
                    # stop_flag here Ã¢â‚¬â€ we want in-flight hermes
                    # subprocesses to complete so we don't leak
                    # processes (each worker holds a subprocess.Popen).
                    # A kill signal via the outer while-loop's stop
                    # check will catch the next tick.
                    for f in futures:
                        try:
                            f.result()
                        except Exception as e:
                            click.echo(
                                f"[daemon] worker exception: {type(e).__name__}: {e}"
                            )
            if once:
                click.echo("[daemon] --once: exiting after one tick")
                return
            # (v3.10.4: `_apply_pending_configs_inline()` moved ABOVE
            # the task pool Ã¢â‚¬â€ see the comment in the pre-tick block.
            # The old call here is gone; configs are now applied every
            # tick, BEFORE the workers start, so a long-running task
            # can never starve a new profile's SOUL apply.)
            # Periodic zombie-session sweep (Plan B for the long-standing
            # "49 active sessions in super/state.db" issue). Throttled to
            # once per day per profile Ã¢â‚¬â€ the SQL scan is cheap but the
            # hermes sessions delete subprocess calls aren't, and a fresh
            # sweep every 5s would be wasteful. Each profile is swept
            # independently so a slow hermes on one profile doesn't block
            # the others.
            now_ts = time_mod.time()
            for pname, pcfg in profiles_cfg.items():
                last = _last_zombie_sweep.get(pname, 0)
                if (now_ts - last) < _ZOMBIE_SESSION_SWEEP_INTERVAL_S:
                    continue
                try:
                    _sweep_zombie_sessions_inline(
                        pname, pcfg, hermes_profiles_dir=hermes_profiles_dir,
                    )
                    _last_zombie_sweep[pname] = now_ts
                except Exception as e:
                    click.echo(f"[daemon] zombie-sweep {pname} tick error: {e}")
            # Periodic auto-sync of skills from disk (throttled per profile).
            # Catches self-taught skills the agent wrote into skills/ without
            # going through the dashboard. We use a single short-lived client
            # and only run if enough time has passed since the last sync for
            # this profile Ã¢â‚¬â€ the file scan is cheap but no point doing it
            # every 5s.
            now_ts = time_mod.time()
            with httpx.Client(timeout=30, verify=_agent_http_verify()) as sync_client:
                for pname, pcfg in profiles_cfg.items():
                    last = _last_skill_sync.get(pname, 0)
                    if (now_ts - last) < _SKILL_AUTO_SYNC_INTERVAL:
                        continue
                    try:
                        n = _sync_one_profile_skills(sync_client, pname, pcfg)
                        if n:
                            click.echo(
                                f"[daemon] auto-sync {pname}: "
                                f"{n} new/changed skill(s)"
                            )
                    except Exception as e:
                        click.echo(f"[daemon] auto-sync {pname} error: {e}")
                    _last_skill_sync[pname] = now_ts
            # Sleep, but check stop flag every second
            for _ in range(interval):
                if stop_flag["stop"]:
                    break
                time_mod.sleep(1)
            # Self-restart on source change: if any watched .py in our
            # package has been edited since this process started, exit
            # cleanly so the service manager (NSSM on Windows, systemd
            # on Linux) respawns us with the new code. Without this,
            # editing `agent_cli.py` has no effect on the running
            # wrapper Ã¢â‚¬â€ the user would have to manually bounce the
            # service, which on Windows 11 requires admin and a slow
            # stop/start cycle. Detected within `interval` seconds of
            # the save.
            #
            # The check compares the source .py mtime against the
            # process start time, NOT against the previous tick. This
            # avoids a self-trigger loop where the running process's
            # imports regenerate .pyc files (changing their mtime but
            # not the .py mtime) and falsely restart us. We watch
            # agent_cli.py and agent_paths.py since those are the
            # only two hermes_orch modules the wrapper imports.
            #
            # Opt-out: set HERMES_WRAPPER_NO_SELF_RESTART=1 (useful
            # for production deployments where the supervisor manages
            # restarts itself).
            if os.environ.get("HERMES_WRAPPER_NO_SELF_RESTART") != "1":
                try:
                    _src_dir = os.path.dirname(os.path.abspath(__file__))
                    _watched = [
                        os.path.join(_src_dir, "agent_cli.py"),
                        os.path.join(_src_dir, "agent_paths.py"),
                    ]
                    _now = time_mod.time()
                    for _wp in _watched:
                        if not os.path.exists(_wp):
                            continue
                        if os.path.getmtime(_wp) > _self_start_ts:
                            click.echo(
                                f"[daemon] source changed ({os.path.basename(_wp)} "
                                f"mtime > start time) Ã¢â‚¬â€ exiting for self-restart"
                            )
                            # v3.12.5: exit code 1 (not 0) so Task Scheduler
                            # RecoveryConfig.RestartOnFailure can trigger a
                            # respawn. Previously the wrapper used `return`
                            # (clean exit 0), which leaves the service in a
                            # "Success" state -- Task Scheduler treats that
                            # as "nothing to do" and the wrapper stays down
                            # until the next reboot. NSSM (production target)
                            # does not care about exit code, so this only
                            # affects Task Scheduler deployments.
                            # HERMES_WRAPPER_NO_SELF_RESTART=1 still opts out.
                            sys.exit(1)
                except Exception as _e:
                    # Never let the watchdog crash the daemon
                    click.echo(f"[daemon] self-restart check error: {_e}")
    finally:
        click.echo("[daemon] stopped")


@cli.command()
def stop() -> None:
    """Stop the wrapper daemon.

    Note: this is a placeholder Ã¢â‚¬â€ the daemon is single-process, so to stop
    it just send Ctrl-C / SIGINT to the running process. For multi-process
    service (NSSM on Windows, systemd on Linux), use the OS service manager.
    """
    click.echo("To stop the daemon, send Ctrl-C / SIGINT to the running process.")
    click.echo("For service-managed daemons, use the OS service manager (sc/nssm on Windows, systemctl on Linux).")


@cli.command()
@click.option(
    "--config",
    "config_file",
    default="~/.hermes-orchestrator/wrapper-config.json",
    help="Path to wrapper-config.json",
)
def sync_config(config_file: str) -> None:
    """Sync wrapper-config.json from orchestrator + detected hermes profiles dir.

    Reads:
      - agent_id, secret_file, orchestrator_url from existing config
      - role list from orchestrator (GET /api/agents/{id})
      - hermes profiles dir from HERMES_PROFILES_DIR or OS auto-detect

    Writes a fresh config with all roles mapped to
    `<detected_dir>/<role>`. Existing custom roots are kept (only adds
    missing roles; doesn't overwrite).

    Idempotent Ã¢â‚¬â€ safe to run anytime (e.g., after adding a new profile in
    the dashboard).
    """
    import time as time_mod
    from hermes_orch.agent_paths import detect_hermes_profiles_dir

    cfg_path = Path(config_file).expanduser()
    if not cfg_path.exists():
        raise click.ClickException(
            f"Config not found: {cfg_path}. Run 'hermes-orch-agent register' first."
        )
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    agent_id = cfg.get("agent_id")
    orchestrator_url = cfg.get("orchestrator_url", "").rstrip("/")
    secret_path = Path(cfg.get("secret_file", "")).expanduser()
    if not agent_id or not orchestrator_url:
        raise click.ClickException("Config missing agent_id or orchestrator_url")
    if not secret_path.exists():
        raise click.ClickException(f"Secret file not found: {secret_path}")
    secret = secret_path.read_text(encoding="utf-8-sig").strip()

    # Fetch agent's role list from orchestrator
    try:
        r = _httpx_get(
            f"{orchestrator_url}/api/agents/{agent_id}",
            headers=_hmac_headers(
                agent_id, secret,
                method="GET",
                path=f"/api/agents/{agent_id}",
            ),
            timeout=10,
        )
        if r.status_code != 200:
            raise click.ClickException(
                f"Orchestrator returned {r.status_code}: {r.text[:200]}"
            )
        agent = r.json()
    except httpx.RequestError as e:
        raise click.ClickException(f"Cannot reach orchestrator: {e}")

    # Get the list of role names
    orch_roles = [p["name"] for p in agent.get("profiles", [])]
    click.echo(f"Orchestrator says agent '{agent_id}' has {len(orch_roles)} role(s): {orch_roles}")

    # Detect hermes profiles dir
    detected_dir = detect_hermes_profiles_dir()
    if detected_dir:
        click.echo(f"Detected hermes profiles dir: {detected_dir}")
    else:
        click.echo("WARN: no hermes profiles dir detected. Set HERMES_PROFILES_DIR.")

    # Merge: keep existing roots, add missing roles
    existing_profiles = cfg.get("profiles") or {}
    merged: dict = dict(existing_profiles)  # start with existing
    added: list[str] = []
    for role in orch_roles:
        if role in merged:
            continue  # don't overwrite user customization
        # Use template "<profiles_dir>/<role>" if detected, else leave empty
        if detected_dir:
            candidate = detected_dir / role
            if candidate.exists():
                merged[role] = {"root": f"<profiles_dir>/{role}"}
                added.append(role)
                continue
        # Last resort: absolute path guess
        merged[role] = {"root": f"./{role}"}  # daemon will try to resolve
        added.append(role)

    cfg["profiles"] = merged
    if detected_dir:
        cfg["_profiles_dir"] = str(detected_dir)

    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    click.echo(f"Wrote {cfg_path}")
    if added:
        click.echo(f"  added {len(added)} role(s): {added}")
    click.echo(f"  total profiles: {len(merged)}")
    if not detected_dir:
        click.echo("")
        click.echo("  NOTE: no HERMES_PROFILES_DIR detected. Set it to your hermes")
        click.echo("  profile directory so the daemon knows where to find each role.")
        click.echo("  Example: export HERMES_PROFILES_DIR=/home/<user>/.hermes/profiles")


@cli.command()
def status() -> None:
    """Show current wrapper status.

    Reads wrapper-config.json + secret file and reports:
    - Agent ID, orchestrator URL
    - Profile roots resolved (with HERMES_PROFILES_DIR + OS detection)
    - Last heartbeat (if we can ping the orchestrator)
    """
    import time as time_mod
    from hermes_orch.agent_paths import detect_hermes_profiles_dir, resolve_profile_root

    cfg_path = Path("~/.hermes-orchestrator/wrapper-config.json").expanduser()
    if not cfg_path.exists():
        click.echo(f"Config not found: {cfg_path}")
        click.echo("Run 'hermes-orch-agent register' first.")
        return
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    agent_id = cfg.get("agent_id")
    orchestrator_url = cfg.get("orchestrator_url", "").rstrip("/")
    secret_path = Path(cfg.get("secret_file", "")).expanduser()
    profiles_cfg = cfg.get("profiles") or {}

    click.echo(f"Config: {cfg_path}")
    click.echo(f"  agent_id: {agent_id}")
    click.echo(f"  orchestrator: {orchestrator_url}")
    click.echo(f"  secret: {secret_path} ({'exists' if secret_path.exists() else 'MISSING'})")
    click.echo(f"  profiles: {len(profiles_cfg)}")
    hermes_profiles_dir = detect_hermes_profiles_dir()
    if hermes_profiles_dir:
        click.echo(f"  hermes profiles dir: {hermes_profiles_dir}")
    for role, pcfg in profiles_cfg.items():
        try:
            root = resolve_profile_root(pcfg["root"], role, profiles_dir=hermes_profiles_dir)
            exists = root.exists()
            click.echo(f"    {role}: {root}  [{'OK' if exists else 'MISSING'}]")
        except FileNotFoundError as e:
            click.echo(f"    {role}: {e}")

    # Try a heartbeat to see if we can reach the orchestrator
    if secret_path.exists() and orchestrator_url:
        secret = secret_path.read_text(encoding="utf-8-sig").strip()
        try:
            _hb_body = json.dumps({"status": "idle"}).encode("utf-8")
            r = _httpx_post(
                f"{orchestrator_url}/api/agents/{agent_id}/heartbeat",
                headers=_hmac_headers(
                    agent_id, secret,
                    method="POST",
                    path=f"/api/agents/{agent_id}/heartbeat",
                    body=_hb_body,
                ),
                content=_hb_body,
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                click.echo(f"\nOrchestrator OK. tasks pending for this agent: {len(data.get('tasks', []))}")
            else:
                click.echo(f"\nOrchestrator returned {r.status_code}: {r.text[:200]}")
        except httpx.RequestError as e:
            click.echo(f"\nCannot reach orchestrator: {e}")


# ===== Config apply (profile_configs table) =====


@cli.command("apply-configs")
@click.option(
    "--config",
    "config_path",
    default="~/.hermes-orchestrator/wrapper-config.json",
    help="Path to wrapper config JSON",
)
@click.option(
    "--profile",
    "profile_filter",
    default=None,
    help="Only apply for this profile name (default: all in config)",
)
def apply_configs(config_path: str, profile_filter: str | None) -> None:
    """One-shot: poll orchestrator for pending profile configs and apply them.

    For each profile, calls GET /configs/pending. If a config is returned,
    writes the file atomically to <profile.root>/<config.file_path> and acks.

    Idempotent - safe to run repeatedly.
    """
    cfg = _load_wrapper_config(Path(config_path).expanduser())
    secret = _read_secret(Path(cfg["secret_file"]))
    agent_id_for_apply = cfg["agent_id"]
    base = cfg["orchestrator_url"].rstrip("/")
    profiles = cfg.get("profiles", {})

    if profile_filter:
        if profile_filter not in profiles:
            raise click.ClickException(f"Profile not in config: {profile_filter}")
        profiles = {profile_filter: profiles[profile_filter]}

    if not profiles:
        click.echo("No profiles configured; nothing to do.")
        return

    applied_count = 0
    with httpx.Client(timeout=30, verify=_agent_http_verify()) as client:
        for pname, pcfg in profiles.items():
            root = Path(pcfg["root"])
            click.echo(f"[{pname}] root = {root}")
            # Drain all pending for this profile (in case multiple are queued)
            while True:
                try:
                    cfg_row = _claim_one(client, base, agent_id_for_apply, pname, secret)
                except Exception as e:
                    click.echo(f"  poll error: {e}", err=True)
                    break
                if cfg_row is None:
                    break
                # Apply
                target = root / cfg_row["file_path"]
                try:
                    # Safety: file_path must stay inside root
                    target_resolved = target.resolve()
                    root_resolved = root.resolve()
                    try:
                        target_resolved.relative_to(root_resolved)
                    except ValueError:
                        raise RuntimeError(
                            f"refusing to write outside profile root: {target} (root={root})"
                        )
                    content = cfg_row["desired_content"] or ""
                    if content == "" and cfg_row["file_path"].startswith("skills/"):
                        # Empty content for a skills/ path = delete on host.
                        # Folder layout (skills/<name>/SKILL.md) Ã¢â€ â€™ rmtree the
                        # whole folder. Flat layout (skills/<name>.md) Ã¢â€ â€™
                        # rmtree the whole skill folder
                        skill_folder = target_resolved.parent
                        if skill_folder.exists() and skill_folder.is_dir():
                            import shutil as _shutil
                            _shutil.rmtree(skill_folder)
                            click.echo(f"  deleted folder {skill_folder}")
                        else:
                            click.echo(f"  delete folder {skill_folder} (already absent)")
                        actual_sha = hashlib.sha256(b"").hexdigest()
                    else:
                        _atomic_write(target, content)
                        actual_sha = hashlib.sha256(content.encode()).hexdigest()
                        click.echo(f"  wrote {target} (sha={actual_sha[:12]}...)")
                    _ack(client, base, agent_id_for_apply, pname, cfg_row["id"],
                         "applied", actual_sha=actual_sha, secret=secret)
                    applied_count += 1
                except Exception as e:
                    click.echo(f"  write failed: {e}", err=True)
                    try:
                        _ack(client, base, agent_id_for_apply, pname, cfg_row["id"],
                             "failed", error=str(e), secret=secret)
                    except Exception as ee:
                        click.echo(f"  ack-failed also failed: {ee}", err=True)
                    break  # don't keep trying if writes are failing
    click.echo(f"done; applied={applied_count}")


@cli.command("apply-configs-loop")
@click.option(
    "--config",
    "config_path",
    default="~/.hermes-orchestrator/wrapper-config.json",
    help="Path to wrapper config JSON",
)
@click.option("--interval", default=5, help="Poll interval seconds")
def apply_configs_loop(config_path: str, interval: int) -> None:
    """Daemon: keep polling and applying profile configs every N seconds."""
    click.echo(f"loop mode: interval={interval}s (Ctrl-C to stop)")
    while True:
        # Re-invoke apply-configs; click doesn't easily recurse to other cmd.
        try:
            apply_configs.callback(config_path=config_path, profile_filter=None)  # type: ignore[attr-defined]
        except SystemExit:
            pass
        time.sleep(interval)


def _claim_one(
    client: httpx.Client, base: str, agent_id: str, profile: str, secret: str
) -> dict | None:
    path = f"/api/agents/{agent_id}/profiles/{profile}/configs/pending"
    headers = _hmac_headers(agent_id, secret, method="GET", path=path)
    r = client.get(
        f"{base}{path}",
        headers=headers,
    )
    r.raise_for_status()
    if r.status_code == 200 and r.content and r.json():
        return r.json()
    return None


def _ack(
    client: httpx.Client,
    base: str,
    agent_id: str,
    profile: str,
    cfg_id: str,
    status: str,
    *,
    actual_sha: str | None = None,
    error: str | None = None,
    secret: str,
) -> None:
    body: dict = {"status": status}
    if actual_sha:
        body["actual_sha256"] = actual_sha
    if error:
        body["error"] = error
    body_bytes = json.dumps(body).encode("utf-8")
    path = f"/api/agents/{agent_id}/profiles/{profile}/configs/{cfg_id}/ack"
    headers = _hmac_headers(agent_id, secret, method="POST", path=path, body=body_bytes)
    r = client.post(
        f"{base}{path}",
        content=body_bytes,
        headers=headers,
    )
    r.raise_for_status()


if __name__ == "__main__":
    cli()


