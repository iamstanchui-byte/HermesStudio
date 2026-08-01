"""Tests for the v3.9.0 Phase 2 UX (2026-08-01).

Covers the three sub-tasks described in
`docs/soul-routing-design.md` §"Phased plan → Phase 2":

  Sub-task A: Hide the "SOUL presets" section on the project page by
              default; show a "🔧 Show SOUL editor" toggle.

  Sub-task B: Add a "🎯 SOUL: cpi-analyst" pill on each plan step in
              the visual editor, colored by whether a preset exists.

  Sub-task C: Dark-mode palette for the new UI elements (the toggle
              button + the SOUL pill).

Tests:
  1. test_project_page_hides_soul_presets_by_default
       GET /projects/{id} with a fresh session should NOT include the
       SOUL editor form. The toggle button is the only SOUL-related
       thing on the page.

  2. test_project_page_shows_soul_presets_after_toggle
       POST /api/users/me/ui-prefs with show_soul_editor=true, then
       GET /projects/{id} should include the editor (the "SOUL
       presets" h2 + the new-preset form). POST again with
       show_soul_editor=false and the editor should disappear.

  3. test_plan_visualization_includes_soul_pills
       Load the visual plan editor with two steps (one with a role
       that has a preset, one without) and verify:
         (a) Each step card has a .vp-node-soul element.
         (b) The card whose role matches a preset has .bound.
         (c) The card whose role has no preset has .unbound.

  4. test_dark_mode_pill_colors
       Load the plan editor in dark mode and verify the .bound pill
       uses the dark-mode palette (green-300 on green-800-ish
       background) — i.e. body.dark rules override the light-mode
       defaults. We assert the computed background color is in the
       dark-mode range rather than the light-mode range.

Strategy:
  - Tests 1 + 2 are server-side rendering checks: they only need
    the in-process AsyncClient (matches test_users_api.py's
    pattern). No browser needed.
  - Tests 3 + 4 are JS-rendering checks: they need a real browser
    to evaluate the .vp-node-soul class the JS adds. We use
    Playwright with a fresh in-process app via ASGITransport.
    Playwright talks to the in-process app over a localhost
    socket opened by the test (we wrap the app in uvicorn for
    the duration of the test) — see _browser_client() below.

The session cookie for ui-prefs is a separate `orch_ui_prefs` cookie
sibling to the `hermes_orch_session` login cookie. The test client
needs both to render the project page (login) AND to opt into the
SOUL editor (ui-prefs).
"""
from __future__ import annotations

import socket
import threading
import time
import uuid
from contextlib import contextmanager

import pytest
import pytest_asyncio
import uvicorn
from httpx import ASGITransport, AsyncClient
from playwright.async_api import async_playwright

from hermes_orch import db as db_mod
from hermes_orch import main as main_mod
from hermes_orch.auth.cookie import (
    ROLE_ADMIN, create_user, hash_password,
)
from hermes_orch.main import create_app


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


# ===== In-process app fixture (mirrors tests/test_users_api.py) =====


async def _bootstrap_admin(app) -> str:
    """Create the bootstrap admin with a known password. Idempotent.

    Mimics `hermes-orch init` + first-login /setup-password. We
    set the password directly here so we don't need the web flow.
    """
    db = app.state.db
    existing = await db.fetchone(
        "SELECT id, password_hash FROM users WHERE username = ?", (ADMIN_USERNAME,)
    )
    if existing:
        if not existing.get("password_hash"):
            await db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(ADMIN_PASSWORD), existing["id"]),
            )
        return existing["id"]
    return await create_user(
        db,
        username=ADMIN_USERNAME,
        password=ADMIN_PASSWORD,
        role=ROLE_ADMIN,
        is_bootstrap_admin=True,
    )


async def _login(ac: AsyncClient, username: str, password: str) -> None:
    r = await ac.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"


