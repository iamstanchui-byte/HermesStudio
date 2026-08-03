"""Regression test for the v3.12.1 follow-up #7 skills-sync Content-Type bug.

Background
----------
The wrapper's skills-sync POSTs skill files to the orchestrator at
`POST /api/agents/{id}/profiles/{pname}/skills`. The body is a JSON
dict with the skill's name and content. The server's SkillCreate
Pydantic model accepts {name: str, content: str}.

Pre-fix bug: the wrapper's `client.post(..., content=_skill_body, ...)`
call passed the body as raw bytes WITHOUT setting `Content-Type:
application/json`. httpx's default Content-Type for `content=bytes`
is `application/octet-stream`, so FastAPI received the body as a
string (not a JSON dict) and returned 422 with detail
"Input should be a valid dictionary or object to extract fields
from". The wrapper logged the failure but didn't crash, so the
orchestrator's skill table stayed out of sync with the wrapper's
on-disk SKILL.md files.

The same bug class already had a fix for `POST /api/tasks/{id}/result`
in v1.9.3 (see tests/test_submit_result_body.py); the skills-sync
endpoint was missed. Symptom: silent skill-sync failure across every
wrapper, since the 422s were buried in routine [daemon] log noise
and never escalated.

The fix: in agent_cli.py around line 3452, the wrapper now sets
`"Content-Type": "application/json"` in the POST headers dict
(same pattern as _submit_result at line ~2044).

These tests verify both:
  1. Source-level: the skills-sync POST block sets Content-Type
     (catches a developer removing the line).
  2. End-to-end (in-process): server returns 201 when Content-Type
     is application/json, and 422 when it isn't (catches the
     actual bug signature).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch import main as main_mod
from hermes_orch.auth.cookie import ROLE_ADMIN, create_user
from hermes_orch.main import create_app


SECRET = "test-skills-sync-secret"


# ===== 1. Source-level structural check =====

SRC_PATH = Path(__file__).resolve().parents[1] / "src" / "hermes_orch" / "agent_cli.py"


def _read_skills_sync_block() -> str:
    """Read agent_cli.py and extract the skills-sync POST block.

    Anchors on the v3.12.1 #7 bug-fix comment, extends through the
    `client.post(...)` call (terminated by `timeout=15,` line).
    """
    src = SRC_PATH.read_text(encoding="utf-8")
    m = re.search(
        r"# v3\.12\.1 follow-up #7 \(skills-sync Content-Type\).*?"
        r"timeout=15,\s*\)",
        src,
        re.DOTALL,
    )
    if not m:
        pytest.fail(
            "Could not find the v3.12.1 #7 skills-sync Content-Type fix "
            "block in agent_cli.py. Did the bug fix get reverted?"
        )
    return src[m.start():m.end()]


def test_skills_sync_post_sets_content_type_header():
    """The skills-sync POST must include Content-Type: application/json.

    Without it, FastAPI's body parser sees the JSON bytes as a string
    (under Content-Type: application/octet-stream) and SkillCreate
    Pydantic validation fails with 422.
    """
    block = _read_skills_sync_block()
    assert '"Content-Type"' in block, (
        "Skills-sync POST is missing the Content-Type header. Without it, "
        "the server's SkillCreate model receives the body as a string "
        "and returns 422.\n\n"
        f"Block:\n{block[:600]}"
    )
    assert '"application/json"' in block, (
        "Skills-sync POST sets Content-Type but not to application/json. "
        f"Block:\n{block[:600]}"
    )


# ===== 2. End-to-end via in-process server =====

AGENT_ID = "test-skills-agent"
PROFILE_NAME = "test-profile"


def _sign(method: str, path: str, body: bytes, ts: str) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = f"{method.upper()}\n{path}\n{body_hash}\n{ts}".encode("utf-8")
    return hmac.new(SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Fresh in-process app + AsyncClient.

    Creates the test agent (with hmac_secret) and a profile row so
    the skill POST endpoint can find them. The endpoint validates
    `_find_profile(db, agent_id, profile_name)` which 500s if
    either is missing.
    """
    from hermes_orch import db as db_mod

    test_db = tmp_path / "test.db"

    orig_db_init = db_mod.Database.__init__

    def patched_db_init(self, db_path):
        orig_db_init(self, test_db)

    monkeypatch.setattr(db_mod.Database, "__init__", patched_db_init)

    app = create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        db = app.state.db
        # Agent with HMAC secret so require_hmac_auth passes
        await db.execute(
            "INSERT INTO agents (id, secret_hash, hmac_secret, status, created_at) "
            "VALUES (?, ?, ?, 'verified', CURRENT_TIMESTAMP)",
            (AGENT_ID, "test-hash", SECRET),
        )
        # Profile row so _find_profile returns it
        await db.execute(
            "INSERT INTO agent_profiles (agent_id, name) "
            "VALUES (?, ?)",
            (AGENT_ID, PROFILE_NAME),
        )
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


