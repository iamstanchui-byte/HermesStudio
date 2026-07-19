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
        "host": "0.0.0.0",
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
    },
}


def find_config_path() -> Path | None:
    """Find config file. Order: $HERMES_ORCH_CONFIG > ~/.hermes-orchestrator/config.yaml > ./config.yaml."""
    env_path = os.environ.get("HERMES_ORCH_CONFIG")
    if env_path:
        return Path(env_path)
    home_path = Path.home() / ".hermes-orchestrator" / "config.yaml"
    if home_path.exists():
        return home_path
    local_path = Path("./config.yaml")
    if local_path.exists():
        return local_path
    return None


def load_config() -> dict[str, Any]:
    """Load config with env var overrides."""
    cfg: dict[str, Any] = {k: dict(v) for k, v in DEFAULT_CONFIG.items()}

    # Load from file
    config_path = find_config_path()
    if config_path:
        with open(config_path) as f:
            file_cfg = yaml.safe_load(f) or {}
        _deep_merge(cfg, file_cfg)

    # Apply env var overrides (HERMES_ORCH_<SECTION>_<KEY>)
    _apply_env_overrides(cfg)
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
