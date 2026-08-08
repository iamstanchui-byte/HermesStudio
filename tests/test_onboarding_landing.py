# coding: utf-8
"""Tests for v1.0.1 onboarding landing-page routing (GET /).

Verifies the spec contract:
  - T1.1: GET / shows the 4-step checklist when onboarding incomplete;
          redirects to /agents when complete
  - The Skip endpoint returns the user to the normal dashboard on next /
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch.auth.cookie import ROLE_ADMIN, ROLE_USER, create_user
from hermes_orch.core.onboarding import (
    set_user_signal,
    serialize_state,
    set_skipped,
    empty_state,
    reset_state,
    SIGNAL_AGENT_CONNECTED,
    SIGNAL_FIRST_TASK_COMPLETED,
    SIGNAL_LLM_CONFIGURED,
    SIGNAL_PASSWORD_SET,
)
from hermes_orch.main import create_app

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


async def _bootstrap_admin(app) -> None:
    from hermes_orch.auth.cookie import set_user_password
    db = app.state.db
    row = await db.fetchone(
        "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    await set_user_password(db, row["id"], ADMIN_PASSWORD)


async def _login(ac: AsyncClient, username: str, password: str) -> None:
    r = await ac.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "orchestrator:\n  port: 18765\n  bind_host: 127.0.0.1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))

    import hermes_orch.db as db_mod
    test_db = tmp_path / "test.db"
    orig_db_init = db_mod.Database.__init__

    def patched_db_init(self, db_path):
        orig_db_init(self, test_db)

    monkeypatch.setattr(db_mod.Database, "__init__", patched_db_init)

    app = create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        await _bootstrap_admin(app)
        await create_user(
            app.state.db, username="alice", password="AlicePass123!",
            role=ROLE_USER,
        )
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


# ===== GET / routing =====

@pytest.mark.asyncio
async def test_get_root_unauthenticated_redirects_to_login(client):
    """Unauth users get redirected to /login, not the onboarding page."""
    ac, _ = client
    ac.cookies.clear()
    r = await ac.get("/", follow_redirects=False)
    # The root handler returns a Redirect to /login (with a `next`
    # query param so the auth flow can return the user here after
    # login). We check the path prefix to be robust to query params.
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


@pytest.mark.asyncio
async def test_get_root_fresh_user_renders_onboarding_html(client):
    """Fresh user (all signals false) sees the 4-step checklist."""
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Reset state to all-false (the auto-backfill ran at startup
    # before our password set, so the state is the backfill result
    # for admin which is the empty state — but be explicit)
    row = await app.state.db.fetchone(
        "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    await app.state.db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(reset_state()), row["id"]),
    )
    r = await ac.get("/", follow_redirects=False)
    assert r.status_code == 200
    # The response body should contain the 4 step labels
    body = r.text
    assert "Welcome to Hermes Orchestrator" in body
    assert "Set your password" in body
    assert "Configure the LLM" in body
    assert "Connect an agent host" in body
    assert "Run your first task" in body
    assert "Skip for now" in body


@pytest.mark.asyncio
async def test_get_root_complete_user_redirects_to_agents(client):
    """All 4 success signals true → redirect to /agents (no checklist)."""
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    row = await app.state.db.fetchone(
        "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    # Flip all 4 success signals to true
    from hermes_orch.core.onboarding import (
        set_signal,
        SIGNAL_PASSWORD_SET, SIGNAL_LLM_CONFIGURED,
        SIGNAL_AGENT_CONNECTED, SIGNAL_FIRST_TASK_COMPLETED,
    )
    state = empty_state()
    for sig in (SIGNAL_PASSWORD_SET, SIGNAL_LLM_CONFIGURED,
                SIGNAL_AGENT_CONNECTED, SIGNAL_FIRST_TASK_COMPLETED):
        state = set_signal(state, sig, True)
    await app.state.db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(state), row["id"]),
    )
    r = await ac.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/agents"


@pytest.mark.asyncio
async def test_get_root_partial_user_still_renders_checklist(client):
    """User with 2 of 4 signals → checklist still shows (3 steps left)."""
    from hermes_orch.core.onboarding import set_signal
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    row = await app.state.db.fetchone(
        "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    state = empty_state()
    state = set_signal(state, SIGNAL_PASSWORD_SET, True)
    state = set_signal(state, SIGNAL_LLM_CONFIGURED, True)
    await app.state.db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(state), row["id"]),
    )
    r = await ac.get("/", follow_redirects=False)
    assert r.status_code == 200
    body = r.text
    # Password + LLM are done
    assert "Done</span>" in body


@pytest.mark.asyncio
async def test_get_root_skipped_user_redirects_to_agents(client):
    """User who hit Skip → no checklist, goes to /agents."""
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    row = await app.state.db.fetchone(
        "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    state = set_skipped(empty_state(), True)
    await app.state.db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(state), row["id"]),
    )
    r = await ac.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/agents"


@pytest.mark.asyncio
async def test_get_root_completed_state_takes_precedence_over_skip(client):
    """Spec: completion dominates skip. Once all 4 are true, skip
    doesn't matter — user goes to /agents."""
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    row = await app.state.db.fetchone(
        "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    from hermes_orch.core.onboarding import set_signal
    state = set_skipped(empty_state(), True)
    for sig in (SIGNAL_PASSWORD_SET, SIGNAL_LLM_CONFIGURED,
                SIGNAL_AGENT_CONNECTED, SIGNAL_FIRST_TASK_COMPLETED):
        state = set_signal(state, sig, True)
    await app.state.db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(state), row["id"]),
    )
    r = await ac.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/agents"


