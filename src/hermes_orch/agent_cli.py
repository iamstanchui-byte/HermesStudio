"""Agent-side CLI (hermes-orch-agent command).

Commands (per REVIEW.md §8.1):
- register            Register with orchestrator (get secret, write to .secret file)
- start               Start the wrapper daemon (heartbeat + ready for tasks)
- stop                Stop the daemon
- status              Show current status
- apply-configs       One-shot: poll orchestrator for pending profile configs and apply
- apply-configs-loop  Daemon: keep polling and applying

Implementation note: this is a SEPARATE process from the orchestrator.
On Windows: registered as Windows Service via NSSM.
On Linux:   registered as systemd service.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import platform
import re
import time
from pathlib import Path

import click
import httpx

import shutil

from hermes_orch.agent_paths import detect_hermes_profiles_dir


# ===== Helpers =====


def _resolve_hermes_bin() -> str | None:
    """Find the hermes CLI binary.

    Order:
      1. HERMES_BIN env var (user override, takes absolute path)
      2. shutil.which("hermes") — uses current PATH
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
    return secret_path.read_text(encoding="utf-8").strip()


# Cap on the cleaned task summary stored in the DB. 32KB is enough for
# long research / backtest tasks; a "Show full" button on the dashboard
# lets the user read the whole thing in a scrollable block.
MAX_SUMMARY_CHARS = 32_000


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

def _hmac_headers(agent_id: str, secret: str) -> dict:
    import time as _t
    import hashlib as _h
    return {
        "X-Agent-Id": agent_id,
        "X-Timestamp": str(int(_t.time())),
        "X-Signature": _h.sha256(secret.encode()).hexdigest(),
    }


