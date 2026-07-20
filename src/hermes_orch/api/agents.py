"""Agent endpoints (per REVIEW.md §6, §6.4 multi-role Model A).

Endpoints:
- POST   /api/agents                       — register (returns one-time setup secret)
- GET    /api/agents                       — list all agents with profiles
- GET    /api/agents/{id}                  — get one agent + profiles
- PUT    /api/agents/{id}                  — update agent metadata (ip, os_type)
- DELETE /api/agents/{id}                  — delete agent
- POST   /api/agents/{id}/heartbeat        — agent heartbeat (HMAC-authed)
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

Auth (per §6.1): HMAC-SHA256 with X-Agent-Id, X-Timestamp, X-Signature.
For MVP, heartbeat verifies presence of headers (real HMAC check TODO).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from hermes_orch.core.audit import audit_log
from hermes_orch.utils import now_iso as _now_iso

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


class AgentUpdate(BaseModel):
    ip: str | None = None
    os_type: str | None = None


class AgentProfileCreate(BaseModel):
    name: str
    description: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)


class AgentProfileUpdate(BaseModel):
    description: str | None = None
    capabilities: dict[str, bool] | None = None


class HeartbeatBody(BaseModel):
    status: str | None = None  # agent's reported state (e.g. 'busy', 'idle')


class AgentProfile(BaseModel):
    id: str
    agent_id: str
    name: str
    description: str | None = None
    status: str = "idle"
    current_task_id: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)
    created_at: str | None = None


class Agent(BaseModel):
    id: str
    ip: str | None = None
    os_type: str | None = None
    status: str = "verifying"
    last_heartbeat_at: str | None = None
    created_at: str | None = None
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
    return AgentProfile(
        id=row["id"],
        agent_id=row["agent_id"],
        name=row["name"],
        description=row.get("description"),
        status=row["status"],
        current_task_id=row.get("current_task_id"),
        capabilities=caps,
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
        profiles=[_row_to_profile(p) for p in profile_rows],
    )


# ===== Agent endpoints =====


@router.post("/", response_model=AgentRegistrationResponse, status_code=201)
async def register_agent(body: AgentRegister, request: Request) -> AgentRegistrationResponse:
    """Register a new agent. Returns one-time setup secret.

    One-time secret shown in response — user copies to agent OS.
    Stored as SHA-256 hash in DB.
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
            "ip": body.ip,
            "os_type": body.os_type,
            "status": "verifying",
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
        actor="operator",
        agent_id=body.agent_id,
        payload={
            "ip": body.ip,
            "os_type": body.os_type,
            "roles_ignored": roles_ignored,
        },
    )
    return AgentRegistrationResponse(
        agent=agent,
        setup_secret=secret,
        setup_instructions=setup_instructions,
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
                profiles=[_row_to_profile(p) for p in profile_rows],
            ).model_dump()
        )
    return {"agents": agents}


@router.get("/{agent_id}", response_model=Agent)
async def get_agent(agent_id: str, request: Request) -> Agent:
    """Get agent + profiles."""
    db = request.app.state.db
    return await _agent_with_profiles(db, agent_id)


