"""Object Layer API (2026-07-26).

First-class read endpoints for the three core object types:
  - Skills (file-based + optional sidecar schema)
  - Tools (registered tool_definitions + per-profile MCP status)
  - Resources (promoted from agent_profiles.storage_refs, no schema change)

The Object Layer is the "what can the system actually use" view —
the LLM planner queries it during design-time to suggest
deterministic substitutions, the operator UI shows the registry
on a single page, and (later) the code-gen meta-feature will
write new Skill objects back into the registry.

Endpoints:
  GET    /api/objects/skills                  — list all skills (with sidecar if present)
  GET    /api/objects/skills/{profile_id}/{name}  — get one skill (with sidecar)
  GET    /api/objects/tools                   — list tool definitions
  GET    /api/objects/tools/{id}              — get one tool definition
  GET    /api/objects/tools/{id}/availability — list which profiles have this tool
  POST   /api/objects/tools/{id}/check-mcp    — mark a tool's MCP status (manual)
  GET    /api/objects/resources               — list all storage_refs across profiles
  GET    /api/objects/registry                — all 3 types in one call (UI / single page)

All endpoints except check-mcp are read-only. Mutating endpoints
(create tool, register tool on profile, add resource) live in their
respective owning routers (agents endpoint). We keep this router
read-only to enforce the registry-as-truth mental model: skills
are managed via the wrapper / agents endpoint, tools are managed
via the agent profile edit, resources are managed via the same.
The Object Layer API is a denormalized READ view that joins
across those tables so the UI / planner don't have to.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes_orch.core.audit import audit_log
from hermes_orch.core.skill_loader import SkillLoader, SkillRecord
from hermes_orch.utils import now_iso as _now_iso

router = APIRouter()


# ===== Pydantic response models =====


class SkillSchemaOut(BaseModel):
    """Wire format for SkillSchema (input/output as object, not dict of str)."""
    input_schema: dict[str, str] = Field(default_factory=dict)
    output_schema: dict[str, str] = Field(default_factory=dict)
    deterministic: bool = False
    llm_required: bool = True
    requires_capabilities: list[str] = Field(default_factory=list)


class SkillOut(BaseModel):
    # The sidecar schema is exposed as `skill_schema` in the Python
    # model (because Pydantic v2 reserves `schema` on BaseModel for
    # `model_json_schema()`) but serialized as `schema` in the API
    # response via the alias. This way the public API contract
    # stays "schema" while the Python attribute doesn't shadow a
    # built-in.
    profile_id: str
    name: str
    file_path: str
    size: int
    sha256: str | None = None
    status: str
    created_at: str | None = None
    applied_at: str | None = None
    skill_schema: SkillSchemaOut = Field(
        alias="schema",
        serialization_alias="schema",
    )

    model_config = {"populate_by_name": True}


class ToolAvailability(BaseModel):
    """One profile's registration of a tool."""
    profile_id: str
    profile_name: str
    mcp_status: str  # unknown | up | down | error
    last_checked_at: str | None = None


class ToolOut(BaseModel):
    id: str
    name: str
    version: str
    kind: str
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    mcp_server_name: str = ""
    availability: list[ToolAvailability] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class ResourceOut(BaseModel):
    """One storage_ref (Resource Layer) from any agent profile."""
    profile_id: str
    profile_name: str
    kind: str  # smb | local | gdrive | s3 | url
    uri: str
    description: str = ""
    auth_ref: str = ""  # optional, may be redacted in responses


class RegistryOut(BaseModel):
    """Aggregate view for the UI / planner — one call to get everything."""
    skills: list[SkillOut]
    tools: list[ToolOut]
    resources: list[ResourceOut]
    counts: dict[str, int]  # {skills, tools, resources, deterministic_skills}


