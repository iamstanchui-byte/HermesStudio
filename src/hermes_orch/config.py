# coding: utf-8
"""Configuration loader.

Priority: env vars > config.yaml > defaults
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "orchestrator": {
        "port": 8765,
        # v1.0.1 (new-user-activation): bind host defaults to loopback only.
        # Operators who want LAN access must explicitly enable it via
        # /settings#network — that requires a server restart, gated by the
        # `restart-required` flag (see `core/restart.py`). The legacy `host`
        # key is still read for backward-compat in `load_config()`; once the
        # next config save lands, the legacy key is replaced.
        #
        # NOTE: `bind_host` is intentionally NOT in DEFAULT_CONFIG. The
        # `load_config()` migration block (below) needs to be able to
        # distinguish "no bind_host in file, no legacy host either → use
        # loopback default" from "no bind_host in file, legacy host present
        # → migrate to bind_host = legacy value". If bind_host were in
        # DEFAULT_CONFIG, _deep_merge would always add it before the
        # migration block could fire.
        "log_level": "INFO",
    },
    "artifacts": {
        "max_size_mb": 50,
        "storage_root": "./artifacts",
    },
    "projects": {
        "storage_root": "./projects",
    },
    "auth": {
        "hmac_timestamp_tolerance_seconds": 300,
        "key_grace_period_days": 7,
    },
    "supervisor": {
        "poll_interval_seconds": 5,
        "planner_timeout_seconds": 60,
        "stuck_planning_warn_minutes": 10,
        "session_turn_warn_threshold": 50,
        # v3.12.1 follow-up #6: per-task LLM-call history window.
        # Caps how many of the most recent turns the wrapper
        # passes to hermes when resuming a session. Actual data
        # (see commit 20fb097) shows prompts grow ~4x per task
        # iteration (proj-cc43d7ed went 80K -> 320K across
        # 8 calls) because the conversation history is fully
        # carried forward. 6 turns ≈ 3-6K tokens of history
        # overhead (a small fraction of the total prompt) and
        # gives the LLM enough context to keep multi-iteration
        # coherence.
        #
        # Per-workflow opt-in: ProjectPlan.max_history_turns
        # overrides this default. NULL/None in the plan = use
        # this server default. The wrapper picks up changes to
        # this default on its next config-poll cycle (no
        # wrapper restart needed).
        "default_max_history_turns": 6,
        # Auto-cleanup for hermes sessions created BY the orchestrator
        # wrapper. Sessions older than `session_ttl_days` are deleted
        # from the hermes backend during the supervisor's hourly
        # sweep. The sweeper only touches sessions in the
        # `project_sessions` table (orch-created), not user-created
        # ones. Set to 0 to disable.
        #
        # Note: the orchestrator does NOT reuse hermes sessions across
        # tasks — each task gets a fresh session (or resumes its own
        # role's session, but only for the lifetime of the project).
        # So a long TTL is wasted disk + memory; 1 day is plenty for
        # debug-ability (you can `hermes sessions list` to see what
        # an agent did yesterday) while keeping the store lean.
        "session_ttl_days": 1,
        "session_sweep_interval_seconds": 3600,
    },
    "llm": {
        # Orchestrator's own LLM (used by planner to break goal into tasks).
        # OpenAI-compatible API. Default to MiniMax; user overrides with their
        # own key + model. Set mock=true to skip API calls (hard-coded plan).
        "base_url": "https://api.minimax.io/v1",
        "api_key": "",
        "model": "MiniMax-M3",
        "mock": True,            # fallback when api_key is empty
        "timeout_seconds": 60,
    },
    "telegram": {
        # Supervisor failure notifications. Disabled by default.
        # To enable: set enabled=true, bot_token=<from BotFather>, chat_id=<your id>
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "timeout_seconds": 10,
    },
    "logging": {
        "audit_log_path": "./audit.log",
        "audit_log_retention_days": 90,
    },
    "cleanup": {
        # Hard-delete projects in 'deleted' state older than this many
        # days. Set to 0 to disable (the Settings page still lets you
        # run cleanup manually with a one-off retention override).
        # - On server startup, cleanup runs once (fire-and-forget).
        # - The supervisor runs cleanup every 24h if daily_sweep=true.
        # - The Settings page has a "Run cleanup now" button.
        # Hard-delete = DELETE FROM projects (cascades to tasks,
        # artifacts, project_sessions, project_soul_presets) +
        # shutil.rmtree the project folder. Audit_log is preserved.
        "retention_days": 30,
        "daily_sweep": True,
        "sweep_interval_seconds": 86400,
        # Audit-log rotation (separate from project cleanup above).
        # The audit_log table grows monotonically and once caused a
        # 1GB DB bloat (2026-07-25: 132k rows from a runaway skill-
        # upload loop). On the daily tick, rows older than
        # `audit_log_retention_days` are moved to a separate
        # `~/.hermes-orchestrator/audit_log.archive.db` file and
        # the main DB is VACUUMed. Off by default because the audit
        # log is genuinely useful for post-mortems — set this to
        # true to opt in. You can also run it manually:
        #   python scripts/rotate_audit_log.py --keep-days 30
        "audit_log_daily_sweep": False,
        "audit_log_retention_days": 30,
        "audit_log_sweep_interval_seconds": 86400,
    },
    "https": {
        # Optional TLS termination at the orchestrator itself. Default
        # is HTTP for easy local dev. Enable by setting https_enabled=true
        # and pointing ssl_cert_path / ssl_key_path at PEM files (user-
        # supplied, or generated by `hermes-orch gen-cert`).
        #
        # When enabled:
        #   - `hermes-orch serve` boots uvicorn with --ssl-keyfile +
        #     --ssl-certfile
        #   - `set_session_cookie` auto-sets the `Secure` cookie flag
        #     so the session cookie is never sent over plain HTTP
        #   - HMAC agent auth still works (TLS is at transport layer,
        #     HMAC is at application layer)
        #
        # When disabled (default): plain HTTP, no Secure flag, dev-friendly.
        "enabled": False,
        "ssl_cert_path": "",   # absolute path to PEM-encoded cert (or chain)
        "ssl_key_path": "",    # absolute path to PEM-encoded private key
    },
    "server": {
        # Security hotfix 2026-08-11 (B12): canonical public origin
        # used by the CSRF helper in `auth.csrf.require_same_origin`.
        # MUST be set in config.yaml or via env var
        # HERMES_ORCH_PUBLIC_ORIGIN. Format: bare absolute URL
        # `scheme://hostname:port` (no path, no query, no fragment,
        # no userinfo, no trailing slash).
        #
        # The server refuses to start at `lifespan()` if this is
        # missing or invalid (see `auth.origin_validation.validate_public_origin`).
        # This is fail-closed: a misconfigured server cannot start.
        "public_origin": "",
    },
}


def find_config_path() -> Path | None:
    """Find config file. Order: $HERMES_ORCH_CONFIG > ~/.hermes-orchestrator/config.yaml > ./config.yaml.

    R7-C contract (2026-08-11 review): the returned path is ALWAYS
    absolute. This makes the downstream DB-path derivation
    (`config_path.parent / "hermes-orch.db"`) deterministic
    regardless of the process cwd at the moment aiosqlite opens the
    DB. Without `.resolve()` on the local branch, a local config
    yields a relative `Path("./config.yaml")` whose `.parent` is
    `Path(".")` and whose DB suffix lands as the bare
    `Path("hermes-orch.db")` -- which aiosqlite would then open
    relative to whatever cwd the worker thread is in. The
    lifespan's `Path.cwd()` is the natural anchor; we use it
    explicitly so a chdir between resolution and DB connect
    cannot drift the DB location.
    """
    env_path = os.environ.get("HERMES_ORCH_CONFIG")
    if env_path:
        p = Path(env_path)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = p.resolve()
        return p
    home_path = Path.home() / ".hermes-orchestrator" / "config.yaml"
    if home_path.exists():
        return home_path.resolve()
    local_path = Path("config.yaml")
    if local_path.exists():
        return (Path.cwd() / local_path).resolve()
    return None


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load config with env var overrides.

    R7-C contract (2026-08-11 review): callers that derive downstream
    paths from the same config (e.g. the lifespan, which derives the
    DB path from the config's parent dir) should pass an already-
    resolved `config_path` so `load_config()` and the downstream
    derivation use the SAME object. If `config_path` is None, the
    config path is resolved via `find_config_path()` -- which means
    a caller that also calls `find_config_path()` independently may
    end up with two different paths. The lifespan passes its
    resolved path explicitly; all other callers (tests, scripts)
    leave it as None to preserve the historical "auto-resolve" behavior.
    """
    cfg: dict[str, Any] = {k: dict(v) for k, v in DEFAULT_CONFIG.items()}

    # Load from file
    if config_path is None:
        config_path = find_config_path()
    if config_path and config_path.exists():
        with open(config_path) as f:
            file_cfg = yaml.safe_load(f) or {}
        _deep_merge(cfg, file_cfg)

    # v1.0.1: legacy `host:` key migration (see docs/v1.0.1-new-user-activation.md §3.1.1).
    # If the loaded config has the legacy `host` key in `orchestrator` but no
    # `bind_host`, copy the value over and log a one-time migration notice.
    # This protects operators who previously opted into `host: 0.0.0.0` from
    # being silently downgraded to loopback-only on upgrade.
    #
    # Note: `bind_host` is intentionally NOT in DEFAULT_CONFIG (see comment
    # there). This lets us detect the "no bind_host in file" case below.
    orch = cfg.get("orchestrator") or {}
    if isinstance(orch, dict) and "bind_host" not in orch:
        if "host" in orch:
            # Legacy migration: copy value, log to stderr once.
            legacy_host = orch["host"]
            orch["bind_host"] = legacy_host
            import sys
            print(
                f"[config-migration] Migrating legacy config key 'host' -> "
                f"'bind_host' (value: {legacy_host!r} retained).",
                file=sys.stderr,
                flush=True,
            )
        else:
            # Neither legacy `host` nor new `bind_host` in the file — fall
            # back to the loopback default.
            orch["bind_host"] = "127.0.0.1"

    # Apply env var overrides (HERMES_ORCH_<SECTION>_<KEY>)
    _apply_env_overrides(cfg)

    # Security hotfix 2026-08-11 (B12): explicit mapping for the
    # canonical public origin. We need a single env var with a flat
    # name (`HERMES_ORCH_PUBLIC_ORIGIN`) but the underscore-splitting
    # in `_apply_env_overrides` would put it at `cfg["public"]["origin"]`,
    # not the desired `cfg["server"]["public_origin"]`. So we map it
    # explicitly here. This runs AFTER `_apply_env_overrides` so a
    # `server.public_origin` value in config.yaml can still be the
    # authoritative source — the env var only overrides if the YAML
    # didn't already set it (or the env var is explicitly set).
    _env_public_origin = os.environ.get("HERMES_ORCH_PUBLIC_ORIGIN", "").strip()
    if _env_public_origin:
        # env var wins over file when both are set
        cfg.setdefault("server", {})["public_origin"] = _env_public_origin

    return cfg


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Recursively merge override into base."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _apply_env_overrides(cfg: dict[str, Any]) -> None:
    """HERMES_ORCH_ORCHESTRATOR_PORT=9000 -> cfg['orchestrator']['port']=9000"""
    prefix = "HERMES_ORCH_"
    for env_key, env_value in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        path = env_key[len(prefix):].lower().split("_")
        current = cfg
        for part in path[:-1]:
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]
        # Try to parse as int/bool, fallback to str
        last = path[-1]
        if env_value.lower() in ("true", "false"):
            current[last] = env_value.lower() == "true"
        else:
            try:
                current[last] = int(env_value)
            except ValueError:
                try:
                    current[last] = float(env_value)
                except ValueError:
                    current[last] = env_value