@router.put("/{agent_id}", response_model=Agent)
async def update_agent(agent_id: str, body: AgentUpdate, request: Request) -> Agent:
    """Update agent metadata (ip, os_type)."""
    db = request.app.state.db
    agent = await db.fetchone("SELECT id FROM agents WHERE id = ?", (agent_id,))
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
    if updates:
        params.append(agent_id)
        await db.execute(
            f"UPDATE agents SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
    return await _agent_with_profiles(db, agent_id)


@router.post("/{agent_id}/heartbeat")
async def heartbeat(
    agent_id: str,
    request: Request,
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> dict:
    """Agent heartbeat. HMAC-authed (per §6.1).

    For MVP: verifies presence of X-Agent-Id/X-Timestamp/X-Signature headers.
    Real HMAC signature verification TODO (when wrapper sends real requests).

    Returns list of tasks currently assigned to this agent.
    """
    if not x_agent_id or not x_timestamp or not x_signature:
        raise HTTPException(
            401, "Missing auth headers (X-Agent-Id, X-Timestamp, X-Signature)"
        )

    db = request.app.state.db
    agent = await db.fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))
    if not agent:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    if agent["id"] != x_agent_id:
        raise HTTPException(401, "X-Agent-Id does not match URL")

    # Optional body
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

    return {
        "status": "ok",
        "timestamp": now,
        "agent_status": body_data.get("status", "idle"),
        "tasks": tasks,
        "cleanup_session_ids": [r["session_id"] for r in cleanup_rows],
    }


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, request: Request) -> Response:
    """Delete an agent.

    - Marks in-flight tasks (assigned/running) as failed (with reason 'agent deleted')
    - CASCADE deletes agent_profiles
    - CASCADE deletes any DB rows referencing agent (heartbeat, etc.)
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
        actor="operator",
        agent_id=agent_id,
    )

    # Delete (CASCADE removes profiles via FK)
    await db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    return Response(status_code=204)


@router.post("/{agent_id}/rotate-key")
async def rotate_key(agent_id: str, request: Request) -> dict:
    """Rotate agent's secret. Old key valid for grace period (default 7 days).

    Returns: new_secret + old_secret_expires_at
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
        actor="operator",
        agent_id=agent_id,
        payload={"old_expires_at": expires},
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
    agent_id: str, body: AgentProfileCreate, request: Request
) -> AgentProfile:
    """Add a new profile to an existing agent."""
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
    await db.insert(
        "agent_profiles",
        {
            "id": profile_id,
            "agent_id": agent_id,
            "name": body.name,
            "description": body.description,
            "status": "idle",
            "capabilities": json.dumps(body.capabilities or {}),
        },
    )
    row = await db.fetchone("SELECT * FROM agent_profiles WHERE id = ?", (profile_id,))
    await audit_log(
        db, "agent.profile_added",
        actor="operator",
        agent_id=agent_id,
        payload={
            "profile_name": body.name,
            "description": body.description,
            "capabilities": body.capabilities or {},
        },
    )
    return _row_to_profile(row)


@router.delete("/{agent_id}/profiles/{profile_name}", status_code=204)
async def remove_profile(agent_id: str, profile_name: str, request: Request) -> Response:
    """Remove a profile (fails if profile has in-flight task)."""
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
        actor="operator",
        agent_id=agent_id,
        payload={"profile_name": profile_name},
    )
    return Response(status_code=204)


@router.patch("/{agent_id}/profiles/{profile_name}", response_model=AgentProfile)
async def update_profile(
    agent_id: str,
    profile_name: str,
    body: AgentProfileUpdate,
    request: Request,
) -> AgentProfile:
    """Update a profile (description)."""
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
    row = await db.fetchone("SELECT * FROM agent_profiles WHERE id = ?", (profile["id"],))
    await audit_log(
        db, "agent.profile_updated",
        actor="operator",
        agent_id=agent_id,
        payload={
            "profile_name": profile_name,
            "description": body.description,
            "capabilities": body.capabilities,
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
    agent_id: str, profile_name: str, request: Request
) -> ProfileConfig | None:
    """Wrapper poll: atomically claim the oldest pending config.

    Marks status='applying' so other polls don't grab it. Returns None if none.
    """
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
) -> ProfileConfig:
    """Wrapper ack after attempting to write the file. status=applied|failed."""
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


@router.post("/{agent_id}/sessions/{session_id}/cleanup-ack")
async def session_cleanup_ack(
    agent_id: str,
    session_id: str,
    request: Request,
) -> dict:
    """Wrapper called this after deleting the hermes session locally.

    Flips the matching project_sessions row from `pending_cleanup` to
    `deleted`. Idempotent: if the row is already `deleted` (e.g. another
    ack raced us), the call returns 200 with `already_deleted: true`.

    We match by (agent_id, session_id) — the role can vary (e.g. coord
    review tasks delete a session belonging to a different profile than
    the wrapper's own, but the wrapper's role is the one that owned the
    hermes session in its local backend).
    """
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

