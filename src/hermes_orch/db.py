# coding: utf-8
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Real HMAC shared secret (v1.6, 2026-07-29). Plaintext; needed
    -- server-side to recompute the signature for each request. NULL
    -- for agents that haven't been bootstrapped yet (legacy mode).
    -- Populated by either:
    --   - register_agent (for new agents; stored alongside the hash)
    --   - POST /api/agents/{id}/secret (admin-side, one-shot migration
    --     of an existing agent from its .secret-<id> file)
    -- See src/hermes_orch/auth/hmac.py for the verification scheme.
    hmac_secret TEXT
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
    -- plan_json: Per the 2026-07-27 plan-layer foundation. NULL for
    -- projects that don't use a plan (legacy behavior, tasks added
    -- directly). When set (even to '{}'), the project is in plan
    -- mode — the orchestrator reads the plan, the user can edit it
    -- via the JSON view, and "Run plan" materializes it into tasks.
    -- Plan vs tasks separation: plan is the intent, tasks are the
    -- execution; plans are immutable per "run" (each Run creates a
    -- new execution row, not a new plan).
    , plan_json TEXT DEFAULT NULL
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
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    error TEXT,
    result TEXT,
    required_capability TEXT,
    -- feedback_to: list of EARLIER step names whose failure should
    -- re-dispatch this task (visual workflow builder Phase 0, 2026-07-24).
    -- JSON-encoded list, e.g. '["audit"]'. Default '[]' = no loop-back.
    -- Populated at workflow-run time from the step's feedback_to field.
    -- The supervisor reads this on every tick to decide whether to
    -- cascade-reset this task when one of its referenced steps fails.
    feedback_to TEXT NOT NULL DEFAULT '[]',
    -- archived: Phase 4+ clone-chain (2026-07-26). When 1, this task
    -- is hidden from the project's default view because a "clone
    -- chain" has replaced it with a fresh task. Old result/history
    -- is preserved in this row + audit_log + artifacts. 0 = active
    -- (default for all new tasks).
    archived INTEGER NOT NULL DEFAULT 0,
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
-- Covering index for the "show active tasks" filter
-- (project_id, archived=0, status). The default project views
-- always filter archived=0; this index lets the query stay
-- fast even when many projects accumulate archived history
-- over time. Phase 4+ clone-chain (2026-07-26).
CREATE INDEX IF NOT EXISTS idx_tasks_project_archived ON tasks(project_id, archived);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_profile_configs_status ON profile_configs(status);
-- Covering index for the list_skills / _load_profile_skills queries.
-- Filters by (profile_id, file_path LIKE 'skills/%/SKILL.md') and orders
-- by (file_path ASC, created_at DESC). Without this index, both
-- endpoints do a full table scan and the dashboard's /agents page
-- took 4.5s with 35k rows. With the index, both queries return in
-- <20ms regardless of total table size. Added 2026-07-25 after the
-- runaway-skill-upload loop caused profile_configs to balloon to
-- 35k rows (see agent memory entry "agents page 5s reload").
CREATE INDEX IF NOT EXISTS idx_profile_configs_profile_path_created
    ON profile_configs(profile_id, file_path, created_at DESC);

-- v3.4: users (dashboard auth). Bootstrap admin is created with
-- password_hash=NULL by `hermes-orch init`; the first login through
-- the web UI sets the password (see src/hermes_orch/auth/cookie.py).
-- The `disabled` flag is a soft-delete so we never lose audit history.
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT,            -- bcrypt; NULL = must set (bootstrap admin only)
    role TEXT NOT NULL DEFAULT 'user',  -- 'admin' | 'user'
    is_bootstrap_admin INTEGER NOT NULL DEFAULT 0,  -- 1 only for the first admin row
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_login_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);

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