class McpCheckBody(BaseModel):
    """Body for POST /tools/{id}/check-mcp — manual MCP status update.

    The orch doesn't actively probe MCP servers (per user-stated
    2026-07-26: 'tool 是否能用orch server 操制不了'). This endpoint lets
    the operator (or a future wrapper heartbeat) record the latest
    known status. The four states mirror the existing mcp_servers
    JSON shape used on agent_profiles.
    """
    profile_id: str
    status: str  # up | down | error


# ===== Helpers =====


def _skill_record_to_out(r: SkillRecord) -> SkillOut:
    s = r.schema
    return SkillOut(
        profile_id=r.profile_id,
        name=r.name,
        file_path=r.file_path,
        size=r.size,
        sha256=r.sha256,
        status=r.status,
        created_at=r.created_at,
        applied_at=r.applied_at,
        skill_schema=SkillSchemaOut(
            input_schema=s.input_schema,
            output_schema=s.output_schema,
            deterministic=s.deterministic,
            llm_required=s.llm_required,
            requires_capabilities=s.requires_capabilities,
        ),
    )


def _row_to_tool_out(row: dict[str, Any], availability: list[dict[str, Any]]) -> ToolOut:
    caps_raw = row.get("capabilities") or "[]"
    try:
        caps = json.loads(caps_raw) if isinstance(caps_raw, str) else caps_raw
        if not isinstance(caps, list):
            caps = []
    except (json.JSONDecodeError, TypeError):
        caps = []
    return ToolOut(
        id=row["id"],
        name=row["name"],
        version=row.get("version", "1.0.0"),
        kind=row.get("kind", "external_app"),
        description=row.get("description", ""),
        capabilities=caps,
        mcp_server_name=row.get("mcp_server_name", ""),
        availability=[
            ToolAvailability(
                profile_id=a["profile_id"],
                profile_name=a.get("profile_name", ""),
                mcp_status=a.get("mcp_status", "unknown"),
                last_checked_at=a.get("last_checked_at"),
            )
            for a in availability
        ],
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _row_to_resource_out(row: dict[str, Any], profile_id: str, profile_name: str) -> ResourceOut:
    """One storage_ref parsed from agent_profiles.storage_refs JSON."""
    return ResourceOut(
        profile_id=profile_id,
        profile_name=profile_name,
        kind=str(row.get("kind", "")),
        uri=str(row.get("uri", "")),
        description=str(row.get("description", "")),
        auth_ref=str(row.get("auth_ref", "")),
    )


# ===== Endpoints =====


@router.get("/skills", response_model=list[SkillOut])
async def list_skills(
    request: Request,
    profile_id: str | None = None,
    deterministic_only: bool = False,
    requires_capability: str | None = None,
) -> list[SkillOut]:
    """List skills across all profiles (or one), with parsed sidecar schema.

    Filters:
      profile_id            — restrict to one agent profile
      deterministic_only    — only skills with `deterministic: true` sidecar
      requires_capability   — only skills whose sidecar lists this capability
    """
    db = request.app.state.db
    loader = SkillLoader(db)
    recs = await loader.list_all(
        profile_id=profile_id,
        deterministic_only=deterministic_only,
        requires_capability=requires_capability,
    )
    return [_skill_record_to_out(r) for r in recs]


@router.get("/skills/{profile_id}/{name:path}", response_model=SkillOut)
async def get_skill(profile_id: str, name: str, request: Request) -> SkillOut:
    """Get one skill by (profile_id, name) with sidecar.

    `name:path` (catch-all) so names with slashes (e.g.
    'apple/apple-notes') match as a single segment. Without it,
    FastAPI's path parser splits on / and the route doesn't match.
    """
    db = request.app.state.db
    loader = SkillLoader(db)
    rec = await loader.get(profile_id, name)
    if not rec:
        raise HTTPException(404, f"Skill not found: {profile_id}/{name}")
    return _skill_record_to_out(rec)


@router.get("/tools", response_model=list[ToolOut])
async def list_tools(request: Request) -> list[ToolOut]:
    """List all tool definitions, each with its per-profile availability."""
    db = request.app.state.db
    rows = await db.fetchall(
        "SELECT * FROM tool_definitions ORDER BY name ASC"
    )
    if not rows:
        return []
    # Bulk-fetch availability in one JOIN to avoid N+1
    avail_rows = await db.fetchall(
        "SELECT pt.profile_id, pt.tool_id, pt.mcp_status, pt.last_checked_at, "
        "       ap.name AS profile_name "
        "FROM profile_tools pt "
        "JOIN agent_profiles ap ON ap.id = pt.profile_id "
        "WHERE pt.tool_id IN ("
        "  SELECT id FROM tool_definitions"
        ")"
    )
    by_tool: dict[str, list[dict]] = {r["id"]: [] for r in rows}
    for a in avail_rows:
        by_tool.setdefault(a["tool_id"], []).append(a)
    return [_row_to_tool_out(r, by_tool.get(r["id"], [])) for r in rows]


@router.get("/tools/{tool_id}", response_model=ToolOut)
async def get_tool(tool_id: str, request: Request) -> ToolOut:
    """Get one tool definition with availability."""
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT * FROM tool_definitions WHERE id = ?", (tool_id,)
    )
    if not row:
        raise HTTPException(404, f"Tool not found: {tool_id}")
    avail_rows = await db.fetchall(
        "SELECT pt.profile_id, pt.tool_id, pt.mcp_status, pt.last_checked_at, "
        "       ap.name AS profile_name "
        "FROM profile_tools pt "
        "JOIN agent_profiles ap ON ap.id = pt.profile_id "
        "WHERE pt.tool_id = ?",
        (tool_id,),
    )
    return _row_to_tool_out(row, avail_rows)


