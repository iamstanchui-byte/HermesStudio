# coding: utf-8
"""SOUL template library (v3.9.0 Phase 3, 2026-08-01).

Admin-published persona templates that workflow authors can pull
into a project preset by name (e.g. "cpi-analyst", "code-reviewer",
"data-engineer"). Distinct from per-project presets: the template is
the "design" and a preset is the "instance" — editing the template
does NOT change existing presets that were instantiated from it
(those are independent snapshots).

Endpoints (all mounted at /api, no prefix — see main.py):
  GET    /api/soul-templates                — list all (optional ?category=X)
  GET    /api/soul-templates/{name}         — get one
  POST   /api/soul-templates                — create (admin only)
  PUT    /api/soul-templates/{name}         — update (admin only)
  DELETE /api/soul-templates/{name}         — delete (admin only)
  POST   /api/projects/{id}/soul-presets/from-template/{name}
                                          — instantiate a preset for a
                                            project from this template

Storage: `project_soul_templates` table. `name` is UNIQUE
COLLATE NOCASE so "cpi-analyst" and "CPI-Analyst" don't both exist.
The from-template endpoint uses the existing `upsert_soul_preset`
shape (a preset is a per-project snapshot bound to a profile); the
template's `content` becomes the new preset's `content`, the
template's `name` becomes the preset's `role_name`, and the
operator must pass the target profile (agent_id + profile_name).
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from hermes_orch.core.audit import audit_log
from hermes_orch.db import Database


router = APIRouter()


# ===== Pydantic models =====


class SoulTemplateIn(BaseModel):
    """Body for POST /soul-templates and PUT /soul-templates/{name}.

    `name` is required and is the template's unique identifier
    (kebab-case recommended; uniqueness is case-insensitive to
    avoid accidental duplicates). `category` is optional and is
    used only for the `?category=X` filter + the admin page's
    grouping. `content` is the SOUL.md body. `description` is
    a 1-line summary for browsing.
    """
    name: str = Field(min_length=1, max_length=128)
    category: str = Field(default="", max_length=64)
    content: str
    description: str = Field(default="", max_length=512)


class SoulTemplate(BaseModel):
    """Shape returned by GET /soul-templates and the create/update
    responses. `created_by` is the admin username from the session
    (or "system" if no admin is logged in — should not happen in
    practice because create/update require admin)."""
    id: str
    name: str
    category: str
    content: str
    description: str
    created_by: str
    created_at: str | None = None
    updated_at: str | None = None


class SoulTemplateInstantiateIn(BaseModel):
    """Body for POST /projects/{id}/soul-presets/from-template/{name}.

    The target profile is required (agent_id + profile_name) — the
    template is just the persona text; it doesn't carry profile
    bindings because templates are project-agnostic.
    """
    agent_id: str
    profile_name: str


# ===== Admin guard (reused from users.py) =====
#
# We import lazily inside the dependency to avoid a circular
# import at module load (users.py imports from auth/cookie, which
# transitively could load this file via main.py if we imported
# at the top).


async def _require_admin(request: Request) -> dict[str, Any]:
    """Same as api/users.py::require_admin — kept here as a local
    dependency so the template router is self-contained.

    Returns the user row (for audit) or raises 401/403.
    """
    from hermes_orch.auth.cookie import ROLE_ADMIN, current_user_id
    user_id = await current_user_id(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    db: Database = request.app.state.db
    user = await db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(401, "Not authenticated")
    if user.get("disabled"):
        raise HTTPException(401, "Account disabled")
    if user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "Admin role required")
    return user


def _row_to_template(row: dict[str, Any]) -> SoulTemplate:
    return SoulTemplate(
        id=row["id"],
        name=row["name"],
        category=row.get("category") or "",
        content=row.get("content") or "",
        description=row.get("description") or "",
        created_by=row.get("created_by") or "",
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


# ===== Endpoints =====


@router.get("/api/soul-templates", response_model=list[SoulTemplate])
async def list_soul_templates(
    request: Request,
    category: str | None = None,
) -> list[SoulTemplate]:
    """List all SOUL templates, optionally filtered by `?category=`.

    Returns templates sorted by (category, name) so the admin
    page can render them grouped by category without a second
    sort on the client.

    No auth required for READ: operators need to be able to
    browse templates when authoring a workflow. Only CREATE /
    UPDATE / DELETE require admin.
    """
    db: Database = request.app.state.db
    if category:
        rows = await db.fetchall(
            "SELECT * FROM project_soul_templates "
            "WHERE LOWER(category) = LOWER(?) "
            "ORDER BY category, name",
            (category,),
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM project_soul_templates "
            "ORDER BY category, name"
        )
    return [_row_to_template(r) for r in rows]


@router.get("/api/soul-templates/{name}", response_model=SoulTemplate)
async def get_soul_template(name: str, request: Request) -> SoulTemplate:
    """Get one SOUL template by name (case-insensitive).

    404 if the template doesn't exist. Used by the
    from-template instantiator (to load the content) and by the
    admin edit form (to populate the edit fields).
    """
    db: Database = request.app.state.db
    row = await db.fetchone(
        "SELECT * FROM project_soul_templates WHERE name = ?",
        (name,),
    )
    if not row:
        # COLLATE NOCASE on the column means a case-mismatched
        # query still matches; but we re-check with a separate
        # case-insensitive lookup so the 404 message is
        # unambiguous (instead of silently returning a
        # case-mismatched row).
        row = await db.fetchone(
            "SELECT * FROM project_soul_templates "
            "WHERE LOWER(name) = LOWER(?)",
            (name,),
        )
    if not row:
        raise HTTPException(404, f"SOUL template not found: {name}")
    return _row_to_template(row)


@router.post(
    "/api/soul-templates",
    response_model=SoulTemplate,
    status_code=201,
)
async def create_soul_template(
    body: SoulTemplateIn,
    request: Request,
    admin: dict[str, Any] = Depends(_require_admin),
) -> SoulTemplate:
    """Create a new SOUL template (admin only).

    409 if a template with the same name already exists (the
    UNIQUE COLLATE NOCASE constraint enforces case-insensitive
    uniqueness; the explicit pre-check gives a clean error
    message instead of an IntegrityError 500).
    """
    db: Database = request.app.state.db
    existing = await db.fetchone(
        "SELECT id FROM project_soul_templates WHERE LOWER(name) = LOWER(?)",
        (body.name,),
    )
    if existing:
        raise HTTPException(
            409,
            f"SOUL template already exists: {body.name!r}. "
            f"Use PUT to update it, or pick a different name.",
        )
    new_id = str(uuid.uuid4())
    await db.insert(
        "project_soul_templates",
        {
            "id": new_id,
            "name": body.name,
            "category": body.category or "",
            "content": body.content,
            "description": body.description or "",
            "created_by": admin.get("username") or "system",
        },
    )
    row = await db.fetchone(
        "SELECT * FROM project_soul_templates WHERE id = ?", (new_id,)
    )
    await audit_log(
        db, "project.soul_template_created",
        actor=admin.get("username") or "admin",
        payload={
            "name": body.name,
            "category": body.category or "",
            "size": len(body.content),
        },
    )
    return _row_to_template(row)


@router.put(
    "/api/soul-templates/{name}",
    response_model=SoulTemplate,
)
async def update_soul_template(
    name: str,
    body: SoulTemplateIn,
    request: Request,
    admin: dict[str, Any] = Depends(_require_admin),
) -> SoulTemplate:
    """Update an existing SOUL template (admin only).

    Edit-in-place; does NOT propagate to existing presets that
    were instantiated from this template (presets are independent
    snapshots — the user's "create a preset from template" action
    captured the content at that moment). If the operator wants
    to push the new template content to existing presets, they
    can re-run from-template (which creates a new version row
    for the existing preset).

    404 if the template doesn't exist. The `name` in the URL is
    the canonical name; `body.name` is also accepted (lets the
    admin rename via PUT) — if the new name conflicts with
    another template, returns 409.
    """
    db: Database = request.app.state.db
    existing = await db.fetchone(
        "SELECT * FROM project_soul_templates WHERE LOWER(name) = LOWER(?)",
        (name,),
    )
    if not existing:
        raise HTTPException(404, f"SOUL template not found: {name}")
    # If the operator is renaming, check the new name doesn't
    # collide with a different template.
    if body.name.lower() != name.lower():
        collision = await db.fetchone(
            "SELECT id FROM project_soul_templates "
            "WHERE LOWER(name) = LOWER(?) AND id != ?",
            (body.name, existing["id"]),
        )
        if collision:
            raise HTTPException(
                409,
                f"Cannot rename: another template already has "
                f"name {body.name!r}.",
            )
    await db.execute(
        "UPDATE project_soul_templates "
        "SET name = ?, category = ?, content = ?, description = ?, "
        "    updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (
            body.name,
            body.category or "",
            body.content,
            body.description or "",
            existing["id"],
        ),
    )
    row = await db.fetchone(
        "SELECT * FROM project_soul_templates WHERE id = ?",
        (existing["id"],),
    )
    await audit_log(
        db, "project.soul_template_updated",
        actor=admin.get("username") or "admin",
        payload={
            "old_name": name,
            "new_name": body.name,
            "size": len(body.content),
        },
    )
    return _row_to_template(row)


@router.delete(
    "/api/soul-templates/{name}",
    status_code=204,
)
async def delete_soul_template(
    name: str,
    request: Request,
    admin: dict[str, Any] = Depends(_require_admin),
) -> None:
    """Delete a SOUL template (admin only).

    Deleting a template does NOT delete presets that were
    instantiated from it — those are independent. Audit
    records the deletion for traceability.
    """
    db: Database = request.app.state.db
    existing = await db.fetchone(
        "SELECT * FROM project_soul_templates WHERE LOWER(name) = LOWER(?)",
        (name,),
    )
    if not existing:
        raise HTTPException(404, f"SOUL template not found: {name}")
    await db.execute(
        "DELETE FROM project_soul_templates WHERE id = ?",
        (existing["id"],),
    )
    await audit_log(
        db, "project.soul_template_deleted",
        actor=admin.get("username") or "admin",
        payload={"name": name, "id": existing["id"]},
    )
    return None


@router.post(
    "/api/projects/{project_id}/soul-presets/from-template/{name}",
    response_model=dict,
    status_code=201,
)
async def create_preset_from_template(
    project_id: str,
    name: str,
    body: SoulTemplateInstantiateIn,
    request: Request,
) -> dict:
    """Create a project SOUL preset from a published template.

    Binds the template's content to the (project, profile) pair
    the operator specified in the body. The preset's `role_name`
    is the template's name (so a preset "from cpi-analyst" is
    `role_name='cpi-analyst'`); the operator can rename later
    via the regular PUT /soul-presets endpoint.

    404 if either the project, the profile, or the template is
    missing. 409 if the project already has a preset bound to
    that profile (use the regular PUT endpoint to update it;
    the from-template flow is a "first time" helper).
    """
    import uuid as _uuid
    db: Database = request.app.state.db
    # 1. Project must exist.
    project = await db.fetchone(
        "SELECT id FROM projects WHERE id = ?", (project_id,)
    )
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")
    # 2. Profile must exist.
    profile = await db.fetchone(
        "SELECT * FROM agent_profiles "
        "WHERE agent_id = ? AND name = ?",
        (body.agent_id, body.profile_name),
    )
    if not profile:
        raise HTTPException(
            404,
            f"Profile not found: {body.agent_id}/{body.profile_name}",
        )
    # 3. Template must exist.
    template = await db.fetchone(
        "SELECT * FROM project_soul_templates "
        "WHERE LOWER(name) = LOWER(?)",
        (name,),
    )
    if not template:
        raise HTTPException(404, f"SOUL template not found: {name}")
    # 4. (project, profile) must not already have a preset — this
    #    is a "create new" flow. The operator uses PUT to update
    #    an existing preset (which is also how v1 versioning
    #    works).
    existing_preset = await db.fetchone(
        "SELECT id FROM project_soul_presets "
        "WHERE project_id = ? AND profile_id = ?",
        (project_id, profile["id"]),
    )
    if existing_preset:
        raise HTTPException(
            409,
            f"Project {project_id} already has a preset for "
            f"{body.agent_id}/{body.profile_name} "
            f"(id={existing_preset['id']}). Use PUT "
            f"/api/projects/{project_id}/soul-presets to update "
            f"it (or the from-template flow is not the right "
            f"tool for this case).",
        )
    # 5. Insert the preset with the template's content. role_name
    #    is the template's name (per the design: a preset
    #    "instantiated from" a template carries the template's
    #    identity as its role_name).
    preset_id = str(_uuid.uuid4())
    await db.insert(
        "project_soul_presets",
        {
            "id": preset_id,
            "project_id": project_id,
            "profile_id": profile["id"],
            "role_name": template["name"],
            "content": template["content"],
            "default_soul": None,
        },
    )
    # 6. Append a v1 version row (so the versioning UI shows
    #    "v1 of 1" immediately; later edits bump to v2/v3/etc).
    from hermes_orch.auth.cookie import current_user_id
    operator = "orch_server"
    try:
        uid = await current_user_id(request)
        if uid:
            u = await db.fetchone(
                "SELECT username FROM users WHERE id = ?", (uid,)
            )
            if u and u.get("username"):
                operator = u["username"]
    except Exception:
        pass
    await db.insert(
        "project_soul_preset_versions",
        {
            "id": str(_uuid.uuid4()),
            "preset_id": preset_id,
            "version_number": 1,
            "content": template["content"],
            "default_soul": None,
            "created_by": operator,
        },
    )
    await audit_log(
        db, "project.soul_preset_from_template",
        actor=operator,
        project_id=project_id,
        payload={
            "preset_id": preset_id,
            "template_id": template["id"],
            "template_name": template["name"],
            "agent_id": body.agent_id,
            "profile_name": body.profile_name,
        },
    )
    return {
        "preset_id": preset_id,
        "template_name": template["name"],
        "role_name": template["name"],
        "content": template["content"],
    }
