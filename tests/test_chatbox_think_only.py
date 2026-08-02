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
    # v3.10.4 follow-up: the hint now mentions concrete next steps
    # (clear history / shorter question / JSON editor) — the
    # earlier "rephrase" wording was too generic to be useful.
    assert (
        "clear" in detail.lower()  # "clear its history"
        or "shorter" in detail.lower()  # "a shorter, more focused question"
    ), (
        f"502 message must include a concrete next step "
        f"(clear history / shorter question / JSON editor): {detail!r}"
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


@pytest.mark.asyncio
async def test_chat_retry_succeeds_on_second_attempt(client, monkeypatch):
    """v3.10.4 follow-up: when the 1st LLM call returns think-only,
    the endpoint automatically retries with a directive "be concise,
    no lengthy reasoning" follow-up. If the retry returns a real
    answer, the user gets a 200 (not a 502). This is the most
    common path: the LLM just needed a nudge to skip the lengthy
    internal reasoning and produce an answer.
    """
    ac, app = client
    await _login_admin(ac)
    pid = await _create_project(app)

    # Track how many LLM calls the test makes
    call_count = {"n": 0}

    class _SequenceAsyncClient:
        """Returns a different response for each call. The first
        call is think-only (no answer); the second is the retry
        with a real answer."""
        def __init__(self):
            self._responses = [
                "<think>\nLots of internal reasoning that consumes the entire output budget.\n</think>",
                "Here is my concise answer: the agents know about project_temp_folder via their profile's storage_refs. Each agent profile has a list of storage paths the agent can use.",
            ]
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def post(self, url, json, headers):
            call_count["n"] += 1
            idx = min(call_count["n"] - 1, len(self._responses) - 1)
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = _mock_llm_response(self._responses[idx])
            m.raise_for_status = lambda: None
            return m

    from hermes_orch.api import projects as projects_mod
    monkeypatch.setattr(
        projects_mod.httpx, "AsyncClient",
        lambda *a, **kw: _SequenceAsyncClient(),
    )

    r = await ac.post(
        f"/api/projects/{pid}/chat",
        json={"message": "我不懂task 要怎樣set 才會放在project_temp_folder"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # The retry succeeded — user sees the real answer
    assert "Here is my concise answer" in data["message"]
    # Both calls were made (1st think-only, 2nd retry)
    assert call_count["n"] == 2, (
        f"expected 2 LLM calls (1st think-only + 1 retry), got "
        f"{call_count['n']}"
    )
    # Assistant row persisted with the real answer
    counts = await _count_chat_rows(app, pid)
    assert counts.get("assistant", 0) == 1


@pytest.mark.asyncio
async def test_chat_retry_also_think_only_raises_502_with_actionable_hint(client, monkeypatch):
    """If BOTH the 1st call and the retry are think-only, the
    endpoint raises 502 with a concrete, actionable hint (not
    the generic "rephrase" message). The hint tells the user:
      - Clear the chat history
      - Ask a shorter question
      - Or use the JSON editor directly"""
    ac, app = client
    await _login_admin(ac)
    pid = await _create_project(app)

    class _BothThinkOnlyAsyncClient:
        def __init__(self):
            self._responses = [
                "<think>\nFirst attempt: just thinking.\n</think>",
                "<think>\nSecond attempt: still just thinking.\n</think>",
            ]
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, url, json, headers):
            m = MagicMock()
            m.status_code = 200
            # Round-robin the canned responses
            idx = 0  # always return first
            m.json.return_value = _mock_llm_response(self._responses[idx])
            m.raise_for_status = lambda: None
            return m

    from hermes_orch.api import projects as projects_mod
    monkeypatch.setattr(
        projects_mod.httpx, "AsyncClient",
        lambda *a, **kw: _BothThinkOnlyAsyncClient(),
    )

    r = await ac.post(
        f"/api/projects/{pid}/chat",
        json={"message": "我不懂task 要怎樣set 才會放在project_temp_folder"},
    )
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    # The actionable hint is more concrete than just "rephrase"
    assert "clear" in detail.lower() or "history" in detail.lower(), (
        f"502 hint should mention clearing chat history: {detail!r}"
    )
    assert "shorter" in detail.lower() or "focused" in detail.lower(), (
        f"502 hint should suggest a shorter question: {detail!r}"
    )
    # No empty assistant row persisted
    counts = await _count_chat_rows(app, pid)
    assert counts.get("assistant", 0) == 0


@pytest.mark.asyncio
async def test_chat_max_tokens_is_at_least_4000(client, monkeypatch):
    """v3.10.4 follow-up: max_tokens was bumped from 1500 to 4000.
    The 1500 budget was consumed by the LLM's internal reasoning
    (~360 tokens) leaving ~1140 for the actual reply — not enough
    for a multi-paragraph answer. 4000 gives the LLM ~3500 tokens
    of headroom after reasoning. This test pins the value so a
    future refactor that lowers it again is caught immediately."""
    ac, app = client
    await _login_admin(ac)
    pid = await _create_project(app)
    captured_max_tokens = {"v": 0}

    class _CaptureMaxTokensAsyncClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, url, json, headers):
            captured_max_tokens["v"] = json.get("max_tokens")
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = _mock_llm_response(
                "Short answer."
            )
            m.raise_for_status = lambda: None
            return m

    from hermes_orch.api import projects as projects_mod
    monkeypatch.setattr(
        projects_mod.httpx, "AsyncClient",
        lambda *a, **kw: _CaptureMaxTokensAsyncClient(),
    )

    r = await ac.post(
        f"/api/projects/{pid}/chat",
        json={"message": "hi"},
    )
    # 200 because the LLM returned a real answer
    assert r.status_code == 200
    # Critical: max_tokens must be at least 4000
    assert captured_max_tokens["v"] >= 4000, (
        f"max_tokens was {captured_max_tokens['v']}, expected >= 4000. "
        f"With less, MiniMax M3's internal reasoning (~360 tokens) "
        f"eats too much of the budget and the model produces no final "
        f"answer (proj-cef60586 repro on 2026-08-02)."
    )
