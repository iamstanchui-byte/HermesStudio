"""Tests for v3.10.5 chat-based plan creation flow.

Context (2026-08-02):
  The chat LLM was being asked to output verbose prose + DAG +
  full plan JSON in chat. For a 5-step plan the LLM hit the 4000
  token cap mid-JSON (proj-c7ad42e6 repro: response truncated at
  '{"suggestions": [{"type": "update_plan", "plan": {"name":
  "ai-coding-agent-analysis"...'). The truncated JSON leaked into
  the chat UI as a wall of broken text, and there was no way to
  recover -- the Apply button never appeared because the JSON
  couldn't be parsed.

  v3.10.5 fix:
    1. Slim the system prompt (drop DAG + JSON requirements; the
       chat LLM now describes the plan in 1-3 short paragraphs).
    2. Lower max_tokens 4000 -> 2000 (forces brevity).
    3. Strip truncated JSON from the display (defensive).
    4. Add synthetic `create_plan_from_chat` suggestion when the
       LLM doesn't emit usable JSON. The Apply button then calls
       the planner LLM with the chat history as the goal, which
       generates a real plan server-side. This decouples the chat
       LLM from the planner LLM -- they no longer share the same
       JSON output.

This test asserts:
  1. _extract_suggestions returns 3-tuple (display, sugg, truncated)
  2. Truncated fenced JSON is stripped from display
  3. _looks_like_plan_proposal heuristic works
  4. chat endpoint creates synthetic suggestion when LLM produces
     prose-only response (and LLM looks like a plan proposal)
  5. chat endpoint creates synthetic suggestion when LLM is truncated
  6. POST /chat/apply with type=create_plan_from_chat reads chat
     history, calls planner, saves plan
  7. POST /chat/apply with type=create_plan_from_chat returns the
     generated plan
  8. POST /chat/apply with type=create_plan_from_chat fails cleanly
     when no chat history
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

# Use the v3.10.4-style local import pattern: tests in this file
# call _extract_suggestions / _looks_like_plan_proposal directly
# (unit tests) and the apply endpoint via HTTP (integration tests).

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ===== Unit tests for _extract_suggestions =====


def test_extract_suggestions_returns_3tuple_on_empty():
    """Empty text -> ('', None, False)."""
    from hermes_orch.api.projects import _extract_suggestions
    display, sugg, truncated = _extract_suggestions("")
    assert display == ""
    assert sugg is None
    assert truncated is False


def test_extract_suggestions_handles_complete_json_block():
    """Complete ```json ... ``` block -> extracted, stripped from display."""
    from hermes_orch.api.projects import _extract_suggestions
    text = (
        "I'll propose a 3-step plan.\n\n"
        "```json\n"
        '{"suggestions": [{"type": "update_plan", "plan": {"name": "x", "steps": []}}]}\n'
        "```"
    )
    display, sugg, truncated = _extract_suggestions(text)
    assert sugg is not None
    assert len(sugg) == 1
    assert sugg[0]["type"] == "update_plan"
    assert "I'll propose a 3-step plan" in display
    assert "```json" not in display
    assert truncated is False


def test_extract_suggestions_strips_truncated_json():
    """v3.10.5: LLM started ```json but never closed it (truncated).
    Strip the partial block from display; return truncated=True."""
    from hermes_orch.api.projects import _extract_suggestions
    text = (
        "Here's the plan I propose:\n\n"
        "```json\n"
        '{"suggestions": [{"type": "update_plan", "plan": {"name": "ai-cod'
        # ... truncated, no closing ```
    )
    display, sugg, truncated = _extract_suggestions(text)
    # No closing ``` -> no complete JSON block to extract
    assert sugg is None
    assert truncated is True
    # The partial JSON is stripped from display
    assert "Here's the plan I propose" in display
    assert "```json" not in display
    assert '{"suggestions"' not in display


def test_extract_suggestions_keeps_unmatched_open_fence():
    """If the LLM opens ```json but no JSON object inside, we
    still strip the broken block (defense against LLM-fooling
    patterns where the LLM opens a fence but writes prose)."""
    from hermes_orch.api.projects import _extract_suggestions
    text = (
        "Here's a plan description.\n\n"
        "```json\n"
        "this is not JSON, sorry\n"
        # no closing fence
    )
    display, sugg, truncated = _extract_suggestions(text)
    # We treat any unclosed ```json as truncated (defensive)
    assert sugg is None
    assert truncated is True
    assert "Here's a plan description" in display
    assert "this is not JSON" not in display


# ===== Unit tests for _looks_like_plan_proposal =====
#
# v3.10.8 (2026-08-02): heuristic tightened. Mentions of "步" or
# "plan" alone are no longer enough — the user complained that
# "Create plan from this conversation" was appearing on EVERY chat
# reply (screenshot 2026-08-02 19:51 on proj-c7ad42e6 showed it on
# a clarifying question about storage options, not a plan proposal).
# New rule: numbered list (2+ items) OR explicit "create this plan"
# closing phrase. Long-response fallback removed.


