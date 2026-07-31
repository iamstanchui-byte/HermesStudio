# coding: utf-8
"""CLI entry point (hermes-orch command).

Commands:
- init     Initialize orchestrator (config, DB, admin token)
- serve    Start FastAPI server
- agent    Agent subcommands (register, start, stop) — see agent_cli.py
"""
from __future__ import annotations

import asyncio
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
@click.option("--admin-username", default="admin", show_default=True, help="Username for the bootstrap admin user (created with no password; first web login sets it).")
def init(config_dir: str | None, admin_username: str) -> None:
    """Initialize orchestrator: create config dir, projects/, artifacts/, DB schema, admin token.

    v3.4: also creates a bootstrap admin user (no password set). The
    first time someone visits /login with that username, the web UI
    walks them through setting the password. This avoids storing a
    default password in the CLI that operators might forget to change.
    """
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

    # DB schema (initialize) + bootstrap admin user
    from hermes_orch.db import SCHEMA, Database
    from hermes_orch.auth.cookie import (
        BOOTSTRAP_ADMIN_USERNAME, ROLE_ADMIN, create_user, get_user_by_username,
    )

    async def _init_db() -> None:
        db = Database(base / "hermes-orch.db")
        await db.connect()
        # Bootstrap admin: only created if NO admin user exists yet.
        # We pick the username from --admin-username, but if a previous
        # init created "admin" we keep that. Subsequent re-inits with
        # --admin-username different from the existing one are rejected
        # loudly so operators don't silently end up with two admins.
        existing = await get_user_by_username(db, admin_username)
        if existing:
            click.echo(f"Admin user '{admin_username}' exists (id={existing['id']})")
        else:
            # Check if any admin exists at all (someone re-ran init with
            # a different --admin-username)
            any_admin = await db.fetchone(
                "SELECT username FROM users WHERE role = ? LIMIT 1",
                (ROLE_ADMIN,),
            )
            if any_admin:
                click.echo(
                    f"WARNING: an admin user '{any_admin['username']}' already "
                    f"exists. Skipping bootstrap admin creation. To add "
                    f"another admin, use 'hermes-orch user add --admin'."
                )
            else:
                uid = await create_user(
                    db,
                    username=admin_username,
                    password=None,  # bootstrap: no password until first web login
                    role=ROLE_ADMIN,
                    is_bootstrap_admin=True,
                )
                click.echo(f"Created bootstrap admin: {admin_username} (id={uid})")
                click.echo(
                    f"  → Visit /login and enter '{admin_username}' + any "
                    f"password; you'll be prompted to set the initial password."
                )
        await db.close()

    import asyncio
    asyncio.run(_init_db())
    click.echo(f"Initialized DB: {base / 'hermes-orch.db'}")

    # Admin token (legacy — pre-v3.4). Kept for backward compat with the
    # agent-bootstrap flow (HMAC agent registration). The dashboard
    # user is separate.
    token_file = base / "admin-token.txt"
    if not token_file.exists():
        token = secrets.token_urlsafe(32)
        token_file.write_text(token)
        token_file.chmod(0o600)
        click.echo(f"Generated admin token (for agent bootstrap): {token_file}")
        click.echo(f"  Token: {token}")
        click.echo("  (Save this — first agent register uses it)")
    else:
        click.echo(f"Admin token exists: {token_file}")

    click.echo(f"\n[OK] Init complete. Next: hermes-orch serve")
    click.echo(f"   Then visit: http://localhost:<port>/login")


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
            click.echo(f"  (dry-run — pass --no-dry-run to actually mark pending_cleanup)")
        else:
            # Two-phase: sweeper marks as 'pending_cleanup' so the
            # wrapper picks it up on its next heartbeat, runs
            # `hermes sessions delete <id> --yes`, and acks. The
            # report dict uses 'marked_pending' for the count of
            # rows we just flipped; subsequent 'deleted' transitions
            # happen asynchronously on the wrapper.
            click.echo(f"  marked_pending:  {report.get('marked_pending', 0)}")
            click.echo(f"  (wrappers will delete from local hermes backends on next heartbeat)")
        if report.get("errors"):
            click.echo(f"  errors:  {len(report['errors'])}")
            for e in report["errors"][:5]:
                click.echo(f"    - {e}")

    asyncio.run(_run())


# ===== user subcommands (v3.4 dashboard auth) =====

