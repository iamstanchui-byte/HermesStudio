# coding: utf-8
"""Server lifecycle endpoints (v1.0.1 new-user-activation §3.1.2).

Currently:
- POST /api/server/restart  admin-only; triggers a server restart in the
  appropriate mode for the current process (supervised / direct /
  undetectable). Returns 202 on supervised/direct (the process is
  about to exit) or 501 on undetectable (caller must restart by hand).

The restart endpoint must be admin-only because a restart is a brief
window of unavailability that affects every concurrent user, plus
there's a TOCTOU race: between setting the restart-required flag and
the restart actually completing, anyone could hit POST /bind-host
again and overwrite the desired bind.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from hermes_orch.auth.cookie import ROLE_ADMIN, current_user
from hermes_orch.core.restart import (
    PROCESS_MODE_DIRECT,
    PROCESS_MODE_UNDETECTABLE,
    _find_non_respawning_launcher_name,
    detect_process_mode,
    is_restart_required,
    perform_restart,
)

router = APIRouter()


class RestartOut(BaseModel):
    mode: str  # "supervised" | "direct" | "manual"
    message: str
    restart_command: str = ""  # for manual mode only


@router.post("/restart", response_model=RestartOut)
async def restart_server(request: Request, response: Response) -> RestartOut:
    """Trigger a server restart (admin-only).

    Modes (per spec §3.1.2):
      - supervised: HERMES_SUPERVISED env var set (systemd / NSSM).
        Server calls sys.exit(0); supervisor restarts us.
        Response: 202 Accepted.
      - direct: normal Python process, safe to os.execv in-place.
        Response: 202 Accepted (sent before execv; the next request
        will hit the new process).
      - undetectable: frozen / embedded / no reliable argv.
        We CANNOT safely restart ourselves. Response: 501 Not
        Implemented, with a copyable restart command in the body.

    The endpoint is a no-op (400) if no restart-required flag is set.
    We don't want operators triggering restarts without a reason (it
    briefly disconnects everyone). The bind-host endpoint sets the
    flag; future endpoints (e.g. a future "rotate session secret"
    flow) will also set the flag.
    """
    user = await current_user(request)
    if not user or user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "Admin-only endpoint")

    restart = is_restart_required()
    if not restart.required:
        raise HTTPException(
            400,
            "No restart is required (no bind_host change pending). "
            "Change a setting first to set the restart-required flag.",
        )

    mode = detect_process_mode()

    if mode == PROCESS_MODE_UNDETECTABLE:
        # Cannot safely restart ourselves. Tell the operator how. The
        # specific command depends on whether we're under a known
        # non-respawning launcher (e.g. hermes-orch.exe) — checked via
        # the immediate parent AND the ancestor chain, since the
        # worker process typically has uvicorn (python.exe) as its
        # immediate parent with hermes-orch.exe several levels up.
        launcher_name = _find_non_respawning_launcher_name()
        if launcher_name:
            # hermes-orch.exe / hermes-orch launcher — use the restart script
            message = (
                f"Server is running under the {launcher_name} launcher, "
                f"which does not auto-respawn its child. Please run "
                f"`restart-server.ps1` (in the project root) to apply "
                f"the new bind_host."
            )
            restart_command = (
                "cd <project root>\n"
                ".\\scripts\\restart-server.ps1\n\n"
                "The new bind_host will be applied on next start."
            )
        else:
            # Generic / dev mode (frozen binary, embedded, etc.)
            message = (
                "Cannot restart automatically in this environment. "
                "Please restart the server manually."
            )
            restart_command = (
                "Ctrl+C and re-run `hermes-orch serve` (or your service "
                "manager's restart command). The new bind_host will be "
                "applied on next start."
            )
        response.status_code = 501
        return RestartOut(
            mode="manual",
            message=message,
            restart_command=restart_command,
        )

    # For supervised + direct, we respond BEFORE perform_restart()
    # so the client gets a clean HTTP response before the process
    # dies / replaces itself.
    response.status_code = 202
    if mode == PROCESS_MODE_DIRECT:
        message = "Restarting in place via os.execv..."
    else:
        message = f"Restarting via supervisor ({mode})..."

    # IMPORTANT: from this point, the process is going to die. We must
    # NOT do any more DB / state work after perform_restart(). The
    # response has been queued by FastAPI; uvicorn will flush it on
    # the next event loop tick before our sys.exit / execv takes
    # effect.
    out = RestartOut(mode=mode, message=message)
    perform_restart()
    # Unreachable in supervised/direct mode (process is gone).
    # Unreachable in undetectable mode too (handled above).
    return out


# ===== Wrapper self-heal scheme discovery (2026-08-16) =====
#
# When the server flips between HTTP and HTTPS (or changes
# public_origin for any other reason), every wrapper that hard-codes
# the old URL in wrapper-config.json breaks. We don't want operators
# to SSH into every agent host and edit the JSON by hand.
#
# This endpoint is the source of truth: the wrapper calls it (over
# whatever URL it currently has configured) to learn the canonical
# scheme + public_origin + cert fingerprint, then writes that to
# wrapper-config.json locally. Next restart uses the new URL.
#
# No auth: the response is just public config -- anyone who can
# reach the port can already learn whether it's TLS. The
# fingerprint is bonus data for a future v0.7.3-style pinning path.
class ServerInfoOut(BaseModel):
    scheme: str                    # "http" | "https"
    public_origin: str             # bare URL, e.g. "https://hermes-win:8765"
    cert_fingerprint_sha256: str   # 64-char hex; "" if scheme=http


def _compute_cert_fingerprint(cert_path: str) -> str:
    """Return lower-case hex SHA-256 of the cert DER bytes.

    Empty string if the file is missing or unreadable (we don't
    want this endpoint to 500 on a misconfigured cert -- it's a
    discovery helper, not a critical-path).
    """
    if not cert_path:
        return ""
    try:
        from pathlib import Path
        data = Path(cert_path).read_bytes()
    except (OSError, ValueError):
        return ""
    # PEM → DER: strip the header/footer + base64-decode. We use
    # stdlib only to avoid a new dependency. (cryptography is already
    # a project dep and used in tests, but keeping the server
    # hot-path stdlib-only is a small win.)
    import base64
    import re as _re
    m = _re.search(
        rb"-----BEGIN CERTIFICATE-----\s*(.+?)\s*-----END CERTIFICATE-----",
        data,
        _re.DOTALL,
    )
    if not m:
        return ""
    der = base64.b64decode(m.group(1))
    import hashlib
    return hashlib.sha256(der).hexdigest()


@router.get("/info", response_model=ServerInfoOut)
async def server_info(request: Request) -> ServerInfoOut:
    """Return the orchestrator's current scheme + public_origin + cert fingerprint.

    Used by agent wrappers for self-heal after the server flips between
    HTTP and HTTPS (or any other public_origin change). The wrapper
    re-reads this on heartbeat and persists the URL to
    wrapper-config.json on the agent host.

    See tests/test_server_info_endpoint.py for the contract.
    """
    cfg = request.app.state.config or {}
    https_cfg = (cfg.get("https") or {})
    scheme = "https" if https_cfg.get("enabled") else "http"
    public_origin = request.app.state.public_origin or ""
    cert_path = (https_cfg.get("ssl_cert_path") or "").strip()
    return ServerInfoOut(
        scheme=scheme,
        public_origin=public_origin,
        cert_fingerprint_sha256=_compute_cert_fingerprint(cert_path),
    )
