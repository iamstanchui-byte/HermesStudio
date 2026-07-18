"""CLI entry point (hermes-orch command).

Commands:
- init     Initialize orchestrator (config, DB, admin token)
- serve    Start FastAPI server
- agent    Agent subcommands (register, start, stop) — see agent_cli.py
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

import click
import uvicorn

from hermes_orch import __version__


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """Hermes Orchestrator — local network multi-agent orchestrator for Hermes runtime."""


@cli.command()
@click.option("--config-dir", type=click.Path(), default=None, help="Config directory (default: ~/.hermes-orchestrator)")
def init(config_dir: str | None) -> None:
    """Initialize orchestrator: create config dir, projects/, artifacts/, DB schema, admin token."""
    base = Path(config_dir) if config_dir else Path.home() / ".hermes-orchestrator"
    base.mkdir(parents=True, exist_ok=True)

    # Subdirectories
    (base / "projects").mkdir(exist_ok=True)
    (base / "artifacts").mkdir(exist_ok=True)

    # Config file (if not exists)
    config_file = base / "config.yaml"
    if not config_file.exists():
        config_file.write_text(
            """# Hermes Orchestrator config (see REVIEW.md §8.2 for full reference)
orchestrator:
  port: 8765
  host: "0.0.0.0"
  log_level: INFO

artifacts:
  max_size_mb: 50
  storage_root: ./artifacts

projects:
  storage_root: ./projects

auth:
  hmac_timestamp_tolerance_seconds: 300
  key_grace_period_days: 7

supervisor:
  session_turn_warn_threshold: 50

logging:
  audit_log_path: ./audit.log
  audit_log_retention_days: 90
"""
        )
        click.echo(f"Created config: {config_file}")
    else:
        click.echo(f"Config exists: {config_file}")

    # DB schema (initialize)
    from hermes_orch.db import SCHEMA, Database

    async def _init_db() -> None:
        db = Database(base / "hermes-orch.db")
        await db.connect()
        await db.close()

    import asyncio
    asyncio.run(_init_db())
    click.echo(f"Initialized DB: {base / 'hermes-orch.db'}")

    # Admin token
    token_file = base / "admin-token.txt"
    if not token_file.exists():
        token = secrets.token_urlsafe(32)
        token_file.write_text(token)
        token_file.chmod(0o600)
        click.echo(f"Generated admin token: {token_file}")
        click.echo(f"  Token: {token}")
        click.echo("  (Save this — first dashboard login uses it)")
    else:
        click.echo(f"Admin token exists: {token_file}")

    click.echo(f"\n✅ Init complete. Next: hermes-orch serve")


@cli.command()
@click.option("--host", default=None, help="Bind host (default from config)")
@click.option("--port", default=None, type=int, help="Bind port (default from config)")
@click.option("--reload/--no-reload", default=False, help="Enable auto-reload (dev)")
def serve(host: str | None, port: int | None, reload: bool) -> None:
    """Start the FastAPI server."""
    from hermes_orch.config import load_config

    cfg = load_config()
    bind_host = host or cfg["orchestrator"]["host"]
    bind_port = port or cfg["orchestrator"]["port"]

    click.echo(f"Starting Hermes Orchestrator on http://{bind_host}:{bind_port}")
    click.echo(f"  Dashboard: http://localhost:{bind_port}/")
    click.echo(f"  API docs:  http://localhost:{bind_port}/docs")
    click.echo(f"  Health:    http://localhost:{bind_port}/api/health")

    uvicorn.run(
        "hermes_orch.main:app",
        host=bind_host,
        port=bind_port,
        reload=reload,
        log_level=cfg["orchestrator"]["log_level"].lower(),
    )


if __name__ == "__main__":
    cli()
