# coding: utf-8
"""Agent endpoints (per REVIEW.md §6, §6.4 multi-role Model A).

Endpoints:
- POST   /api/agents                       — register (returns one-time setup secret)
- GET    /api/agents                       — list all agents with profiles
- GET    /api/agents/{id}                  — get one agent + profiles
- PUT    /api/agents/{id}                  — update agent metadata (ip, os_type)
- DELETE /api/agents/{id}                  — delete agent
- POST   /api/agents/{id}/secret           — set/push the HMAC shared secret (v1.6 bootstrap)
- POST   /api/agents/{id}/heartbeat        — agent heartbeat (HMAC-authed)
- GET    /api/agents/{id}/status           — v0.7 agent status (HMAC-authed via v0.7 §1.4)
- POST   /api/agents/{id}/rotate-key        — rotate secret
- POST   /api/agents/{id}/profiles         — add new profile
- DELETE /api/agents/{id}/profiles/{name}  — remove profile
- PATCH  /api/agents/{id}/profiles/{name}  — update profile (description)
- GET    /api/agents/{id}/profiles/{name}/configs            — list configs
- POST   /api/agents/{id}/profiles/{name}/configs            — create new config (status=pending)
- GET    /api/agents/{id}/profiles/{name}/configs/pending    — wrapper poll (atomic claim)
- POST   /api/agents/{id}/profiles/{name}/configs/{cid}/ack  — wrapper ack (applied/failed)
- GET    /api/agents/{id}/profiles/{name}/skills            — list skills (latest version per name)
- GET    /api/agents/{id}/profiles/{name}/skills/{name}     — get one skill's content
- POST   /api/agents/{id}/profiles/{name}/skills            — create or update a skill
- DELETE /api/agents/{id}/profiles/{name}/skills/{name}     — delete a skill (via empty-content config)

Auth (per §6.1, v1.6 enforced): HMAC-SHA256 with X-Agent-Id, X-Timestamp, X-Signature.
The signature binds (method, path, body_sha256, timestamp) so captured
requests can't be replayed. See src/hermes_orch/auth/hmac.py for the
full scheme. The legacy "X-Signature = SHA256(secret)" placeholder
was removed; all wrapper endpoints now require real HMAC.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from hermes_orch.auth import require_hmac_auth
from hermes_orch.auth.hmac_v07 import require_hmac_auth_v07

from hermes_orch.core.audit import audit_log
from hermes_orch.utils import now_iso as _now_iso
# Security hotfix 2026-08-11 (B12, B10): import the canonical
# admin guard + CSRF helper. These are wired into the 7
# admin-mutation routes (§4 of the design doc) and the
# non-admin `/secret` route is replaced with a 410 stub
# (§5). The legacy `from hermes_orch.api.users import
# require_admin` was used only by reset_live_soul; we keep
# that import lazy INSIDE that function to avoid the
# import-time cycle (api/users.py imports from us).
from hermes_orch.auth.admin_guard import require_admin
from hermes_orch.auth.csrf import require_same_origin

router = APIRouter()


# ===== Pydantic models =====


class AgentRegister(BaseModel):
    agent_id: str
    ip: str | None = None
    os_type: str | None = None  # 'windows' | 'linux'
    # roles is DEPRECATED: profiles are now created separately (via
    # /api/agents/{id}/profiles POST after register, or auto-detected
    # by the wrapper on first heartbeat). Kept for backward compat —
    # if present, it's silently ignored and recorded in the audit log
    # as `roles_ignored` so operators can tell old clients are still
    # sending it. Remove in a future major version.
    roles: list[str] = Field(default_factory=list)
    # v3.6.0: per-agent concurrent task cap. How many tasks the
    # wrapper's ThreadPoolExecutor can run in parallel. Default 1 =
    # backward compatible. 1..32 enforced here (single source of truth;
    # the DB has no CHECK constraint). The orchestrator's _assign_task
    # uses this to skip dispatch when the agent is at capacity; the
    # wrapper reads it from the heartbeat response and sizes its
    # pool accordingly.
    max_concurrent_tasks: int = Field(default=1, ge=1, le=32)


class AgentUpdate(BaseModel):
    ip: str | None = None
    os_type: str | None = None
    # v3.6.0: editable from the Agent page settings (PUT
    # /api/agents/{id}). Same 1..32 range as register. The wrapper
    # picks up the new value on its next heartbeat tick.
    max_concurrent_tasks: int | None = Field(default=None, ge=1, le=32)


class StorageRef(BaseModel):
    """One storage reference for an agent profile. Operator-curated.

    The agent can write large outputs directly to any of these paths
    (bypassing the orch's 15MB per-file cap). The wrapper injects
    the configured list into the task prompt as an [AVAILABLE STORAGE]
    block so the agent knows where to put data.

    Common kinds:
      - "smb"   : Windows file share, e.g. "\\\\nas01\\reports"
      - "local" : Local folder the agent host has mounted, e.g. "S:\\reports"
      - "gdrive": Google Drive folder ID or URL
      - "s3"    : S3 bucket/prefix
      - "url"   : Generic URL the agent has credentials for

    `name` (optional, 2026-07-22): short alias tasks can reference via
    `params.output_to = "stanley"`. The wrapper resolves the alias to
    the matching ref at task dispatch and injects a [STORAGE HINT] block
    so the agent doesn't have to guess. Without a name, the task must
    pass the full ref string in params.output_to. Names are
    per-profile (operator can rename without touching task params).
    """
    name: str | None = None
    kind: str
    ref: str
    description: str = ""


class AgentProfileCreate(BaseModel):
    name: str
    description: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)
    storage_refs: list[StorageRef] = Field(default_factory=list)
    # v3.9.0 (SOUL routing): flat list of capability tags (e.g.
    # ["web_search", "python"]). Used by the routing engine to match
    # workflow steps that declare `required_capabilities`. Distinct
    # from file-based skills (which live under <profile>/skills/).
    # Optional — default [] = no capabilities declared.
    skills: list[str] = Field(default_factory=list)
    # v3.13.0 (Profile root path): explicit path on the agent host
    # for this profile's directory. None or empty = auto-derive
    # from <profiles_dir>/<role> (the existing behavior). Set this
    # when the user has a non-standard install (custom path, NAS
    # mount, per-profile directory layout). Wrapper reads this via
    # the next sync-config and writes it into wrapper-config.json
    # as the explicit "root" field. See
    # docs/v3.13.0-agent-profile-root-path.md.
    root_path: str | None = None


class AgentProfileUpdate(BaseModel):
    description: str | None = None
    capabilities: dict[str, bool] | None = None
    storage_refs: list[StorageRef] | None = None
    # v3.9.0 (SOUL routing): optional — only update the column when
    # the caller provides it. None = no change (backward compat for
    # existing PATCH calls that don't touch skills).
    skills: list[str] | None = None
    # v3.13.0 (Profile root path): optional — only update the column
    # when the caller provides it. None = no change (preserve current
    # value, including NULL). Empty string "" = clear the explicit
    # root and fall back to auto-derive on the next wrapper sync.
    root_path: str | None = None


class HeartbeatBody(BaseModel):
    status: str | None = None  # agent's reported state (e.g. 'busy', 'idle')
    profile: str | None = None  # which profile this heartbeat is for (optional, legacy single-profile path)
    # LLM model (reported by wrapper, read from <profile>/config.yaml).
    # All three are independent; any subset can be sent and the rest
    # stays unchanged. To clear a value, send the empty string "".
    # Used by the legacy single-profile path (top-level fields). The
    # bulk `profiles` field below supersedes this for the new
    # per-agent heartbeat that reports all profiles at once.
    model_default: str | None = None
    model_base_url: str | None = None
    model_provider: str | None = None
    # MCP server list (reported by wrapper, read from <profile>/config.yaml
    # mcp_servers section). Each entry must have a 'name' (str); optional
    # 'enabled' (bool, default True). Sent as a list of dicts.
    mcp_servers: list[dict] | None = None
    # Bulk per-profile metadata. The wrapper now reads each profile's
    # config.yaml and reports them all in one heartbeat. If `profiles`
    # is set, the endpoint updates each profile's llm_* and mcp_servers
    # columns. If a single `profile` is also set, the bulk update is
    # skipped for that profile (legacy path takes precedence).
    profiles: list[dict] | None = None  # [{name, model_default, model_base_url, model_provider, mcp_servers}]


class AgentProfile(BaseModel):
    id: str
    agent_id: str
    name: str
    description: str | None = None
    status: str = "idle"
    current_task_id: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)
    # LLM model (NULL = wrapper hasn't reported yet)
    llm_model_default: str | None = None
    llm_model_base_url: str | None = None
    llm_model_provider: str | None = None
    # MCP server list (parsed from JSON column, default [])
    mcp_servers: list[dict] = Field(default_factory=list)
    # Storage references for large outputs (parsed from JSON column).
    # Per orch-as-coordinator principle: agent writes large data
    # directly to these paths; orch only stores metadata + reference.
    storage_refs: list[StorageRef] = Field(default_factory=list)
    # v3.9.0 (SOUL routing): file-based skills (list of dicts loaded
    # from <profile>/skills/*/SKILL.md via dashboard's
    # _load_profile_skills). `_row_to_profile` does NOT populate this —
    # the dashboard adds it per-render. The API response from the
    # non-dashboard path (e.g. GET /api/agents/{id}) returns [] here;
    # the dashboard does its own loading for the agents page.
    # See api/dashboard.py for the canonical fill-in path.
    skills: list[dict] = Field(default_factory=list)
    # v3.9.0 (SOUL routing): profile capability tags (JSON list of
    # strings, e.g. ["web_search", "python"]). The routing engine
    # matches workflow step `required_capabilities` against this list.
    # Distinct from the file-based `skills` above (declared at
    # registration, not loaded from disk). Default [] = no
    # capabilities declared; routing engine falls back to "any
    # online profile" with a warning.
    capability_tags: list[str] = Field(default_factory=list)
    # v3.13.0 (Profile root path): NULL or absent = auto-derive
    # from <profiles_dir>/<role>. Set = explicit path on the agent
    # host for this profile's directory. See
    # docs/v3.13.0-agent-profile-root-path.md.
    root_path: str | None = None
    created_at: str | None = None


class Agent(BaseModel):
    id: str
    ip: str | None = None
    os_type: str | None = None
    status: str = "verifying"
    last_heartbeat_at: str | None = None
    created_at: str | None = None
    # v3.6.0: per-agent concurrent task cap (1..32). Surfaced to the
    # dashboard so the operator can see at a glance how parallel an
    # agent's wrapper will run. Editable via PUT /api/agents/{id}.
    max_concurrent_tasks: int = 1
    profiles: list[AgentProfile] = Field(default_factory=list)


class AgentRegistrationResponse(BaseModel):
    agent: Agent
    setup_secret: str
    setup_instructions: str


class ProfileConfigCreate(BaseModel):
    file_path: str = "soul.md"
    content: str | None = None  # full file content (or sha256 only)
    content: str


class ProfileConfigAck(BaseModel):
    status: str  # 'applied' | 'failed'
    error: str | None = None
    actual_sha256: str | None = None


class ProfileConfig(BaseModel):
    id: str
    profile_id: str
    file_path: str
    desired_sha256: str
    desired_content: str
    status: str
    error: str | None = None
    created_at: str | None = None
    applied_at: str | None = None


class SkillCreate(BaseModel):
    """Body for creating/updating a skill.

    `name` is filename-safe (no path separators, no leading dot).
    The skill is always written as `skills/<name>/SKILL.md` (hermes
    0.17+ folder layout). Skill creation is normally done by
    agents (via the wrapper's auto-sync of self-taught SKILL.md
    files); the dashboard POST endpoint exists for the wrapper
    flow and is not exposed in the operator UI.
    """
    name: str
    content: str = ""


class SkillInfo(BaseModel):
    """One skill as seen by the dashboard. Latest applied/pending version
    of each `skills/<name>.md` file on the profile."""
    name: str
    file_path: str
    status: str  # 'applied' | 'pending' | 'applying' | 'failed' | 'deleted'
    size: int  # byte length of desired_content (UTF-8 encoded)
    sha256: str | None = None  # hex sha256 of desired_content bytes; the
    # wrapper uses this for content-addressed change detection (more
    # reliable than byte-length comparison, which is sensitive to
    # multi-byte chars + encoding round-trips).
    created_at: str | None = None
    applied_at: str | None = None
    error: str | None = None
    content: str | None = None  # only included when ?content=1


# ===== Helpers =====
# _now_iso is now imported from hermes_orch.utils (consolidated).


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _row_to_profile(row: dict[str, Any]) -> AgentProfile:
    # capabilities stored as JSON text. Defensive: handle missing/malformed.
    caps_raw = row.get("capabilities")
    caps: dict[str, bool] = {}
    if caps_raw:
        try:
            parsed = json.loads(caps_raw) if isinstance(caps_raw, str) else caps_raw
            if isinstance(parsed, dict):
                caps = {str(k): bool(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, TypeError):
            pass
    # MCP server list — JSON array of {name, enabled}. Defensive: handle
    # missing/malformed. Schema guarantees default '[]' on new rows.
    mcp_raw = row.get("mcp_servers")
    mcps: list[dict] = []
    if mcp_raw:
        try:
            parsed = json.loads(mcp_raw) if isinstance(mcp_raw, str) else mcp_raw
            if isinstance(parsed, list):
                # Normalize each entry to {name: str, enabled: bool}; skip
                # entries missing 'name' (the dashboard needs it as the
                # primary key for the dot color).
                for m in parsed:
                    if isinstance(m, dict) and "name" in m:
                        mcps.append({
                            "name": str(m["name"]),
                            "enabled": bool(m.get("enabled", True)),
                        })
        except (json.JSONDecodeError, TypeError):
            pass
    # Storage references (user-stated 2026-07-22). Operator-curated
    # list of paths/URLs the agent can use to write large outputs
    # directly. Same defensive parsing pattern as MCP servers.
    sref_raw = row.get("storage_refs")
    srefs: list[dict] = []
    if sref_raw:
        try:
            parsed = json.loads(sref_raw) if isinstance(sref_raw, str) else sref_raw
            if isinstance(parsed, list):
                for s in parsed:
                    if isinstance(s, dict) and "kind" in s and "ref" in s:
                        # `name` is optional (alias for params.output_to)
                        srefs.append({
                            "name": str(s.get("name", "")).strip() or None,
                            "kind": str(s["kind"]),
                            "ref": str(s["ref"]),
                            "description": str(s.get("description", "")),
                        })
        except (json.JSONDecodeError, TypeError):
            pass
    # v3.9.0 (SOUL routing): profile capability tags. The DB column
    # is still named `skills` (no migration to rename it) but the
    # Pydantic response field is `capability_tags` to distinguish it
    # from the file-based `skills` list (which the dashboard loads
    # separately via _load_profile_skills). Defensive: schema
    # guarantees default '[]' on new rows; for pre-migration DBs the
    # column is missing entirely and row.get() returns None (treated
    # as empty list). Malformed JSON also falls back to [] — the
    # routing engine logs a warning and treats the profile as "no
    # capabilities declared".
    skills_raw = row.get("skills")
    capability_tags: list[str] = []
    if skills_raw:
        try:
            parsed = json.loads(skills_raw) if isinstance(skills_raw, str) else skills_raw
            if isinstance(parsed, list):
                capability_tags = [str(s) for s in parsed if isinstance(s, (str, int, float))]
        except (json.JSONDecodeError, TypeError):
            pass

    return AgentProfile(
        id=row["id"],
        agent_id=row["agent_id"],
        name=row["name"],
        description=row.get("description"),
        status=row["status"],
        current_task_id=row.get("current_task_id"),
        capabilities=caps,
        # LLM model (plain text columns; safe to pass None)
        llm_model_default=row.get("llm_model_default"),
        llm_model_base_url=row.get("llm_model_base_url"),
        llm_model_provider=row.get("llm_model_provider"),
        mcp_servers=mcps,
        storage_refs=srefs,
        # File-based skills (skills/<name>/SKILL.md) are NOT loaded
        # here — the dashboard adds them per-render via
        # _load_profile_skills. Non-dashboard API consumers that
        # need the file-based list can hit
        # GET /api/agents/{id}/profiles/{name}/skills. Empty list
        # here is the correct "not loaded" sentinel.
        skills=[],
        capability_tags=capability_tags,
        # v3.13.0 (Profile root path). NULL = auto-derive from
        # <profiles_dir>/<role>; otherwise explicit path on the
        # agent host. row.get() returns None for missing column
        # (pre-migration DB) or NULL value (post-migration).
        # See docs/v3.13.0-agent-profile-root-path.md.
        root_path=row.get("root_path"),
        created_at=row.get("created_at"),
    )


def _agent_dir(agent_id: str) -> Path:
    """Path to where agent's setup files would be on the agent OS.
    Just for display in setup instructions — actual file is on agent OS, not here."""
    from pathlib import Path

    return Path("~") / ".hermes-orchestrator" / f".secret-{agent_id}"


async def _agent_with_profiles(db: Any, agent_id: str) -> Agent:
    """Load agent + profiles from DB."""
    row = await db.fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))
    if not row:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    profile_rows = await db.fetchall(
        "SELECT * FROM agent_profiles WHERE agent_id = ? ORDER BY name",
        (agent_id,),
    )
    return Agent(
        id=row["id"],
        ip=row.get("ip"),
        os_type=row.get("os_type"),
        status=row["status"],
        last_heartbeat_at=row.get("last_heartbeat_at"),
        created_at=row.get("created_at"),
        # v3.6.0: per-agent concurrent task cap. Row.get returns None
        # if the column is missing (defensive: the migration should
        # have added it, but a hand-crafted DB may not have it). Coerce
        # None -> 1 to keep the Agent model happy (it's non-Optional
        # because the column is NOT NULL with DEFAULT 1 in the schema).
        max_concurrent_tasks=int(row.get("max_concurrent_tasks") or 1),
        profiles=[_row_to_profile(p) for p in profile_rows],
    )


# ===== Agent endpoints =====


@router.post("/", response_model=AgentRegistrationResponse, status_code=201)
async def register_agent(
    body: AgentRegister,
    request: Request,
    user: dict = Depends(require_admin),
    _csrf: None = Depends(require_same_origin),
) -> AgentRegistrationResponse:
    """Register a new agent. Returns one-time setup secret.

    One-time secret shown in response — user copies to agent OS.
    Stored as SHA-256 hash in DB.

    Security (B12 hotfix 2026-08-11): admin-gated. Unauthenticated
    → 401, non-admin → 403, cross-origin POST → 403. Admin
    identity is recorded in the audit log.
    """
    db = request.app.state.db

    existing = await db.fetchone("SELECT id FROM agents WHERE id = ?", (body.agent_id,))
    if existing:
        raise HTTPException(409, f"Agent already exists: {body.agent_id}")

    secret = secrets.token_urlsafe(32)
    secret_hash = _hash_secret(secret)

    await db.insert(
        "agents",
        {
            "id": body.agent_id,
            "secret_hash": secret_hash,
            "hmac_secret": secret,  # v1.6: plaintext for HMAC verify
            "ip": body.ip,
            "os_type": body.os_type,
            "status": "verifying",
            # v3.6.0: per-agent concurrent task cap. Pydantic already
            # validated 1..32 (Field ge=1, le=32), so we just pass
            # the value through. Default 1 = backward compatible.
            "max_concurrent_tasks": body.max_concurrent_tasks,
        },
    )

    # Backward-compat: old clients still send `roles` but profiles are
    # now managed separately (see POST /api/agents/{id}/profiles). If
    # roles is non-empty, audit it as `roles_ignored` so operators can
    # tell stale clients are still around. Don't create any profile rows.
    roles_ignored = list(body.roles) if body.roles else []

    agent = await _agent_with_profiles(db, body.agent_id)
    setup_instructions = (
        f"On the agent machine, run:\n"
        f'  echo "{secret}" > ~/.hermes-orchestrator/.secret-{body.agent_id}\n'
        f"  chmod 600 ~/.hermes-orchestrator/.secret-{body.agent_id}\n"
        f"  hermes-orch-agent start"
    )
    await audit_log(
        db, "agent.registered",
        actor=f"admin:{user['username']}",
        agent_id=body.agent_id,
        payload={
            "ip": body.ip,
            "os_type": body.os_type,
            "roles_ignored": roles_ignored,
            # v3.6.0: record the initial cap so the audit log shows
            # the starting configuration. Subsequent changes are
            # captured by `agent.max_concurrent_tasks_changed`.
            "max_concurrent_tasks": body.max_concurrent_tasks,
            # B12: caller identity + route. `remote_addr` is best-effort
            # (None when no client — e.g. unit tests via ASGI in-process).
            "remote_addr": request.client.host if request.client else None,
            "route": "POST /api/agents/",
        },
    )
    return AgentRegistrationResponse(
        agent=agent,
        setup_secret=secret,
        setup_instructions=setup_instructions,
    )


class AgentSecretSetBody(BaseModel):
    """Body for POST /api/agents/{id}/secret (v1.6 HMAC bootstrap).

    Wrappers (or admins) call this once per agent to push the
    shared HMAC secret into the DB. After this, the agent's
    hmac_secret column is set and all subsequent wrapper requests
    are HMAC-verified.
    """
    secret: str = Field(min_length=16, max_length=256)


@router.get("/{agent_id}/max_history_config")
async def get_max_history_config(
    agent_id: str, request: Request,
    _agent_id: str = Depends(require_hmac_auth),
) -> dict:
    """Return the server's max_history_turns default (v3.12.1 #6).

    The wrapper polls this endpoint on its existing config-poll
    cycle (the same one that fetches profile_configs/pending).
    The default value lives in `config.supervisor.default_max_history_turns`
    (default 6). Operators tune it via config.yaml; the change
    takes effect on the wrapper's next poll cycle (no restart).

    Per-workflow overrides live in the project's plan_json
    (ProjectPlan.max_history_turns); they're resolved per-task
    in the orchestrator's dispatch path and written into
    task.params. The wrapper reads `_max_history_turns` from
    task.params for each dispatch (immediate effect, no poll
    involved).

    Returns:
        {
          "agent_id": "linux-a-01",
          "value": 6,
          "source": "default",  # or "config" once we have
                                  # non-default values; for now
                                  # the only source is the
                                  # DEFAULT_CONFIG default
        }
    """
    db = request.app.state.db
    # Sanity check: agent_id should exist. If it doesn't, the
    # wrapper should know (e.g. typo in agent_id) — but we
    # don't reject the request, because the operator may have
    # intentionally set up a config before the agent registers.
    # The wrapper will keep polling; eventually the agent
    # registers and the value is consumed.
    cfg = request.app.state.config
    sup_cfg = (cfg.get("supervisor") or {})
    value = int(sup_cfg.get("default_max_history_turns", 6))
    return {
        "agent_id": agent_id,
        "value": value,
        "source": "default",
    }


@router.post("/{agent_id}/secret", status_code=410)
async def set_agent_secret(
    agent_id: str,
    request: Request,
) -> dict:
    """B10 disposition (security hotfix 2026-08-11).

    Anonymous legacy secret-bootstrap removed. New-flow agents (post
    2026-08-11, via /api/agents/enroll) have their hmac_secret written
    atomically in the enroll transaction; this endpoint is unnecessary
    for normal flow. Legacy agents (pre-enroll) that lost their
    hmac_secret should be handled via the admin-authenticated recovery
    flow tracked under security/agent-secret-at-rest (B11).

    The endpoint returns 410 Gone for ALL callers (unauth, non-admin,
    admin). It is intentionally NOT gated by `require_admin` because
    the 410 contract is unconditional — the route is permanently
    disabled, not "admin-only".

    IMPLEMENTATION TRAP: do NOT add `Depends(require_admin)` here. If
    the implementer accidentally does so, an admin caller would get
    200/201 instead of 410, breaking the B10 contract.
    """
    raise HTTPException(
        410,
        "POST /api/agents/{id}/secret is deprecated. New-flow agents "
        "have their HMAC secret set at enroll time. For legacy recovery, "
        "use the admin-authenticated recovery flow (tracked in B11).",
    )


@router.get("/")
async def list_agents(request: Request) -> dict:
    """List all agents (with profiles)."""
    db = request.app.state.db
    agent_rows = await db.fetchall("SELECT * FROM agents ORDER BY created_at DESC")
    agents = []
    for row in agent_rows:
        profile_rows = await db.fetchall(
            "SELECT * FROM agent_profiles WHERE agent_id = ? ORDER BY name",
            (row["id"],),
        )
        agents.append(
            Agent(
                id=row["id"],
                ip=row.get("ip"),
                os_type=row.get("os_type"),
                status=row["status"],
                last_heartbeat_at=row.get("last_heartbeat_at"),
                created_at=row.get("created_at"),
                # v3.6.0: same defensive coercion as _agent_with_profiles
                # (None -> 1). See comment there for why.
                max_concurrent_tasks=int(row.get("max_concurrent_tasks") or 1),
                profiles=[_row_to_profile(p) for p in profile_rows],
            ).model_dump()
        )
    return {"agents": agents}


@router.get("/{agent_id}", response_model=Agent)
async def get_agent(
    agent_id: str,
    request: Request,
    x_agent_id: str = Depends(require_hmac_auth),
) -> Agent:
    """Get agent + profiles (v1.6: HMAC-authed)."""
    if x_agent_id != agent_id:
        raise HTTPException(
            401, f"X-Agent-Id ({x_agent_id}) does not match URL ({agent_id})"
        )
    db = request.app.state.db
    return await _agent_with_profiles(db, agent_id)


@router.put("/{agent_id}", response_model=Agent)
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    request: Request,
    user: dict = Depends(require_admin),
    _csrf: None = Depends(require_same_origin),
) -> Agent:
    """Update agent metadata (ip, os_type, max_concurrent_tasks).

    Security (B12 hotfix 2026-08-11): admin-gated.
    """
    db = request.app.state.db
    agent = await db.fetchone(
        "SELECT id, max_concurrent_tasks FROM agents WHERE id = ?", (agent_id,)
    )
    if not agent:
        raise HTTPException(404, f"Agent not found: {agent_id}")

    updates = []
    params: list[Any] = []
    if body.ip is not None:
        updates.append("ip = ?")
        params.append(body.ip)
    if body.os_type is not None:
        updates.append("os_type = ?")
        params.append(body.os_type)
    # v3.6.0: per-agent concurrent task cap. Audit-logged so operators
    # can see when a cap was raised/lowered and from what to what
    # (the supervisor's dispatch decisions need to be traceable —
    # "did we skip this task because the cap was at N?").
    if body.max_concurrent_tasks is not None:
        new_cap = body.max_concurrent_tasks
        old_cap = int(agent.get("max_concurrent_tasks") or 1)
        if new_cap != old_cap:
            updates.append("max_concurrent_tasks = ?")
            params.append(new_cap)
    if updates:
        params.append(agent_id)
        await db.execute(
            f"UPDATE agents SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
    # Audit log: only emit if a change was actually applied. Skipping
    # no-op PUTs keeps the log from filling up with PATCH-equivalent
    # calls that the operator UI may make on autosave.
    if body.max_concurrent_tasks is not None:
        new_cap = body.max_concurrent_tasks
        old_cap = int(agent.get("max_concurrent_tasks") or 1)
        if new_cap != old_cap:
            await audit_log(
                db, "agent.max_concurrent_tasks_changed",
                actor=f"admin:{user['username']}",
                agent_id=agent_id,
                payload={
                    "old": old_cap,
                    "new": new_cap,
                    "source": "PUT /api/agents/{id}",
                    # B12: caller identity + route
                    "remote_addr": request.client.host if request.client else None,
                    "route": "PUT /api/agents/{id}",
                },
            )
    return await _agent_with_profiles(db, agent_id)


@router.post("/{agent_id}/heartbeat")
async def heartbeat(
    agent_id: str,
    request: Request,
    x_agent_id: str = Depends(require_hmac_auth),
) -> dict:
    """Agent heartbeat. HMAC-authed (v1.6).

    Verifies the X-Agent-Id / X-Timestamp / X-Signature headers via
    require_hmac_auth, and that X-Agent-Id matches the URL path
    agent_id (so a wrapper can't heartbeat on behalf of another
    agent even with a valid signature).

    Returns list of tasks currently assigned to this agent.
    """
    if x_agent_id != agent_id:
        raise HTTPException(
            401, f"X-Agent-Id ({x_agent_id}) does not match URL ({agent_id})"
        )

    db = request.app.state.db
    agent = await db.fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))
    if not agent:
        raise HTTPException(404, f"Agent not found: {agent_id}")

    # Optional body. Note: the HMAC dep already read request.body()
    # to verify the signature. We need to parse the JSON from the
    # raw bytes — but request.body() can only be read once. Use
    # json.loads on the bytes we already read; OR re-read if needed.
    # Simpler: parse from a fresh await request.body() — the cache
    # means subsequent calls return the same bytes.
    body_data: dict[str, Any] = {}
    try:
        body_bytes = await request.body()
        if body_bytes:
            import json

            body_data = json.loads(body_bytes)
    except Exception:
        pass

    now = _now_iso()
    await db.execute(
        "UPDATE agents SET status = 'verified', last_heartbeat_at = ? WHERE id = ?",
        (now, agent_id),
    )

    # Stale-busy cleanup: if any of this agent's profiles still claims a
    # current_task_id but that task is already terminal, free the profile.
    # This handles the case where the daemon died mid-task and a fresh
    # daemon (or restarted daemon) is now heartbeating.
    await db.execute(
        "UPDATE agent_profiles "
        "SET status = 'idle', current_task_id = NULL, updated_at = ? "
        "WHERE agent_id = ? "
        "AND current_task_id IS NOT NULL "
        "AND current_task_id IN ("
        "  SELECT id FROM tasks WHERE status IN "
        "  ('completed', 'failed', 'cancelled', 'interrupted', 'skipped')"
        ")",
        (now, agent_id),
    )

    # Optional LLM model fields from the wrapper. The wrapper reads
    # <profile>/config.yaml (model.default / model.base_url / model.provider)
    # and pushes them on every heartbeat. Any subset can be sent; we
    # only UPDATE the columns that were provided. The `profile` field
    # scopes the update to a single profile; if missing, fan out to
    # all profiles of this agent.
    if any(body_data.get(k) is not None for k in ("model_default", "model_base_url", "model_provider", "mcp_servers")):
        body = HeartbeatBody(**body_data)
        sets: list[str] = []
        params: list[Any] = []
        for field, col in (
            ("model_default", "llm_model_default"),
            ("model_base_url", "llm_model_base_url"),
            ("model_provider", "llm_model_provider"),
        ):
            val = getattr(body, field, None)
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        # MCP server list — store as JSON string. Validated server-side:
        # each entry must have a 'name' (str). Malformed entries are
        # dropped silently (the dashboard defensive parse will then show
        # the empty list).
        if body.mcp_servers is not None:
            cleaned: list[dict] = []
            for m in body.mcp_servers:
                if isinstance(m, dict) and "name" in m and isinstance(m["name"], str):
                    cleaned.append({
                        "name": m["name"],
                        "enabled": bool(m.get("enabled", True)),
                    })
            sets.append("mcp_servers = ?")
            params.append(json.dumps(cleaned))
        if sets:
            sql = (
                f"UPDATE agent_profiles SET {', '.join(sets)}, updated_at = ? "
                f"WHERE agent_id = ?"
            )
            params.extend([now, agent_id])
            if body.profile:
                # Scoped to a single profile
                sql += " AND name = ?"
                params.append(body.profile)
            await db.execute(sql, tuple(params))

    # Bulk per-profile metadata (preferred path). The wrapper reads
    # each profile's config.yaml and reports them all in one heartbeat.
    # Iterates each entry and updates llm_model_* + mcp_servers for
    # the named profile. Validation: 'name' must be a non-empty str;
    # mcp_servers entries must have a 'name' (str); other fields are
    # optional (None = don't change). Malformed entries are dropped
    # silently so a bad config doesn't break the whole heartbeat.
    body = body_data.get("profiles")
    if isinstance(body, list):
        for p in body:
            if not isinstance(p, dict):
                continue
            name = p.get("name")
            if not isinstance(name, str) or not name:
                continue
            sets: list[str] = []
            params2: list[Any] = []
            for field, col in (
                ("model_default", "llm_model_default"),
                ("model_base_url", "llm_model_base_url"),
                ("model_provider", "llm_model_provider"),
            ):
                val = p.get(field)
                if val is not None:
                    sets.append(f"{col} = ?")
                    params2.append(val)
            mcp = p.get("mcp_servers")
            if mcp is not None:
                cleaned: list[dict] = []
                if isinstance(mcp, list):
                    for m in mcp:
                        if isinstance(m, dict) and "name" in m and isinstance(m["name"], str):
                            cleaned.append({
                                "name": m["name"],
                                "enabled": bool(m.get("enabled", True)),
                            })
                sets.append("mcp_servers = ?")
                params2.append(json.dumps(cleaned))
            if sets:
                sql = (
                    f"UPDATE agent_profiles SET {', '.join(sets)}, updated_at = ? "
                    f"WHERE agent_id = ? AND name = ?"
                )
                params2.extend([now, agent_id, name])
                await db.execute(sql, tuple(params2))

    # Return assigned + running tasks for this agent
    task_rows = await db.fetchall(
        "SELECT * FROM tasks WHERE assigned_agent_id = ? AND status IN ('assigned', 'running')",
        (agent_id,),
    )
    tasks = []
    import json

    for t in task_rows:
        for col in ("depends_on", "params", "result"):
            v = t.get(col)
            if isinstance(v, str):
                try:
                    t[col] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    t[col] = {} if col != "depends_on" else []
        # Inject parent task metadata so the wrapper can download input files.
        # No project_folder (filesystem path) is injected — the wrapper uses
        # the file API to download inputs and upload outputs, so cross-host
        # works without shared filesystem.
        if t.get("depends_on"):
            parent_outputs: dict[str, str | None] = {}
            for parent_id in t["depends_on"]:
                parent = await db.fetchone(
                    "SELECT id, output_path FROM tasks WHERE id = ?", (parent_id,)
                )
                if parent:
                    parent_outputs[parent_id] = parent.get("output_path")
                else:
                    parent_outputs[parent_id] = None
            t["parent_outputs"] = parent_outputs
        tasks.append(t)

    # Session cleanup: list hermes sessions the wrapper should delete from
    # its local hermes backend. These are sessions the supervisor's sweeper
    # has aged out (status='pending_cleanup'); the wrapper runs
    # `hermes sessions delete <id> --yes` and acks via
    # /sessions/{session_id}/cleanup-ack, which flips the DB row to
    # status='deleted'. Without this, hermes's local session store grows
    # unbounded; the orchestrator's DB mark-as-deleted alone is purely
    # audit-trail.
    cleanup_rows = await db.fetchall(
        "SELECT id, project_id, session_id, role FROM project_sessions "
        "WHERE status = 'pending_cleanup' AND source = 'orchestrator' "
        "AND role IN (SELECT name FROM agent_profiles WHERE agent_id = ?)",
        (agent_id,),
    )

    # Per-profile storage_refs (user-stated 2026-07-22): operator-curated
    # list of paths/URLs the agent can use to write large outputs directly
    # (bypassing the 15MB per-file cap). Wrapper reads this on heartbeat
    # and caches it, injecting as an [AVAILABLE STORAGE] block into the
    # task prompt. Keyed by profile name so the wrapper can look up
    # the right entry for the role it's running as.
    profile_rows = await db.fetchall(
        "SELECT name, storage_refs FROM agent_profiles WHERE agent_id = ?",
        (agent_id,),
    )
    storage_refs_by_profile: dict[str, list[dict]] = {}
    for pr in profile_rows:
        sref_raw = pr["storage_refs"]
        srefs: list[dict] = []
        if sref_raw:
            try:
                parsed = json.loads(sref_raw) if isinstance(sref_raw, str) else sref_raw
                if isinstance(parsed, list):
                    for s in parsed:
                        if isinstance(s, dict) and "kind" in s and "ref" in s:
                            srefs.append({
                                "name": str(s.get("name", "")).strip() or None,
                                "kind": str(s["kind"]),
                                "ref": str(s["ref"]),
                                "description": str(s.get("description", "")),
                            })
            except (json.JSONDecodeError, TypeError):
                pass
        storage_refs_by_profile[pr["name"]] = srefs

    return {
        "status": "ok",
        "timestamp": now,
        "agent_status": body_data.get("status", "idle"),
        "tasks": tasks,
        "cleanup_session_ids": [r["session_id"] for r in cleanup_rows],
        # Profile name -> list of {kind, ref, description}. Empty list
        # if operator hasn't configured any storage for that profile.
        # Wrapper caches this and injects as [AVAILABLE STORAGE] block.
        "storage_refs_by_profile": storage_refs_by_profile,
        # v3.6.0: per-agent concurrent task cap. The wrapper sizes
        # its ThreadPoolExecutor from this value; size changes take
        # effect on the next tick (the pool is rebuilt per tick).
        # Defensive: same None -> 1 coercion as _agent_with_profiles
        # (legacy DB without the column would have None here).
        "max_concurrent_tasks": int(agent.get("max_concurrent_tasks") or 1),
    }


# === v0.7 §1.4: GET /api/agents/{id}/status ===

@router.get("/{agent_id}/status")
async def get_agent_status(
    agent_id: str,
    request: Request,
    auth_agent_id: str = Depends(require_hmac_auth_v07),
) -> dict:
    """v0.7 §1.4 agent status endpoint. HMAC-authenticated.

    The orch client (bootstrapper's Wait-ForEnrollment + the running
    orch-client service) polls this endpoint during enrollment and
    afterwards. Returns the agent's current status (one of:
    'verifying', 'verified', 'blocked', 'suspended') plus timestamps.

    Authentication is via the v0.7 §1.4 7-header format
    (require_hmac_auth_v07 dependency). The verifier:
      1. Checks all 7 X-Hermes-* headers are present
      2. Validates timestamp within window
      3. Rejects query strings (v0.7 §1.4 forbids)
      4. Validates body SHA-256 (always the empty-body hash for GET)
      5. Looks up the agent by hmac_key_id (NOT by id)
      6. Constant-time compares the signature
      7. Rejects replayed nonces

    On success, the verifier returns the looked-up agent_id. We
    then check that it matches the URL path's {agent_id} (so a
    validly-signed v0.7 request for agent A can't read agent B's
    status — same security check as v1.6 heartbeat).

    Returns: {"status": "...", "agent_id": "...", "last_heartbeat_at": "..."}
    The 1-field "status" response is what the bootstrapper's
    Wait-ForEnrollment polls for (it returns when status == "verified").
    """
    # Defense in depth: the URL {agent_id} must match the
    # verifier-returned auth_agent_id (looked up by hmac_key_id).
    if auth_agent_id != agent_id:
        raise HTTPException(
            401,
            f"Auth agent_id ({auth_agent_id}) does not match URL "
            f"path agent_id ({agent_id})",
        )

    db = request.app.state.db
    row = await db.fetchone(
        "SELECT id, status, last_heartbeat_at FROM agents WHERE id = ?",
        (agent_id,),
    )
    if not row:
        raise HTTPException(404, f"Agent not found: {agent_id}")

    return {
        "agent_id": row["id"],
        "status": row.get("status") or "unknown",
        "last_heartbeat_at": row.get("last_heartbeat_at"),
    }


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    request: Request,
    user: dict = Depends(require_admin),
    _csrf: None = Depends(require_same_origin),
) -> Response:
    """Delete an agent.

    - Marks in-flight tasks (assigned/running) as failed (with reason 'agent deleted')
    - CASCADE deletes agent_profiles
    - CASCADE deletes any DB rows referencing agent (heartbeat, etc.)

    Security (B12 hotfix 2026-08-11): admin-gated. This is the
    B12 highest-priority route (any caller on the LAN could previously
    DELETE any agent without auth). Admin identity is recorded in the
    audit log.
    """
    db = request.app.state.db
    agent = await db.fetchone("SELECT id FROM agents WHERE id = ?", (agent_id,))
    if not agent:
        raise HTTPException(404, f"Agent not found: {agent_id}")

    # Mark in-flight tasks as failed
    now = _now_iso()
    await db.execute(
        "UPDATE tasks SET status = 'failed', error = ?, updated_at = ? "
        "WHERE assigned_agent_id = ? AND status IN ('assigned', 'running')",
        (f"agent '{agent_id}' deleted", now, agent_id),
    )

    # Audit log (before delete, in case of cascade issues)
    await audit_log(
        db, "agent.deleted",
        actor=f"admin:{user['username']}",
        agent_id=agent_id,
        payload={
            # B12: caller identity + route
            "remote_addr": request.client.host if request.client else None,
            "route": "DELETE /api/agents/{id}",
        },
    )

    # Delete (CASCADE removes profiles via FK)
    await db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    return Response(status_code=204)


@router.post("/{agent_id}/rotate-key")
async def rotate_key(
    agent_id: str,
    request: Request,
    user: dict = Depends(require_admin),
    _csrf: None = Depends(require_same_origin),
) -> dict:
    """Rotate agent's secret. Old key valid for grace period (default 7 days).

    Returns: new_secret + old_secret_expires_at

    Security (B12 hotfix 2026-08-11): admin-gated. Previously any caller
    could rotate any agent's key and receive the new secret in the
    response, which is a permanent identity-takeover vector.
    """
    db = request.app.state.db
    agent = await db.fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))
    if not agent:
        raise HTTPException(404, f"Agent not found: {agent_id}")

    cfg = request.app.state.config
    grace_days = cfg["auth"]["key_grace_period_days"]
    new_secret = secrets.token_urlsafe(32)
    new_hash = _hash_secret(new_secret)
    expires = (datetime.now(timezone.utc) + timedelta(days=grace_days)).isoformat()

    await db.execute(
        "UPDATE agents SET old_secret_hash = ?, old_secret_expires_at = ?, secret_hash = ? WHERE id = ?",
        (agent["secret_hash"], expires, new_hash, agent_id),
    )
    await audit_log(
        db, "agent.key_rotated",
        actor=f"admin:{user['username']}",
        agent_id=agent_id,
        payload={
            "old_expires_at": expires,
            # B12: caller identity + route
            "remote_addr": request.client.host if request.client else None,
            "route": "POST /api/agents/{id}/rotate-key",
        },
    )

    return {
        "agent_id": agent_id,
        "new_secret": new_secret,
        "old_secret_expires_at": expires,
        "setup_instructions": (
            f"Replace the secret on the agent machine:\n"
            f'  echo "{new_secret}" > ~/.hermes-orchestrator/.secret-{agent_id}\n'
            f"  chmod 600 ~/.hermes-orchestrator/.secret-{agent_id}\n"
            f"Old key valid until: {expires}"
        ),
    }


# ===== Profile endpoints =====


@router.post("/{agent_id}/profiles", response_model=AgentProfile, status_code=201)
async def add_profile(
    agent_id: str,
    body: AgentProfileCreate,
    request: Request,
    user: dict = Depends(require_admin),
    _csrf: None = Depends(require_same_origin),
) -> AgentProfile:
    """Add a new profile to an existing agent.

    Security (B12 hotfix 2026-08-11): admin-gated.
    """
    db = request.app.state.db
    agent = await db.fetchone("SELECT id FROM agents WHERE id = ?", (agent_id,))
    if not agent:
        raise HTTPException(404, f"Agent not found: {agent_id}")

    existing = await db.fetchone(
        "SELECT id FROM agent_profiles WHERE agent_id = ? AND name = ?",
        (agent_id, body.name),
    )
    if existing:
        raise HTTPException(409, f"Profile already exists: {body.name}")

    profile_id = str(uuid.uuid4())
    cleaned_refs: list[dict] = []
    for s in (body.storage_refs or []):
        if isinstance(s, dict) and s.get("kind") and s.get("ref"):
            name = s.get("name")
            cleaned_refs.append({
                "name": str(name).strip() if name else None,
                "kind": str(s["kind"]),
                "ref": str(s["ref"]),
                "description": str(s.get("description", "")),
            })
    # v3.9.0 (SOUL routing): normalize skills to a list[str] of
    # non-empty strings. Drop empties / non-string entries silently so
    # a bad input doesn't 400 the whole profile creation.
    cleaned_skills: list[str] = [
        str(s).strip() for s in (body.skills or []) if str(s).strip()
    ]
    # v3.13.0: explicit profile root path. Treat empty string the
    # same as null (both mean "no explicit root, use auto-derive").
    # The OS-specific path syntax (C:\..., \\nas\..., /...) is
    # passed through unchanged — the wrapper on the agent host is
    # responsible for interpreting it.
    cleaned_root_path: str | None = None
    if body.root_path is not None and str(body.root_path).strip():
        cleaned_root_path = str(body.root_path).strip()
    await db.insert(
        "agent_profiles",
        {
            "id": profile_id,
            "agent_id": agent_id,
            "name": body.name,
            "description": body.description,
            "status": "idle",
            "capabilities": json.dumps(body.capabilities or {}),
            "storage_refs": json.dumps(cleaned_refs),
            "skills": json.dumps(cleaned_skills),
            "root_path": cleaned_root_path,
        },
    )
    row = await db.fetchone("SELECT * FROM agent_profiles WHERE id = ?", (profile_id,))
    await audit_log(
        db, "agent.profile_added",
        actor=f"admin:{user['username']}",
        agent_id=agent_id,
        payload={
            "profile_name": body.name,
            "description": body.description,
            "capabilities": body.capabilities or {},
            "skills": cleaned_skills,
            "root_path": cleaned_root_path,
            # B12: caller identity + route
            "remote_addr": request.client.host if request.client else None,
            "route": "POST /api/agents/{id}/profiles",
        },
    )
    return _row_to_profile(row)


@router.delete("/{agent_id}/profiles/{profile_name}", status_code=204)
async def remove_profile(
    agent_id: str,
    profile_name: str,
    request: Request,
    user: dict = Depends(require_admin),
    _csrf: None = Depends(require_same_origin),
) -> Response:
    """Remove a profile (fails if profile has in-flight task).

    Security (B12 hotfix 2026-08-11): admin-gated.
    """
    db = request.app.state.db
    profile = await db.fetchone(
        "SELECT * FROM agent_profiles WHERE agent_id = ? AND name = ?",
        (agent_id, profile_name),
    )
    if not profile:
        raise HTTPException(404, f"Profile not found: {profile_name}")

    task = await db.fetchone(
        "SELECT id FROM tasks WHERE assigned_profile_id = ? AND status IN ('assigned', 'running')",
        (profile["id"],),
    )
    if task:
        raise HTTPException(400, f"Profile has in-flight task: {task['id']}")

    await db.execute("DELETE FROM agent_profiles WHERE id = ?", (profile["id"],))
    await audit_log(
        db, "agent.profile_removed",
        actor=f"admin:{user['username']}",
        agent_id=agent_id,
        payload={
            "profile_name": profile_name,
            # B12: caller identity + route
            "remote_addr": request.client.host if request.client else None,
            "route": "DELETE /api/agents/{id}/profiles/{name}",
        },
    )
    return Response(status_code=204)


@router.patch("/{agent_id}/profiles/{profile_name}", response_model=AgentProfile)
async def update_profile(
    agent_id: str,
    profile_name: str,
    body: AgentProfileUpdate,
    request: Request,
    user: dict = Depends(require_admin),
    _csrf: None = Depends(require_same_origin),
) -> AgentProfile:
    """Update a profile (description).

    Security (B12 hotfix 2026-08-11): admin-gated.
    """
    db = request.app.state.db
    profile = await db.fetchone(
        "SELECT * FROM agent_profiles WHERE agent_id = ? AND name = ?",
        (agent_id, profile_name),
    )
    if not profile:
        raise HTTPException(404, f"Profile not found: {profile_name}")

    if body.description is not None:
        await db.execute(
            "UPDATE agent_profiles SET description = ? WHERE id = ?",
            (body.description, profile["id"]),
        )
    if body.capabilities is not None:
        await db.execute(
            "UPDATE agent_profiles SET capabilities = ? WHERE id = ?",
            (json.dumps(body.capabilities), profile["id"]),
        )
    # v3.9.0 (SOUL routing): only update skills when the caller
    # explicitly provided the field. None = leave unchanged (backward
    # compat). Same defensive normalize as add_profile — drop empties
    # / non-strings silently.
    if body.skills is not None:
        cleaned_skills: list[str] = [
            str(s).strip() for s in body.skills if str(s).strip()
        ]
        await db.execute(
            "UPDATE agent_profiles SET skills = ? WHERE id = ?",
            (json.dumps(cleaned_skills), profile["id"]),
        )
    if body.storage_refs is not None:
        # Validate each entry: kind non-empty, ref non-empty. Drop
        # malformed entries silently rather than 400 (operator can
        # fix the bad entry from the UI). body.storage_refs is
        # list[StorageRef] (Pydantic models) — access via attribute,
        # not dict key. Defensive: also handle the dict case in case
        # a non-Pydantic caller sends raw dicts.
        cleaned_refs: list[dict] = []
        for s in body.storage_refs:
            if isinstance(s, dict):
                kind = s.get("kind")
                ref = s.get("ref")
                desc = s.get("description", "")
                name = s.get("name")
            elif hasattr(s, "kind"):
                # Pydantic v2 StorageRef
                kind = getattr(s, "kind", None)
                ref = getattr(s, "ref", None)
                desc = getattr(s, "description", "")
                name = getattr(s, "name", None)
            else:
                continue
            if kind and ref:
                cleaned_refs.append({
                    "name": str(name).strip() if name else None,
                    "kind": str(kind),
                    "ref": str(ref),
                    "description": str(desc) if desc else "",
                })
        await db.execute(
            "UPDATE agent_profiles SET storage_refs = ? WHERE id = ?",
            (json.dumps(cleaned_refs), profile["id"]),
        )
    # v3.13.0: explicit profile root path. We need to distinguish
    # "user didn't send root_path" (leave unchanged) from "user sent
    # root_path=null" (clear it). Pydantic v2's `model_fields_set` gives
    # us exactly this — the set of fields the caller explicitly set.
    #   not in model_fields_set = leave unchanged (backward compat
    #                                for existing PATCH calls that
    #                                don't touch root_path)
    #   root_path in model_fields_set AND value is None/"" = clear
    #   root_path in model_fields_set AND value is "C:\..." = set
    # The OS-specific path syntax is passed through unchanged; the
    # wrapper on the agent host interprets it. We do NOT verify the
    # path exists — that's the wrapper's responsibility.
    if "root_path" in body.model_fields_set:
        # Caller explicitly sent root_path (or null). Apply it.
        raw_value = body.root_path
        cleaned_root_path: str | None = (
            str(raw_value).strip() if raw_value is not None else None
        ) or None  # empty string → None
        await db.execute(
            "UPDATE agent_profiles SET root_path = ? WHERE id = ?",
            (cleaned_root_path, profile["id"]),
        )
    row = await db.fetchone("SELECT * FROM agent_profiles WHERE id = ?", (profile["id"],))
    # Audit log payload — convert Pydantic StorageRef list to plain
    # dicts so json.dumps (in core/audit.py) can serialize. Audit
    # is informational ("operator set these refs") — full fidelity
    # not needed; just preserve kind + ref + description. Note:
    # body.storage_refs is list[StorageRef] (Pydantic models), so
    # we access via attribute (s.kind) not dict key (s["kind"]).
    srefs_audit: list[dict] = []
    if body.storage_refs is not None:
        for s in body.storage_refs:
            kind = getattr(s, "kind", None)
            ref = getattr(s, "ref", None)
            if kind and ref:
                name = getattr(s, "name", None)
                srefs_audit.append({
                    "name": str(name).strip() if name else None,
                    "kind": str(kind),
                    "ref": str(ref),
                    "description": str(getattr(s, "description", "")),
                })
    await audit_log(
        db, "agent.profile_updated",
        actor=f"admin:{user['username']}",
        agent_id=agent_id,
        payload={
            "profile_name": profile_name,
            "description": body.description,
            "capabilities": body.capabilities,
            "storage_refs": srefs_audit,
            # v3.9.0 (SOUL routing): surface the new skills field too
            # so the audit log captures what the operator changed.
            "skills": body.skills,
            # B12: caller identity + route
            "remote_addr": request.client.host if request.client else None,
            "route": "PATCH /api/agents/{id}/profiles/{name}",
        },
    )
    return _row_to_profile(row)


# ===== Profile config endpoints (soul.md etc., wrapper-mediated) =====


def _row_to_config(row: dict[str, Any]) -> ProfileConfig:
    return ProfileConfig(
        id=row["id"],
        profile_id=row["profile_id"],
        file_path=row["file_path"],
        desired_sha256=row["desired_sha256"],
        desired_content=row["desired_content"],
        status=row["status"],
        error=row.get("error"),
        created_at=row.get("created_at"),
        applied_at=row.get("applied_at"),
    )


async def _find_profile(db: Any, agent_id: str, profile_name: str) -> dict[str, Any]:
    profile = await db.fetchone(
        "SELECT * FROM agent_profiles WHERE agent_id = ? AND name = ?",
        (agent_id, profile_name),
    )
    if not profile:
        raise HTTPException(404, f"Profile not found: {profile_name}")
    return profile


@router.get("/{agent_id}/profiles/{profile_name}/configs", response_model=list[ProfileConfig])
async def list_configs(
    agent_id: str, profile_name: str, request: Request
) -> list[ProfileConfig]:
    """List all configs for a profile (newest first)."""
    db = request.app.state.db
    profile = await _find_profile(db, agent_id, profile_name)
    rows = await db.fetchall(
        "SELECT * FROM profile_configs WHERE profile_id = ? ORDER BY created_at DESC",
        (profile["id"],),
    )
    return [_row_to_config(r) for r in rows]


@router.post(
    "/{agent_id}/profiles/{profile_name}/configs",
    response_model=ProfileConfig,
    status_code=201,
)
async def create_config(
    agent_id: str, profile_name: str, body: ProfileConfigCreate, request: Request
) -> ProfileConfig:
    """Submit a new desired config (e.g. soul.md content). status=pending.

    Wrapper polls /configs/pending, applies, and acks.
    """
    db = request.app.state.db
    profile = await _find_profile(db, agent_id, profile_name)
    cfg_id = str(uuid.uuid4())
    sha = hashlib.sha256(body.content.encode()).hexdigest()
    await db.insert(
        "profile_configs",
        {
            "id": cfg_id,
            "profile_id": profile["id"],
            "file_path": body.file_path,
            "desired_sha256": sha,
            "desired_content": body.content,
            "status": "pending",
        },
    )
    row = await db.fetchone("SELECT * FROM profile_configs WHERE id = ?", (cfg_id,))
    await audit_log(
        db, "profile.config_submitted",
        actor="operator",
        agent_id=agent_id,
        payload={
            "profile_name": profile_name,
            "file_path": body.file_path,
            "sha256": sha,
            "size": len(body.content),
        },
    )
    return _row_to_config(row)


@router.get(
    "/{agent_id}/profiles/{profile_name}/configs/pending",
    response_model=ProfileConfig | None,
)
async def claim_pending_config(
    agent_id: str,
    profile_name: str,
    request: Request,
    x_agent_id: str = Depends(require_hmac_auth),
) -> ProfileConfig | None:
    """Wrapper poll: atomically claim the oldest pending config.

    Marks status='applying' so other polls don't grab it. Returns None if none.
    v1.6: HMAC-authed.
    """
    if x_agent_id != agent_id:
        raise HTTPException(
            401, f"X-Agent-Id ({x_agent_id}) does not match URL ({agent_id})"
        )
    db = request.app.state.db
    profile = await _find_profile(db, agent_id, profile_name)

    # Atomic claim: pick oldest pending then flip to applying.
    # Use the fetch+check+update pattern (see REVIEW §3.1) — state changes are
    # always the target state, never "update WHERE status=pending" because
    # between SELECT and UPDATE another worker may have already claimed it.
    row = await db.fetchone(
        "SELECT * FROM profile_configs WHERE profile_id = ? AND status = 'pending' "
        "ORDER BY created_at ASC LIMIT 1",
        (profile["id"],),
    )
    if not row:
        return None
    now = _now_iso()
    await db.execute(
        "UPDATE profile_configs SET status = 'applying' WHERE id = ? AND status = 'pending'",
        (row["id"],),
    )
    # Re-fetch to confirm
    claimed = await db.fetchone(
        "SELECT * FROM profile_configs WHERE id = ?", (row["id"],)
    )
    if not claimed or claimed["status"] != "applying":
        # Lost race; try once more (rare, but safe)
        return await claim_pending_config(agent_id, profile_name, request)
    await audit_log(
        db, "profile.config_claimed",
        actor="wrapper",
        agent_id=agent_id,
        payload={"profile_name": profile_name, "config_id": row["id"]},
    )
    return _row_to_config(claimed)


@router.post(
    "/{agent_id}/profiles/{profile_name}/configs/{cfg_id}/ack",
    response_model=ProfileConfig,
)
async def ack_config(
    agent_id: str,
    profile_name: str,
    cfg_id: str,
    body: ProfileConfigAck,
    request: Request,
    x_agent_id: str = Depends(require_hmac_auth),
) -> ProfileConfig:
    """Wrapper ack after attempting to write the file. status=applied|failed.

    v1.6: HMAC-authed.
    """
    if x_agent_id != agent_id:
        raise HTTPException(
            401, f"X-Agent-Id ({x_agent_id}) does not match URL ({agent_id})"
        )
    db = request.app.state.db
    profile = await _find_profile(db, agent_id, profile_name)

    row = await db.fetchone(
        "SELECT * FROM profile_configs WHERE id = ? AND profile_id = ?",
        (cfg_id, profile["id"]),
    )
    if not row:
        raise HTTPException(404, f"Config not found: {cfg_id}")
    if row["status"] not in ("applying", "pending"):
        raise HTTPException(409, f"Config not in apply-able state: {row['status']}")

    if body.status not in ("applied", "failed"):
        raise HTTPException(400, f"Invalid ack status: {body.status}")

    now = _now_iso()
    if body.status == "applied":
        await db.execute(
            "UPDATE profile_configs SET status = 'applied', applied_at = ? WHERE id = ?",
            (now, cfg_id),
        )
    else:
        await db.execute(
            "UPDATE profile_configs SET status = 'failed', error = ? WHERE id = ?",
            (body.error or "unknown error", cfg_id),
        )
    updated = await db.fetchone("SELECT * FROM profile_configs WHERE id = ?", (cfg_id,))
    await audit_log(
        db, "profile.config_acked",
        actor="wrapper",
        agent_id=agent_id,
        payload={
            "profile_name": profile_name,
            "config_id": cfg_id,
            "status": body.status,
            "error": body.error,
            "actual_sha256": body.actual_sha256,
        },
    )
    return _row_to_config(updated)


@router.post(
    "/{agent_id}/profiles/{profile_name}/soul/reset",
    response_model=ProfileConfig,
    status_code=201,
)
async def reset_live_soul(
    agent_id: str,
    profile_name: str,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> ProfileConfig:
    """Reset a profile's live SOUL.md to empty (admin only).

    v3.9.0 (Phase 3): writes a `profile_configs` row with
    `desired_content=''` (sha256 of empty string). The wrapper
    picks it up on the next tick and writes an empty SOUL.md
    on the host — the agent then falls back to its default
    persona (whatever the hermes-agent binary ships with).

    Intended for fleet-reset: the operator just spent a few hours
    debugging a bad persona and wants the agent to start fresh.
    The existing preset rows in the DB are NOT touched (the
    preset is the project's snapshot, not the live file) — the
    operator can re-apply a preset to restore a persona.

    The `apply` path (api/projects.py::apply_soul_presets) uses
    the same `profile_configs` flow, so a reset can be safely
    interleaved with applies (they serialize per-profile via
    the atomic claim in `claim_pending_config`).

    Idempotent in the sense that two consecutive resets produce
    the same end state (empty SOUL.md). The two rows are still
    both written (no SHA256 dedup on the reset path) so the
    audit trail is intact.

    Admin only — see the dependency. The wrapper's ack
    endpoint (`/configs/{id}/ack`) confirms the file was
    written; the caller can poll the row to see `status`.
    """
    import hashlib as _hashlib
    import uuid as _uuid

    db = request.app.state.db
    profile = await _find_profile(db, agent_id, profile_name)
    empty_sha = _hashlib.sha256(b"").hexdigest()
    cfg_id = str(_uuid.uuid4())
    await db.insert(
        "profile_configs",
        {
            "id": cfg_id,
            "profile_id": profile["id"],
            "file_path": "SOUL.md",
            "desired_sha256": empty_sha,
            "desired_content": "",
            "status": "pending",
        },
    )
    row = await db.fetchone(
        "SELECT * FROM profile_configs WHERE id = ?", (cfg_id,)
    )
    await audit_log(
        db, "profile.soul_reset",
        actor=admin.get("username") or "admin",
        agent_id=agent_id,
        payload={
            "profile_name": profile_name,
            "config_id": cfg_id,
            "desired_sha256": empty_sha,
            "size": 0,
        },
    )
    return _row_to_config(row)


@router.post("/{agent_id}/sessions/{session_id}/cleanup-ack")
async def session_cleanup_ack(
    agent_id: str,
    session_id: str,
    request: Request,
    x_agent_id: str = Depends(require_hmac_auth),
) -> dict:
    """Wrapper called this after deleting the hermes session locally.

    v1.6: HMAC-authed. X-Agent-Id must match the URL agent_id.

    Flips the matching project_sessions row from `pending_cleanup` to
    `deleted`. Idempotent: if the row is already `deleted` (e.g. another
    ack raced us), the call returns 200 with `already_deleted: true`.

    We match by (agent_id, session_id) — the role can vary (e.g. coord
    review tasks delete a session belonging to a different profile than
    the wrapper's own, but the wrapper's role is the one that owned the
    hermes session in its local backend).
    """
    if x_agent_id != agent_id:
        raise HTTPException(
            401, f"X-Agent-Id ({x_agent_id}) does not match URL ({agent_id})"
        )
    db = request.app.state.db
    # Match against project_sessions rows where the session_id is owned
    # by a profile belonging to this agent.
    rows = await db.fetchall(
        "SELECT ps.id, ps.role FROM project_sessions ps "
        "JOIN agent_profiles ap ON ap.id = ps.profile_id "
        "WHERE ap.agent_id = ? AND ps.session_id = ? "
        "AND ps.status = 'pending_cleanup'",
        (agent_id, session_id),
    )
    if not rows:
        # Already deleted, or never existed, or no matching pending row.
        # Return 200 with already_deleted=true to keep the wrapper
        # idempotent.
        return {
            "ok": True,
            "already_deleted": True,
            "session_id": session_id,
        }
    now = _now_iso()
    deleted = 0
    for row in rows:
        await db.execute(
            "UPDATE project_sessions SET status = 'deleted', deleted_at = ? "
            "WHERE id = ?",
            (now, row["id"]),
        )
        deleted += 1
        await audit_log(
            db, "project.session_cleaned",
            actor="wrapper",
            agent_id=agent_id,
            payload={"session_id": session_id, "role": row["role"]},
        )
    return {
        "ok": True,
        "deleted": deleted,
        "session_id": session_id,
    }


# ===== Skill endpoints (skills/<name>.md via profile_configs) =====


import re as _re

# Skill names map directly to a relative path under the profile root:
#   profile_root / skills / <name> / SKILL.md       (flat: "xlsx")
#   profile_root / skills / <cat> / <name> / SKILL.md   (subfolder: "productivity/xlsx")
# We restrict each path segment to a safe filename subset so it can never
# escape the skills/ directory on the agent host. The wrapper applies via
# atomic_write(profile_root / file_path, content); with this rule the
# resolved path always stays inside the profile.
#
# Subfolder support was added 2026-07-24 alongside the wrapper's
# recursive rglob("SKILL.md") discovery (commit 56f9ed0). We allow at
# most one level of nesting ("<cat>/<name>") to keep the API surface
# simple and match the wrapper's depth cap. Anything deeper (e.g.
# "a/b/c/SKILL.md") is rejected here; the wrapper also skips it.
_SKILL_SEGMENT_RE = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_SKILL_NAME_RE = _re.compile(
    rf"^(?:{_SKILL_SEGMENT_RE}|{_SKILL_SEGMENT_RE}/{_SKILL_SEGMENT_RE})$"
)


def _validate_skill_name(name: str) -> str:
    """Validate and return a safe skill name, else raise HTTPException.

    Accepts either a flat name ("xlsx") or a one-level subfolder
    category ("productivity/xlsx"). Rejects empty, ".", "..", and any
    name with more than one "/".
    """
    if not name or not isinstance(name, str):
        raise HTTPException(400, "skill name is required")
    if name in (".", "..") or not _SKILL_NAME_RE.match(name):
        raise HTTPException(
            400,
            f"invalid skill name {name!r}: must match {_SKILL_NAME_RE.pattern} "
            f"and not be '.' or '..'",
        )
    return name


def _skill_file_path(skill_name: str) -> str:
    """Map a skill name to its canonical file_path in profile_configs.

    Hermes 0.17+ (and later) only reads the folder layout
    `skills/<name>/SKILL.md`. We dropped flat-file support entirely
    on 2026-07-19 (commit d5b7c9a follow-up to a7516ba) because
    every wrapper-uploaded flat-path skill was a no-op anyway --
    the file never got read by hermes. See also the
    SKILL LAYOUT BUG entry in agent memory for the full history.
    """
    return f"skills/{skill_name}/SKILL.md"


async def _latest_skill_config(db: Any, profile_id: str, skill_name: str) -> dict[str, Any] | None:
    """Return the newest profile_configs row for a skill, or None.

    Searches only the canonical folder path
    (skills/<name>/SKILL.md) since flat-path support was dropped
    on 2026-07-19 (commit d5b7c9a).
    """
    return await db.fetchone(
        "SELECT * FROM profile_configs WHERE profile_id = ? AND file_path = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (profile_id, _skill_file_path(skill_name)),
    )


def _row_to_skill(row: dict[str, Any], include_content: bool = False) -> SkillInfo:
    """Build a SkillInfo from a profile_configs row.

    'status' semantics from the dashboard's point of view:
      - applied + non-empty content  -> 'applied'
      - applied + empty content      -> 'deleted' (file was removed on host)
      - pending                      -> 'pending' (waiting for wrapper)
      - applying                     -> 'applying'
      - failed                       -> 'failed' (check `error`)
    """
    raw_status = row.get("status", "pending")
    content = row["desired_content"] or ""
    if raw_status == "applied":
        status = "deleted" if content == "" else "applied"
    else:
        status = raw_status
    content_bytes = content.encode("utf-8") if content else b""
    # Path is skills/<name>/SKILL.md -- extract <name>
    fp = row["file_path"]
    if fp.endswith("/SKILL.md"):
        name = fp[len("skills/"):-len("/SKILL.md")]
    else:
        # Defensive: should not happen post-d5b7c9a, but if a flat
        # record sneaks in via direct DB write, fall back gracefully.
        name = fp.removeprefix("skills/").removesuffix(".md")
    return SkillInfo(
        name=name,
        file_path=row["file_path"],
        status=status,
        size=len(content_bytes),
        sha256=row.get("desired_sha256"),
        created_at=row.get("created_at"),
        applied_at=row.get("applied_at"),
        error=row.get("error"),
        content=content if include_content else None,
    )


@router.get(
    "/{agent_id}/profiles/{profile_name}/skills",
    response_model=list[SkillInfo],
)
async def list_skills(
    agent_id: str, profile_name: str, request: Request,
    x_agent_id: str = Depends(require_hmac_auth),
) -> list[SkillInfo]:
    """List all skills known for this profile, with their latest status (v1.6: HMAC)."""
    if x_agent_id != agent_id:
        raise HTTPException(
            401, f"X-Agent-Id ({x_agent_id}) does not match URL ({agent_id})"
        )
    db = request.app.state.db
    profile = await _find_profile(db, agent_id, profile_name)
    # Sort: flat skills first (alphabetical), then subfolder skills
    # (also alphabetical — the full "cat/name" string sorts as
    # "category prefix then skill name" naturally). The CASE puts
    # flat (no '/') at the top, subfolder at the bottom. User
    # asked for this ordering on 2026-07-24 because the previous
    # created_at DESC order was just insertion-order and made the
    # list hard to scan when there's a mix of flat and subfolder.
    # CRITICAL: must ORDER BY created_at DESC so the wrapper's SHA cache
    # (agent_cli.py:2446) hits the NEWEST row's sha256 — otherwise it
    # sees an old row's sha and thinks the file changed, re-uploads, and
    # we grow profile_configs unboundedly. 2026-07-25: discovered 35k
    # duplicate rows in production because of this missing DESC.
    rows = await db.fetchall(
        "SELECT * FROM profile_configs WHERE profile_id = ? "
        "AND file_path LIKE 'skills/%/SKILL.md' "
        "ORDER BY (CASE WHEN file_path LIKE 'skills/%/%/SKILL.md' THEN 1 ELSE 0 END), "
        "file_path ASC, created_at DESC",
        (profile["id"],),
    )
    include_deleted = request.query_params.get("include_deleted") == "1"
    # Flat-path support dropped 2026-07-19 (commit d5b7c9a). The newest
    # row per (profile, skill_name) wins — guaranteed by the
    # `created_at DESC` tiebreak in the ORDER BY above. The dedup
    # loop preserves the (flat-first, subfolder, alphabetical) order
    # from the primary ORDER BY.
    seen: set[str] = set()
    out: list[SkillInfo] = []
    for r in rows:
        # Path is skills/<name>/SKILL.md
        name = r["file_path"][len("skills/"):-len("/SKILL.md")]
        if name in seen:
            continue
        seen.add(name)
        info = _row_to_skill(r, include_content=False)
        if info.status == "deleted" and not include_deleted:
            continue
        out.append(info)
    return out


@router.get(
    "/{agent_id}/profiles/{profile_name}/skills/{skill_name:path}",
    response_model=SkillInfo,
)
async def get_skill(
    agent_id: str, profile_name: str, skill_name: str, request: Request,
    x_agent_id: str = Depends(require_hmac_auth),
) -> SkillInfo:
    """Get the latest version of a single skill, including its content (v1.6: HMAC)."""
    if x_agent_id != agent_id:
        raise HTTPException(
            401, f"X-Agent-Id ({x_agent_id}) does not match URL ({agent_id})"
        )
    db = request.app.state.db
    profile = await _find_profile(db, agent_id, profile_name)
    name = _validate_skill_name(skill_name)
    row = await _latest_skill_config(db, profile["id"], name)
    if not row:
        raise HTTPException(404, f"Skill not found: {name}")
    return _row_to_skill(row, include_content=True)


@router.post(
    "/{agent_id}/profiles/{profile_name}/skills",
    response_model=ProfileConfig,
    status_code=201,
)
async def create_or_update_skill(
    agent_id: str, profile_name: str, body: SkillCreate, request: Request, response: Response,
    x_agent_id: str = Depends(require_hmac_auth),
) -> ProfileConfig:
    """Create or update a skill (UPSERT by content SHA, v1.6: HMAC)."""
    if x_agent_id != agent_id:
        raise HTTPException(
            401, f"X-Agent-Id ({x_agent_id}) does not match URL ({agent_id})"
        )
    # v3.12.1 follow-up #7b: this function was missing the
    # `db = request.app.state.db` line that every other route in
    # this file has. The line 1780 `_find_profile(db, ...)` call
    # therefore raised NameError, and EVERY skill POST 500'd with
    # an unhandled exception -- even after the v3.12.1 #7
    # Content-Type fix on the wrapper side. This blocks the
    # 4x -> 1.3x e2e verification, which depends on
    # skills-sync working end-to-end.
    db = request.app.state.db
    profile = await _find_profile(db, agent_id, profile_name)
    name = _validate_skill_name(body.name)
    content = body.content or ""
    sha = hashlib.sha256(content.encode()).hexdigest()
    file_path = _skill_file_path(name)

    # UPSERT check: if the latest row for this (profile, file_path)
    # already has the same SHA, return it without inserting. This is
    # the defense-in-depth check that would have prevented the 35k-row
    # bloat of 2026-07-25 even if the upstream list_skills SQL had
    # been wrong. We look up the newest row first to also cover the
    # "operator edits, saves same content" case (e.g. accidental
    # double-click on Save).
    existing = await db.fetchone(
        "SELECT * FROM profile_configs "
        "WHERE profile_id = ? AND file_path = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (profile["id"], file_path),
    )
    if existing and existing["desired_sha256"] == sha:
        # No-op: content unchanged. Return 200 + existing row.
        # The wrapper checks `if r.status_code == 201` to decide
        # whether to ack — 200 means "server already has this
        # content, no need to apply or ack".
        response.status_code = 200
        return _row_to_config(existing)

    cfg_id = str(uuid.uuid4())
    await db.insert(
        "profile_configs",
        {
            "id": cfg_id,
            "profile_id": profile["id"],
            "file_path": file_path,
            "desired_sha256": sha,
            "desired_content": content,
            "status": "pending",
        },
    )
    row = await db.fetchone("SELECT * FROM profile_configs WHERE id = ?", (cfg_id,))
    skill_source = request.headers.get("X-Skill-Source", "").strip()
    actor = "wrapper:self-taught" if skill_source == "self-taught" else "operator"
    await audit_log(
        db, "profile.skill_submitted",
        actor=actor,
        agent_id=agent_id,
        payload={
            "profile_name": profile_name,
            "skill_name": name,
            "file_path": file_path,
            "sha256": sha,
            "size": len(content),
            "operation": "delete" if content == "" else "upsert",
            "source": skill_source or "operator",
        },
    )
    return _row_to_config(row)


@router.post(
    "/{agent_id}/profiles/{profile_name}/skills/sync",
    response_model=ProfileConfig,
    status_code=201,
)
async def request_skill_sync(
    agent_id: str, profile_name: str, request: Request
) -> ProfileConfig:
    """Trigger a reverse sync: scan the agent's `<profile>/skills/` dir on
    the agent host and register any new/changed files into profile_configs.

    Implementation: we don't read the filesystem here (orchestrator and
    agent are usually on different hosts). Instead we drop a marker config
    with file_path="__sync_skills__" and empty content. The wrapper's
    apply-config loop treats that marker as a sync trigger — it scans the
    local skills/ dir and pushes any new/changed files back to us via
    POST .../skills (with X-Skill-Source: self-taught). The marker is then
    acked as applied, so it doesn't fire again.
    """
    db = request.app.state.db
    profile = await _find_profile(db, agent_id, profile_name)
    cfg_id = str(uuid.uuid4())
    marker_path = "__sync_skills__"
    empty_sha = hashlib.sha256(b"").hexdigest()
    await db.insert(
        "profile_configs",
        {
            "id": cfg_id,
            "profile_id": profile["id"],
            "file_path": marker_path,
            "desired_sha256": empty_sha,
            "desired_content": "",
            "status": "pending",
        },
    )
    row = await db.fetchone("SELECT * FROM profile_configs WHERE id = ?", (cfg_id,))
    await audit_log(
        db, "profile.skills_sync_requested",
        actor="operator",
        agent_id=agent_id,
        payload={
            "profile_name": profile_name,
            "config_id": cfg_id,
            "marker": marker_path,
        },
    )
    return _row_to_config(row)



@router.delete(
    "/{agent_id}/profiles/{profile_name}/skills/{skill_name:path}",
    response_model=ProfileConfig,
    status_code=201,
)
async def delete_skill(
    agent_id: str, profile_name: str, skill_name: str, request: Request,
    x_agent_id: str = Depends(require_hmac_auth),
) -> ProfileConfig:
    """Delete a skill (v1.6: HMAC)."""
    if x_agent_id != agent_id:
        raise HTTPException(
            401, f"X-Agent-Id ({x_agent_id}) does not match URL ({agent_id})"
        )
    db = request.app.state.db
    profile = await _find_profile(db, agent_id, profile_name)
    name = _validate_skill_name(skill_name)
    # Verify the skill actually has a prior version; otherwise it's a 404
    existing = await _latest_skill_config(db, profile["id"], name)
    if not existing:
        raise HTTPException(404, f"Skill not found: {name}")
    cfg_id = str(uuid.uuid4())
    file_path = _skill_file_path(name)
    empty_sha = hashlib.sha256(b"").hexdigest()
    await db.insert(
        "profile_configs",
        {
            "id": cfg_id,
            "profile_id": profile["id"],
            "file_path": file_path,
            "desired_sha256": empty_sha,
            "desired_content": "",
            "status": "pending",
        },
    )
    row = await db.fetchone("SELECT * FROM profile_configs WHERE id = ?", (cfg_id,))
    await audit_log(
        db, "profile.skill_deleted",
        actor="operator",
        agent_id=agent_id,
        payload={
            "profile_name": profile_name,
            "skill_name": name,
            "file_path": file_path,
        },
    )
    return _row_to_config(row)


@router.post(
    "/{agent_id}/profiles/{profile_name}/skills/{skill_name:path}/copy",
    response_model=ProfileConfig,
    status_code=201,
)
async def copy_skill_to_profile(
    agent_id: str,
    profile_name: str,
    skill_name: str,
    to_profile: str,
    request: Request,
    x_agent_id: str = Depends(require_hmac_auth),
) -> ProfileConfig:
    """Copy a skill from one profile to another (same agent, v1.6: HMAC)."""
    if x_agent_id != agent_id:
        raise HTTPException(
            401, f"X-Agent-Id ({x_agent_id}) does not match URL ({agent_id})"
        )
    # Read source
    src_profile = await _find_profile(db, agent_id, profile_name)
    src_cfg = await _latest_skill_config(db, src_profile["id"], skill_name)
    if not src_cfg:
        raise HTTPException(404, f"Skill not found: {skill_name} in {profile_name}")
    src_content = src_cfg.get("desired_content") or ""
    if not src_content:
        raise HTTPException(400, f"Source skill {skill_name!r} has empty content (deleted?)")
    # Validate target
    name = _validate_skill_name(skill_name)
    dst_profile = await _find_profile(db, agent_id, to_profile)
    if dst_profile["id"] == src_profile["id"]:
        raise HTTPException(400, f"Source and target profile are the same: {profile_name}")
    # Read overwrite flag from body (default false)
    body_bytes = await request.body()
    overwrite = False
    if body_bytes:
        try:
            body = json.loads(body_bytes)
            overwrite = bool(body.get("overwrite", False))
        except Exception:
            pass
    # Check collision
    if not overwrite:
        existing = await _latest_skill_config(db, dst_profile["id"], name)
        if existing and (existing.get("desired_content") or ""):
            raise HTTPException(
                409, f"Target {to_profile} already has skill {name!r}; pass overwrite=true to replace"
            )
    # Write new pending config
    file_path = _skill_file_path(name)
    cfg_id = str(uuid.uuid4())
    sha = hashlib.sha256(src_content.encode("utf-8")).hexdigest()
    await db.insert(
        "profile_configs",
        {
            "id": cfg_id,
            "profile_id": dst_profile["id"],
            "file_path": file_path,
            "desired_sha256": sha,
            "desired_content": src_content,
            "status": "pending",
        },
    )
    row = await db.fetchone("SELECT * FROM profile_configs WHERE id = ?", (cfg_id,))
    await audit_log(
        db, "profile.skill_copied",
        actor="operator",
        agent_id=agent_id,
        payload={
            "src_profile": profile_name,
            "dst_profile": to_profile,
            "skill_name": name,
            "file_path": file_path,
            "bytes": len(src_content),
            "overwrite": overwrite,
        },
    )
    return _row_to_config(row)


# v3.3.2: bulk "publish to all profiles" — one click, all wrappers
# get it. Backend equivalent: for each OTHER profile on the same
# agent, copy the skill content (overwrite=true so re-runs are
# idempotent). The wrappers see new profile_configs rows on their
# next /configs/pending poll and apply them in parallel.
@router.post(
    "/{agent_id}/profiles/{profile_name}/skills/{skill_name:path}/sync-to-all",
)
async def sync_skill_to_all_profiles(
    agent_id: str,
    profile_name: str,
    skill_name: str,
    request: Request,
    x_agent_id: str = Depends(require_hmac_auth),
) -> dict[str, Any]:
    """Copy a skill from `profile_name` to EVERY OTHER profile on the agent.

    Idempotent: existing skills are overwritten (overwrite=true). The
    "src == dst" profile is skipped (no self-copy). The response lists
    per-profile results so the dashboard can show "pushed to 3/4
    profiles" without re-fetching.

    This is a "publish" action — useful for team-wide skills that
    every agent profile should have. Distinct from the per-target
    `copy` endpoint (line ~1664) which requires a single destination.
    """
    if x_agent_id != agent_id:
        raise HTTPException(
            401, f"X-Agent-Id ({x_agent_id}) does not match URL ({agent_id})"
        )
    db = request.app.state.db
    src_profile = await _find_profile(db, agent_id, profile_name)
    src_cfg = await _latest_skill_config(db, src_profile["id"], skill_name)
    if not src_cfg:
        raise HTTPException(404, f"Skill not found: {skill_name} in {profile_name}")
    src_content = src_cfg.get("desired_content") or ""
    if not src_content:
        raise HTTPException(400, f"Source skill {skill_name!r} has empty content (deleted?)")
    name = _validate_skill_name(skill_name)
    file_path = _skill_file_path(name)
    sha = hashlib.sha256(src_content.encode("utf-8")).hexdigest()

    other_profiles = await db.fetchall(
        "SELECT id, name FROM agent_profiles WHERE agent_id = ? AND name != ?",
        (agent_id, profile_name),
    )
    results: list[dict[str, Any]] = []
    for prof in other_profiles:
        # Always overwrite (idempotent re-run; the `delete` button is
        # the way to remove, this is the way to install/push).
        cfg_id = str(uuid.uuid4())
        await db.insert(
            "profile_configs",
            {
                "id": cfg_id,
                "profile_id": prof["id"],
                "file_path": file_path,
                "desired_sha256": sha,
                "desired_content": src_content,
                "status": "pending",
            },
        )
        results.append(
            {
                "profile": prof["name"],
                "config_id": cfg_id,
                "status": "queued",
            }
        )
    await audit_log(
        db, "profile.skill_synced_to_all",
        actor="operator",
        agent_id=agent_id,
        payload={
            "src_profile": profile_name,
            "skill_name": name,
            "file_path": file_path,
            "bytes": len(src_content),
            "profiles_pushed": len(results),
        },
    )
    return {
        "ok": True,
        "skill_name": name,
        "src_profile": profile_name,
        "pushed_count": len(results),
        "pushed_to": results,
    }