@contextmanager
def _patch_db_path(test_db_path):
    """Patch the Database class to use a per-test DB path under
    tmp_path. Returns the patched create_app so the caller can
    drive the app's lifespan manually."""
    orig_init = main_mod.create_app
    orig_db_init = db_mod.Database.__init__

    def patched_db_init(self, db_path):
        orig_db_init(self, test_db_path)

    db_mod.Database.__init__ = patched_db_init
    try:
        yield orig_init()
    finally:
        db_mod.Database.__init__ = orig_db_init


@pytest_asyncio.fixture
async def app_client(tmp_path):
    """Fresh app + AsyncClient with a bootstrap admin already in place.

    Yields an AsyncClient wired to the in-process app. Each test gets
    a unique tmp DB so we never touch ~/.hermes-orchestrator.
    """
    test_db = tmp_path / "test.db"
    with _patch_db_path(test_db):
        app = create_app()
        async with app.router.lifespan_context(app):
            await _bootstrap_admin(app)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac, app


@pytest_asyncio.fixture
async def logged_in_client(app_client):
    """Yields (client, app) with the admin already logged in."""
    ac, app = app_client
    await _login(ac, ADMIN_USERNAME, ADMIN_PASSWORD)
    return ac, app


# ===== Helpers =====


async def _create_test_project(ac: AsyncClient, name: str = "phase2-ux-test") -> str:
    """Create a fresh project via the JSON API. Returns the project id."""
    r = await ac.post("/api/projects/", json={"name": name, "action": "do_step"})
    assert r.status_code in (200, 201), f"create project failed: {r.status_code} {r.text}"
    body = r.json()
    assert "id" in body, f"create project response missing id: {body}"
    return body["id"]


async def _put_plan(ac: AsyncClient, pid: str, plan: dict) -> None:
    r = await ac.put(f"/api/projects/{pid}/plan", json={"plan": plan})
    assert r.status_code == 200, f"put plan failed: {r.status_code} {r.text}"


async def _create_profile(app, name: str) -> str:
    """Create a fake agent profile and return its id. The visual plan
    editor's presets endpoint joins project_soul_presets to
    agent_profiles, so we need real rows on both sides.

    The agent_profiles schema (verified via PRAGMA table_info):
        id, agent_id, name, description, status, current_task_id,
        created_at, updated_at, capabilities, llm_model_default,
        llm_model_base_url, llm_model_provider, mcp_servers,
        storage_refs, skills
    Note: no `model` or `provider` columns (those names are surfaced
    via the LLM model columns). We only insert the columns we need.
    """
    import json as _json
    db = app.state.db
    aid = f"agt-{uuid.uuid4().hex[:8]}"
    pid = f"prof-{uuid.uuid4().hex[:8]}"
    now = int(time.time())
    await db.execute(
        "INSERT INTO agents (id, ip, os_type, status, secret_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (aid, "127.0.0.1", "linux", "online", "x" * 32, now),
    )
    await db.execute(
        "INSERT INTO agent_profiles "
        "(id, agent_id, name, description, status, skills, capabilities, "
        "mcp_servers, storage_refs, llm_model_default, llm_model_base_url, "
        "llm_model_provider, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, aid, name, "test profile for phase2 UX", "online",
         _json.dumps([]), _json.dumps({}), _json.dumps([]), _json.dumps([]),
         None, None, None, now, now),
    )
    return pid


async def _create_soul_preset(app, project_id: str, profile_id: str, role_name: str, content: str) -> str:
    """Insert a project_soul_presets row directly (the SOUL editor
    UI is hidden by default; the API for create-preset is in
    api/projects.py which we don't touch here). Returns the preset id."""
    db = app.state.db
    preset_id = f"sp-{uuid.uuid4().hex[:8]}"
    await db.execute(
        "INSERT INTO project_soul_presets (id, project_id, profile_id, role_name, "
        "content, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (preset_id, project_id, profile_id, role_name, content,
         int(time.time()), int(time.time())),
    )
    return preset_id


# ===== Sub-task A: SOUL editor hidden by default =====