# Skill names map directly to a filename under the profile root:
#   profile_root / skills / <name>.md
# We restrict the name to a safe filename subset so it can never escape
# the skills/ directory on the agent host. The wrapper applies via
# atomic_write(profile_root / file_path, content); with this name rule
# the resolved path always stays inside the profile.
_SKILL_NAME_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _validate_skill_name(name: str) -> str:
    """Validate and return a safe skill name, else raise HTTPException."""
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
    agent_id: str, profile_name: str, request: Request
) -> list[SkillInfo]:
    """List all skills known for this profile, with their latest status.

    Iterates profile_configs rows with file_path LIKE 'skills/%', groups by
    skill name, and returns the newest version of each. The returned status
    reflects what the wrapper last did (applied/deleted/pending/etc).

    Skills that have been deleted (applied with empty content) are filtered
    out — there's no point showing them in the dashboard. The raw delete
    config row is still in profile_configs for audit purposes; get_skill
    by name still returns it (handy for debugging "why is this gone?").
    Pass `?include_deleted=1` to include them.
    """
    db = request.app.state.db
    profile = await _find_profile(db, agent_id, profile_name)
    rows = await db.fetchall(
        "SELECT * FROM profile_configs WHERE profile_id = ? "
        "AND file_path LIKE 'skills/%/SKILL.md' "
        "ORDER BY created_at DESC",
        (profile["id"],),
    )
    include_deleted = request.query_params.get("include_deleted") == "1"
    # Flat-path support dropped 2026-07-19 (commit d5b7c9a), so we
    # only ever have one record per (profile, skill_name). The
    # newest one wins by created_at DESC + LIMIT 1 dedup.
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
    # Sort by name for stable display
    out.sort(key=lambda s: s.name)
    return out


@router.get(
    "/{agent_id}/profiles/{profile_name}/skills/{skill_name}",
    response_model=SkillInfo,
)
async def get_skill(
    agent_id: str, profile_name: str, skill_name: str, request: Request
) -> SkillInfo:
    """Get the latest version of a single skill, including its content."""
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
    agent_id: str, profile_name: str, body: SkillCreate, request: Request
) -> ProfileConfig:
    """Create or update a skill.

    Appends a new profile_configs entry (status=pending). The wrapper picks
    it up on its next tick, writes the file to `<profile_root>/skills/<name>.md`,
    and acks. Use empty content to mark a skill for deletion (wrapper removes
    the file from the agent host).

    When the caller is the wrapper pushing a self-taught skill (e.g. agent
    just learned a new capability and wrote `skills/foo.md` on its own host),
    set header `X-Skill-Source: self-taught` to mark the audit log as
    wrapper-initiated rather than operator-initiated.
    """
    db = request.app.state.db
    profile = await _find_profile(db, agent_id, profile_name)
    name = _validate_skill_name(body.name)
    content = body.content or ""
    cfg_id = str(uuid.uuid4())
    sha = hashlib.sha256(content.encode()).hexdigest()
    file_path = _skill_file_path(name)
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
    "/{agent_id}/profiles/{profile_name}/skills/{skill_name}",
    response_model=ProfileConfig,
    status_code=201,
)
async def delete_skill(
    agent_id: str, profile_name: str, skill_name: str, request: Request
) -> ProfileConfig:
    """Delete a skill.

    Implementation note: instead of mutating history, we append a new
    profile_configs entry with empty content. The wrapper treats empty
    content for a skills/ path as a delete-on-host (file is removed).
    This keeps the wrapper-mediated sync pattern uniform: the dashboard
    just writes intent, and the wrapper reconciles.
    """
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

