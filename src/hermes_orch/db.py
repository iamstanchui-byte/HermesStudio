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
from pathlib import Path
from typing import Any

import aiosqlite

from hermes_orch.utils import now_iso as _now_iso

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
    capabilities TEXT NOT NULL DEFAULT '{}',
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
    current_sessions_json TEXT DEFAULT '{}',
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
    required_capability TEXT,
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

-- project_sessions: tracks hermes sessions created BY the orchestrator
-- wrapper, so a background sweeper can delete them on a TTL. The
-- 'source' column distinguishes orch-created ('orchestrator') from
-- user-created sessions (which the sweeper must not touch — even
-- though we don't currently have a path to insert 'user' rows,
-- the column is there for forward-compat). 'status' lets us mark a
-- session as deleted without losing history (audit / undo).
CREATE TABLE IF NOT EXISTS project_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,         -- the hermes session id (e.g. 20260718_212748_d1bc26)
    role TEXT NOT NULL,
    agent_id TEXT,
    profile_id TEXT,
    source TEXT NOT NULL DEFAULT 'orchestrator',  -- 'orchestrator' | 'user'
    status TEXT NOT NULL DEFAULT 'active',         -- 'active' | 'deleted'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,           -- bumped on every task reuse
    deleted_at TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_proj_sessions_project ON project_sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_proj_sessions_status ON project_sessions(status);
CREATE INDEX IF NOT EXISTS idx_proj_sessions_last_used ON project_sessions(last_used_at);

