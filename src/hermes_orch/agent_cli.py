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


def _atomic_write(target: Path, content: str) -> None:
    """Atomic write: write to .tmp then rename. Survives partial writes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    # On Windows, Path.replace is atomic if both files on same volume.
    tmp.replace(target)


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

    def _heartbeat() -> list[dict]:
        try:
            r = httpx.post(
                f"{orchestrator_url}/api/agents/{agent_id}/heartbeat",
                headers=_auth_headers(),
                json={"status": "idle"},
                timeout=10,
            )
            if r.status_code != 200:
                click.echo(f"[daemon] heartbeat {r.status_code}: {r.text[:200]}")
                return []
            return r.json().get("tasks", [])
        except httpx.RequestError as e:
            click.echo(f"[daemon] heartbeat failed: {e}")
            return []

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

        # Session resume: if the project has a current_session_id, pass
        # --resume so the agent has context from prior tasks. This is critical
        # for multi-step workflows (e.g. synth task reading the data tasks did).
        if project_id:
            try:
                r = httpx.get(
                    f"{orchestrator_url}/api/projects/{project_id}/session",
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
                        click.echo(f"  resuming session: {sid}")
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

        try:
            try:
                raw_stdout, raw_stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
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
                summary = stdout[:8000] if stdout else "(no output)"
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
                return result
            return {
                "status": "failed",
                "error": (stderr or stdout or f"hermes exited {rc}")[:8000],
            }
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
    # agent self-taught skills (any *.md the agent drops into skills/ shows
    # up in orchestrator after the next periodic scan).
    _SKILL_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.md$")
    # Hermes skills can be either a flat file (skills/<name>.md) or a
    # folder (skills/<name>/SKILL.md, optionally with references/ + scripts/
    # siblings). We register the SKILL.md entry to the orchestrator so
    # the planner sees the skill; the auxiliary files in the folder are
    # synced as well, but the SKILL.md is what the agent reads at runtime.
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
            for entry in sorted(skills_dir.iterdir()):
                if entry.is_file():
                    if not _SKILL_FILE_RE.match(entry.name):
                        continue
                    name = entry.stem
                    file_path = entry
                elif entry.is_dir():
                    if not _SKILL_FOLDER_RE.match(entry.name):
                        continue
                    file_path = entry / "SKILL.md"
                    if not file_path.exists() or not file_path.is_file():
                        # Folder exists but no SKILL.md — skip silently
                        # (could be a partial install or a non-skill dir)
                        continue
                    name = entry.name
                else:
                    continue
                try:
                    file_bytes = file_path.read_bytes()
                except Exception as e:
                    click.echo(f"{log_prefix} ({pname}/{name}) read error: {e}")
                    continue
                if not file_bytes:
                    continue  # skip empty files
                sha = hashlib.sha256(file_bytes).hexdigest()
                # Compare to current DB state. The list endpoint returns
                # `size` (length of desired_content) — close enough for
                # change detection without a sha roundtrip. (A real
                # byte-identical change would be a stretch.)
                db_skill = db_skills.get(name)
                if (
                    db_skill
                    and db_skill.get("status") in ("applied", "pending", "applying")
                    and db_skill.get("size") == len(file_bytes)
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
                                # Two layouts:
                                #   1. Flat file: skills/<name>.md → unlink
                                #   2. Folder:   skills/<name>/SKILL.md →
                                #      rmtree the whole <name> folder
                                #      (we only register SKILL.md; aux files
                                #      in the folder are owned by the user)
                                if cfg_row["file_path"].endswith("/SKILL.md"):
                                    # Folder layout: nuke the whole folder
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
                                else:
                                    # Flat file layout
                                    if target_resolved.exists():
                                        target_resolved.unlink()
                                        click.echo(
                                            f"[daemon] deleted {cfg_row['file_path']} "
                                            f"from {pname}"
                                        )
                                    else:
                                        click.echo(
                                            f"[daemon] delete {cfg_row['file_path']} "
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
        while not stop_flag["stop"]:
            tasks = _heartbeat()
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
                        # unlink the single file.
                        if cfg_row["file_path"].endswith("/SKILL.md"):
                            skill_folder = target_resolved.parent
                            if skill_folder.exists() and skill_folder.is_dir():
                                import shutil as _shutil
                                _shutil.rmtree(skill_folder)
                                click.echo(f"  deleted folder {skill_folder}")
                            else:
                                click.echo(f"  delete folder {skill_folder} (already absent)")
                        else:
                            # Flat file layout
                            if target_resolved.exists():
                                target_resolved.unlink()
                                click.echo(f"  deleted {target}")
                            else:
                                click.echo(f"  delete {target} (already absent)")
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
