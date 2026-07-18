"""SQLite database connection + schema.

Per REVIEW.md §3-§6, the DB stores denormalized views of:
- agents + agent_profiles (per Model A multi-role)
- projects (per project)
- tasks (per project, denormalized from plan.md)
- artifacts (central + external)
- audit_log (events)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite


def _now_iso() -> str:
    """Local-time ISO-8601 with timezone offset (e.g. 2026-07-18T19:30:00+08:00).

    Used by db.insert to auto-fill created_at/updated_at with local time
    rather than relying on SQLite's CURRENT_TIMESTAMP (which is UTC naive).
    Without this, dashboard timestamps show as if they were local time but
    are actually 8 hours behind (in HK). Mirrors the helper in
    core/supervisor.py and core/audit.py — kept here to avoid a circular
    import.
    """
    return datetime.now().astimezone().isoformat()

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    secret_hash TEXT NOT NULL,
    old_secret_hash TEXT,
    old_secret_expires_at TIMESTAMP,
    ip TEXT,
    os_type TEXT,
    status TEXT NOT NULL DEFAULT 'verifying',
    last_heartbeat_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_profiles (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'idle',
    current_task_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
    UNIQUE(agent_id, name)
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT,
    goal TEXT,
    state TEXT NOT NULL DEFAULT 'planning',
    session_id TEXT,
    current_session_id TEXT,
    supervisor_turn_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT,
    agent_role TEXT NOT NULL,
    assigned_agent_id TEXT,
    assigned_profile_id TEXT,
    depends_on TEXT NOT NULL DEFAULT '[]',
    on_parent_failure TEXT NOT NULL DEFAULT 'skip',
    status TEXT NOT NULL DEFAULT 'pending',
    priority TEXT NOT NULL DEFAULT 'normal',
    action TEXT,
    params TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 2,
    timeout_seconds INTEGER DEFAULT 1800,
    output_path TEXT,
    last_liveness_at TIMESTAMP,
    error TEXT,
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    content_type TEXT,
    size_bytes INTEGER,
    checksum TEXT,
    storage_kind TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    agent_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor TEXT,
    project_id TEXT,
    task_id TEXT,
    agent_id TEXT,
    payload TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS profile_configs (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    file_path TEXT NOT NULL DEFAULT 'soul.md',
    desired_sha256 TEXT NOT NULL,
    desired_content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    applied_at TIMESTAMP,
    FOREIGN KEY (profile_id) REFERENCES agent_profiles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_profile_configs_status ON profile_configs(status);

-- Project SOUL presets: per-project snapshot of agent identity (SOUL.md)
-- content for each role the project plans to use. Lets the user switch
-- "which project is active" by loading a preset into the relevant profile's
-- SOUL.md via the standard profile_configs apply flow. Multiple projects
-- can run concurrently as long as they target DIFFERENT agent profiles, so
-- there's no "wait for idle" requirement — adding more agents unlocks more
-- parallel projects.
CREATE TABLE IF NOT EXISTS project_soul_presets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,  -- which agent profile this preset is for
    role_name TEXT NOT NULL,   -- denormalized for display (profile.role)
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (profile_id) REFERENCES agent_profiles(id) ON DELETE CASCADE,
    UNIQUE (project_id, profile_id)  -- one preset per (project, profile)
);
CREATE INDEX IF NOT EXISTS idx_soul_presets_project ON project_soul_presets(project_id);
"""

# Idempotent migrations for older DBs that may be missing columns added later.
# Each statement is wrapped in try/except in connect(); "duplicate column" errors
# (and similar) are silently ignored.
MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN result TEXT",
    "ALTER TABLE agent_profiles ADD COLUMN updated_at TIMESTAMP DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN output_path TEXT",
    "ALTER TABLE projects ADD COLUMN current_session_id TEXT",
    # Project iteration tracking (Q3): fields that describe how the project
    # is driven at the system level, not per-task. coordinator_role names
    # the agent (or 'auto') that owns iteration. accept_criteria is plain
    # text describing when the project is "done enough" to stop iterating
    # (the supervisor can re-prompt the LLM with this on each iteration
    # to decide if more work is needed). deliverable_path is the final
    # artifact path (optional). max_iterations caps the loop.
    "ALTER TABLE projects ADD COLUMN coordinator_role TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN accept_criteria TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN deliverable_path TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN max_iterations INTEGER DEFAULT 0",
    "ALTER TABLE projects ADD COLUMN current_iteration INTEGER DEFAULT 0",
    "ALTER TABLE projects ADD COLUMN last_iteration_summary TEXT DEFAULT ''",
    # project_soul_presets is a new table; created in CREATE TABLE block above.
    # No ALTER needed for it on existing DBs — IF NOT EXISTS handles it.
]


class Database:
    """Async SQLite wrapper."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(SCHEMA)
        # Idempotent migrations for existing DBs (catch "duplicate column" errors)
        for migration in MIGRATIONS:
            try:
                await self._conn.execute(migration)
            except Exception:
                pass  # Column already exists, or other harmless error
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        async with self.conn.execute(sql, params) as cur:
            await self.conn.commit()
            return cur

    async def fetchone(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def insert(self, table: str, data: dict[str, Any]) -> None:
        """Insert a row. Auto-fills created_at/updated_at with local time
        if the table has those columns and the caller didn't provide them.

        SQLite's DEFAULT CURRENT_TIMESTAMP is UTC-naive (no offset) and
        stores as 'YYYY-MM-DD HH:MM:SS', which the dashboard then renders
        as if it were local time. By overriding at the Python layer, every
        caller — supervisors, API endpoints, wrapper uploads — gets
        consistent local-time + offset timestamps without having to
        remember to set them. Cached per-table column list to keep the
        hot path cheap.
        """
        if not hasattr(self, "_ts_columns_cache"):
            self._ts_columns_cache: dict[str, set[str]] = {}
        cached = self._ts_columns_cache.get(table)
        if cached is None:
            # Discover which timestamp columns this table has. We use a
            # sync cursor here (PRAGMA) — fine for connect-time setup.
            cur = await self.conn.execute(f"PRAGMA table_info({table})")
            cols_info = await cur.fetchall()
            cached = {
                row[1] for row in cols_info
                if row[1] in ("created_at", "updated_at")
            }
            self._ts_columns_cache[table] = cached
        now = _now_iso()
        if "created_at" in cached and "created_at" not in data:
            data["created_at"] = now
        if "updated_at" in cached and "updated_at" not in data:
            data["updated_at"] = now
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        await self.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
            tuple(_jsonify(v) for v in data.values()),
        )


def _jsonify(v: Any) -> Any:
    """Auto-serialize list/dict to JSON for TEXT columns."""
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    return v