def save_config_section(section: str, updates: dict[str, Any]) -> Path:
    """Update one section of the user config.yaml on disk.

    Reads existing file (or starts from defaults), merges `updates` into the
    given section, writes back without BOM (Windows-friendly). Returns the
    path written.

    Does NOT touch in-memory cfg — caller's load_config() must be re-invoked.
    """
    path = find_config_path()
    if not path:
        # Default to home location
        path = Path.home() / ".hermes-orchestrator" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing
    existing: dict[str, Any] = {}
    if path.exists():
        with open(path, encoding="utf-8-sig") as f:
            existing = yaml.safe_load(f) or {}

    # Merge updates into section
    sec = existing.get(section) or {}
    if not isinstance(sec, dict):
        sec = {}
    sec.update(updates)
    existing[section] = sec

    # Write back without BOM
    text = yaml.safe_dump(existing, default_flow_style=False, sort_keys=False)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


# Known LLM provider presets — used by the wizard / settings page
LLM_PROVIDERS: list[dict[str, str]] = [
    {
        "id": "minimax",
        "name": "MiniMax",
        "base_url": "https://api.minimax.io/v1",
        "default_model": "MiniMax-M3",
        "signup_url": "https://platform.minimax.io/",
        "note": "Recommended. Default global endpoint, OpenAI-compatible.",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "signup_url": "https://platform.openai.com/api-keys",
        "note": "Popular. Requires international payment.",
    },
    {
        "id": "anthropic",
        "name": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-5-sonnet-latest",
        "signup_url": "https://console.anthropic.com/",
        "note": "Strong reasoning. OpenAI-compatible via proxy or use direct SDK.",
    },
    {
        "id": "custom",
        "name": "Other (OpenAI-compatible)",
        "base_url": "",
        "default_model": "",
        "signup_url": "",
        "note": "Any OpenAI-compatible endpoint. Fill base URL + model.",
    },
]