@pytest.mark.asyncio
async def test_project_page_hides_soul_presets_by_default(logged_in_client):
    """A fresh session should NOT include the SOUL presets section.

    The page should only have the toggle button ("🔧 Show SOUL
    editor"). The h2 with text "SOUL presets" and the new-preset
    form (id="preset-form") must both be absent.

    We look for the actual rendered h2 element + form by id, not
    the bare string "SOUL presets" — the HTML comment block above
    the conditional also contains that string and would always
    match.
    """
    import re
    ac, _app = logged_in_client
    pid = await _create_test_project(ac, name="phase2-default-hidden")

    r = await ac.get(f"/projects/{pid}")
    assert r.status_code == 200, f"page render failed: {r.status_code}"

    html = r.text
    # The toggle button is the only SOUL-related thing visible.
    assert "Show SOUL editor" in html, "missing the 'Show SOUL editor' toggle"
    # The h2 element (specifically the one whose body is "SOUL presets")
    # should NOT be present. We match an <h2 ...> ... SOUL presets ...
    # </h2> block to avoid catching the comment that explains the
    # conditional (which is always in the HTML).
    h2_match = re.search(
        r'<h2[^>]*>\s*SOUL presets',
        html,
    )
    assert h2_match is None, (
        "SOUL presets h2 should be hidden by default; "
        f"matched at offset {h2_match.start() if h2_match else 'n/a'}"
    )
    # The new-preset form (id="preset-form") should NOT be present.
    assert 'id="preset-form"' not in html, (
        "preset-form should be hidden by default; found in HTML"
    )
    # Sanity: the page still rendered the project name + tasks area.
    assert "phase2-default-hidden" in html