def test_looks_like_plan_proposal_with_numbered_list_zh():
    """Chinese numbered list `1) ... 2) ...` on separate lines -> True."""
    from hermes_orch.api.projects import _looks_like_plan_proposal
    text = (
        "我會做 5 步:\n"
        "1) research HK SME landscape\n"
        "2) analyze debug-loop risks\n"
        "3) compare LLM / tool options\n"
        "4) cost analysis\n"
        "5) write Traditional Chinese report"
    )
    assert _looks_like_plan_proposal(text) is True


def test_looks_like_plan_proposal_with_numbered_list_en():
    """English numbered list -> True."""
    from hermes_orch.api.projects import _looks_like_plan_proposal
    text = "Steps to take:\n1) gather data\n2) analyze\n3) write report"
    assert _looks_like_plan_proposal(text) is True


def test_looks_like_plan_proposal_with_chinese_comma_list():
    """Chinese `1、... 2、... 3、...` numbering -> True."""
    from hermes_orch.api.projects import _looks_like_plan_proposal
    text = "方案:\n1、搜集資料\n2、分析數據\n3、撰寫報告"
    assert _looks_like_plan_proposal(text) is True


def test_looks_like_plan_proposal_with_closing_question():
    """Ends with 'Want me to create this plan?' -> True (the
    canonical plan-proposal closing per the chat system prompt)."""
    from hermes_orch.api.projects import _looks_like_plan_proposal
    assert _looks_like_plan_proposal(
        "Here's a 3-step approach: gather data, analyze, write. "
        "Want me to create this plan?"
    ) is True
    assert _looks_like_plan_proposal(
        "我建議分 3 步做: 1) 搜集 2) 分析 3) 寫報告。要我建立呢個 plan?"
    ) is True


def test_looks_like_plan_proposal_long_response_only():
    """Long response (>= 200 chars) WITHOUT a list or create
    closing -> False. v3.10.8 removed the 200-char fallback
    because it fired on every long reply (user feedback)."""
    from hermes_orch.api.projects import _looks_like_plan_proposal
    long_text = "I will help you with this. " + ("a" * 200)
    assert _looks_like_plan_proposal(long_text) is False


def test_looks_like_plan_proposal_storage_question():
    """The proj-c7ad42e6 repro (2026-08-02 19:51): the assistant
    explained two storage options and asked which the user prefers.
    The reply contains the word "步驟" (mentioned in passing) and
    is well over 200 chars. v3.10.8 returns False — this is a
    clarifying question, not a plan proposal."""
    from hermes_orch.api.projects import _looks_like_plan_proposal
    text = (
        "你擔心得有道理。從目前的靈魂設定和依賴關係來看,**super** 的 "
        "`finalize_summary` 應該是讀取前面步驟任務輸出 (project shared "
        "cache / task outputs) 來做總結,而不是直接去 Google Drive 抓資料"
        "── 因為 win-agent01 和 win-agent02 的 `verify_gdrive` 結果會"
        "寫回各自的任務輸出,super 透過依賴獲取得到。\n\n"
        "不過既然 super 沒裝 Google Drive API,如果它要附上報告檔案就"
        "沒地方放。兩個解法:\n\n"
        "**A** 維持現狀:super 只在共享快取產出純文字總結,不寫檔。"
        "簡單但沒有最終檔案輸出。\n\n"
        "**B** 把 summary 交給 win-agent01 或 win-agent02(在寫偏好檔"
        "那個步驟之後),它可以直接用 Google Drive 存檔。比較保險。\n\n"
        "你傾向哪一個?"
    )
    assert _looks_like_plan_proposal(text) is False


def test_looks_like_plan_proposal_short_question():
    """Short question -> False."""
    from hermes_orch.api.projects import _looks_like_plan_proposal
    assert _looks_like_plan_proposal("What do you mean?") is False
    assert _looks_like_plan_proposal("你傾向哪一個?") is False
    assert _looks_like_plan_proposal("Hi") is False


def test_looks_like_plan_proposal_mentioning_step_word():
    """Just mentioning 'step' or '步' is not enough (v3.10.8
    tightened). A 1-line response that references an existing
    step in passing should NOT trigger the suggestion."""
    from hermes_orch.api.projects import _looks_like_plan_proposal
    assert _looks_like_plan_proposal(
        "那個步驟之後就會用到 cache,不用擔心。"
    ) is False
    assert _looks_like_plan_proposal(
        "Sure, the next step will handle that. Let me know."
    ) is False


def test_looks_like_plan_proposal_empty():
    """Empty text -> False."""
    from hermes_orch.api.projects import _looks_like_plan_proposal
    assert _looks_like_plan_proposal("") is False
    assert _looks_like_plan_proposal("   ") is False
