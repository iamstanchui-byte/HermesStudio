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
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from hermes_orch.core.audit import audit_log

router = APIRouter()


# ===== Pydantic models =====


class AgentRegister(BaseModel):
    agent_id: str
    ip: str | None = None
    os_type: str | None = None  # 'windows' | 'linux'
    roles: list[str] = Field(default_factory=list)  # profile names


class AgentUpdate(BaseModel):
    ip: str | None = None
    os_type: str | None = None


class AgentProfileCreate(BaseModel):
    name: str
    description: str | None = None


class AgentProfileUpdate(BaseModel):
    description: str | None = None


class HeartbeatBody(BaseModel):
    status: str | None = None  # agent's reported state (e.g. 'busy', 'idle')


class AgentProfile(BaseModel):
    id: str
    agent_id: str
    name: str
    description: str | None = None
    status: str = "idle"
    current_task_id: str | None = None
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
    `format` controls the on-disk layout:
      - "file"   (default, backward compatible): skills/<name>.md
      - "folder" (hermes convention): skills/<name>/SKILL.md
        with optional references/ and scripts/ siblings (not tracked by
        the orchestrator; the agent host owns them).
    """
    name: str
    content: str = ""
    format: str = "file"  # "file" | "folder"


class SkillInfo(BaseModel):
    """One skill as seen by the dashboard. Latest applied/pending version
    of each `skills/<name>.md` file on the profile."""
    name: str
    file_path: str
    status: str  # 'applied' | 'pending' | 'applying' | 'failed' | 'deleted'
    size: int
    created_at: str | None = None
    applied_at: str | None = None
    error: str | None = None
    content: str | None = None  # only included when ?content=1


# ===== Helpers =====


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _row_to_profile(row: dict[str, Any]) -> AgentProfile:
    return AgentProfile(
        id=row["id"],
        agent_id=row["agent_id"],
        name=row["name"],
        description=row.get("description"),
        status=row["status"],
        current_task_id=row.get("current_task_id"),
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

    for role in body.roles:
        await db.insert(
            "agent_profiles",
            {
                "id": str(uuid.uuid4()),
                "agent_id": body.agent_id,
                "name": role,
                "status": "idle",
            },
        )

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
        payload={"ip": body.ip, "os_type": body.os_type, "roles": body.roles},
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

    return {
        "status": "ok",
        "timestamp": now,
        "agent_status": body_data.get("status", "idle"),
        "tasks": tasks,
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
        },
    )
    row = await db.fetchone("SELECT * FROM agent_profiles WHERE id = ?", (profile_id,))
    await audit_log(
        db, "agent.profile_added",
        actor="operator",
        agent_id=agent_id,
        payload={"profile_name": body.name, "description": body.description},
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
    row = await db.fetchone("SELECT * FROM agent_profiles WHERE id = ?", (profile["id"],))
    await audit_log(
        db, "agent.profile_updated",
        actor="operator",
        agent_id=agent_id,
        payload={"profile_name": profile_name, "description": body.description},
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


def _skill_file_path(skill_name: str, fmt: str = "file") -> str:
    """Map a skill name to its canonical file_path in profile_configs.

    fmt="file"   → skills/<name>.md       (flat, backward compatible)
    fmt="folder" → skills/<name>/SKILL.md (hermes convention with refs/scripts)
    """
    if fmt == "folder":
        return f"skills/{skill_name}/SKILL.md"
    return f"skills/{skill_name}.md"


async def _latest_skill_config(db: Any, profile_id: str, skill_name: str) -> dict[str, Any] | None:
    """Return the newest profile_configs row for a skill, or None."""
    rel = _skill_file_path(skill_name)
    return await db.fetchone(
        "SELECT * FROM profile_configs WHERE profile_id = ? AND file_path = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (profile_id, rel),
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
    return SkillInfo(
        name=row["file_path"].removeprefix("skills/").removesuffix(".md"),
        file_path=row["file_path"],
        status=status,
        size=len(content),
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
        "AND file_path LIKE 'skills/%' "
        "ORDER BY file_path ASC, created_at DESC",
        (profile["id"],),
    )
    include_deleted = request.query_params.get("include_deleted") == "1"
    # Keep only the newest per file_path
    seen: set[str] = set()
    out: list[SkillInfo] = []
    for r in rows:
        if r["file_path"] in seen:
            continue
        seen.add(r["file_path"])
        info = _row_to_skill(r, include_content=False)
        if info.status == "deleted" and not include_deleted:
            continue
        out.append(info)
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
    fmt = (body.format or "file").strip().lower()
    if fmt not in ("file", "folder"):
        raise HTTPException(400, f"invalid format {fmt!r}: must be 'file' or 'folder'")
    cfg_id = str(uuid.uuid4())
    sha = hashlib.sha256(content.encode()).hexdigest()
    file_path = _skill_file_path(name, fmt)
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
            "format": fmt,
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