@pytest.mark.asyncio
async def test_project_page_shows_soul_presets_after_toggle(logged_in_client):
    """POST /api/users/me/ui-prefs with show_soul_editor=true should
    cause the next GET /projects/{id} to include the editor. POST
    again with show_soul_editor=false should hide it.

    The toggle is per-user (cookie), not per-project, so flipping
    the flag on one project and rendering another should also
    show the editor.

    Same HTML-assertion note as the default-hidden test: we look
    for the rendered h2 element, not the bare string (the comment
    block above the conditional always contains the literal).
    """
    import re
    h2_re = re.compile(r'<h2[^>]*>\s*SOUL presets')
    ac, _app = logged_in_client
    pid = await _create_test_project(ac, name="phase2-toggle-on")

    # Step 1: flip the flag ON.
    r = await ac.post(
        "/api/users/me/ui-prefs",
        json={"show_soul_editor": True},
    )
    assert r.status_code == 200, f"toggle POST failed: {r.status_code} {r.text}"
    body = r.json()
    assert body == {"show_soul_editor": True}

    # Step 2: GET /projects/{id} → editor visible.
    r = await ac.get(f"/projects/{pid}")
    assert r.status_code == 200
    html = r.text
    assert h2_re.search(html), "SOUL presets h2 should be visible after toggle ON"
    assert 'id="preset-form"' in html, "preset-form should be visible after toggle ON"
    # The toggle button should now read "Hide SOUL editor" (inverted).
    # We match the <button ...> ... </button> element to avoid the
    # explanatory comment that also contains "Show SOUL editor".
    button_re = re.compile(
        r'<button[^>]*type="submit"[^>]*>\s*🔧 (Hide|Show) SOUL editor\s*</button>'
    )
    buttons = button_re.findall(html)
    assert "Hide" in buttons, f"expected 'Hide SOUL editor' button when on; got: {buttons}"
    assert "Show" not in buttons, f"expected no 'Show SOUL editor' button when on; got: {buttons}"

    # Step 3: flip the flag OFF.
    r = await ac.post(
        "/api/users/me/ui-prefs",
        json={"show_soul_editor": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {"show_soul_editor": False}

    # Step 4: GET → editor hidden again.
    r = await ac.get(f"/projects/{pid}")
    assert r.status_code == 200
    html = r.text
    assert not h2_re.search(html), "SOUL presets h2 should be hidden after toggle OFF"
    assert 'id="preset-form"' not in html, "preset-form should be hidden after toggle OFF"
    buttons = button_re.findall(html)
    assert "Show" in buttons, f"expected 'Show SOUL editor' button when off; got: {buttons}"
    assert "Hide" not in buttons, f"expected no 'Hide SOUL editor' button when off; got: {buttons}"

    # Step 5: verify the GET /api/users/me/ui-prefs reflects the current
    # state (read-after-write). The latest POST set it to False, so
    # GET should return show_soul_editor: False.
    r = await ac.get("/api/users/me/ui-prefs")
    assert r.status_code == 200
    assert r.json() == {"show_soul_editor": False}

    # Step 6: per-user sticky across project switches. Flip ON,
    # create a second project, render it — editor should be visible.
    r = await ac.post(
        "/api/users/me/ui-prefs",
        json={"show_soul_editor": True},
    )
    assert r.status_code == 200
    pid2 = await _create_test_project(ac, name="phase2-toggle-sticky")
    r = await ac.get(f"/projects/{pid2}")
    assert r.status_code == 200
    assert h2_re.search(r.text), "editor should be visible on a different project (per-user cookie)"


@pytest.mark.asyncio
async def test_from_template_modal_visible_when_no_presets(logged_in_client):
    """Regression test for the "From template" first-time use case.

    Bug: the from-template modal was originally nested inside the
    `{% if soul_presets %}` block in project.html, so a project with
    zero presets rendered the "📚 From template" button but NOT the
    modal markup. Clicking the button called `openFromTemplateModal()`
    which bailed at the `if (!overlay) return;` guard. Net effect:
    the button was a no-op for projects with no existing presets —
    exactly the case where the operator most needs it (creating
    the first preset from a template).

    Fix: move the modal out of `{% if soul_presets %}` so it
    renders whenever the SOUL editor is toggled on, regardless of
    how many presets the project has.

    Test: with show_soul_editor=true AND zero presets, the page
    must contain:
      - The "📚 From template" button (sanity)
      - The `id="from-template-modal-overlay"` element (the fix)
    """
    import re
    ac, _app = logged_in_client

    # Toggle ON.
    r = await ac.post(
        "/api/users/me/ui-prefs",
        json={"show_soul_editor": True},
    )
    assert r.status_code == 200

    # Create a project; do NOT add any presets. The soul_presets
    # list is empty by default for a new project.
    pid = await _create_test_project(ac, name="from-template-empty")
    # Defensive: confirm the project really has zero presets.
    # (The test_create_test_project helper shouldn't add any, but
    # the explicit check makes the regression intent obvious.)
    db = _app.state.db
    n_presets = await db.fetchone(
        "SELECT COUNT(*) AS n FROM project_soul_presets WHERE project_id = ?",
        (pid,),
    )
    assert n_presets["n"] == 0, "precondition: project has zero presets"

    # GET /projects/{id}.
    r = await ac.get(f"/projects/{pid}")
    assert r.status_code == 200
    html = r.text

    # Sanity: the toggle is on, the button is present, the editor
    # h2 is rendered.
    button_re = re.compile(r'<button[^>]*>\s*📚 From template\s*</button>')
    h2_re = re.compile(r'<h2[^>]*>\s*SOUL presets')
    assert button_re.search(html), "From template button must render"
    assert h2_re.search(html), "SOUL presets h2 must render (toggle is on)"

    # The fix: the modal overlay must be in the DOM even though
    # there are zero presets. Before the fix, this assertion failed
    # because the modal was inside the {% if soul_presets %} block.
    assert 'id="from-template-modal-overlay"' in html, (
        "from-template modal overlay must render even when project "
        "has zero presets — this is the first-time use case for "
        "the button. The modal must be OUTSIDE the "
        "{% if soul_presets %} block in project.html."
    )

    # Bonus: the modal must contain the expected child elements
    # (target profile select, template list container, preview area,
    # create button). These are referenced by ID in
    # openFromTemplateModal() / submitFromTemplate() and would
    # NPE if missing.
    for required_id in (
        "from-template-profile",
        "from-template-list",
        "from-template-preview",
        "from-template-preview-content",
        "from-template-status",
    ):
        assert f'id="{required_id}"' in html, (
            f"required modal child #{required_id} is missing"
        )


@pytest.mark.asyncio
async def test_ui_prefs_requires_auth(app_client):
    """The POST endpoint must reject unauthenticated callers.

    The middleware already gates everything, but the test confirms
    the contract: without a login cookie, POSTing to
    /api/users/me/ui-prefs returns 401.
    """
    ac, _app = app_client
    # No _login() call — fresh client.
    r = await ac.post(
        "/api/users/me/ui-prefs",
        json={"show_soul_editor": True},
    )
    assert r.status_code == 401, f"expected 401, got {r.status_code}"


# ===== Sub-task B: SOUL pill on plan editor steps =====


@contextmanager
def _live_server(app, port: int = 0):
    """Run the app in a background uvicorn thread for browser tests.

    We can't use ASGITransport with Playwright (Playwright needs a
    real socket). Start uvicorn in a daemon thread, yield the base
    URL, and shut it down on exit.

    `port=0` lets the OS pick a free port — important because tests
    can run in parallel and we don't want collisions.
    """
    # Bind a socket to get a free port, then close it so uvicorn
    # can re-bind. Race-prone but acceptable for a test fixture.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    chosen_port = s.getsockname()[1]
    s.close()

    config = uvicorn.Config(
        app, host="127.0.0.1", port=chosen_port,
        log_level="error", lifespan="off",  # lifespan is handled by the caller
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for the socket to be listening.
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("uvicorn did not start within 5s")

    base = f"http://127.0.0.1:{chosen_port}"
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest_asyncio.fixture
async def live_server_client(tmp_path, request):
    """A live HTTP server (Playwright-friendly) with a logged-in admin.

    Yields (base_url, app, project_id, plan_data). The plan has
    three steps: one with a preset (cpi-analyst), one without
    (reviewer), and one with no role at all (for the no-pill
    branch).
    """
    test_db = tmp_path / "test.db"
    with _patch_db_path(test_db):
        app = create_app()
        async with app.router.lifespan_context(app):
            await _bootstrap_admin(app)
            # Set up the test fixture: 1 project + 1 profile +
            # 1 preset for cpi-analyst.
            db = app.state.db
            pid = f"proj-{uuid.uuid4().hex[:8]}"
            await db.execute(
                "INSERT INTO projects (id, name, state, created_at) "
                "VALUES (?, ?, ?, ?)",
                (pid, "phase2-pill-test", "planned", int(time.time())),
            )
            profile_id = await _create_profile(app, "cpi-analyst")
            await _create_soul_preset(
                app, pid, profile_id, "cpi-analyst",
                "You are a CPI/PPI correlation analyst. Output JSON.",
            )
            plan = {
                "version": "1.0",
                "name": "phase2-pill-test",
                "steps": [
                    {"name": "fetch", "agent_role": "cpi-analyst",
                     "action": "fetch_cpi_data"},
                    {"name": "review", "agent_role": "reviewer",
                     "action": "review"},
                    {"name": "summarize", "agent_role": "",
                     "action": "summarize"},
                ],
            }
            await _put_plan_async(db, pid, plan)

            with _live_server(app) as base:
                # Log in via the HTTP API (real network call to the
                # in-process uvicorn).
                async with AsyncClient(base_url=base, timeout=10) as ac:
                    r = await ac.post(
                        "/api/auth/login",
                        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
                    )
                    assert r.status_code == 200, f"login failed: {r.status_code}"
                    # Pull the cookies from the AsyncClient jar
                    # (hermes_orch_session) and hand them to
                    # Playwright.
                    cookies = []
                    for c in ac.cookies.jar:
                        cookies.append({
                            "name": c.name,
                            "value": c.value,
                            "domain": "127.0.0.1",
                            "path": "/",
                        })
                yield base, app, pid, plan, cookies


async def _put_plan_async(db, pid: str, plan: dict) -> None:
    import json as _json
    await db.execute(
        "UPDATE projects SET plan_json = ? WHERE id = ?",
        (_json.dumps(plan), pid),
    )


@pytest.mark.asyncio
async def test_plan_visualization_includes_soul_pills(live_server_client):
    """The visual plan editor should render a .vp-node-soul pill on
    each step card. The pill should be marked .bound for roles
    that have a preset and .unbound for roles that don't.

    We use Playwright (not just the in-process client) because the
    pill is rendered by visual_plan.js AFTER the page loads —
    server-side HTML alone won't show the pill.
    """
    base, _app, pid, _plan, cookies = live_server_client

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()

        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on(
            "response",
            lambda r: errors.append(f"http {r.status}: {r.url}")
            if r.status >= 400 and "/api/" in r.url else None,
        )

        await page.goto(f"{base}/api/projects/{pid}/plan/visual",
                        wait_until="domcontentloaded", timeout=30000)
        # Wait for the JS to bootstrap drawflow + render the steps.
        # We poll for .vp-node-soul (a pill on each card); if it
        # doesn't appear in 5s, the JS didn't run.
        try:
            await page.wait_for_selector(".vp-node-soul", timeout=5000)
        except Exception:
            await browser.close()
            pytest.fail(
                "no .vp-node-soul pills rendered within 5s; "
                f"page errors: {errors}"
            )

        # Wait for the preset fetch to complete (the background
        # _loadPresets() re-render switches fetch → bound).
        # We poll the fetch resolution: data-soul-bound="1" should
        # appear for the cpi-analyst step.
        try:
            await page.wait_for_selector(
                '.vp-node[data-step-name="fetch"] .vp-node-soul[data-soul-bound="1"]',
                timeout=5000,
            )
        except Exception:
            await browser.close()
            pytest.fail(
                "fetch step didn't get .bound SOUL pill within 5s; "
                f"page errors: {errors}"
            )

        # (a) Each step with a non-empty agent_role has a .vp-node-soul
        # pill. The test fixture has 3 steps: fetch (cpi-analyst, has
        # preset), review (reviewer, no preset), summarize (empty
        # agent_role). We expect 2 pills — the empty-role step
        # intentionally gets no pill (the JS only renders one when
        # there's a role to bind to).
        pills = await page.query_selector_all(".vp-node .vp-node-soul")
        assert len(pills) == 2, f"expected 2 pills (fetch + review), got {len(pills)}"

        # (b) The card whose role has a preset (fetch/cpi-analyst) is .bound.
        bound = await page.query_selector(
            '.vp-node[data-step-name="fetch"] .vp-node-soul.bound'
        )
        assert bound is not None, "fetch step (cpi-analyst) should have .bound pill"

        # (c) The card whose role has NO preset (review/reviewer) is .unbound.
        unbound = await page.query_selector(
            '.vp-node[data-step-name="review"] .vp-node-soul.unbound'
        )
        assert unbound is not None, "review step (reviewer) should have .unbound pill"

        # Sanity: the empty-role step (summarize) should NOT have a
        # pill (the JS only renders the pill for non-empty roles).
        summarize_pill = await page.query_selector(
            '.vp-node[data-step-name="summarize"] .vp-node-soul'
        )
        assert summarize_pill is None, (
            "summarize step has empty agent_role; should NOT have a pill"
        )

        await browser.close()


@pytest.mark.asyncio
async def test_dark_mode_pill_colors(live_server_client):
    """In dark mode, the .bound pill uses the dark palette
    (green-300 text on green-800-ish background), NOT the light
    palette (dark green text on light green).

    We force dark mode via the orch.theme localStorage key (the
    same key the theme toggle uses in base.html).
    """
    base, _app, pid, _plan, cookies = live_server_client

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        # Seed localStorage with dark theme BEFORE the page loads.
        # base.html reads orch.theme in its init script and sets
        # body.dark accordingly.
        await ctx.add_init_script("localStorage.setItem('orch.theme', 'dark');")
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()

        await page.goto(f"{base}/api/projects/{pid}/plan/visual",
                        wait_until="domcontentloaded", timeout=30000)
        # Wait for the bound pill on the fetch step.
        try:
            await page.wait_for_selector(
                '.vp-node[data-step-name="fetch"] .vp-node-soul.bound',
                timeout=5000,
            )
        except Exception:
            await browser.close()
            pytest.fail("fetch step didn't get .bound pill within 5s")

        # Verify body.dark is set (sanity — base.html's init ran).
        is_dark = await page.evaluate("document.body.classList.contains('dark')")
        assert is_dark, "body.dark should be set after init_script seeded orch.theme=dark"

        # Read the computed background-color of the .bound pill.
        # The light palette is #d1fae5 (rgb 209,250,229) and the
        # dark palette is #064e3b (rgb 6,78,59). We assert the
        # computed color is closer to the dark value.
        bg = await page.evaluate(
            "() => {"
            "  const el = document.querySelector("
            "    '.vp-node[data-step-name=\"fetch\"] .vp-node-soul.bound'"
            "  );"
            "  if (!el) return null;"
            "  return window.getComputedStyle(el).backgroundColor;"
            "}"
        )
        assert bg is not None, "could not read computed background of .bound pill"
        # Parse "rgb(r, g, b)" or "rgba(r, g, b, a)".
        import re
        m = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", bg)
        assert m, f"unexpected background-color format: {bg!r}"
        r, g, _b = (int(m.group(1)), int(m.group(2)), int(m.group(3)))

        # Dark palette: #064e3b = rgb(6, 78, 59)
        # Light palette: #d1fae5 = rgb(209, 250, 229)
        # Assert the red channel is small (≤ 30), which can only
        # happen with the dark palette. (The light palette has
        # red=209.) This is a one-line proxy for "is it the dark
        # palette" that doesn't need exact color matching (which
        # can drift if the palette tweaks to slate-900-edge
        # variants).
        assert r <= 30, (
            f".bound pill background red channel is {r}; expected ≤ 30 "
            f"for dark palette (#064e3b). Got: {bg}"
        )
        # Green channel should be in the dark range (50-100) — the
        # light palette's green is 250.
        assert 40 <= g <= 110, (
            f".bound pill background green channel is {g}; expected "
            f"50-110 for dark palette. Got: {bg}"
        )

        # Also check the .unbound pill. Dark palette is #334155
        # (rgb 51, 65, 85), light is #f3f4f6 (rgb 243, 244, 246).
        # Same check: red channel should be small.
        bg_unbound = await page.evaluate(
            "() => {"
            "  const el = document.querySelector("
            "    '.vp-node[data-step-name=\"review\"] .vp-node-soul.unbound'"
            "  );"
            "  if (!el) return null;"
            "  return window.getComputedStyle(el).backgroundColor;"
            "}"
        )
        assert bg_unbound is not None, "could not read computed background of .unbound pill"
        m2 = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", bg_unbound)
        assert m2, f"unexpected background-color format: {bg_unbound!r}"
        r2, g2, b2 = (int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
        # Dark palette: rgb(51, 65, 85) — all three channels ≤ 100.
        # Light palette: rgb(243, 244, 246) — all three channels ≥ 240.
        assert r2 <= 100 and g2 <= 100 and b2 <= 100, (
            f".unbound pill background {bg_unbound}; expected all "
            f"channels ≤ 100 for dark palette (#334155). Got: rgb({r2},{g2},{b2})"
        )

        await browser.close()
