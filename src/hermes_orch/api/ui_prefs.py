"""Per-user UI preferences + plan-presets lookup (v3.9.0, Phase 2).

Two small endpoints, both of which are out of `api/projects.py` (that
file is being touched in parallel by the backend sub-agent) and out of
`api/dashboard.py` (which is the page-render layer, not the JSON API
layer):

  POST /api/users/me/ui-prefs
      Body: {"show_soul_editor": bool}
      Effect: set a signed itsdangerous cookie (`orch_ui_prefs`) with
              the new value. The project page reads this cookie via
              the session and conditionally renders the SOUL presets
              section. Per-user (cookie), not per-project — switching
              projects doesn't reset the user's opt-in.

  GET /api/users/me/ui-prefs
      Returns the current prefs. Used by the project page's
      `session.get('show_soul_editor')` short-circuit on the server.

  GET /api/projects/{id}/plan/presets
      Returns the project's SOUL presets as
        {presets: [{role_name, profile_id, content_summary, ...}, ...]}
      Cached on the client at 30s TTL (per the visual plan editor's
      30s presets cache) so the pill re-render on every step-card
      redraw doesn't refetch.

Why a new file? The spec said either `api/dashboard.py` or a new
`api/ui_prefs.py`. A new file is cleaner — these endpoints are JSON
APIs (not page-rendering) and they're scoped to a single feature.
Avoids polluting `dashboard.py` with non-page concerns.

The cookie-based session pattern uses the SAME `TimestampSigner` from
`auth/cookie.py` that the login cookie uses — no new secret, no new
signer, just a different cookie name + a different payload.
"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse


router = APIRouter()


# Cookie name distinct from `hermes_orch_session` (login) and
# `orch.*` localStorage keys (theme / sidebar). Stored as a signed
# itsdangerous value (so users can't forge the flag). Max age 7 days
# matches the login cookie so the two expire together.
_PREF_COOKIE_NAME = "orch_ui_prefs"
_PREF_COOKIE_MAX_AGE = 7 * 24 * 60 * 60  # 7 days

# Plan-presets cache TTL on the client. Short enough that a
# freshly-saved preset shows up on the next pill re-render, long
# enough that re-renders during a single editing burst (drag, undo,
# paste) don't refetch.
_PRESETS_TTL_S = 30


def _read_pref_cookie(request: Request) -> dict[str, Any]:
    """Read the ui-prefs cookie. Returns {} on missing/tampered/expired.

    We never raise from here — a corrupted cookie just means "treat as
    default (hidden)" so a one-off tampered cookie can't lock the user
    out of the SOUL editor. They can still re-toggle it via the page.
    """
    from hermes_orch.auth.cookie import _get_signer
    raw = request.cookies.get(_PREF_COOKIE_NAME)
    if not raw:
        return {}
    try:
        unsigned = _get_signer().unsign(raw, max_age=_PREF_COOKIE_MAX_AGE)
        obj = json.loads(unsigned.decode("utf-8"))
        if not isinstance(obj, dict):
            return {}
        return obj
    except Exception:
        return {}


def _write_pref_cookie(response: JSONResponse, prefs: dict[str, Any]) -> None:
    """Attach a signed ui-prefs cookie to the response.

    Same itsdangerous TimestampSigner as the login cookie; different
    cookie name so they don't collide and different payload (a dict of
    booleans, not a user_id).
    """
    from hermes_orch.auth.cookie import _get_signer
    payload = json.dumps(prefs, separators=(",", ":")).encode("utf-8")
    signed = _get_signer().sign(payload).decode("ascii")
    response.set_cookie(
        key=_PREF_COOKIE_NAME,
        value=signed,
        max_age=_PREF_COOKIE_MAX_AGE,
        httponly=True,        # JS doesn't need to read this; the server sets it
        samesite="lax",
        path="/",
    )


# ===== /api/users/me/ui-prefs =====


@router.get("/api/users/me/ui-prefs")
async def get_ui_prefs(request: Request) -> dict[str, Any]:
    """Return the current user's UI prefs. Defaults: show_soul_editor=false.

    Used by the project page's Jinja `session.get('show_soul_editor')`
    to decide whether to render the SOUL presets section. This endpoint
    is a thin wrapper that exists so the toggle button (which posts to
    the same path) has a matching GET to read-after-write.
    """
    prefs = _read_pref_cookie(request)
    return {
        "show_soul_editor": bool(prefs.get("show_soul_editor", False)),
    }


@router.post("/api/users/me/ui-prefs")
async def set_ui_prefs(request: Request) -> JSONResponse:
    """Update the current user's UI prefs.

    Body: {"show_soul_editor": bool}  (other keys are accepted but ignored)

    Always returns 200 with the new prefs so the page can update its
    visible state without a follow-up GET. The Set-Cookie header is
    where the persistence happens.

    401 if no valid session — ui-prefs are per-user.
    """
    # Middleware already gated the route to authenticated users, but
    # check defensively in case this endpoint is reused outside the
    # gated surface.
    from hermes_orch.auth.cookie import current_user_id as _cuid
    uid = await _cuid(request)
    if not uid:
        raise HTTPException(401, "Not authenticated")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object")

    # Merge with the existing prefs (don't clobber other flags the
    # user may have set in earlier toggles).
    current = _read_pref_cookie(request)
    if "show_soul_editor" in body:
        current["show_soul_editor"] = bool(body["show_soul_editor"])
    # Stamp a version so future schema changes can ignore old cookies.
    current.setdefault("v", 1)
    current["u"] = uid  # owner id, makes cross-user tampering noisy (cosmetic only)

    response = JSONResponse({"show_soul_editor": bool(current.get("show_soul_editor", False))})
    _write_pref_cookie(response, current)
    return response


# Helper used by the project page's Jinja template to read the same
# cookie server-side without going through the middleware. Kept here
# (not in dashboard.py) so the page's session dict has a single
# source of truth for the prefs cookie.
def get_session_ui_prefs(request: Request) -> dict[str, Any]:
    """Read the prefs cookie and return the keys the project page
    looks up via `session.get(...)`. Returns a dict with at least
    `show_soul_editor: bool` (defaults to False)."""
    prefs = _read_pref_cookie(request)
    return {
        "show_soul_editor": bool(prefs.get("show_soul_editor", False)),
    }


# ===== /api/projects/{id}/plan/presets =====


@router.get("/api/projects/{project_id}/plan/presets")
async def get_plan_presets(project_id: str, request: Request) -> dict[str, Any]:
    """Return the project's SOUL presets for the visual plan editor.

    Used to color the per-step SOUL pill: green if a preset exists for
    the step's `agent_role`, gray otherwise. The client caches this
    response for 30s (declared via the `_PRESETS_TTL_S` constant above)
    so re-renders during a single editing burst don't re-fetch.

    The shape mirrors what `_compute_plan_agents` returns for the
    chatbox: a flat list of role-relevant records, joined with the
    profile + preset tables. We project only the fields the pill
    actually needs (role_name, profile_id, content_summary) to keep
    the payload small — the visual editor never needs the full SOUL
    text or the timestamp columns.

    content_summary is a truncated version of `content` (first 80
    chars) for tooltips — the full content stays server-side, available
    via GET /api/projects/{id}/soul-presets for the "advanced" page
    that the user has to opt into.
    """
    db = request.app.state.db
    # 404 if the project doesn't exist (matches the rest of the API
    # surface — empty list would be silently misleading for a typo).
    proj = await db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")

    rows = await db.fetchall(
        "SELECT sp.id, sp.profile_id, sp.role_name, sp.content, "
        "sp.default_soul, ap.agent_id, ap.name AS profile_name "
        "FROM project_soul_presets sp "
        "JOIN agent_profiles ap ON ap.id = sp.profile_id "
        "WHERE sp.project_id = ? "
        "ORDER BY ap.agent_id, ap.name",
        (project_id,),
    )

    presets: list[dict[str, Any]] = []
    for r in rows:
        content = r.get("content") or r.get("default_soul") or ""
        # 80 chars is enough for a "what does this preset do?" tooltip
        # without leaking the full SOUL to the JS side. Trim trailing
        # whitespace + collapse newlines so the tooltip is one line.
        summary = " ".join(str(content).split())[:80]
        if len(str(content)) > 80:
            summary = summary.rstrip() + "…"
        presets.append({
            "id": r["id"],
            "role_name": r["role_name"],
            "profile_id": r["profile_id"],
            "agent_id": r["agent_id"],
            "profile_name": r["profile_name"],
            "content_summary": summary,
        })

    return {
        "project_id": project_id,
        "presets": presets,
        "ttl_seconds": _PRESETS_TTL_S,
        "fetched_at": int(time.time()),
    }