-- Per-call LLM token usage log. Every planner / synthesis / wrapper
-- LLM call should write one row, with the prompt/completion/total
-- token counts from the OpenAI-compatible `usage` field. The dashboard
-- aggregates this for the 4h / 24h / 7d overview and the by-model /
-- by-agent / by-project / top-tasks breakdowns.
-- Columns kept as raw integers (no JSON); the call_kind enum makes
-- the dashboards easy to filter on without string parsing.
CREATE TABLE IF NOT EXISTS token_usage (
    id TEXT PRIMARY KEY,
    agent_id TEXT,            -- NULL for planner/synthesis (orchestrator-side)
    profile_id TEXT,          -- NULL when not tied to a specific profile
    project_id TEXT,
    task_id TEXT,
    role TEXT,                -- profile name (matches agent_profiles.name) when wrapper-side
    model TEXT NOT NULL,
    base_url TEXT,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    call_kind TEXT NOT NULL,   -- 'planner' | 'synthesis' | 'agent_task' | 'wrapper_other'
    call_label TEXT,          -- free text: e.g. 'plan_world_cup', 'regen_state'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_token_usage_agent   ON token_usage(agent_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_profile ON token_usage(profile_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_project ON token_usage(project_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_task    ON token_usage(task_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_created ON token_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_token_usage_model   ON token_usage(model);

-- Recurring project schedules (#22): turn any project into a template, attach
-- a cron expression, and the orchestrator's background scheduler will
-- create new project runs (clone) or append task batches (append) on every
-- fire. Skip-if-previous-running rule keeps the schedule from stacking up
-- when a run is slow. See core/scheduler.py for the fire loop.
--
--   is_template flag on projects marks a project as "reusable for schedules"
--   source_schedule_id on projects links a run back to the schedule that
--   created it, so the skip rule and the projects-list badge can find it.
CREATE TABLE IF NOT EXISTS project_schedules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    template_project_id TEXT NOT NULL,    -- FK to projects.id (must be is_template=1)
    cron_expr TEXT NOT NULL,              -- e.g. "0 9 * * 1-5" (in `timezone`)
    timezone TEXT NOT NULL DEFAULT 'Asia/Hong_Kong',
    mode TEXT NOT NULL DEFAULT 'clone',   -- 'clone' = new project each fire; 'append' = add to existing
    enabled INTEGER NOT NULL DEFAULT 1,   -- 0 = paused
    last_fired_at TIMESTAMP,
    next_fire_at TIMESTAMP,               -- updated by scheduler on each tick
    last_skip_reason TEXT,                -- if last fire was skipped, why (debug aid)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (template_project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON project_schedules(enabled);
CREATE INDEX IF NOT EXISTS idx_schedules_template ON project_schedules(template_project_id);
CREATE INDEX IF NOT EXISTS idx_schedules_next_fire ON project_schedules(next_fire_at);
"""

# Idempotent migrations for older DBs that may be missing columns added later.
# Each statement is wrapped in try/except in connect(); "duplicate column" errors
# (and similar) are silently ignored.
MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN result TEXT",
    "ALTER TABLE agent_profiles ADD COLUMN updated_at TIMESTAMP DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN output_path TEXT",
    "ALTER TABLE projects ADD COLUMN current_session_id TEXT",
    # Per-role session map: {"<role>": "<session_id>", ...}. The old
    # `current_session_id` was a single string (latest wins) which caused
    # cross-profile session reuse: a task running on profile X would
    # --resume a session that profile Y created, and hermes would return
    # "Session not found" because hermes session namespaces are
    # per-profile. The new column stores one session per role so the
    # wrapper can resume only sessions that belong to its own role.
    "ALTER TABLE projects ADD COLUMN current_sessions_json TEXT DEFAULT '{}'",
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
    # Phase 4 (smart dispatch): per-profile capability map. JSON object of
    # `{capability_name: true}`. The supervisor checks this against
    # `tasks.required_capability` before assigning — if a task needs
    # capability X and the chosen profile doesn't have X, the task fails
    # with `dispatch.mismatch` (no silent fallback to the wrong tool).
    # Default '{}' = "can do anything", which preserves current behavior
    # for old profiles. Empty `{}` is the safer default (must list
    # explicitly), but we ship the permissive default to avoid breaking
    # existing flows — operators opt in by editing the JSON.
    "ALTER TABLE agent_profiles ADD COLUMN capabilities TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE tasks ADD COLUMN required_capability TEXT",
    # Per-profile LLM model (wrapper-reported via heartbeat). Plain TEXT
    # columns — no JSON parsing needed. The wrapper reads
    # <profile>/config.yaml (the profile's hermes config) and reports
    # the model.default, model.base_url, model.provider triple. NULL
    # means "wrapper hasn't reported yet" — dashboard shows a grey
    # fallback badge with a tooltip.
    "ALTER TABLE agent_profiles ADD COLUMN llm_model_default TEXT",
    "ALTER TABLE agent_profiles ADD COLUMN llm_model_base_url TEXT",
    "ALTER TABLE agent_profiles ADD COLUMN llm_model_provider TEXT",
    # Per-profile MCP server list (wrapper-reported via heartbeat). Stored
    # as a JSON array of {name, enabled} objects. Default '[]' = no MCP
    # servers configured. The wrapper reads <profile>/config.yaml
    # (mcp_servers section) and pushes the list on heartbeat. Each entry
    # must have a 'name' (str); the dashboard renders a green/grey dot
    # per server based on 'enabled'.
    "ALTER TABLE agent_profiles ADD COLUMN mcp_servers TEXT NOT NULL DEFAULT '[]'",
    # project_soul_presets is a new table; created in CREATE TABLE block above.
    # No ALTER needed for it on existing DBs — IF NOT EXISTS handles it.
    # project_sessions is a new table; created in CREATE TABLE block above.
    # No ALTER needed for it on existing DBs — IF NOT EXISTS handles it.
    # Recurring schedules (#22): mark a project as a reusable template
    # (is_template=1), and link a project back to the schedule that
    # created it (source_schedule_id, NULL for ad-hoc projects).
    "ALTER TABLE projects ADD COLUMN is_template INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE projects ADD COLUMN template_description TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN source_schedule_id TEXT DEFAULT ''",
    # project_schedules is a new table; created in CREATE TABLE block above.
    # No ALTER needed for it on existing DBs — IF NOT EXISTS handles it.
    # Procedure.md (#22 Path A): per-project procedure/policy markdown that
    # the agent reads before doing each task. Stored in the project folder
    # (visible to the user when they open the folder in Explorer) AND
    # denormalized into the task row at dispatch time so the wrapper
    # can inject it as prompt context without a separate file fetch.
    "ALTER TABLE tasks ADD COLUMN procedure_md TEXT DEFAULT ''",
    # Per-profile storage references (user-stated 2026-07-22). Operator-
    # curated list of paths/URLs the agent can use to write large outputs
    # directly (bypassing the 15MB per-file orch cap). Stored as a JSON
    # array of {kind, ref, description} objects. Common kinds:
    #   - "smb" : Windows file share, e.g. "\\\\nas01\\reports"
    #   - "local": Local folder the agent host has mounted, e.g. "S:\\reports"
    #   - "gdrive": Google Drive folder ID or URL
    #   - "s3": S3 bucket/prefix
    #   - "url": Generic URL the agent has credentials for
    # Default '[]' = no storage configured. Wrapper reads this column
    # and injects an [AVAILABLE STORAGE] block into the task prompt
    # so the agent knows where to put large outputs. This is the
    # primary mechanism (alongside OS-level mount) for keeping large
    # data out of the orch's project share folder.
    "ALTER TABLE agent_profiles ADD COLUMN storage_refs TEXT NOT NULL DEFAULT '[]'",
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
