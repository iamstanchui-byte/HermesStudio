"""Settings API (LLM provider + API key + model + Telegram).

Endpoints:
- GET    /api/settings/llm              current LLM config (no full key)
- POST   /api/settings/llm              save LLM config (validates, writes yaml)
- POST   /api/settings/llm/test         test LLM connection (does NOT save)
- GET    /api/settings/telegram         current Telegram config (no token)
- POST   /api/settings/telegram        save Telegram config

For MVP, settings are stored in the user config.yaml (~/.hermes-orchestrator/config.yaml).
The in-memory `app.state.config` is NOT auto-reloaded — user must restart
orchestrator (or we add hot-reload later) for changes to take effect.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes_orch.config import LLM_PROVIDERS, load_config, save_config_section

router = APIRouter()


# ===== LLM =====


class LLMConfigIn(BaseModel):
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    mock: bool | None = None
    provider: str | None = None  # informational; helps defaults


class LLMConfigOut(BaseModel):
    api_key_set: bool
    api_key_last4: str | None = None
    model: str | None = None
    base_url: str | None = None
    mock: bool
    provider: str | None = None
    providers: list[dict[str, str]] = Field(default_factory=list)


def _llm_view(cfg: dict[str, Any]) -> LLMConfigOut:
    llm = cfg.get("llm") or {}
    api_key = (llm.get("api_key") or "").strip()
    return LLMConfigOut(
        api_key_set=bool(api_key),
        api_key_last4=api_key[-4:] if len(api_key) >= 4 else None,
        model=llm.get("model"),
        base_url=llm.get("base_url"),
        mock=bool(llm.get("mock", True)),
        provider=llm.get("provider"),
        providers=LLM_PROVIDERS,
    )


@router.get("/llm", response_model=LLMConfigOut)
async def get_llm(request: Request) -> LLMConfigOut:
    return _llm_view(request.app.state.config)


@router.post("/llm", response_model=LLMConfigOut)
async def post_llm(body: LLMConfigIn, request: Request) -> LLMConfigOut:
    """Save LLM config to disk. Reload to apply (we tell the user)."""
    updates: dict[str, Any] = {}
    if body.api_key is not None:
        updates["api_key"] = body.api_key.strip()
    if body.model is not None:
        updates["model"] = body.model.strip()
    if body.base_url is not None:
        updates["base_url"] = body.base_url.rstrip("/")
    if body.mock is not None:
        updates["mock"] = bool(body.mock)
    if body.provider is not None:
        updates["provider"] = body.provider
    if not updates:
        raise HTTPException(400, "no fields to update")
    # If api_key is being set and is non-empty, auto-disable mock
    if "api_key" in updates and updates["api_key"]:
        updates["mock"] = False
    save_config_section("llm", updates)
    # Reload config into in-memory
    from hermes_orch.config import load_config
    request.app.state.config = load_config()
    # Reload supervisor's planner + notifier with new config
    cfg = request.app.state.config
    request.app.state.planner.__init__(cfg)
    request.app.state.notifier.__init__(cfg)
    return _llm_view(cfg)


class LLMTestIn(BaseModel):
    api_key: str
    base_url: str
    model: str | None = None


@router.post("/llm/test")
async def test_llm(body: LLMTestIn, request: Request) -> dict[str, Any]:
    """Test LLM connection with provided credentials. Does NOT save.

    Makes a small chat completion call to verify the key works.
    """
    if not body.api_key.strip() or not body.base_url.strip():
        raise HTTPException(400, "api_key and base_url are required")
    cfg = request.app.state.config
    timeout = float((cfg.get("llm") or {}).get("timeout_seconds", 30))
    headers = {
        "Authorization": f"Bearer {body.api_key.strip()}",
        "Content-Type": "application/json",
    }
    # Use the model from the request, or fall fall back to a tiny test prompt
    test_prompt = "Reply with only the word 'ok' and nothing else."
    payload = {
        "model": body.model or "test",
        "messages": [{"role": "user", "content": test_prompt}],
        "max_tokens": 5,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{body.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
        if r.status_code == 200:
            return {"ok": True, "status": r.status_code, "message": "Connection successful"}
        # Surface the error message
        try:
            err_body = r.json()
        except Exception:
            err_body = {"raw": r.text[:500]}
        return {
            "ok": False,
            "status": r.status_code,
            "error": _extract_error(err_body) or f"HTTP {r.status_code}",
            "raw": err_body,
        }
    except httpx.TimeoutException:
        return {"ok": False, "error": f"timeout after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _extract_error(body: Any) -> str | None:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return err.get("message")
        if isinstance(err, str):
            return err
    return None


# ===== Telegram =====


class TelegramConfigIn(BaseModel):
    enabled: bool | None = None
    bot_token: str | None = None
    chat_id: str | None = None


class TelegramConfigOut(BaseModel):
    enabled: bool
    bot_token_set: bool
    bot_token_last4: str | None = None
    chat_id: str | None = None
    ready: bool


@router.get("/telegram", response_model=TelegramConfigOut)
async def get_telegram(request: Request) -> TelegramConfigOut:
    tg = (request.app.state.config.get("telegram") or {})
    token = (tg.get("bot_token") or "").strip()
    return TelegramConfigOut(
        enabled=bool(tg.get("enabled", False)),
        bot_token_set=bool(token),
        bot_token_last4=token[-4:] if len(token) >= 4 else None,
        chat_id=(tg.get("chat_id") or "").strip() or None,
        ready=bool(tg.get("enabled", False)) and bool(token) and bool(tg.get("chat_id")),
    )


@router.post("/telegram", response_model=TelegramConfigOut)
async def post_telegram(body: TelegramConfigIn, request: Request) -> TelegramConfigOut:
    updates: dict[str, Any] = {}
    if body.enabled is not None:
        updates["enabled"] = bool(body.enabled)
    if body.bot_token is not None:
        updates["bot_token"] = body.bot_token.strip()
    if body.chat_id is not None:
        updates["chat_id"] = body.chat_id.strip()
    if not updates:
        raise HTTPException(400, "no fields to update")
    save_config_section("telegram", updates)
    from hermes_orch.config import load_config
    request.app.state.config = load_config()
    request.app.state.notifier.__init__(request.app.state.config)
    return await get_telegram(request)


class TelegramTestIn(BaseModel):
    text: str = "✅ Test from Hermes Orchestrator"


@router.post("/telegram/test")
async def test_telegram(body: TelegramTestIn, request: Request) -> dict[str, Any]:
    """Send a test message to verify the bot can reach the chat."""
    await request.app.state.notifier.send(body.text, level="info")
    tg = request.app.state.config.get("telegram") or {}
    if not tg.get("enabled"):
        return {"ok": False, "error": "telegram not enabled"}
    token = (tg.get("bot_token") or "").strip()
    chat = (tg.get("chat_id") or "").strip()
    if not token or not chat:
        return {"ok": False, "error": "bot_token or chat_id missing"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": body.text, "disable_web_page_preview": True},
            )
        if r.status_code == 200:
            return {"ok": True, "message": "sent"}
        return {"ok": False, "status": r.status_code, "error": r.text[:200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ===== Project Storage =====


class ProjectStorageIn(BaseModel):
    storage_root: str | None = None


class ProjectStorageOut(BaseModel):
    storage_root: str
    exists: bool
    writable: bool
    project_count: int
    # Whether the current value is the default (./projects) or user-configured
    is_default: bool


def _project_storage_view(cfg: dict[str, Any], request: Request) -> ProjectStorageOut:
    proj = cfg.get("projects") or {}
    root = (proj.get("storage_root") or "").strip()
    is_default = root in ("./projects", "projects", "")
    # Compute project count (best effort — fast count)
    try:
        from pathlib import Path
        project_count = 0
        if root:
            p = Path(root)
            if p.exists() and p.is_dir():
                project_count = sum(1 for x in p.iterdir() if x.is_dir())
    except Exception:
        project_count = -1
    return ProjectStorageOut(
        storage_root=root,
        exists=Path(root).exists() if root else False,
        writable=Path(root).is_dir() and (Path(root) / ".orch-write-test").is_file() or False,
        project_count=project_count,
        is_default=is_default,
    )


@router.get("/project", response_model=ProjectStorageOut)
async def get_project_storage(request: Request) -> ProjectStorageOut:
    return _project_storage_view(request.app.state.config, request)


@router.post("/project", response_model=ProjectStorageOut)
async def post_project_storage(body: ProjectStorageIn, request: Request) -> ProjectStorageOut:
    """Save project storage root. New projects will be created in the new path.

    Note: existing projects stay in their original location. To migrate, move
    the project folders manually after changing this setting.
    """
    if body.storage_root is None or not body.storage_root.strip():
        raise HTTPException(400, "storage_root is required")
    new_root = body.storage_root.strip().rstrip("/\\")
    if not new_root:
        raise HTTPException(400, "storage_root cannot be empty")
    # Create the dir if it doesn't exist (best-effort)
    from pathlib import Path
    p = Path(new_root)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(400, f"cannot create directory: {e}")
    if not p.is_dir():
        raise HTTPException(400, f"path is not a directory: {new_root}")
    # Write to YAML
    save_config_section("projects", {"storage_root": new_root})
    # Reload in-memory config
    from hermes_orch.config import load_config
    request.app.state.config = load_config()
    return _project_storage_view(request.app.state.config, request)


class ProjectStorageTestIn(BaseModel):
    storage_root: str | None = None  # if None, test current


@router.post("/project/test")
async def test_project_storage(body: ProjectStorageTestIn, request: Request) -> dict[str, Any]:
    """Test write access to a project storage path without saving.

    Creates a temp file and removes it. Returns ok if successful.
    """
    from pathlib import Path
    target = body.storage_root or (request.app.state.config.get("projects") or {}).get("storage_root", "")
    if not target:
        return {"ok": False, "error": "no storage_root provided"}
    p = Path(target)
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"cannot create: {type(e).__name__}: {e}"}
    if not p.is_dir():
        return {"ok": False, "error": "path is not a directory"}
    test_file = p / ".orch-write-test"
    try:
        test_file.write_text("ok\n", encoding="utf-8")
        content = test_file.read_text(encoding="utf-8")
        test_file.unlink()
        # Count project folders
        project_count = sum(1 for x in p.iterdir() if x.is_dir() and not x.name.startswith("."))
        return {
            "ok": True,
            "path": str(p),
            "writable": True,
            "project_count": project_count,
            "test_written": len(content) > 0,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class ProjectStorageOpenIn(BaseModel):
    storage_root: str | None = None


@router.post("/project/open")
async def open_project_storage(body: ProjectStorageOpenIn, request: Request) -> dict[str, Any]:
    """Open the project storage path in the OS file explorer.

    The browser can't open local paths directly, so we shell out from the
    server. Works on Windows (explorer.exe), macOS (open), and Linux
    (xdg-open). The path is taken from the request body (so the user can
    open a not-yet-saved path), or falls back to the current config.
    """
    import os
    import platform
    import subprocess
    from pathlib import Path
    target = (body.storage_root or "").strip() or (request.app.state.config.get("projects") or {}).get("storage_root", "")
    if not target:
        return {"ok": False, "error": "no storage_root provided"}
    p = Path(target)
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"cannot create directory: {e}"}

    sysname = platform.system().lower()
    try:
        if sysname.startswith("win"):
            # explorer.exe needs backslashes for some paths
            win_path = str(p).replace("/", "\\")
            subprocess.Popen(["explorer", win_path])
        elif sysname == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return {"ok": True, "path": str(p), "platform": sysname}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"file manager not found: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ===== Cleanup =====


class CleanupConfigIn(BaseModel):
    retention_days: int | None = None
    daily_sweep: bool | None = None


class CleanupConfigOut(BaseModel):
    retention_days: int
    daily_sweep: bool
    last_run_at: str | None = None
    last_run_result: dict[str, Any] | None = None


def _cleanup_view(
    request: Request,
    *,
    extra_result: dict[str, Any] | None = None,
) -> CleanupConfigOut:
    """Build the cleanup config view (current config + last run)."""
    cfg = request.app.state.config
    cleanup = cfg.get("cleanup") or {}
    try:
        rd = int(cleanup.get("retention_days", 30))
    except (TypeError, ValueError):
        rd = 30
    out = CleanupConfigOut(
        retention_days=rd,
        daily_sweep=bool(cleanup.get("daily_sweep", True)),
    )
    job = getattr(request.app.state, "cleanup", None)
    if job is not None:
        out.last_run_at = job.last_run_at
        # Use the freshest result — extra_result (just-finished run)
        # beats the cached one.
        out.last_run_result = extra_result or job.last_run_result
    return out


@router.get("/cleanup", response_model=CleanupConfigOut)
async def get_cleanup(request: Request) -> CleanupConfigOut:
    """Return current cleanup config + last run info."""
    return _cleanup_view(request)


@router.post("/cleanup", response_model=CleanupConfigOut)
async def post_cleanup(
    body: CleanupConfigIn, request: Request
) -> CleanupConfigOut:
    """Update cleanup config (retention_days + daily_sweep).

    Persists to config.yaml and reloads in-memory config so the
    supervisor's next tick sees the new value.
    """
    if body.retention_days is None and body.daily_sweep is None:
        raise HTTPException(400, "retention_days or daily_sweep required")
    if body.retention_days is not None:
        if body.retention_days < 0 or body.retention_days > 3650:
            raise HTTPException(
                400, "retention_days must be in 0..3650 (0 = disable)"
            )
    updates: dict[str, Any] = {}
    if body.retention_days is not None:
        updates["retention_days"] = int(body.retention_days)
    if body.daily_sweep is not None:
        updates["daily_sweep"] = bool(body.daily_sweep)
    save_config_section("cleanup", updates)
    # Reload in-memory config
    new_cfg = load_config()
    request.app.state.config = new_cfg
    job = getattr(request.app.state, "cleanup", None)
    if job is not None:
        job.update_config(new_cfg)
    return _cleanup_view(request)


class CleanupRunIn(BaseModel):
    retention_days: int | None = None
    dry_run: bool = False


@router.post("/cleanup/run")
async def post_cleanup_run(
    body: CleanupRunIn, request: Request
) -> dict[str, Any]:
    """Manually trigger a cleanup run.

    Returns the run result (scanned, deleted, errors, eligible list).
    With dry_run=true, scans but does not delete (for the "Preview"
    button on the settings page).
    """
    job = getattr(request.app.state, "cleanup", None)
    if job is None:
        raise HTTPException(503, "cleanup job not initialized")
    if body.retention_days is not None:
        if body.retention_days < 0 or body.retention_days > 3650:
            raise HTTPException(400, "retention_days must be in 0..3650")
    result = await job.run(
        retention_days=body.retention_days,
        trigger="manual",
        dry_run=body.dry_run,
    )
    return result


@router.get("/cleanup/preview")
async def get_cleanup_preview(request: Request) -> dict[str, Any]:
    """Preview-only: return how many projects are eligible right now.

    Used by the settings page to show the user "N projects would be
    deleted" before they hit "Run cleanup now".
    """
    job = getattr(request.app.state, "cleanup", None)
    if job is None:
        raise HTTPException(503, "cleanup job not initialized")
    return await job.preview()
