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


# ===== sessions subcommand =====

@cli.group()
def sessions() -> None:
    """Manage hermes sessions created by the orchestrator wrapper.

    The orchestrator's wrapper creates a hermes session for every
    task. Without cleanup, those sessions accumulate in the hermes
    backend. Use `sessions list` to see what's there, and `sessions
    cleanup` to mark old ones as deleted.

    IMPORTANT: this only touches sessions the orchestrator itself
    created (tracked in the `project_sessions` table, source =
    'orchestrator'). User-created hermes sessions are never touched.
    """


@sessions.command(name="list")
@click.option("--status", default="active", type=click.Choice(["active", "deleted", "all"]), help="Filter by status")
@click.option("--limit", default=50, type=int, help="Max rows to show")
def sessions_list(status: str, limit: int) -> None:
    """List tracked sessions in the orchestrator's project_sessions table."""
    import asyncio
    from hermes_orch.db import Database

    db = Database(_default_db_path())

    async def _run() -> None:
        await db.connect()
        where = []
        params: list = []
        if status != "all":
            where.append("status = ?")
            params.append(status)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        sql = f"SELECT * FROM project_sessions{where_sql} ORDER BY COALESCE(last_used_at, created_at) DESC LIMIT ?"
        params.append(limit)
        rows = await db.fetchall(sql, tuple(params))
        if not rows:
            click.echo(f"(no sessions matching status={status})")
            return
        click.echo(f"{len(rows)} session(s) [status={status}]:")
        click.echo(f"  {'SESSION ID':32s}  {'ROLE':12s}  {'PROJECT':18s}  LAST USED")
        for r in rows:
            ts = (r.get("last_used_at") or r.get("created_at") or "")[:19]
            role = (r.get("role") or "")[:12]
            pid = (r.get("project_id") or "")[:18]
            sid = (r.get("session_id") or "")[:32]
            click.echo(f"  {sid:32s}  {role:12s}  {pid:18s}  {ts}")
        await db.close()

    asyncio.run(_run())


@sessions.command(name="cleanup")
@click.option("--older-than", default=None, type=int, help="Override config: TTL in days")
@click.option("--dry-run/--no-dry-run", default=True, help="Just show what would be deleted (default: dry-run)")
def sessions_cleanup(older_than: int | None, dry_run: bool) -> None:
    """Mark orch-created sessions older than the TTL as deleted.

    The TTL defaults to supervisor.session_ttl_days in config.yaml
    (7 days). Pass --older-than to override for a one-off cleanup.

    The supervisor's hourly sweep also calls this internally. Use
    --dry-run first to see what would happen.
    """
    import asyncio
    from hermes_orch.config import load_config
    from hermes_orch.db import Database
    from hermes_orch.core.supervisor import Supervisor
    from hermes_orch.core.notifier import Notifier
    from hermes_orch.core.planner import Planner

    cfg = load_config()
    db = Database(_default_db_path())
    ttl = older_than if older_than is not None else int(
        (cfg.get("supervisor") or {}).get("session_ttl_days", 7)
    )

    async def _run() -> None:
        await db.connect()
        # Notifier/planner required by Supervisor __init__ but not used
        # by sweep_sessions(). Notifier has no enabled channels unless
        # configured; planner is unused here.
        notifier = Notifier(cfg.get("telegram") or {})
        planner = Planner(cfg)
        sup = Supervisor(db, cfg, notifier, planner)
        report = await sup.sweep_sessions(ttl_days=ttl, dry_run=dry_run)
        await db.close()
        if report.get("disabled"):
            click.echo("Auto-cleanup disabled (session_ttl_days=0). Nothing to do.")
            return
        click.echo(f"  cutoff:  {ttl} day(s) ago")
        click.echo(f"  candidates:  {report['candidates']}")
        if dry_run:
            click.echo(f"  (dry-run — pass --no-dry-run to actually mark deleted)")
        else:
            click.echo(f"  deleted:  {report['deleted']}")
        if report.get("errors"):
            click.echo(f"  errors:  {len(report['errors'])}")
            for e in report["errors"][:5]:
                click.echo(f"    - {e}")

    asyncio.run(_run())


def _default_db_path() -> Path:
    """Default path to the orchestrator's SQLite DB.

    Matches the convention in the `init` command: ~/.hermes-orchestrator/
    hermes-orch.db. Used by CLI subcommands that need DB access but
    don't have a request.app.state.db to read from.
    """
    return Path.home() / ".hermes-orchestrator" / "hermes-orch.db"


if __name__ == "__main__":
    cli()
