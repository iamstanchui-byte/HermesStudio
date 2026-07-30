# coding: utf-8
"""Artifact endpoints (per REVIEW.md §5).

Storage strategy:
- storage_kind='central': file saved to ./artifacts/<task_id>/<id>.<ext>
                       on the orchestrator (Windows A).
- storage_kind='external': file stays on the agent OS, DB only records path.

For external files, user downloads via scp. The /download endpoint returns
501 Not Implemented for external (UI shows scp command).
"""
from __future__ import annotations

import hashlib
import mimetypes
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from hermes_orch.core.audit import audit_log
from hermes_orch.utils import now_iso as _now_iso

router = APIRouter()


# ===== Pydantic models =====


class Artifact(BaseModel):
    id: str
    task_id: str
    project_id: str
    name: str
    content_type: str | None = None
    size_bytes: int | None = None
    checksum: str | None = None
    storage_kind: str
    storage_path: str
    agent_id: str | None = None
    created_at: str | None = None


class ExternalArtifactCreate(BaseModel):
    task_id: str
    project_id: str
    name: str
    path: str  # Path on the agent's local filesystem
    size_bytes: int
    content_type: str | None = None
    agent_id: str
    checksum: str | None = None


# ===== Helpers =====
# _now_iso is now imported from hermes_orch.utils (consolidated).


def _artifact_id() -> str:
    return "a-" + secrets.token_hex(8)


