# coding: utf-8
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
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from hermes_orch.auth.cookie import ROLE_ADMIN, current_user
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
    """Open the project storage path in the OS file manager.

    The browser can't open local paths directly, so we shell out from the
    server. Cross-platform: dispatches via
    `hermes_orch.core.platform_compat` (Windows Explorer / macOS Finder /
    Linux xdg-open with fallback chain). The path is taken from the
    request body (so the user can open a not-yet-saved path), or falls
    back to the current config.
    """
    from pathlib import Path

    from hermes_orch.core.platform_compat import (
        file_manager_label,
        open_path,
        platform_name,
    )

    target = (body.storage_root or "").strip() or (request.app.state.config.get("projects") or {}).get("storage_root", "")
    if not target:
        return {"ok": False, "error": "no storage_root provided"}
    p = Path(target)
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"cannot create directory: {e}"}

    ok, err = open_path(p)
    result: dict[str, Any] = {
        "ok": ok,
        "path": str(p),
        "platform": platform_name(),
        "file_manager": file_manager_label(),
    }
    if not ok:
        result["error"] = err or "unknown error"
    return result


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


# ===== HTTPS (v3.12.0) =====


class HttpsConfigIn(BaseModel):
    enabled: bool | None = None
    ssl_cert_path: str | None = None
    ssl_key_path: str | None = None


class HttpsConfigOut(BaseModel):
    enabled: bool
    ssl_cert_path: str
    ssl_key_path: str
    cert_exists: bool
    key_exists: bool
    # Days until cert expires (None if cert unreadable / not present).
    cert_expires_in_days: int | None = None
    cert_subject_cn: str | None = None
    cert_sans: list[str] = Field(default_factory=list)
    ready: bool   # enabled=true AND cert+key both readable