# ===== Skip endpoint integration with / =====

@pytest.mark.asyncio
async def test_skip_then_get_root_redirects_to_agents(client):
    """User clicks Skip on the onboarding page → next GET / redirects."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Skip
    r = await ac.post("/api/me/onboarding/skip")
    assert r.status_code == 200
    # Now GET / should redirect
    r2 = await ac.get("/", follow_redirects=False)
    assert r2.status_code == 302
    assert r2.headers["location"] == "/agents"


# ===== Starter gallery on landing page (v1.0.1 §3.4) =====

@pytest.mark.asyncio
async def test_onboarding_page_contains_starter_gallery_placeholder(client):
    """The onboarding page has the #starter-gallery div that JS
    fills in. The cards are rendered client-side via fetch()."""
    from hermes_orch.core.onboarding import reset_state, serialize_state
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    # Reset to fresh state
    row = await app.state.db.fetchone(
        "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    await app.state.db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(reset_state()), row["id"]),
    )
    r = await ac.get("/", follow_redirects=False)
    assert r.status_code == 200
    body = r.text
    assert 'id="starter-gallery"' in body


@pytest.mark.asyncio
async def test_onboarding_page_loads_starters_via_api(client):
    """The JS on the page calls /api/starters. Verify the endpoint
    is reachable from an authenticated browser session."""
    ac, _ = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = await ac.get("/api/starters")
    assert r.status_code == 200
    items = r.json()
    # T1.8: ≥3 starters
    assert len(items) >= 3
    names = {item["name"] for item in items}
    # The smoke-test starter is the one we wire into the onboarding step 4
    assert "system-health" in names


# ===== v1.0.1 hotfix (2026-08-09): password_set signal =====
#
# Legacy users whose password pre-dates the v1.0.1 onboarding JSON
# column may have `password_set=false` in storage despite having a
# real password_hash. The hotfix overrides the stored signal with
# truth at render time so the checklist never shows a "Set your
# password" step (or a broken /setup-password button) for a user
# who already has a password.

@pytest.mark.asyncio
async def test_onboarding_step1_done_when_user_has_password_but_stale_signal(client):
    """Regression: a user with password_hash IS NOT NULL but
    `signals.password_set=false` (stale) should see step 1 as "Done"
    (no "Set password" button, no /setup-password link)."""
    from hermes_orch.core.onboarding import reset_state, serialize_state
    ac, app = client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)

    # Force a stale state: password IS set (via the fixture's
    # bootstrap), but explicitly reset the signal back to false.
    # This is the post-backfill state for a legacy user.
    row = await app.state.db.fetchone(
        "SELECT id, password_hash FROM users WHERE username = ?",
        (ADMIN_USERNAME,),
    )
    assert row["password_hash"], "fixture should have set the password"

    # Leave llm/agent/first_task signals as False (so the
    # checklist still shows for the OTHER 3 steps).
    stale = reset_state()
    stale["signals"]["password_set"] = False  # stale!
    await app.state.db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(stale), row["id"]),
    )

    r = await ac.get("/", follow_redirects=False)
    assert r.status_code == 200
    body = r.text
    # Step 1 title is always rendered
    assert "Set your password" in body
    # But the button must NOT be rendered (effective signal is True)
    # The button text is "Set password" inside the step-password div.
    # Search within the step div to be robust to other "Done" texts.
    import re
    step1 = re.search(
        r'<div[^>]*id="step-password"[^>]*>.*?</div>\s*</div>', body, re.S
    )
    assert step1, "step-password div not found"
    step1_html = step1.group(0)
    # No "Set password" link (button label) inside the step —
    # substring match (the template puts whitespace around the
    # text node, so a `>Set password<` strict check would fail
    # on the surrounding whitespace).
    assert "Set password" not in step1_html, (
        "Set password button must not appear when user has a password "
        "(stale password_set signal must be overridden at render time)"
    )
    # No /setup-password link inside the step
    assert "/setup-password" not in step1_html, (
        "/setup-password link must not appear for a user with a password"
    )
    # "Done" is rendered for step 1
    assert "Done" in step1_html, (
        "Step 1 should show as 'Done' when user has a password"
    )


@pytest.mark.asyncio
async def test_onboarding_step1_button_when_user_has_no_password(client):
    """Counterpart: a user with password_hash IS NULL but with a
    stored `password_set=true` (e.g. from a buggy prior flip) should
    STILL see step 1 as needing action. Wait, that's not a realistic
    scenario — the more useful counter-test is: a user with no
    password at all sees the "Set password" button."""
    # The fixture's _bootstrap_admin sets the password, so to test
    # the no-password case we create a brand-new user that has no
    # password_hash. We do this by direct DB write (bypassing
    # create_user which requires a password) so the user has a
    # login session but no password_hash (e.g. a half-migrated
    # legacy user, or the bootstrap admin pre-setup).
    ac, app = client
    # We can simulate this by directly clearing the admin's
    # password_hash after login (won't affect the session cookie
    # which is still valid).
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    row = await app.state.db.fetchone(
        "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    await app.state.db.execute(
        "UPDATE users SET password_hash = NULL WHERE id = ?", (row["id"],)
    )
    from hermes_orch.core.onboarding import reset_state, serialize_state
    await app.state.db.execute(
        "UPDATE users SET onboarding_state = ? WHERE id = ?",
        (serialize_state(reset_state()), row["id"]),
    )
    r = await ac.get("/", follow_redirects=False)
    assert r.status_code == 200
    body = r.text
    # The "Set password" button must be rendered
    assert 'href="/setup-password"' in body, (
        "User with no password must see the 'Set password' button "
        "linking to /setup-password"
    )
    # No "Done" for step 1
    import re
    step1 = re.search(
        r'<div[^>]*id="step-password"[^>]*>.*?</div>\s*</div>', body, re.S
    )
    assert step1, "step-password div not found"
    step1_html = step1.group(0)
    # The button label "Set password" must appear (substring — the
    # template puts whitespace + newlines around the text node)
    assert "Set password" in step1_html
    # Defensive: the button must link to /setup-password, not the
    # legacy /settings#onboarding self-link
    assert 'href="/setup-password"' in step1_html