def _artifacts_root(request: Request) -> Path:
    cfg = request.app.state.config
    root = Path(cfg["artifacts"]["storage_root"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _max_size_bytes(request: Request) -> int:
    cfg = request.app.state.config
    return cfg["artifacts"]["max_size_mb"] * 1024 * 1024


def _guess_content_type(filename: str) -> str:
    """Guess content type from filename using stdlib mimetypes."""
    ctype, _ = mimetypes.guess_type(filename)
    return ctype or "application/octet-stream"


def _validate_external_path(path: str) -> str:
    if not path:
        raise HTTPException(400, "Path required")
    if "\x00" in path:
        raise HTTPException(400, "Path contains null byte")
    return path


def _row_to_artifact(row: dict[str, Any]) -> Artifact:
    return Artifact(
        id=row["id"],
        task_id=row["task_id"],
        project_id=row["project_id"],
        name=row["name"],
        content_type=row.get("content_type"),
        size_bytes=row.get("size_bytes"),
        checksum=row.get("checksum"),
        storage_kind=row["storage_kind"],
        storage_path=row["storage_path"],
        agent_id=row.get("agent_id"),
        created_at=row.get("created_at"),
    )


def _scp_command(artifact: Artifact) -> str:
    if not artifact.agent_id:
        return f"# Error: no agent_id for external artifact {artifact.id}"
    return f"scp {artifact.agent_id}:{artifact.storage_path} ./"


# ===== Endpoints =====


@router.post("/", response_model=Artifact, status_code=201)
async def upload_artifact(
    request: Request,
    file: UploadFile = File(...),
    task_id: str = Form(...),
    project_id: str = Form(...),
) -> Artifact:
    """Upload an artifact (multipart). File stored as central."""
    db = request.app.state.db
    max_size = _max_size_bytes(request)

    task = await db.fetchone("SELECT id FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {task_id}")
    project = await db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")

    content = await file.read()
    size = len(content)
    if size > max_size:
        raise HTTPException(
            413,
            f"File too large: {size} bytes > limit {max_size} bytes. "
            f"Register as external artifact instead.",
        )

    artifact_id = _artifact_id()
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "file")
    storage_dir = _artifacts_root(request) / task_id
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / f"{artifact_id}_{safe_name}"
    storage_path.write_bytes(content)

    checksum = hashlib.sha256(content).hexdigest()
    content_type = file.content_type or _guess_content_type(safe_name)

    await db.insert(
        "artifacts",
        {
            "id": artifact_id,
            "task_id": task_id,
            "project_id": project_id,
            "name": file.filename or safe_name,
            "content_type": content_type,
            "size_bytes": size,
            "checksum": checksum,
            "storage_kind": "central",
            "storage_path": str(storage_path),
            "agent_id": None,
        },
    )
    row = await db.fetchone("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
    await audit_log(
        db, "artifact.uploaded",
        actor="agent",  # typically wrapper uploads
        project_id=project_id,
        task_id=task_id,
        payload={"name": file.filename or safe_name, "size": size, "kind": "central"},
    )
    return _row_to_artifact(row)


@router.post("/external", response_model=Artifact, status_code=201)
async def register_external(body: ExternalArtifactCreate, request: Request) -> Artifact:
    """Register an external artifact (file stays on agent OS)."""
    db = request.app.state.db
    path = _validate_external_path(body.path)

    task = await db.fetchone("SELECT id FROM tasks WHERE id = ?", (body.task_id,))
    if not task:
        raise HTTPException(404, f"Task not found: {body.task_id}")
    project = await db.fetchone("SELECT id FROM projects WHERE id = ?", (body.project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {body.project_id}")
    agent = await db.fetchone("SELECT id FROM agents WHERE id = ?", (body.agent_id,))
    if not agent:
        raise HTTPException(404, f"Agent not found: {body.agent_id}")

    artifact_id = _artifact_id()
    await db.insert(
        "artifacts",
        {
            "id": artifact_id,
            "task_id": body.task_id,
            "project_id": body.project_id,
            "name": body.name,
            "content_type": body.content_type or _guess_content_type(body.name),
            "size_bytes": body.size_bytes,
            "checksum": body.checksum,
            "storage_kind": "external",
            "storage_path": path,
            "agent_id": body.agent_id,
        },
    )
    row = await db.fetchone("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
    await audit_log(
        db, "artifact.registered_external",
        actor="agent",
        project_id=body.project_id,
        task_id=body.task_id,
        agent_id=body.agent_id,
        payload={"name": body.name, "size": body.size_bytes, "kind": "external"},
    )
    return _row_to_artifact(row)


@router.get("/")
async def list_artifacts(
    request: Request,
    task_id: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
) -> dict:
    """List artifacts (filterable)."""
    db = request.app.state.db
    where = []
    params: list[Any] = []
    if task_id:
        where.append("task_id = ?")
        params.append(task_id)
    if project_id:
        where.append("project_id = ?")
        params.append(project_id)
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    sql = "SELECT * FROM artifacts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    rows = await db.fetchall(sql, tuple(params))
    return {"artifacts": [_row_to_artifact(r).model_dump() for r in rows]}


@router.get("/{artifact_id}", response_model=Artifact)
async def get_artifact(artifact_id: str, request: Request) -> Artifact:
    """Get artifact metadata."""
    db = request.app.state.db
    row = await db.fetchone("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
    if not row:
        raise HTTPException(404, f"Artifact not found: {artifact_id}")
    return _row_to_artifact(row)


@router.get("/{artifact_id}/download")
async def download_artifact(artifact_id: str, request: Request):
    """Download artifact. Central: stream file. External: 501 with scp command."""
    db = request.app.state.db
    row = await db.fetchone("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
    if not row:
        raise HTTPException(404, f"Artifact not found: {artifact_id}")

    artifact = _row_to_artifact(row)
    if artifact.storage_kind == "external":
        raise HTTPException(
            status_code=501,
            detail={
                "error": "External artifact not downloadable via orchestrator",
                "reason": "file is on the agent OS, not orchestrator",
                "scp_command": _scp_command(artifact),
            },
        )

    storage = Path(artifact.storage_path)
    if not storage.exists():
        raise HTTPException(404, f"File missing on disk: {storage}")
    return FileResponse(
        path=str(storage),
        filename=artifact.name,
        media_type=artifact.content_type or "application/octet-stream",
    )


@router.delete("/{artifact_id}", status_code=204)
async def delete_artifact(artifact_id: str, request: Request):
    """Delete artifact. Central: also removes file. External: just DB record."""
    db = request.app.state.db
    row = await db.fetchone("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
    if not row:
        raise HTTPException(404, f"Artifact not found: {artifact_id}")

    artifact = _row_to_artifact(row)
    if artifact.storage_kind == "central":
        storage = Path(artifact.storage_path)
        if storage.exists() and storage.is_file():
            storage.unlink()
    await db.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
    await audit_log(
        db, "artifact.deleted",
        actor="operator",
        project_id=artifact.project_id,
        task_id=artifact.task_id,
        payload={"name": artifact.name, "kind": artifact.storage_kind},
    )
    return Response(status_code=204)
