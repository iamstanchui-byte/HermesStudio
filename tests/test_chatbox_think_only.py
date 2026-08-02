# coding: utf-8
"""Regression test: chat endpoint returns actionable 502 when LLM emits only <think> tags.

Background (v3.10.4, 2026-08-02):
  User reported that the chatbox in the plan editor returned
  "(empty response)" for the prompt "現在用多agent 做research, 可以
  把搜的資料放在project_temp_folder的新folder上". The assistant
  message was persisted with content_len=0 and no suggestions.

Root cause: MiniMax M3 sometimes returns ONLY a <think>...</think>
block (the model's internal reasoning) without producing a final
answer. The total 1500-token budget was consumed by the <think>
trace, leaving 0 tokens for the actual reply. The previous code:

  1. Read `data["choices"][0]["message"]["content"]` — got 2269
     chars of <think> content
  2. Checked `if not text.strip(): raise 502` — passed (text
     was non-empty, just all <think>)
  3. Stripped `<think>...</think>` regex — ate the entire content,
     leaving ""
  4. `_extract_suggestions("")` → `("", None)` → display_text=""
  5. Persisted content="" with has_suggestions=false
  6. Returned 200 with `{"message": "", "suggestions": []}`
  7. UI showed "(empty response)" — no actionable hint

Fix (v3.10.4):
  - AFTER stripping <think>, check if any text remains. If not,
    raise 502 with a context-aware hint:
      * If <think> block was present (think-only case) → "model's
        reasoning ate the token budget, try rephrase / shorter
        context / lower reasoning_effort"
      * Otherwise (truly empty content) → "LLM returned empty
        reply, rephrase or check provider status"
  - UI renders the actionable error as a red card in the chat
    history (instead of a tiny scrolled-out status bar), with the
    "isError: true" flag in the render function.

This test asserts:
  1. The 502 message is actionable (mentions rephrase / context
     / reasoning_effort) for the think-only case
  2. The 502 message is actionable for the truly-empty case
  3. No persisted assistant row is created on failure
     (the user message IS persisted, that's correct — they
     sent it; the assistant row is only for successful replies)
  4. The endpoint does NOT persist a row with content="" and
     suggestions=[] (the old buggy behavior)
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hermes_orch import db as db_mod
from hermes_orch import main as main_mod
from hermes_orch.auth.cookie import ROLE_ADMIN


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "AdminPass123!"


async def _bootstrap_admin(app) -> str:
    db = app.state.db
    from hermes_orch.auth.cookie import hash_password
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
    from hermes_orch.auth.cookie import create_user
    user_id = await create_user(
        db, username=ADMIN_USERNAME, password=ADMIN_PASSWORD,
        role=ROLE_ADMIN, is_bootstrap_admin=True,
    )
    await db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(ADMIN_PASSWORD), user_id),
    )
    return user_id


async def _login_admin(ac: AsyncClient) -> None:
    r = await ac.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert r.status_code in (200, 201), r.text


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    orig_db_init = db_mod.Database.__init__

    def patched_db_init(self, db_path):
        orig_db_init(self, test_db)

    monkeypatch.setattr(db_mod.Database, "__init__", patched_db_init)
    app = main_mod.create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        await _bootstrap_admin(app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, app


async def _create_project(app, name: str = "chat test") -> str:
    pid = f"proj-chat-{uuid.uuid4().hex[:8]}"
    await app.state.db.execute(
        "INSERT INTO projects (id, name, goal, state, coordinator_role, "
        "accept_criteria, deliverable_path, max_iterations, "
        "current_iteration, last_iteration_summary) "
        "VALUES (?, ?, 'x', 'planned', '', '', '', 0, 0, '')",
        (pid, name),
    )
    return pid


def _mock_llm_response(content: str) -> dict:
    """Build a mock OpenAI-compatible chat completion response."""
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200},
    }


class _MockAsyncClient:
    """Mock httpx.AsyncClient that returns a canned LLM response."""
    def __init__(self, content: str):
        self._content = content
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None
    async def post(self, url, json, headers):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = _mock_llm_response(self._content)
        m.raise_for_status = lambda: None
        return m


async def _count_chat_rows(app, project_id: str) -> dict:
    """Return counts of user/assistant rows in project_chat_messages."""
    rows = await app.state.db.fetchall(
        "SELECT role, COUNT(*) c FROM project_chat_messages "
        "WHERE project_id = ? GROUP BY role",
        (project_id,),
    )
    return {r["role"]: r["c"] for r in rows}


@pytest.mark.asyncio
async def test_chat_502_when_llm_returns_only_think_block(client, monkeypatch):
    """The actual bug: LLM returns ONLY <think>...</think> with no
    final answer. The endpoint must raise 502 with a rephrase hint,
    not silently persist an empty assistant row."""
    ac, app = client
    await _login_admin(ac)
    pid = await _create_project(app)

    # The exact kind of LLM response from the wild:
    # 2269 chars of <think> reasoning, no final answer.
    think_only = (
        "<think>\nThe user is writing in Chinese and seems to be describing "
        "something about using multi-agent systems for research, and "
        "mentions placing searched data in a new folder in project_temp_folder.\n\n"
        "Let me parse this request:\n"
        "- \"現在用多agent 做research\" = \"Now using multi-agent to do research\"\n"
        "- \"可以把搜的資料放在project_temp_folder的新folder上\" = "
        "\"Can put the searched data in a new folder under project_temp_folder\"\n\n"
        "... lots more reasoning, no actual reply ..."
        "</think>"
    )

    # Patch httpx.AsyncClient to return the canned response
    from hermes_orch.api import projects as projects_mod
    monkeypatch.setattr(
        projects_mod.httpx, "AsyncClient",
        lambda *a, **kw: _MockAsyncClient(think_only),
    )

    r = await ac.post(
        f"/api/projects/{pid}/chat",
        json={"message": "現在用多agent 做research, 可以把搜的資料放在project_temp_folder的新folder上"},
    )
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    # The fix exposes actionable hints to the user
    assert "internal reasoning" in detail.lower() or "think" in detail.lower(), (
        f"502 message must explain the think-only case to the user: "
        f"{detail!r}"
    )
    assert "rephrase" in detail.lower() or "shorter context" in detail.lower(), (
        f"502 message must include a rephrase / context-shortening hint: "
        f"{detail!r}"
    )
    # CRITICAL: the user message IS persisted (they sent it), but
    # NO empty assistant row should be created.
    counts = await _count_chat_rows(app, pid)
    assert counts.get("user", 0) == 1, f"user row should be persisted: {counts}"
    assert counts.get("assistant", 0) == 0, (
        f"no assistant row should be persisted on 502 (this was the "
        f"v3.10.3 bug — empty rows with content_len=0 piled up in the "
        f"DB): {counts}"
    )


@pytest.mark.asyncio
async def test_chat_502_when_llm_returns_empty_content(client, monkeypatch):
    """The simpler case: LLM returns content="" (truly empty)."""
    ac, app = client
    await _login_admin(ac)
    pid = await _create_project(app)

    from hermes_orch.api import projects as projects_mod
    monkeypatch.setattr(
        projects_mod.httpx, "AsyncClient",
        lambda *a, **kw: _MockAsyncClient(""),
    )

    r = await ac.post(
        f"/api/projects/{pid}/chat",
        json={"message": "test"},
    )
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert "empty" in detail.lower() or "rephrase" in detail.lower(), (
        f"502 message must explain the empty case: {detail!r}"
    )
    # No empty assistant row
    counts = await _count_chat_rows(app, pid)
    assert counts.get("assistant", 0) == 0


@pytest.mark.asyncio
async def test_chat_200_when_llm_returns_think_block_plus_answer(client, monkeypatch):
    """Sanity: <think> + actual answer is preserved (only the
    <think> trace is stripped, the answer remains)."""
    ac, app = client
    await _login_admin(ac)
    pid = await _create_project(app)

    content = (
        "<think>\nBrief reasoning about the user's request.\n</think>\n\n"
        "Here is my plan suggestion: "
        "```json\n"
        '{"suggestions": [{"type": "update_plan", "steps": []}]}\n'
        "```"
    )

    from hermes_orch.api import projects as projects_mod
    monkeypatch.setattr(
        projects_mod.httpx, "AsyncClient",
        lambda *a, **kw: _MockAsyncClient(content),
    )

    r = await ac.post(
        f"/api/projects/{pid}/chat",
        json={"message": "test"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # The answer (after the <think> block) is preserved
    assert "Here is my plan suggestion" in data["message"]
    # Suggestions are extracted
    assert len(data["suggestions"]) == 1
    # Assistant row persisted
    counts = await _count_chat_rows(app, pid)
    assert counts.get("assistant", 0) == 1
    assert counts.get("user", 0) == 1