@router.get("/tools/{tool_id}/availability", response_model=list[ToolAvailability])
async def get_tool_availability(tool_id: str, request: Request) -> list[ToolAvailability]:
    """List which profiles have registered this tool, with MCP status."""
    db = request.app.state.db
    tool = await db.fetchone(
        "SELECT id FROM tool_definitions WHERE id = ?", (tool_id,)
    )
    if not tool:
        raise HTTPException(404, f"Tool not found: {tool_id}")
    rows = await db.fetchall(
        "SELECT pt.profile_id, pt.mcp_status, pt.last_checked_at, "
        "       ap.name AS profile_name "
        "FROM profile_tools pt "
        "JOIN agent_profiles ap ON ap.id = pt.profile_id "
        "WHERE pt.tool_id = ? "
        "ORDER BY ap.name",
        (tool_id,),
    )
    return [
        ToolAvailability(
            profile_id=r["profile_id"],
            profile_name=r.get("profile_name", ""),
            mcp_status=r.get("mcp_status", "unknown"),
            last_checked_at=r.get("last_checked_at"),
        )
        for r in rows
    ]


@router.post("/tools/{tool_id}/check-mcp", response_model=ToolAvailability)
async def check_tool_mcp(tool_id: str, body: McpCheckBody, request: Request) -> ToolAvailability:
    """Record the latest known MCP status for one (profile, tool) registration.

    Per user-stated 2026-07-26, the orch doesn't actively probe MCP
    servers (we don't have a way to reach into the agent host). This
    endpoint is the manual / heartbeat-fed write path so the UI can
    still show up-to-date status. A future enhancement: the wrapper
    heartbeat could POST here when it sees an MCP server's status
    change.
    """
    db = request.app.state.db
    if body.status not in ("up", "down", "error"):
        raise HTTPException(400, f"status must be up|down|error (got {body.status!r})")
    tool = await db.fetchone(
        "SELECT id FROM tool_definitions WHERE id = ?", (tool_id,)
    )
    if not tool:
        raise HTTPException(404, f"Tool not found: {tool_id}")
    profile = await db.fetchone(
        "SELECT id, name FROM agent_profiles WHERE id = ?", (body.profile_id,)
    )
    if not profile:
        raise HTTPException(404, f"Profile not found: {body.profile_id}")
    # Auto-create the junction row if missing. Some users register a
    # tool ad-hoc without going through the (future) profile-edit
    # endpoint; this lets the UI/heartbeat path be the source of truth.
    now = _now_iso()
    existing = await db.fetchone(
        "SELECT 1 FROM profile_tools WHERE profile_id = ? AND tool_id = ?",
        (body.profile_id, tool_id),
    )
    if existing:
        await db.execute(
            "UPDATE profile_tools SET mcp_status = ?, last_checked_at = ? "
            "WHERE profile_id = ? AND tool_id = ?",
            (body.status, now, body.profile_id, tool_id),
        )
    else:
        await db.execute(
            "INSERT INTO profile_tools "
            "(profile_id, tool_id, mcp_status, last_checked_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (body.profile_id, tool_id, body.status, now, now),
        )
    await audit_log(
        db, "object.tool.mcp_status", actor="operator",
        payload={
            "tool_id": tool_id,
            "profile_id": body.profile_id,
            "status": body.status,
        },
    )
    return ToolAvailability(
        profile_id=body.profile_id,
        profile_name=profile["name"],
        mcp_status=body.status,
        last_checked_at=now,
    )


