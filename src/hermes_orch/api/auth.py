"""Auth / bootstrap endpoints (per REVIEW.md §6.2).

TODO:
- POST /api/auth/agent-bootstrap    — agent first-time setup (with admin token)
- POST /api/auth/verify             — verify HMAC signature (middleware helper)
- POST /api/auth/rotate             — rotate agent key
- GET  /api/auth/admin-token        — get current admin token (first-time only)
"""
from fastapi import APIRouter

router = APIRouter()


@router.post("/agent-bootstrap")
async def agent_bootstrap() -> dict:
    """Bootstrap endpoint: agent first-time setup with admin token.

    Body: {"admin_token": "...", "agent_id": "...", "ip": "...", "os_type": "..."}
    Returns: {"secret": "<setup-secret>"}  (one-time, copy to agent OS)
    """
    # TODO: implement
    return {"secret": "TODO"}