def _fetch_project_state_http(
    orchestrator_url: str, agent_id: str, secret: str, project_id: str
) -> str | None:
    """Fetch the project's L3 (state.md) via HTTP API. None if missing.

    Returns the full state.md content (no truncation; we apply 2KB cap
    client-side via simple char count, since L3 is already capped at
    2KB by the synthesis module).
    """
    try:
        r = httpx.get(
            f"{orchestrator_url}/api/projects/{project_id}/memory/state",
            headers=_hmac_headers(agent_id, secret),
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
        content += "\n[…truncated…]"
    return content


def _fetch_user_recent_http(
    orchestrator_url: str, agent_id: str, secret: str
) -> str | None:
    """Fetch the user-level L3 (recent.md) via HTTP API. None if missing.

    Returns the recent.md content (4KB cap). Used for cross-project
    context: "what has the user been up to lately".
    """
    try:
        r = httpx.get(
            f"{orchestrator_url}/api/projects/memory/recent",
            headers=_hmac_headers(agent_id, secret),
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
        content += "\n[…recent truncated…]"
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
        r = httpx.get(
            f"{orchestrator_url}/api/projects/{project_id}/memory/facts",
            headers=_hmac_headers(agent_id, secret),
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

    We keep the first MAX_SUMMARY_CHARS chars (32KB by default — large
    enough for research / backtest / multi-step tasks; small enough to
    keep the DB row light). The dashboard's "Show full" button + max-h-96
    scroll lets the user read the whole thing.
    """
    import re
    s = stdout[:MAX_SUMMARY_CHARS]
    # Strip ANSI escape codes (color, cursor moves, etc.)
    s = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)
    # Strip the box-drawing borders that wrap "Hermes" tool headers.
    # First: long runs of box-drawing chars (─━━╭╮╰╯│...).
    s = re.sub(r"[─━╭╮╰╯│┌┐└┘├┤┬┴┼]{4,}", "[…]", s)
    # Then: drop lines that are mostly box-drawing chrome
    # (e.g. individual `╭─ ⚕ Hermes ─...╮` lines, or short `╰─...╯`).
    s = re.sub(r"^\s*[╭╮╰╯│┌┐└┘├┤┬┴┼─━┃━╾╼]+\s*[^\n]*$", "", s, flags=re.MULTILINE)
    # Drop the standalone "⚕ Hermes" brand mark on its own line.
    s = re.sub(r"^\s*⚕\s*Hermes[^\n]*$", "", s, flags=re.MULTILINE)
    # Strip hermes' "tool call" rows like "┊ 💻 preparing terminal…"
    # and "┊ 💻 $         curl ... 1.2s". These dominate the output.
    s = re.sub(r"^\s*┊\s*[^\n]*$", "", s, flags=re.MULTILINE)
    # Strip the trailing session metadata block that hermes prints on
    # exit. Everything from "Resume this session with:" to EOF is noise
    # for a human reading the task summary.
    s = re.sub(
        r"\n*Resume this session with:.*$",
        "\n[…session metadata stripped…]",
        s,
        flags=re.DOTALL,
    )
    # Also strip the equivalent "Session: <id>" / "Duration:" / "Messages:"
    # block that sometimes appears at the start of the output.
    s = re.sub(
        r"^Session:\s+\S+.*?Messages:\s+\d+.*$",
        "[…session metadata stripped…]",
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


def _strip_prompt_echo(s: str) -> str:
    """Strip the prompt template echo that hermes prepends to its output.

    The wrapper builds the prompt as:
        {action}({params})\\n\\n--- PROJECT CONTEXT ---\\n{context_block}\\n--- END CONTEXT ---

    Hermes echoes this prompt at the top of its stdout (because it was the
    system message it received), so without stripping, the orchestrator's
    task summary contains "Query: create_file(...) --- PROJECT CONTEXT ---
    LOCAL WORKING DIR: ... SKILL SELF-TEACHING..." which is just noise
    from the human reader's perspective and pollutes the L2 (facts.md)
    Task Results section that gets injected into future task prompts.

    Strategy: only strip if the markers are near the start (first ~1500
    chars) -- if the body of the analysis references these strings later
    in the output, we leave them alone.
    """
    head = s[:1500]
    # If we find the closing marker near the start, drop everything up to it
    m = re.search(r"\s*--- END CONTEXT ---\s*\n", head)
    if m:
        return s[m.end():].lstrip()
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
    raises — heartbeat must not break on a single bad config.yaml.

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
            col_list = ", ".join(cols)
            # Don't filter by ended_at — most hermes sessions don't commit
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
@click.option("--roles", default=None, help="Comma-separated roles (e.g. data-analyst,backtest-runner). Optional — defaults to auto-detected hermes profile names if --profiles-dir is set or HERMES_PROFILES_DIR is set.")
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
        r = httpx.post(
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
            click.echo("    Skeleton will use '~' templates — edit later.")

    # Write wrapper-config.json skeleton
    if not config_file:
        config_file = str(Path.home() / ".hermes-orchestrator" / "wrapper-config.json")
    cfg_path = Path(config_file)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg_path.exists():
        # Don't clobber an existing config — just inform
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
                    # Use template — daemon will warn if it can't find this role
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
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
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
        click.echo("WARN: no profiles in wrapper-config.json — wrapper will idle")

    secret = secret_path.read_text(encoding="utf-8").strip()
    if not secret:
        raise click.ClickException(f"Secret file {secret_path} is empty")

    # Auto-sync config from orchestrator (picks up newly added roles)
    if not no_sync:
        click.echo("[daemon] syncing config from orchestrator...")
        # Reuse the sync-config logic by calling it inline (it's small enough)
        try:
            r = httpx.get(
                f"{orchestrator_url}/api/agents/{agent_id}",
                headers={
                    "X-Agent-Id": agent_id,
                    "X-Timestamp": str(int(time_mod.time())),
                    "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
                },
                timeout=10,
            )
            if r.status_code == 200:
                agent = r.json()
                orch_roles = [p["name"] for p in agent.get("profiles", [])]
                existing = cfg.get("profiles") or {}
                added = [r for r in orch_roles if r not in existing]
                if added:
                    # Re-read config in case another process wrote it
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
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

    def _auth_headers() -> dict:
        return {
            "X-Agent-Id": agent_id,
            "X-Timestamp": str(int(time_mod.time())),
            # Real HMAC TODO; this is the placeholder the orchestrator accepts
            "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
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
            r = httpx.post(
                f"{orchestrator_url}/api/agents/{agent_id}/heartbeat",
                headers=_auth_headers(),
                json={
                    "status": "idle",
                    "profiles": profile_meta,
                },
                timeout=10,
            )
            if r.status_code != 200:
                click.echo(f"[daemon] heartbeat {r.status_code}: {r.text[:200]}")
                return [], []
            body = r.json() or {}
            return body.get("tasks", []), body.get("cleanup_session_ids", []) or []
        except httpx.RequestError as e:
            click.echo(f"[daemon] heartbeat failed: {e}")
            return [], []

    def _cleanup_local_sessions(session_ids: list[str]) -> None:
        """Run `hermes sessions delete <id> --yes` for each session ID,
        then POST /cleanup-ack to the orchestrator so the DB row flips
        from pending_cleanup to deleted. Best-effort: a hermes delete
        failure (e.g. session already gone) is non-fatal — we still
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
                    # "Session not found" / "no such session" is fine —
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
            # Always ack — even on failure, we don't want the row stuck
            # in pending_cleanup. The audit log retains the failure
            # context via the stdout/stderr we just printed.
            try:
                httpx.post(
                    f"{orchestrator_url}/api/agents/{agent_id}/sessions/{sid}/cleanup-ack",
                    headers=_auth_headers(),
                    timeout=10,
                )
            except Exception as e:
                click.echo(f"[daemon] cleanup-ack failed for {sid}: {e}")

    def _claim(task_id: str) -> bool:
        """Atomically flip task from 'assigned' to 'running'."""
        try:
            r = httpx.post(
                f"{orchestrator_url}/api/tasks/{task_id}/start",
                headers=_auth_headers(),
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
            r = httpx.post(
                f"{orchestrator_url}/api/tasks/{task_id}/result",
                headers=_auth_headers(),
                json=result,
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
                r = httpx.get(
                    f"{orchestrator_url}/api/projects/{project_id}/files/{rel}",
                    headers={
                        "X-Agent-Id": agent_id,
                        "X-Timestamp": str(int(time_mod.time())),
                        "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
                    },
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
        # This is the n8n-style "how to do this workflow" doc — the agent
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
                    r = httpx.get(
                        f"{orchestrator_url}/api/tasks/{parent_id}",
                        headers={
                            "X-Agent-Id": agent_id,
                            "X-Timestamp": str(int(time_mod.time())),
                            "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
                        },
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

        # Build prompt for hermes
        params_str = json.dumps(params, ensure_ascii=False) if params else "{}"
        if context_block:
            prompt = f"{action}({params_str})\n\n--- PROJECT CONTEXT ---\n{context_block}\n--- END CONTEXT ---"
        else:
            prompt = f"{action}({params_str})"
        click.echo(f"[daemon] task {tid}: running")
        click.echo(f"  role={role}  profile_root={profile_root}")
        click.echo(f"  cache_dir={cache_dir}")
        click.echo(f"  output_path={output_path}")
        click.echo(f"  prompt={prompt[:200]}")

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
                r = httpx.get(
                    f"{orchestrator_url}/api/projects/{project_id}/session",
                    params={"role": role},
                    headers={
                        "X-Agent-Id": agent_id,
                        "X-Timestamp": str(int(time_mod.time())),
                        "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
                    },
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

        # Use Popen + communicate (NOT subprocess.run with capture_output=True).
        # On Windows, subprocess.run's reader threads can throw OSError after
        # the child exits, crashing the daemon. Popen + communicate is safer.
        #
        # Use bytes mode (text=False) + explicit UTF-8 decode. On Windows,
        # `text=True` would use cp1252 and choke on hermes's UTF-8 / box-drawing
        # / Chinese output. With bytes mode, we get raw bytes and can decode
        # safely with errors='replace'.
        try:
            # Run hermes in the local cache dir so file tools work there
            # (the agent sees a real filesystem; we sync via API).
            hermes_cwd = str(cache_dir) if cache_dir else str(profile_root)
            proc = subprocess.Popen(
                hermes_args,
                cwd=hermes_cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # env=...: force UTF-8 output from hermes even on Windows
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            )
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
                    r = httpx.post(
                        f"{orchestrator_url}/api/tasks/{tid}/poll",
                        headers={
                            "X-Agent-Id": agent_id,
                            "X-Timestamp": str(int(time_mod.time())),
                            "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
                        },
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

        try:
            try:
                raw_stdout, raw_stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                # stop the liveness poller before any other handling
                stop_poll.set()
                return {"status": "failed", "error": f"hermes timeout after {timeout}s"}
            # Decode bytes as UTF-8, replacing bad chars (Windows consoles can
            # produce mixed encodings in edge cases)
            try:
                stdout = raw_stdout.decode("utf-8", errors="replace")
            except Exception:
                stdout = raw_stdout.decode("cp1252", errors="replace")
            try:
                stderr = raw_stderr.decode("utf-8", errors="replace")
            except Exception:
                stderr = raw_stderr.decode("cp1252", errors="replace")
            stdout = (stdout or "").strip()
            stderr = (stderr or "").strip()
            rc = proc.returncode
            click.echo(f"  exit={rc} stdout_len={len(stdout)} stderr_len={len(stderr)}")

            # Save session_id to the project for future resume.
            # Hermes prints "Session: <id>" near the end of its output.
            if rc == 0 and project_id:
                import re
                m = re.search(r"Session:\s+(\S+)", stdout)
                if m:
                    new_sid = m.group(1).rstrip(".,;:")
                    if new_sid and not new_sid.startswith("-"):
                        try:
                            httpx.post(
                                f"{orchestrator_url}/api/projects/{project_id}/session",
                                headers={
                                    "X-Agent-Id": agent_id,
                                    "X-Timestamp": str(int(time_mod.time())),
                                    "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
                                },
                                json={"session_id": new_sid, "role": role},
                                timeout=10,
                            )
                            click.echo(f"  saved session: {new_sid}")
                        except Exception as e:
                            click.echo(f"  WARN: session save failed: {e}")

            if rc == 0:
                summary = _clean_hermes_output(stdout) if stdout else "(no output)"
                result = {"status": "completed", "summary": summary}
                # If task declared an output_path: check local cache, upload
                # to orchestrator via PUT file API, then attach artifact meta.
                if cache_dir and output_path:
                    output_local = cache_dir / output_path
                    if output_local.exists() and output_local.is_file():
                        file_bytes = output_local.read_bytes()
                        file_sha = hashlib.sha256(file_bytes).hexdigest()
                        # Upload to orchestrator (relative path)
                        rel = output_path.lstrip("/").replace("\\", "/")
                        try:
                            r = httpx.put(
                                f"{orchestrator_url}/api/projects/{project_id}/files/{rel}",
                                content=file_bytes,
                                headers={
                                    "X-Agent-Id": agent_id,
                                    "X-Timestamp": str(int(time_mod.time())),
                                    "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
                                },
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
                if cache_dir and project_id:
                    artifacts_extra = []
                    try:
                        cache_root = cache_dir.resolve()
                        # Find files modified during this task run. We use a
                        # generous cutoff: 5 min before the task started (so
                        # parent tasks' cached files are also captured) and
                        # now as the upper bound.
                        from datetime import datetime, timezone, timedelta
                        # Best-effort time filter (skip if mtime is unreliable)
                        for f in cache_root.rglob("*"):
                            if not f.is_file():
                                continue
                            # Skip our own internal test files and the cache
                            # structure (we use relative paths for the upload)
                            rel = f.relative_to(cache_root).as_posix()
                            if rel in (output_path,):
                                continue  # already uploaded
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
                            try:
                                file_bytes = f.read_bytes()
                            except Exception:
                                continue
                            if not file_bytes:
                                continue
                            file_sha = hashlib.sha256(file_bytes).hexdigest()
                            try:
                                r2 = httpx.put(
                                    f"{orchestrator_url}/api/projects/{project_id}/files/{rel}",
                                    content=file_bytes,
                                    headers={
                                        "X-Agent-Id": agent_id,
                                        "X-Timestamp": str(int(time_mod.time())),
                                        "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
                                    },
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
                    # Merge extra artifacts into the result
                    if artifacts_extra:
                        result.setdefault("artifacts", []).extend(artifacts_extra)
                # Capture hermes session tokens (input/output/cache/etc) from
                # the profile's state.db so the orchestrator can record real
                # per-task token usage. Best-effort: returns None if the
                # schema is older or the session isn't committed yet.
                token_usage = _capture_session_tokens(profile_root)
                if token_usage:
                    result["token_usage"] = token_usage
                    click.echo(
                        f"  tokens: in={token_usage.get('prompt_tokens', 0)} "
                        f"out={token_usage.get('completion_tokens', 0)} "
                        f"cache_r={token_usage.get('cache_read_tokens', 0)} "
                        f"cache_w={token_usage.get('cache_write_tokens', 0)}"
                    )
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
            # Stop the liveness poller before returning
            stop_poll.set()
            return failed_result
        except Exception as e:
            # Catch-all: Popen.communicate can throw on Windows if the child
            # process closes pipes abruptly. Treat as a task failure.
            try:
                proc.kill()
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
    _SKILL_FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
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

        The wrapper is the source of truth for what's on disk — if a skill
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
        # but we'll only ever push new/upsert here — deletes are dashboard-driven)
        try:
            r = client.get(
                f"{orchestrator_url}/api/agents/{agent_id}/profiles/{pname}/skills?include_deleted=1",
                headers=_auth_headers(),
                timeout=10,
            )
            r.raise_for_status()
            db_skills = {s["name"]: s for s in r.json() or []}
        except Exception as e:
            click.echo(f"{log_prefix} ({pname}) DB fetch error: {e}")
            return 0
        registered = 0
        try:
            # Two layouts are supported:
            #   1. Flat file: skills/<name>.md → register <name>
            #   2. Folder:   skills/<name>/SKILL.md → register <name> (we
            #      only sync the SKILL.md content; references/ and scripts/
            #      siblings are NOT registered, they're treated as opaque
            #      files in the agent host's filesystem).
            # Flat-file skill support dropped 2026-07-19 (commit d5b7c9a).
            # Hermes 0.17+ only reads skills/<name>/SKILL.md, so we no
            # longer scan for skills/<name>.md at the top level.
            for entry in sorted(skills_dir.iterdir()):
                if not entry.is_dir():
                    continue  # skip files (no more flat skills)
                if not _SKILL_FOLDER_RE.match(entry.name):
                    continue
                file_path = entry / "SKILL.md"
                if not file_path.exists() or not file_path.is_file():
                    # Folder exists but no SKILL.md — skip silently
                    # (could be a partial install or a non-skill dir)
                    continue
                name = entry.name
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
                # unchanged — don't re-post. This is content-addressed so
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
                # New or changed file — push to orchestrator. The orchestrator
                # creates a pending row, we immediately ack as applied (file
                # is already on disk), so the apply-pending loop won't try
                # to write the file again.
                try:
                    content_text = file_bytes.decode("utf-8", errors="replace")
                except Exception:
                    content_text = file_bytes.decode("cp1252", errors="replace")
                r = client.post(
                    f"{orchestrator_url}/api/agents/{agent_id}/profiles/{pname}/skills",
                    headers={
                        **_auth_headers(),
                        "X-Skill-Source": "self-taught",
                    },
                    json={"name": name, "content": content_text},
                    timeout=15,
                )
                if r.status_code == 201:
                    cfg_row = r.json()
                    # Immediately ack as applied
                    try:
                        client.post(
                            f"{orchestrator_url}/api/agents/{agent_id}/profiles/{pname}/configs/{cfg_row['id']}/ack",
                            headers=_auth_headers(),
                            json={"status": "applied", "actual_sha256": sha},
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

    def _apply_pending_configs_inline() -> int:
        """Drain pending profile_configs from the orchestrator and apply.

        Returns the number of configs applied. Runs cheaply (no file I/O
        when nothing pending) so we can call it every tick.
        """
        if not profiles_cfg:
            return 0
        applied = 0
        try:
            with httpx.Client(timeout=10) as client:
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
                                headers=_auth_headers(),
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
                                with httpx.Client(timeout=30) as sync_client:
                                    n = _sync_one_profile_skills(sync_client, pname, pcfg)
                                click.echo(
                                    f"[daemon] sync-skills trigger for {pname}: "
                                    f"{n} new skill(s) registered"
                                )
                                actual_sha = hashlib.sha256(
                                    f"sync:{n}".encode()
                                ).hexdigest()
                                ack = client.post(
                                    f"{orchestrator_url}/api/agents/{agent_id}"
                                    f"/profiles/{pname}/configs/{cfg_row['id']}/ack",
                                    headers=_auth_headers(),
                                    json={"status": "applied", "actual_sha256": actual_sha},
                                    timeout=10,
                                )
                                ack.raise_for_status()
                                applied += 1
                                continue
                            # Safety check: file_path must resolve inside `root`
                            # (defense-in-depth — the API also validates skill
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
                                # Folder layout: skills/<name>/SKILL.md →
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
                            ack = client.post(
                                f"{orchestrator_url}/api/agents/{agent_id}"
                                f"/profiles/{pname}/configs/{cfg_row['id']}/ack",
                                headers=_auth_headers(),
                                json={"status": "applied", "actual_sha256": actual_sha},
                                timeout=10,
                            )
                            ack.raise_for_status()
                            applied += 1
                        except Exception as e:
                            click.echo(f"[daemon] config apply error: {e}")
                            try:
                                client.post(
                                    f"{orchestrator_url}/api/agents/{agent_id}"
                                    f"/profiles/{pname}/configs/{cfg_row['id']}/ack",
                                    headers=_auth_headers(),
                                    json={"status": "failed", "error": str(e)},
                                    timeout=10,
                                )
                            except Exception:
                                pass
                            break
        except Exception as e:
            click.echo(f"[daemon] config sync outer error: {e}")
        return applied

    try:
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
            for t in assigned:
                if stop_flag["stop"]:
                    break
                if not _claim(t["id"]):
                    continue
                result = _run_task(t)
                _submit_result(t["id"], result)
                if once:
                    click.echo("[daemon] --once: exiting after one task")
                    return
            if once and not assigned:
                click.echo("[daemon] --once: no assigned tasks found, exiting")
                return
            # Apply pending profile configs (e.g. soul.md) every tick.
            # Cheap when nothing pending; no separate loop needed.
            _apply_pending_configs_inline()
            # Periodic auto-sync of skills from disk (throttled per profile).
            # Catches self-taught skills the agent wrote into skills/ without
            # going through the dashboard. We use a single short-lived client
            # and only run if enough time has passed since the last sync for
            # this profile — the file scan is cheap but no point doing it
            # every 5s.
            now_ts = time_mod.time()
            with httpx.Client(timeout=30) as sync_client:
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
    finally:
        click.echo("[daemon] stopped")


@cli.command()
def stop() -> None:
    """Stop the wrapper daemon.

    Note: this is a placeholder — the daemon is single-process, so to stop
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

    Idempotent — safe to run anytime (e.g., after adding a new profile in
    the dashboard).
    """
    import time as time_mod
    from hermes_orch.agent_paths import detect_hermes_profiles_dir

    cfg_path = Path(config_file).expanduser()
    if not cfg_path.exists():
        raise click.ClickException(
            f"Config not found: {cfg_path}. Run 'hermes-orch-agent register' first."
        )
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    agent_id = cfg.get("agent_id")
    orchestrator_url = cfg.get("orchestrator_url", "").rstrip("/")
    secret_path = Path(cfg.get("secret_file", "")).expanduser()
    if not agent_id or not orchestrator_url:
        raise click.ClickException("Config missing agent_id or orchestrator_url")
    if not secret_path.exists():
        raise click.ClickException(f"Secret file not found: {secret_path}")
    secret = secret_path.read_text(encoding="utf-8").strip()

    # Fetch agent's role list from orchestrator
    try:
        r = httpx.get(
            f"{orchestrator_url}/api/agents/{agent_id}",
            headers={
                "X-Agent-Id": agent_id,
                "X-Timestamp": str(int(time_mod.time())),
                "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
            },
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
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
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
        secret = secret_path.read_text(encoding="utf-8").strip()
        try:
            r = httpx.post(
                f"{orchestrator_url}/api/agents/{agent_id}/heartbeat",
                headers={
                    "X-Agent-Id": agent_id,
                    "X-Timestamp": str(int(time_mod.time())),
                    "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
                },
                json={"status": "idle"},
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
    headers = _auth_headers(secret)
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
    with httpx.Client(timeout=30) as client:
        for pname, pcfg in profiles.items():
            root = Path(pcfg["root"])
            click.echo(f"[{pname}] root = {root}")
            # Drain all pending for this profile (in case multiple are queued)
            while True:
                try:
                    cfg_row = _claim_one(client, base, cfg["agent_id"], pname, headers)
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
                        # Folder layout (skills/<name>/SKILL.md) → rmtree the
                        # whole folder. Flat layout (skills/<name>.md) →
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
                    _ack(client, base, cfg["agent_id"], pname, cfg_row["id"],
                         "applied", actual_sha=actual_sha, headers=headers)
                    applied_count += 1
                except Exception as e:
                    click.echo(f"  write failed: {e}", err=True)
                    try:
                        _ack(client, base, cfg["agent_id"], pname, cfg_row["id"],
                             "failed", error=str(e), headers=headers)
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


def _auth_headers(secret: str) -> dict[str, str]:
    """HMAC-style headers (per REVIEW §6.1).

    For MVP, just stamp the headers so the orchestrator accepts the request.
    Real HMAC verification TODO.
    """
    return {
        "X-Agent-Id": "wrapper",
        "X-Timestamp": str(int(time.time())),
        "X-Signature": hashlib.sha256(secret.encode()).hexdigest(),
    }


def _claim_one(
    client: httpx.Client, base: str, agent_id: str, profile: str, headers: dict
) -> dict | None:
    r = client.get(
        f"{base}/api/agents/{agent_id}/profiles/{profile}/configs/pending",
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
    headers: dict,
) -> None:
    body: dict = {"status": status}
    if actual_sha:
        body["actual_sha256"] = actual_sha
    if error:
        body["error"] = error
    r = client.post(
        f"{base}/api/agents/{agent_id}/profiles/{profile}/configs/{cfg_id}/ack",
        json=body,
        headers=headers,
    )
    r.raise_for_status()


if __name__ == "__main__":
    cli()
