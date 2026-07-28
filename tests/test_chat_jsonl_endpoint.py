"""Tests for GET /api/projects/{id}/chat.jsonl endpoint (Phase 2, 2026-07-29).

This endpoint exposes the chat.jsonl file over HTTP so the
dashboard "View chat log" button can open it in a new tab.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

BASE = "http://127.0.0.1:8765"
PROJECTS_ROOT = Path(r"C:\Project\minimax code\hermes-project")


def _http_raw(method: str, path: str, body: dict | None = None) -> tuple[int, str, str]:
    """Return (status, body, content_type). The HTTPMessage headers
    are unreliable to convert to dict; we just need the content-type
    for these tests."""
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", errors="replace"), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), e.headers.get("Content-Type", "")


def _create_test_project() -> str:
    name = f"chat-jsonl-ep-{uuid.uuid4().hex[:8]}"
    s, body, _ = _http_raw("POST", "/api/projects/", {"name": name})
    if s == 201:
        return json.loads(body)["id"]
    if s == 200:
        return json.loads(body)["id"]
    pytest.fail(f"create project failed: {s} {body}")


def _delete_project(project_id: str) -> None:
    try:
        _http_raw("DELETE", f"/api/projects/{project_id}")
    except Exception:
        pass


# ===== Endpoint behavior =====


def test_endpoint_404_when_no_log():
    """A project with no chat history returns 404 (no chat.jsonl)."""
    pid = _create_test_project()
    try:
        s, body, _ = _http_raw("GET", f"/api/projects/{pid}/chat.jsonl")
        assert s == 404
    finally:
        _delete_project(pid)


def test_endpoint_returns_text_after_chat():
    """After a chat message, GET /chat.jsonl returns the file content
    as plain text with text/plain mime."""
    pid = _create_test_project()
    try:
        # Post a message (LLM may fail but user message is persisted)
        _http_raw("POST", f"/api/projects/{pid}/chat", {"message": "Hello"})
        s, body, ct = _http_raw("GET", f"/api/projects/{pid}/chat.jsonl")
        assert s == 200
        # Should be plain text
        assert "text/plain" in ct, f"expected text/plain, got: {ct!r}"
        # The body should have at least one line
        lines = [l for l in body.splitlines() if l.strip()]
        assert len(lines) >= 1
        # First line is the user message
        first = json.loads(lines[0])
        assert first["role"] == "user"
        assert first["content"] == "Hello"
    finally:
        _delete_project(pid)


def test_endpoint_returns_full_file_when_under_1mb():
    """The endpoint returns the full file (up to 1MB cap)."""
    pid = _create_test_project()
    try:
        # Add a few messages
        for i in range(3):
            _http_raw("POST", f"/api/projects/{pid}/chat", {"message": f"msg-{i}"})
        s, body, _ = _http_raw("GET", f"/api/projects/{pid}/chat.jsonl")
        assert s == 200
        # Count lines, should be 3+ (3 user msgs + 0 or 3 assistant)
        lines = [l for l in body.splitlines() if l.strip()]
        assert len(lines) >= 3
    finally:
        _delete_project(pid)


def test_endpoint_content_matches_file_on_disk():
    """The HTTP response body must equal the chat.jsonl file content
    on disk (no transformation)."""
    pid = _create_test_project()
    try:
        _http_raw("POST", f"/api/projects/{pid}/chat", {"message": "test"})
        # Read the file directly
        jsonl_path = PROJECTS_ROOT / pid / "chat.jsonl"
        if not jsonl_path.exists():
            pytest.skip("chat.jsonl not written")
        file_content = jsonl_path.read_text(encoding="utf-8")
        # Get via HTTP
        s, http_content, _ = _http_raw("GET", f"/api/projects/{pid}/chat.jsonl")
        assert s == 200
        # Bodies should match (allowing for CRLF→LF normalization on Windows)
        normalized_file = file_content.replace("\r\n", "\n")
        normalized_http = http_content.replace("\r\n", "\n")
        assert normalized_http == normalized_file
    finally:
        _delete_project(pid)


def test_endpoint_truncates_files_over_1mb():
    """Files larger than 1MB are truncated to the last 1MB and
    a header X-Chat-Log-Truncated is set."""
    import urllib.request as ur
    pid = _create_test_project()
    try:
        jsonl_path = PROJECTS_ROOT / pid / "chat.jsonl"
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        # Write a 1.5MB file of valid JSONL
        big = ""
        for i in range(15000):
            big += json.dumps({
                "id": i, "project_id": pid, "role": "user",
                "content": "x" * 100, "suggestions": [], "created_at": "2026-07-29",
            }, ensure_ascii=False) + "\n"
        jsonl_path.write_text(big, encoding="utf-8")
        # Use raw urllib so we can access full headers
        url = f"{BASE}/api/projects/{pid}/chat.jsonl"
        req = ur.Request(url)
        with ur.urlopen(req, timeout=15) as r:
            s = r.status
            body = r.read().decode("utf-8", errors="replace")
            trunc = r.headers.get("X-Chat-Log-Truncated")
            orig_size = r.headers.get("X-Chat-Log-Original-Size")
        assert s == 200
        # Truncation header set
        assert trunc == "1", f"expected X-Chat-Log-Truncated=1, got: {trunc!r}"
        # Body is at most ~1MB
        assert len(body.encode("utf-8")) <= 1_100_000  # 1MB + some slack
        # Original size reported
        assert int(orig_size or 0) > 1_000_000, f"expected >1MB, got: {orig_size!r}"
        # First line of truncated body should be a complete JSON
        # object (we seek from end, so first line could be partial
        # — verify it parses)
        first_line = body.splitlines()[0] if body.splitlines() else ""
        try:
            json.loads(first_line)
        except json.JSONDecodeError:
            # If first line is partial, the endpoint should advance
            # to the next newline first. Either way, this is a bug.
            pytest.fail(f"truncated body starts with a partial line: {first_line[:80]}")
    finally:
        _delete_project(pid)
        if jsonl_path.exists():
            jsonl_path.unlink()
