# coding: utf-8
"""Starter catalog API (v1.0.1 new-user-activation §3.4).

Endpoints:
  - GET    /api/starters                    list all bundled starters
  - GET    /api/starters/{name}             single starter (with template)
  - POST   /api/starters/{name}/clone       clone into a user-owned
                                            workflow_packages row

The catalog is loaded at server startup (in-process) into
`request.app.state.starters`. Clone writes a new workflow_packages
row with a unique name (starter_name + short random suffix) so
the user can clone the same starter multiple times.

For the `system-health` starter (which has a `_server_healthcheck`
step action handled in-process by the supervisor — see §3.5), the
clone just creates the workflow_packages row like any other
starter. The supervisor's action-routing logic is what makes
`_server_healthcheck` skip agent dispatch.
"""
from __future__ import annotations

import json
import secrets
import string
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hermes_orch.auth.cookie import current_user

router = APIRouter()


class CloneOut(BaseModel):
    """Response from POST /api/starters/{name}/clone."""
    workflow_id: str
    workflow_name: str
    cloned_from: str  # starter name


def _catalog(request: Request) -> dict[str, Any]:
    """Read the in-process catalog (loaded at startup).

    Returns a dict keyed by starter name. Empty dict if the
    catalog wasn't loaded (e.g. tests).
    """
    return getattr(request.app.state, "starters", {}) or {}


@router.get("/starters")
async def get_starters(request: Request) -> list[dict[str, Any]]:
    """List all bundled starters (summary shape)."""
    catalog = _catalog(request)
    return [s.to_summary_dict() for s in catalog.values()]


@router.get("/starters/{name}")
async def get_starter(name: str, request: Request) -> dict[str, Any]:
    """Single starter, with the full step_template + variables."""
    catalog = _catalog(request)
    starter = catalog.get(name)
    if not starter:
        raise HTTPException(404, f"Starter not found: {name}")
    return starter.to_detail_dict()


@router.post("/starters/{name}/clone", response_model=CloneOut)
async def clone_starter(name: str, request: Request) -> CloneOut:
    """Clone a starter into a user-owned workflow_packages row.

    Per spec §3.4 versioning: the clone is a SNAPSHOT. Updates to
    the bundled catalog do NOT auto-migrate user clones.

    The clone name is `{starter_name}-{6-char-suffix}` to keep it
    unique (the workflow_packages.name column has a UNIQUE
    constraint). The user can rename later via the existing
    workflow edit UI.
    """
    user = await current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    catalog = _catalog(request)
    starter = catalog.get(name)
    if not starter:
        raise HTTPException(404, f"Starter not found: {name}")

    db = request.app.state.db
    # Generate a unique workflow name. 6 random chars is enough
    # to avoid collisions in the typical "operator clones the
    # same starter a few times" use case (~36^6 = 2B combinations).
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(6))
    workflow_name = f"{starter.name}-{suffix}"
    workflow_id = f"wf-{secrets.token_hex(8)}"

    await db.execute(
        "INSERT INTO workflow_packages "
        "(id, name, version, description, step_template, variables, visual_layout) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            workflow_id,
            workflow_name,
            starter.version,
            starter.display.description,
            json.dumps(starter.step_template),
            json.dumps(starter.variables),
            json.dumps({}),  # visual_layout: default empty
        ),
    )

    return CloneOut(
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        cloned_from=starter.name,
    )