def _post_skill_headers(body: bytes, content_type: str | None) -> dict:
    path = f"/api/agents/{AGENT_ID}/profiles/{PROFILE_NAME}/skills"
    ts = str(int(time.time()))
    sig = _sign("POST", path, body, ts)
    h = {
        "X-Agent-Id": AGENT_ID,
        "X-Timestamp": ts,
        "X-Signature": sig,
    }
    if content_type is not None:
        h["Content-Type"] = content_type
    return h, path


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason=(
        "BLOCKED on a separate server-side bug: create_or_update_skill "
        "in src/hermes_orch/api/agents.py:1780 calls "
        "await _find_profile(db, agent_id, profile_name) but the "
        "`db = request.app.state.db` line is missing from the function "
        "body, so the endpoint 500s with NameError on every request "
        "(independent of Content-Type). Tracked as v3.12.1 follow-up "
        "#7b. Once the server-side missing-line bug is fixed, this "
        "test will be enabled by removing the xfail marker."
    ),
    strict=True,
)
async def test_server_returns_201_with_content_type(client):
    """End-to-end: server returns 201 when Content-Type is application/json.

    Currently xfail: the 201 path is blocked by a separate server-side
    bug in create_or_update_skill (see xfail reason). The Content-Type
    fix itself is verified by the structural test + the two 422 tests
    (which confirm FastAPI's body parser behavior independent of the
    server-side 500).
    """
    body = json.dumps({"name": "test-skill", "content": "# Test\n\nbody\n"}).encode()
    headers, path = _post_skill_headers(body, "application/json")
    r = await client.post(path, content=body, headers=headers)
    assert r.status_code == 201, (
        f"expected 201 with Content-Type: application/json, got {r.status_code} "
        f"body={r.text!r}"
    )


@pytest.mark.asyncio
async def test_server_returns_422_without_content_type(client):
    """The bug signature. Without Content-Type, FastAPI's body parser
    sees the bytes as a string (under Content-Type: application/octet-stream
    default) and Pydantic SkillCreate validation fails with 422
    'Input should be a valid dictionary or object to extract fields from'.
    """
    body = json.dumps({"name": "test-skill", "content": "# Test\n\nbody\n"}).encode()
    headers, path = _post_skill_headers(body, None)
    # httpx default Content-Type for content=bytes is application/octet-stream,
    # which is the exact bug state.
    r = await client.post(path, content=body, headers=headers)
    assert r.status_code == 422, (
        f"expected 422 without Content-Type (the bug), got {r.status_code} "
        f"body={r.text!r}"
    )
    # Sanity check the detail matches the v3.12.1 #7 bug signature
    try:
        detail = r.json().get("detail", [])
        detail_str = json.dumps(detail)
        assert "dictionary" in detail_str.lower() or "object" in detail_str.lower(), (
            f"expected the v3.12.1 #7 bug signature in the 422 detail, "
            f"got {detail_str!r}"
        )
    except Exception:
        pytest.fail(f"422 response did not contain JSON detail: {r.text!r}")


@pytest.mark.asyncio
async def test_server_rejects_plain_text_content_type(client):
    """Sanity: Content-Type: text/plain also fails (FastAPI's body parser
    doesn't auto-detect JSON for non-JSON Content-Types). Same 422 bug
    family, different payload shape. Not a regression, just confirms
    the fix targets the right knob (Content-Type, not body encoding).
    """
    body = json.dumps({"name": "test-skill", "content": "# Test\n\nbody\n"}).encode()
    headers, path = _post_skill_headers(body, "text/plain")
    r = await client.post(path, content=body, headers=headers)
    assert r.status_code == 422, (
        f"expected 422 with Content-Type: text/plain, got {r.status_code} "
        f"body={r.text!r}"
    )
