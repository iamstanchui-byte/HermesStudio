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
        # Cannot safely restart ourselves. Tell the operator how.
        response.status_code = 501
        return RestartOut(
            mode="manual",
            message=(
                "Cannot restart automatically in this environment. "
                "Please restart the server manually."
            ),
            restart_command=(
                "Ctrl+C and re-run `hermes-orch serve` (or your service "
                "manager's restart command). The new bind_host will be "
                "applied on next start."
            ),
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
