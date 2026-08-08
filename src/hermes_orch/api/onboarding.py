# coding: utf-8
"""Onboarding state API (v1.0.1 new-user-activation §3.2).

Endpoints:
- GET    /api/me/onboarding         current user's onboarding state
- POST   /api/me/onboarding/skip    user opts out of the checklist
- POST   /api/me/onboarding/reset  admin-only: reset for re-demo

The /api/me/onboarding endpoint is the source of truth that the
landing page (`GET /`) reads to decide whether to show the 4-step
checklist (templates/onboarding.html) or redirect to /agents.

The skip endpoint is the "Skip for now" affordance on the checklist —
it's a user choice to dismiss the onboarding UI, not a deletion of
progress. Signals already flipped to true stay true; the user can
re-open the checklist later via /settings#onboarding → Reset.

The reset endpoint is admin-only because it's a re-demo tool:
flipping all signals back to false makes the user see the
checklist again, which is useful for demoing the onboarding
flow without waiting for a fresh install.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hermes_orch.auth.cookie import ROLE_ADMIN, current_user
from hermes_orch.core import onboarding as onboarding_mod

router = APIRouter()


class OnboardingOut(BaseModel):
    """Response shape for /api/me/onboarding.

    `should_show_checklist` is the single boolean the UI cares about.
    `state` is the full parsed state (signals + skipped + completed_at)
    so the UI can render per-step completion badges without re-computing.
    """

    should_show_checklist: bool
    is_complete: bool
    state: dict


class SkipOut(BaseModel):
    skipped: bool


class ResetOut(BaseModel):
    """Response after an admin reset."""

    ok: bool
    state: dict


@router.get("/me/onboarding", response_model=OnboardingOut)
async def get_my_onboarding(request: Request) -> OnboardingOut:
    """Return the current user's onboarding state + display hint."""
    user = await current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    state = await onboarding_mod.get_user_state(request.app.state.db, user["id"])
    return OnboardingOut(
        should_show_checklist=onboarding_mod.should_show_checklist(state),
        is_complete=onboarding_mod.is_checklist_complete(state),
        state=state,
    )


@router.post("/me/onboarding/skip", response_model=SkipOut)
async def post_my_onboarding_skip(request: Request) -> SkipOut:
    """Mark the current user's onboarding as skipped.

    Skipping hides the checklist from `GET /` but does NOT clear
    signals already flipped to true. The user can re-open the
    checklist via /settings#onboarding → Reset (admin-only).
    """
    user = await current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    new_state = await onboarding_mod.set_user_skipped(
        request.app.state.db, user["id"], True
    )
    return SkipOut(skipped=bool(new_state.get("skipped", False)))


@router.post("/me/onboarding/reset", response_model=ResetOut)
async def post_my_onboarding_reset(request: Request) -> ResetOut:
    """Admin-only: reset the current user's onboarding state to all-false.

    Used for re-demoing the checklist flow. The user is then routed
    to /api/me/onboarding (or just reloaded) to see the fresh
    checklist on next /.
    """
    user = await current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "Admin-only endpoint")
    new_state = await onboarding_mod.reset_user_state(
        request.app.state.db, user["id"]
    )
    return ResetOut(ok=True, state=new_state)