-- Phase 4+ chat box (2026-07-25): the project-page LLM chat assistant
-- stores one row per turn. role: 'user' or 'assistant'. content: the
-- raw markdown response. suggestions_json: optional JSON list of
-- structured actions the assistant proposed (create_task, run, replan).
-- CASCADE on project delete so cleanup is automatic. The chat is a
-- per-project running conversation — no per-user identity yet.
CREATE TABLE IF NOT EXISTS project_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    suggestions_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_proj_chat_project_created
    ON project_chat_messages(project_id, created_at);

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
    -- v3.1.2: cache hit tokens (separate from prompt_tokens). Captured
    -- from the LLM's `usage` block:
    --   - Anthropic: usage.cache_read_input_tokens (prompt cached hits)
    --   - OpenAI compatible: usage.prompt_tokens_details.cached_tokens
    -- Always 0 for providers that don't report cache (most non-Anthropic).
    -- Kept as a separate column (not subtracted from prompt_tokens) so
    -- the dashboard can show "true new prompt" vs "cache hit" side by
    -- side, and so the running total stays comparable across models.
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
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
--
-- Workflow packages (Stage 1, 2026-07-23): a reusable execution template
-- synthesized from a completed project. Different from `is_template` /
-- project_schedules (those are for simple cron-fired re-runs of the same
-- project). A workflow package is a *long-lived reusable asset* that
-- contains a parameterized step_template (with {{var}} placeholders) and
-- a variables list describing each placeholder's type/required/default.
-- Stage 2b will add POST /api/workflows/{id}/run that substitutes variables
-- and spawns a fresh project.
--
--   source_project_id: which project this was synthesized from (NULL if
--     authored by hand).
--   step_template: JSON array of step objects mirroring the tasks table
--     shape (name, agent_role, action, depends_on, params_template, output_path).
--     params_template uses {{var}} for any value that should be substituted
--     at run time.
--   variables: JSON array of {name, type, description, required, default?}
--     describing each unique {{var}} in step_template.
CREATE TABLE IF NOT EXISTS workflow_packages (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,        -- kebab-case, e.g. "google-drive-list-files"
    version TEXT NOT NULL DEFAULT '0.1.0',
    description TEXT NOT NULL,
    step_template TEXT NOT NULL,      -- JSON array (string-encoded)
    variables TEXT NOT NULL,          -- JSON array (string-encoded)
    visual_layout TEXT NOT NULL DEFAULT '{}',  -- JSON {step_name: {x, y}} card positions; visual editor only
    source_project_id TEXT,           -- FK to projects.id (NULL if hand-authored)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_project_id) REFERENCES projects(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_workflows_name ON workflow_packages(name);
CREATE INDEX IF NOT EXISTS idx_workflows_source ON workflow_packages(source_project_id);
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
    # (visible to the user when they open the folder in the OS file manager)
    # AND denormalized into the task row at dispatch time so the wrapper
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
    # Real task start/end timestamps (set on the server side at /start and
    # terminal-state transitions). Replaces the old hack of computing
    # duration from `updated_at - last_liveness_at`, which always gave
    # 1-30s because both fields were set within 1-2s of task completion
    # (last poll + final status UPDATE). With these columns, the dashboard
    # shows the actual hermes subprocess runtime (typically 30-180s for
    # real work, not 6-30s of false "took"). For pre-migration tasks,
    # backfill from `last_liveness_at` (≈ wrapper claim) and `updated_at`
    # (≈ status change to terminal).
    "ALTER TABLE tasks ADD COLUMN started_at TIMESTAMP",
    "ALTER TABLE tasks ADD COLUMN ended_at TIMESTAMP",
    # Workflow packages Stage 2b (2026-07-23): link a run-back project
    # to the workflow it was created from. The projects-list page can
    # show a "🔁 from workflow X" badge like the existing schedule
    # badge. NULL for projects not from a workflow (most projects).
    "ALTER TABLE projects ADD COLUMN source_workflow_id TEXT DEFAULT ''",
    # Workflow packages (Stage 1, 2026-07-23): schema-versioning
    # safety. If the column was added after some old DBs were created,
    # this ALTER adds it. No-op on fresh DBs (CREATE TABLE above handles).
    # (No columns to ALTER for workflow_packages yet — all in CREATE TABLE
    # block above. Adding this comment as a placeholder for future columns.)
    # Visual workflow builder Phase 0 (2026-07-24): feedback_to on tasks.
    # Default '[]' — every existing task gets an empty list, which means
    # "no loop-back" (safe default; old workflows behave exactly as before).
    # workflow-run populates this from step.feedback_to at creation time.
    "ALTER TABLE tasks ADD COLUMN feedback_to TEXT NOT NULL DEFAULT '[]'",
    # Phase 4+ clone-chain (2026-07-26): when a task is replaced by
    # a clone, the old task is marked archived=1 instead of deleted.
    # Preserves history but hides from default view. See the
    # /api/tasks/{id}/clone-and-cascade endpoint and the `archived`
    # column comment above.
    "ALTER TABLE tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
    # Visual workflow builder Phase 2.5 (2026-07-26): persist the
    # visual card positions so refresh doesn't re-stack everything
    # top-to-bottom. Stored as JSON {step_name: {x, y}}; visual
    # editor only — never read by the runner. Default '{}' = no
    # saved positions, fall back to vertical stack on render.
    "ALTER TABLE workflow_packages ADD COLUMN visual_layout TEXT NOT NULL DEFAULT '{}'",
    # ===== Object Layer foundation (2026-07-26) =====
    # See src/hermes_orch/api/objects.py for the read API.
    #
    # tool_definitions: GLOBAL catalog of tools that any agent profile
    # MAY register (the same MT5 / TradingView / browser tool can be
    # registered by multiple profiles). Stored once, referenced from
    # profile_tools (junction). For Phase 1 (skill/tool/resource
    # foundation) the orch only USES this for capability declaration
    # + MCP health check; actual tool execution goes through MCP
    # servers, not through the orch. See user-stated 2026-07-26:
    # "tool 是否能用orch server 操制不了, 其本上要用mcp 連上這些tools".
    # name is the canonical tool id (e.g. "mt5", "tradingview",
    # "browser"); version tracks the on-host install.
    "CREATE TABLE IF NOT EXISTS tool_definitions ("
    "  id TEXT PRIMARY KEY,"
    "  name TEXT NOT NULL UNIQUE,"
    "  version TEXT NOT NULL DEFAULT '1.0.0',"
    "  kind TEXT NOT NULL DEFAULT 'external_app',"  # external_app | mcp_server | script | api | cli
    "  description TEXT NOT NULL DEFAULT '',"
    "  capabilities TEXT NOT NULL DEFAULT '[]',"  # JSON array of capability names
    "  mcp_server_name TEXT NOT NULL DEFAULT '',"  # optional: MCP server name if wrapped
    "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
    "  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_tool_defs_name ON tool_definitions(name)",
    #
    # profile_tools: junction table — which profiles have which tools,
    # plus per-profile MCP health status. PK is (profile_id, tool_id)
    # so a profile can register each tool at most once. mcp_status is
    # set by /api/objects/tools/{id}/check-mcp; 'unknown' = never
    # checked. last_checked_at drives dashboard staleness badge.
    "CREATE TABLE IF NOT EXISTS profile_tools ("
    "  profile_id TEXT NOT NULL,"
    "  tool_id TEXT NOT NULL,"
    "  mcp_status TEXT NOT NULL DEFAULT 'unknown',"  # unknown | up | down | error
    "  last_checked_at TIMESTAMP,"
    "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
    "  PRIMARY KEY (profile_id, tool_id),"
    "  FOREIGN KEY (profile_id) REFERENCES agent_profiles(id) ON DELETE CASCADE,"
    "  FOREIGN KEY (tool_id) REFERENCES tool_definitions(id) ON DELETE CASCADE"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_profile_tools_profile ON profile_tools(profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_profile_tools_tool ON profile_tools(tool_id)",
    "CREATE INDEX IF NOT EXISTS idx_profile_tools_status ON profile_tools(mcp_status)",
    #
    # tasks.is_single_task: 1 = single task (no project context),
    # lives in the virtual __single_tasks__ project. 0 = regular
    # project task (default). Lets the UI filter single tasks out of
    # the project task list and into a separate "Single tasks" page,
    # while keeping all task history / audit / artifact rows in the
    # same tables. See src/hermes_orch/api/objects.py and
    # project_visual_page (which now hides single tasks from the
    # project canvas).
    "ALTER TABLE tasks ADD COLUMN is_single_task INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_tasks_is_single ON tasks(is_single_task)",
    # Plan layer foundation (2026-07-27): nullable plan_json on projects.
    # Fresh DBs get the column from CREATE TABLE above; this ALTER
    # backfills existing DBs that were created before plan_json was
    # a thing. The 'duplicate column' error is caught silently in
    # connect() so the migration is idempotent.
    "ALTER TABLE projects ADD COLUMN plan_json TEXT DEFAULT NULL",
    # ===== HMAC agent auth (v1.6, 2026-07-29) =====
    # Per-agent shared secret stored plaintext (needed for HMAC verify
    # on the server side). See src/hermes_orch/auth/hmac.py for the
    # full scheme. Populated by register_agent (new agents) or
    # POST /api/agents/{id}/secret (admin migration). NULL for
    # legacy agents that haven't bootstrapped yet — those are still
    # accepted if HERMES_HMAC_REQUIRED is unset.
    "ALTER TABLE agents ADD COLUMN hmac_secret TEXT",
    # v3.1.2: cache_read_tokens on token_usage. Captured from the LLM
    # `usage` block (Anthropic: usage.cache_read_input_tokens, OpenAI:
    # usage.prompt_tokens_details.cached_tokens). DEFAULT 0 so existing
    # rows stay valid and the column is additive — no data migration
    # needed. Older calls that don't pass cache_read just write 0.
    "ALTER TABLE token_usage ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0",
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


# ===== Single-tasks virtual project (Object Layer foundation, 2026-07-26) =====
#
# Single tasks (code-gen, ad-hoc summarize, etc.) live in a virtual project
# `__single_tasks__` so we can reuse the existing tasks / artifacts /
# audit_log tables without changing their schema (tasks.project_id stays
# NOT NULL). The project's row is created on first need (lazy, idempotent)
# and its state stays 'completed' forever — the supervisor ignores it.
#
# Why a virtual project instead of making tasks.project_id NULL:
# - SQLite ALTER TABLE can't drop NOT NULL without a table rebuild, which
#   would block large DBs (we have 30k+ task rows) and risk data loss
# - The projects table already has the columns we need (id, name, state,
#   created_at), so adding a single row is free
# - is_single_task column (indexed) gives us fast filtering; the
#   project_id value just identifies which bucket the single task lives in
SINGLE_TASKS_PROJECT_ID = "__single_tasks__"
SINGLE_TASKS_PROJECT_NAME = "Single tasks (virtual container)"


async def ensure_single_tasks_project(db: Database) -> None:
    """Idempotently create the virtual single-tasks project if missing.

    Called from the FastAPI lifespan on startup, so the first single
    task creation doesn't race the lookup. Safe to call multiple times.
    """
    existing = await db.fetchone(
        "SELECT id FROM projects WHERE id = ?", (SINGLE_TASKS_PROJECT_ID,)
    )
    if existing:
        return
    now = _now_iso()
    # Use raw execute to avoid the timestamp-auto-fill path's assumptions
    # (created_at/updated_at on projects are TIMESTAMP columns, not the
    # _now_iso() ISO format that fetchone() returns on read).
    await db.execute(
        "INSERT OR IGNORE INTO projects "
        "(id, name, goal, state, created_at, updated_at) "
        "VALUES (?, ?, ?, 'completed', ?, ?)",
        (
            SINGLE_TASKS_PROJECT_ID,
            SINGLE_TASKS_PROJECT_NAME,
            "Virtual container for one-off tasks (code-gen, ad-hoc "
            "summarization, etc.) that don't belong to any project.",
            now,
            now,
        ),
    )
    # INSERT OR IGNORE is silent on duplicate, so log only on actual insert.
    # (We don't have rowcount here, but the existence check above means
    # this insert only runs on first startup — fine to be quiet.)