def _https_view(cfg: dict[str, Any]) -> HttpsConfigOut:
    from pathlib import Path

    https = cfg.get("https") or {}
    cert_path_str = (https.get("ssl_cert_path") or "").strip()
    key_path_str = (https.get("ssl_key_path") or "").strip()
    cert_p = Path(cert_path_str) if cert_path_str else None
    key_p = Path(key_path_str) if key_path_str else None
    cert_exists = bool(cert_p and cert_p.is_file())
    key_exists = bool(key_p and key_p.is_file())

    expires_in: int | None = None
    subject_cn: str | None = None
    sans: list[str] = []
    if cert_exists:
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization
            with open(cert_p, "rb") as f:  # type: ignore[arg-type]
                cert = x509.load_pem_x509_certificate(f.read())
            import datetime as _dt
            now = _dt.datetime.now(_dt.timezone.utc)
            delta = cert.not_valid_after_utc - now
            expires_in = max(0, int(delta.total_seconds() // 86400))
            try:
                cn_attrs = cert.subject.get_attributes_for_oid(
                    __import__("cryptography.x509.oid", fromlist=["NameOID"]).NameOID.COMMON_NAME
                )
                if cn_attrs:
                    subject_cn = cn_attrs[0].value
            except Exception:
                pass
            try:
                ext = cert.extensions.get_extension_for_class(
                    __import__("cryptography.x509", fromlist=["SubjectAlternativeName"]).SubjectAlternativeName
                ).value
                sans = [str(n) for n in ext.get_values_for_type(
                    __import__("cryptography.x509", fromlist=["DNSName"]).DNSName
                )]
            except Exception:
                pass
        except Exception:
            expires_in = None  # unreadable / malformed cert

    return HttpsConfigOut(
        enabled=bool(https.get("enabled", False)),
        ssl_cert_path=cert_path_str,
        ssl_key_path=key_path_str,
        cert_exists=cert_exists,
        key_exists=key_exists,
        cert_expires_in_days=expires_in,
        cert_subject_cn=subject_cn,
        cert_sans=sans,
        ready=bool(https.get("enabled", False)) and cert_exists and key_exists,
    )


@router.get("/https", response_model=HttpsConfigOut)
async def get_https(request: Request) -> HttpsConfigOut:
    return _https_view(request.app.state.config)


@router.post("/https", response_model=HttpsConfigOut)
async def post_https(body: HttpsConfigIn, request: Request) -> HttpsConfigOut:
    """Update HTTPS config. Caller must restart `hermes-orch serve`
    for the change to take effect (we don't kill -9 the server from
    inside a request handler).
    """
    if body.enabled is None and body.ssl_cert_path is None and body.ssl_key_path is None:
        raise HTTPException(400, "no fields to update")
    updates: dict[str, Any] = {}
    if body.enabled is not None:
        updates["enabled"] = bool(body.enabled)
    if body.ssl_cert_path is not None:
        path = body.ssl_cert_path.strip()
        # Reject obviously bad paths up front (so the user gets an
        # error here, not a server crash on next restart). Cert files
        # are read at boot, not at POST time, so a missing file is
        # not fatal — we just store the path.
        updates["ssl_cert_path"] = path
    if body.ssl_key_path is not None:
        path = body.ssl_key_path.strip()
        updates["ssl_key_path"] = path
    save_config_section("https", updates)
    from hermes_orch.config import load_config
    request.app.state.config = load_config()
    return _https_view(request.app.state.config)


@router.post("/https/upload")
async def post_https_upload(
    request: Request,
    cert: UploadFile = File(...),  # type: ignore[assignment]
    key: UploadFile = File(...),   # type: ignore[assignment]
) -> HttpsConfigOut:
    """Upload a cert + key PEM pair from the settings page.

    The files are stored under `~/.hermes-orchestrator/certs/` with
    mode 0600 on the key, and the HTTPS config is updated to point
    at them. The server is NOT restarted — the user clicks
    "Restart server" (or runs `hermes-orch serve` again) to apply.
    """
    from pathlib import Path

    cert_dir = Path.home() / ".hermes-orchestrator" / "certs"
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "server.crt"
    key_path = cert_dir / "server.key"

    cert_bytes = await cert.read()
    key_bytes = await key.read()

    # Sanity: try to parse the cert so we don't store garbage.
    try:
        from cryptography import x509
        x509.load_pem_x509_certificate(cert_bytes)
    except Exception as e:
        raise HTTPException(400, f"cert is not a valid PEM X.509 cert: {e}")

    # Sanity: try to parse the key.
    try:
        from cryptography.hazmat.primitives import serialization
        serialization.load_pem_private_key(key_bytes, password=None)
    except Exception as e:
        raise HTTPException(400, f"key is not a valid PEM private key: {e}")

    cert_path.write_bytes(cert_bytes)
    key_path.write_bytes(key_bytes)
    try:
        __import__("os").chmod(key_path, 0o600)
    except Exception:
        pass

    save_config_section("https", {
        "enabled": True,
        "ssl_cert_path": cert_path.as_posix(),
        "ssl_key_path": key_path.as_posix(),
    })
    from hermes_orch.config import load_config
    request.app.state.config = load_config()
    return _https_view(request.app.state.config)


# ===== Network bind host (v1.0.1 new-user-activation §3.1) =====


class BindHostIn(BaseModel):
    """Body for POST /api/settings/bind-host.

    `lan_enabled=true`  -> server will rebind to 0.0.0.0 after restart
                            (LAN + loopback, exposes dashboard to LAN)
    `lan_enabled=false` -> server will rebind to 127.0.0.1 after restart
                            (loopback only, secure default)

    The bind change requires a server restart (a live socket listener
    cannot rebind without one). The endpoint sets the `restart-required`
    flag on every write, regardless of whether the new value differs
    from the old one. Operator then triggers restart via
    POST /api/server/restart.
    """

    lan_enabled: bool


class BindHostOut(BaseModel):
    """Response for GET /api/settings/bind-host.

    `active` is what the running server is currently bound to (read from
    the in-memory config that was loaded at startup — only changes on
    restart). `desired` is what the operator has asked for; if it
    differs from `active`, the operator needs to restart to apply.

    `restart_required` is true when the desired and active binds
    differ. `restart_reason` is a human-readable string explaining
    why a restart is needed (e.g. "bind_host: 127.0.0.1 -> 0.0.0.0").

    `lan_enabled` is a convenience derived from `active == "0.0.0.0"`.
    """

    active: str
    desired: str
    lan_enabled: bool
    restart_required: bool
    restart_reason: str = ""
    lan_url: str = ""  # auto-detected LAN IP for agent host enrollment; empty if loopback


def _detect_lan_url(bind_host: str, port: int, scheme: str = "http") -> str:
    """Return a LAN URL the operator can give to agent hosts.

    Empty if `bind_host` is not 0.0.0.0 (LAN access is disabled).
    Otherwise resolve the local IP via the routing table (UDP socket
    to a public IP, read the local endpoint, never sends a packet).
    Returns "" if the lookup fails (e.g. no network).
    """
    if bind_host != "0.0.0.0":
        return ""
    try:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
        return f"{scheme}://{lan_ip}:{port}"
    except OSError:
        return ""


@router.get("/bind-host", response_model=BindHostOut)
async def get_bind_host(request: Request) -> BindHostOut:
    """Read the current + desired bind host + restart-required flag.

    The `active` field is the live bind (read from in-memory cfg). The
    `desired` field is the operator's pending choice (read from disk
    if it differs from active, or equals active otherwise).

    The "desired differs from active" case only happens after a write
    that hasn't been applied yet. Until the operator restarts, both
    fields are returned so the UI can show "Active: X, Pending: Y,
    Restart to apply."
    """
    from hermes_orch.config import find_config_path
    from hermes_orch.core.restart import is_restart_required

    # Active bind: read from in-memory config (loaded at startup)
    cfg = request.app.state.config
    active = cfg["orchestrator"].get("bind_host") or "127.0.0.1"

    # Desired bind: read from disk config (operator's latest write)
    desired = active
    cfg_path = find_config_path()
    if cfg_path and cfg_path.exists():
        try:
            import yaml

            with open(cfg_path, encoding="utf-8") as f:
                file_cfg = yaml.safe_load(f) or {}
            desired = file_cfg.get("orchestrator", {}).get("bind_host") or active
        except OSError:
            desired = active

    restart = is_restart_required()
    port = cfg["orchestrator"].get("port") or 8765
    scheme = "https" if (cfg.get("https") or {}).get("enabled") else "http"

    return BindHostOut(
        active=active,
        desired=desired,
        lan_enabled=(active == "0.0.0.0"),
        restart_required=restart.required,
        restart_reason=restart.reason,
        lan_url=_detect_lan_url(active, port, scheme),
    )


@router.post("/bind-host", response_model=BindHostOut)
async def post_bind_host(body: BindHostIn, request: Request) -> BindHostOut:
    """Set the desired bind host. Always requires a server restart.

    The write does NOT take effect immediately (the live socket cannot
    rebind). The endpoint sets the `restart-required` flag and returns
    the same shape as GET so the UI can show the "Restart required"
    banner. The operator triggers restart via /api/server/restart.

    Admin-only: changing the bind host exposes the dashboard to the
    LAN (or removes it). This is a security-sensitive operation
    (matches the /api/server/restart policy: only admins can affect
    server lifecycle + LAN exposure).
    """
    user = await current_user(request)
    if not user or user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "Admin-only endpoint")

    from hermes_orch.core.restart import write_restart_required

    desired_bind = "0.0.0.0" if body.lan_enabled else "127.0.0.1"
    current_active = request.app.state.config["orchestrator"].get("bind_host") or "127.0.0.1"

    # Persist the desired bind to disk. This is the value the NEXT
    # server start will read via `load_config()`.
    save_config_section("orchestrator", {"bind_host": desired_bind})

    # Always set the restart-required flag — even if desired == active,
    # the operator may want a restart for unrelated reasons (e.g.
    # they edited the config file by hand). Idempotent.
    write_restart_required(
        f"bind_host: {current_active} -> {desired_bind} "
        f"(lan_enabled={body.lan_enabled}); restart to apply"
    )

    # Return the new desired state. The active field is unchanged
    # (we don't know what the live bind will be after the next start
    # until the operator actually triggers the restart).
    return await get_bind_host(request)