@router.get("/resources", response_model=list[ResourceOut])
async def list_resources(request: Request) -> list[ResourceOut]:
    """List all storage_refs across all agent profiles (the Resource Layer).

    The storage_refs column already exists on agent_profiles (added
    2026-07-22). This endpoint just exposes it as a first-class
    Object Layer view so the UI / planner can list Resources without
    having to walk every profile. The Resource kind enum is enforced
    by the agents endpoint that writes to storage_refs; we don't
    re-validate here because the column is free-form JSON.
    """
    db = request.app.state.db
    rows = await db.fetchall(
        "SELECT id, name, storage_refs FROM agent_profiles "
        "WHERE storage_refs IS NOT NULL AND storage_refs != '[]'"
    )
    out: list[ResourceOut] = []
    for r in rows:
        raw = r.get("storage_refs")
        if not raw:
            continue
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, list):
            continue
        for ref in parsed:
            if isinstance(ref, dict):
                out.append(_row_to_resource_out(ref, r["id"], r["name"]))
    return out


@router.get("/registry", response_model=RegistryOut)
async def get_registry(
    request: Request,
    deterministic_only: bool = False,
) -> RegistryOut:
    """All three object types in one call — for the UI single-page registry.

    `deterministic_only` applies to the skills list (handy when the
    LLM planner wants to find token-saving candidates).
    """
    db = request.app.state.db
    # Skills
    loader = SkillLoader(db)
    skill_recs = await loader.list_all(deterministic_only=deterministic_only)
    skills_out = [_skill_record_to_out(r) for r in skill_recs]
    # Tools
    tool_rows = await db.fetchall(
        "SELECT * FROM tool_definitions ORDER BY name ASC"
    )
    avail_rows = await db.fetchall(
        "SELECT pt.profile_id, pt.tool_id, pt.mcp_status, pt.last_checked_at, "
        "       ap.name AS profile_name "
        "FROM profile_tools pt "
        "JOIN agent_profiles ap ON ap.id = pt.profile_id"
    )
    by_tool: dict[str, list[dict]] = {r["id"]: [] for r in tool_rows}
    for a in avail_rows:
        by_tool.setdefault(a["tool_id"], []).append(a)
    tools_out = [_row_to_tool_out(r, by_tool.get(r["id"], [])) for r in tool_rows]
    # Resources — call the dedicated endpoint function directly so
    # the aggregation logic isn't duplicated.
    resources_out = await list_resources(request)
    return RegistryOut(
        skills=skills_out,
        tools=tools_out,
        resources=resources_out,
        counts={
            "skills": len(skills_out),
            "tools": len(tools_out),
            "resources": len(resources_out),
            "deterministic_skills": sum(
                1 for s in skills_out if s.skill_schema.deterministic
            ),
        },
    )