@cli.group()
def user() -> None:
    """Manage dashboard users (v3.4).

    After `hermes-orch init` creates the bootstrap admin, use these
    subcommands to add more users, reset passwords, or disable
    accounts. The dashboard only sees users listed in the `users`
    table; HMAC agent identities are separate.
    """


@user.command(name="add")
@click.option("--username", required=True, help="Username (case-insensitive, must be unique)")
@click.option("--password", required=True, help="Initial password (min 8 chars). User can change via /api/auth/password after first login.")
@click.option("--admin/--no-admin", default=False, help="Grant admin role (default: regular user)")
def user_add(username: str, password: str, admin: bool) -> None:
    """Add a new dashboard user."""
    from hermes_orch.auth.cookie import create_user, get_user_by_username, ROLE_ADMIN, ROLE_USER

    async def _run() -> None:
        from hermes_orch.db import Database
        db = Database(_default_db_path())
        await db.connect()
        try:
            existing = await get_user_by_username(db, username)
            if existing:
                raise click.ClickException(f"User '{username}' already exists (id={existing['id']})")
            if len(password) < 8:
                raise click.ClickException("Password must be at least 8 characters")
            uid = await create_user(
                db,
                username=username,
                password=password,
                role=ROLE_ADMIN if admin else ROLE_USER,
            )
            click.echo(f"Created user '{username}' (id={uid}, role={'admin' if admin else 'user'})")
        finally:
            await db.close()

    asyncio.run(_run())


@user.command(name="list")
def user_list() -> None:
    """List all dashboard users."""
    from hermes_orch.auth.cookie import list_users

    async def _run() -> None:
        from hermes_orch.db import Database
        db = Database(_default_db_path())
        await db.connect()
        try:
            rows = await list_users(db)
        finally:
            await db.close()
        if not rows:
            click.echo("(no users)")
            return
        click.echo(f"{len(rows)} user(s):")
        click.echo(f"  {'ID':14s}  {'USERNAME':20s}  {'ROLE':8s}  {'DISABLED':8s}  LAST LOGIN")
        for r in rows:
            last = r.get("last_login_at") or 0
            last_str = (
                __import__("datetime").datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M")
                if last else "-"
            )
            click.echo(
                f"  {r['id']:14s}  {r['username']:20s}  {r['role']:8s}  "
                f"{'yes' if r['disabled'] else '-':8s}  {last_str}"
            )

    asyncio.run(_run())


@user.command(name="disable")
@click.argument("username")
def user_disable(username: str) -> None:
    """Disable a user (revokes their session on next request; doesn't delete history)."""
    from hermes_orch.auth.cookie import get_user_by_username, set_user_disabled

    async def _run() -> None:
        from hermes_orch.db import Database
        db = Database(_default_db_path())
        await db.connect()
        try:
            u = await get_user_by_username(db, username)
            if not u:
                raise click.ClickException(f"No such user: '{username}'")
            await set_user_disabled(db, u["id"], True)
            click.echo(f"Disabled user '{username}' (id={u['id']}). Their existing cookies stop working on next request.")
        finally:
            await db.close()

    asyncio.run(_run())


@user.command(name="enable")
@click.argument("username")
def user_enable(username: str) -> None:
    """Re-enable a previously disabled user."""
    from hermes_orch.auth.cookie import get_user_by_username, set_user_disabled

    async def _run() -> None:
        from hermes_orch.db import Database
        db = Database(_default_db_path())
        await db.connect()
        try:
            u = await get_user_by_username(db, username)
            if not u:
                raise click.ClickException(f"No such user: '{username}'")
            await set_user_disabled(db, u["id"], False)
            click.echo(f"Enabled user '{username}' (id={u['id']})")
        finally:
            await db.close()

    asyncio.run(_run())


@user.command(name="passwd")
@click.argument("username")
@click.option("--password", required=True, help="New password (min 8 chars).")
def user_passwd(username: str, password: str) -> None:
    """Reset a user's password. Admin-only operation; no old password required."""
    from hermes_orch.auth.cookie import get_user_by_username, set_user_password

    async def _run() -> None:
        from hermes_orch.db import Database
        db = Database(_default_db_path())
        await db.connect()
        try:
            u = await get_user_by_username(db, username)
            if not u:
                raise click.ClickException(f"No such user: '{username}'")
            if len(password) < 8:
                raise click.ClickException("Password must be at least 8 characters")
            await set_user_password(db, u["id"], password)
            click.echo(f"Password reset for '{username}' (id={u['id']}). Their existing cookies stay valid until expiry; new logins use the new password.")
        finally:
            await db.close()

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
