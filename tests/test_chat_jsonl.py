"""Tests for chat.jsonl append-only log (Phase 2, 2026-07-29).

The DB table `project_chat_messages` is the source of truth for
the chat UI. A parallel `projects/{id}/chat.jsonl` file is
appended on every chat message write, for operator inspection
(cat/tail/grep) and easy backup. This test verifies the file
gets written and is JSONL-valid.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

BASE = "http://127.0.0.1:8765"


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | str | None]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw or b"null")
            except (json.JSONDecodeError, TypeError):
                return r.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except (json.JSONDecodeError, TypeError):
            return e.code, raw.decode("utf-8", errors="replace")


def _create_test_project() -> str:
    name = f"chat-jsonl-test-{uuid.uuid4().hex[:8]}"
    s, body = _http("POST", "/api/projects/", {"name": name})
    if s == 201 and isinstance(body, dict) and "id" in body:
        return body["id"]
    if isinstance(body, dict) and "id" in body:
        return body["id"]
    pytest.fail(f"create project failed: {s} {body}")


def _delete_project(project_id: str) -> None:
    try:
        _http("DELETE", f"/api/projects/{project_id}")
    except Exception:
        pass


def _project_dir(project_id: str) -> Path:
    """Resolve the project's on-disk folder.

    The project dir layout is <projects_root>/<project_id>/. The
    chat.jsonl file lives inside this folder. We look up the
    projects root the same way the server does — under the
    orchestrator config (config.yaml). For this test we use the
    documented location: C:/Project/minimax code/hermes-project.
    """
    root = Path(r"C:\Project\minimax code\hermes-project") / project_id
    return root


# ===== JSONL file is created on chat message =====


def test_chat_message_creates_jsonl_file():
    """Posting a chat message should create projects/{id}/chat.jsonl
    with at least one line (the user message)."""
    pid = _create_test_project()
    try:
        pdir = _project_dir(pid)
        jsonl = pdir / "chat.jsonl"
        # Ensure no pre-existing file
        if jsonl.exists():
            jsonl.unlink()
        # Post a chat message
        s, _ = _http("POST", f"/api/projects/{pid}/chat", {
            "message": "Test message 1",
        })
        # 200 (LLM call may fail in test env, but the user message
        # is persisted first regardless of LLM outcome)
        if s != 200:
            # The LLM might be unavailable in the test env. The
            # user message is still persisted before the LLM call,
            # so the JSONL should still be written.
            pass
        # JSONL file should now exist
        assert jsonl.exists(), f"chat.jsonl not created at {jsonl}"
        # Read the lines
        lines = jsonl.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) >= 1
        # First line is the user message
        first = json.loads(lines[0])
        assert first["role"] == "user"
        assert first["content"] == "Test message 1"
        assert first["project_id"] == pid
        assert "created_at" in first
    finally:
        _delete_project(pid)


def test_jsonl_file_is_valid_jsonl():
    """Every line in chat.jsonl must be a valid JSON object. No
    multiline JSON, no trailing garbage."""
    pid = _create_test_project()
    try:
        # Post several messages
        for msg in ["First", "Second", "Third"]:
            _http("POST", f"/api/projects/{pid}/chat", {
                "message": msg,
            })
        jsonl = _project_dir(pid) / "chat.jsonl"
        if not jsonl.exists():
            pytest.skip("chat.jsonl not created (LLM may have failed)")
        text = jsonl.read_text(encoding="utf-8")
        # Every non-empty line must be valid JSON
        for i, line in enumerate(text.splitlines()):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                pytest.fail(f"line {i+1} is not valid JSON: {e}\n  line: {line[:200]}")
            assert isinstance(obj, dict), f"line {i+1} not a JSON object"
            assert "id" in obj
            assert "project_id" in obj
            assert "role" in obj
            assert "content" in obj
            assert "created_at" in obj
    finally:
        _delete_project(pid)


def test_jsonl_suggestions_field_is_array():
    """The suggestions field in JSONL must be a list (possibly empty),
    not null/missing. This makes downstream parsing (jq, grep) reliable."""
    pid = _create_test_project()
    try:
        _http("POST", f"/api/projects/{pid}/chat", {"message": "Hi"})
        jsonl = _project_dir(pid) / "chat.jsonl"
        if not jsonl.exists():
            pytest.skip("chat.jsonl not created")
        text = jsonl.read_text(encoding="utf-8")
        for line in text.splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            assert "suggestions" in obj
            assert isinstance(obj["suggestions"], list)
    finally:
        _delete_project(pid)


def test_jsonl_appends_does_not_overwrite():
    """Subsequent messages append to the existing file rather
    than overwriting it. Operator can keep accumulating history."""
    pid = _create_test_project()
    try:
        # Clear any existing
        jsonl = _project_dir(pid) / "chat.jsonl"
        if jsonl.exists():
            jsonl.unlink()
        # First message
        _http("POST", f"/api/projects/{pid}/chat", {"message": "M1"})
        n1 = sum(1 for _ in jsonl.read_text(encoding="utf-8").splitlines() if _.strip())
        # Second message
        _http("POST", f"/api/projects/{pid}/chat", {"message": "M2"})
        n2 = sum(1 for _ in jsonl.read_text(encoding="utf-8").splitlines() if _.strip())
        # n2 should be > n1
        assert n2 > n1, f"file not appended (n1={n1}, n2={n2})"
    finally:
        _delete_project(pid)


def test_jsonl_in_special_chars_unicode():
    """Multilingual content (Cantonese / Mandarin / box-drawing
    chars) round-trips correctly through the JSONL file."""
    pid = _create_test_project()
    try:
        # Box-drawing chars + Cantonese in the message
        special_msg = "幫我加 step └─ 落呢度 嘅 task graph"
        _http("POST", f"/api/projects/{pid}/chat", {"message": special_msg})
        jsonl = _project_dir(pid) / "chat.jsonl"
        if not jsonl.exists():
            pytest.skip("chat.jsonl not created")
        text = jsonl.read_text(encoding="utf-8")
        assert special_msg in text, "special chars not preserved in JSONL"
    finally:
        _delete_project(pid)
