"""Tests for the Live Output Streaming endpoints (v1.1, 2026-07-29).

v1.6 update: wrapper endpoints now require real HMAC auth. We use
the test helper `tests/_hmac_util.py` to register a test agent with
a known secret and sign every wrapper request.

Endpoints under test:
  POST /api/projects/{project_id}/tasks/{task_id}/output-chunk
    (wrapper -> server, HMAC-authed, audit_log write)
  GET  /api/projects/{project_id}/tasks/{task_id}/output?since=N
    (frontend -> server, no auth, returns ordered chunks)
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest


BASE = "http://127.0.0.1:8765"
DB_PATH = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"


# ===== HTTP helpers =====


def _http(
    method: str, path: str, body: dict | None = None, headers: dict | None = None
) -> tuple[int, object, dict]:
    """Plain HTTP helper for dashboard reads (no auth)."""
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json"} if data else {}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw or b"null"), dict(r.headers)
            except (json.JSONDecodeError, TypeError):
                return r.status, raw.decode("utf-8", errors="replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null"), dict(e.headers)
        except (json.JSONDecodeError, TypeError):
            return e.code, raw.decode("utf-8", errors="replace"), dict(e.headers)


# HMAC test util
from tests._hmac_util import register_test_agent, signed_request, unregister_test_agent


def _signed_http(
    method: str, path: str, body: dict | None, agent_id: str, secret: str
) -> tuple[int, object, dict]:
    return signed_request(method, path, body, agent_id, secret)


# ===== Seed helpers =====


def _create_project(name: str | None = None) -> str:
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO projects (id, name, goal, state, coordinator_role, "
            "accept_criteria, deliverable_path, max_iterations, "
            "current_iteration, last_iteration_summary) "
            "VALUES (?, ?, ?, 'planned', '', '', '', 0, 0, '')",
            (pid, name or f"output-test-{pid[-8:]}", "output streaming test"),
        )
        conn.commit()
    finally:
        conn.close()
    return pid


def _make_agent() -> tuple[str, str]:
    """Create a test agent row with a known secret. Returns (agent_id, secret)."""
    import secrets
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    secret = secrets.token_urlsafe(24)
    register_test_agent(agent_id, secret)
    return agent_id, secret


def _drop_agent(agent_id: str) -> None:
    unregister_test_agent(agent_id)


def _insert_task(
    project_id: str,
    agent_id: str,
    status: str = "running",
) -> str:
    """Insert a task assigned to agent_id. Returns task_id."""
    tid = f"t-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, status, "
            "depends_on, on_parent_failure, priority, assigned_agent_id) "
            "VALUES (?, ?, ?, 'super', ?, '[]', 'skip', 'normal', ?)",
            (tid, project_id, f"task-{tid[-8:]}", status, agent_id),
        )
        conn.commit()
    finally:
        conn.close()
    return tid


def _delete_project(project_id: str) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM audit_log WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()


# ===== POST /output-chunk tests =====


def test_post_chunk_happy_path():
    """The wrapper posts a stdout chunk; server writes audit_log row
    and returns the new id."""
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        s, body, _ = _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            {"seq": 1, "text": "hello world", "stream": "stdout"},
            agent_id, secret,
        )
        assert s == 200, f"{s} {body}"
        assert body["ok"] is True
        assert body["seq"] == 1
        assert "id" in body and isinstance(body["id"], int)
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_post_chunk_stderr_accepted():
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        s, body, _ = _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            {"seq": 1, "text": "warning: foo", "stream": "stderr"},
            agent_id, secret,
        )
        assert s == 200
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_post_chunk_401_missing_headers():
    """No HMAC headers at all -> 401."""
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            body={"seq": 1, "text": "x", "stream": "stdout"},
        )
        assert s == 401
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_post_chunk_401_wrong_secret():
    """Valid X-Agent-Id but wrong secret -> 401."""
    pid = _create_project()
    agent_id, _ = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        s, body, _ = _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            {"seq": 1, "text": "x", "stream": "stdout"},
            agent_id, "wrong-secret",
        )
        assert s == 401
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_post_chunk_404_unknown_task():
    """Valid HMAC, but task doesn't exist -> 404."""
    pid = _create_project()
    agent_id, secret = _make_agent()
    try:
        s, body, _ = _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/t-fake/output-chunk",
            {"seq": 1, "text": "x", "stream": "stdout"},
            agent_id, secret,
        )
        assert s == 404
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_post_chunk_404_task_in_other_project():
    """IDOR guard: a chunk for a task in project A must not be
    accepted via project B's path."""
    pid_a = _create_project("A")
    pid_b = _create_project("B")
    agent_id, secret = _make_agent()
    tid = _insert_task(pid_a, agent_id)
    try:
        s, _, _ = _signed_http(
            "POST",
            f"/api/projects/{pid_b}/tasks/{tid}/output-chunk",
            {"seq": 1, "text": "x", "stream": "stdout"},
            agent_id, secret,
        )
        assert s == 404
    finally:
        _delete_project(pid_a)
        _delete_project(pid_b)
        _drop_agent(agent_id)


def test_post_chunk_400_missing_seq():
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        s, body, _ = _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            {"text": "no seq", "stream": "stdout"},
            agent_id, secret,
        )
        assert s == 400
        assert "seq" in str(body).lower()
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_post_chunk_400_invalid_stream():
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        s, _, _ = _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            {"seq": 1, "text": "x", "stream": "wtf"},
            agent_id, secret,
        )
        assert s == 400
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_post_chunk_64kb_text_truncated():
    """Defensive cap: a 100KB text gets truncated to 64KB so a
    misbehaving wrapper can't OOM the server."""
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        big = "x" * 100_000
        s, body, _ = _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            {"seq": 1, "text": big, "stream": "stdout"},
            agent_id, secret,
        )
        assert s == 200
        s, get_body, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid}/output")
        assert s == 200
        assert get_body["count"] == 1
        assert len(get_body["chunks"][0]["text"]) == 65536
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


# ===== GET /output tests (dashboard reads, no HMAC) =====


def test_get_output_404_unknown_task():
    pid = _create_project()
    try:
        s, _, _ = _http("GET", f"/api/projects/{pid}/tasks/t-fake/output")
        assert s == 404
    finally:
        _delete_project(pid)


def test_get_output_empty_when_no_chunks():
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        s, body, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid}/output")
        assert s == 200
        assert body["chunks"] == []
        assert body["count"] == 0
        assert body["next_since"] == 0
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_get_output_returns_chunks_in_order():
    """3 chunks posted in order come back in the same order with
    increasing ids and seqs preserved."""
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        for i in range(1, 4):
            s, _, _ = _signed_http(
                "POST",
                f"/api/projects/{pid}/tasks/{tid}/output-chunk",
                {"seq": i, "text": f"line {i}\n", "stream": "stdout"},
                agent_id, secret,
            )
            assert s == 200
        s, body, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid}/output")
        assert s == 200
        assert body["count"] == 3
        seqs = [c["seq"] for c in body["chunks"]]
        assert seqs == [1, 2, 3]
        texts = [c["text"] for c in body["chunks"]]
        assert texts == ["line 1\n", "line 2\n", "line 3\n"]
        ids = [c["id"] for c in body["chunks"]]
        assert ids == sorted(ids)
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_get_output_since_filter():
    """since=N returns only chunks with id > N."""
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        for i in range(1, 5):
            _signed_http(
                "POST",
                f"/api/projects/{pid}/tasks/{tid}/output-chunk",
                {"seq": i, "text": f"x{i}", "stream": "stdout"},
                agent_id, secret,
            )
        s, body, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid}/output")
        assert s == 200
        all_ids = [c["id"] for c in body["chunks"]]
        cursor = all_ids[1]
        s, body2, _ = _http(
            "GET", f"/api/projects/{pid}/tasks/{tid}/output?since={cursor}"
        )
        assert s == 200
        remaining = [c["id"] for c in body2["chunks"]]
        assert all(i > cursor for i in remaining)
        assert body2["count"] == len(remaining)
        assert body2["next_since"] == (remaining[-1] if remaining else cursor)
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_get_output_scoped_to_task():
    """Chunks for task A don't show up under task B's output."""
    pid = _create_project()
    agent_a, secret_a = _make_agent()
    agent_b, secret_b = _make_agent()
    tid_a = _insert_task(pid, agent_a)
    tid_b = _insert_task(pid, agent_b)
    try:
        _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid_a}/output-chunk",
            {"seq": 1, "text": "A1", "stream": "stdout"},
            agent_a, secret_a,
        )
        _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid_b}/output-chunk",
            {"seq": 1, "text": "B1", "stream": "stdout"},
            agent_b, secret_b,
        )
        s, body_a, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid_a}/output")
        s, body_b, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid_b}/output")
        assert [c["text"] for c in body_a["chunks"]] == ["A1"]
        assert [c["text"] for c in body_b["chunks"]] == ["B1"]
    finally:
        _delete_project(pid)
        _drop_agent(agent_a)
        _drop_agent(agent_b)


def test_get_output_500_chunk_cap():
    """The GET endpoint caps at 500 chunks per request. The
    next_since returned lets the client paginate."""
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        for i in range(1, 511):
            _signed_http(
                "POST",
                f"/api/projects/{pid}/tasks/{tid}/output-chunk",
                {"seq": i, "text": str(i), "stream": "stdout"},
                agent_id, secret,
            )
        s, body, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid}/output")
        assert s == 200
        assert body["count"] == 500
        assert body["next_since"] == body["chunks"][-1]["id"]
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


# ===== ANSI strip (v1.4, 2026-07-29) =====


def test_ansi_color_codes_stripped_in_get_output():
    """Hermes writes colored output to its stdout. The GET endpoint
    must strip these CSI sequences so the dashboard's <pre> block
    shows clean text instead of raw escape bytes."""
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        raw = (
            "\x1b[1;38;2;255;215;0m╺─━━━━ Hermes ━━━━╸\x1b[0m\n"
            "\x1b[38;2;248;220mThe previous batch had a ls issue.\x1b[0m\n"
            "\x1b[2J\x1b[3A"  # clear screen + cursor up
        )
        _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            {"seq": 1, "text": raw, "stream": "stdout"},
            agent_id, secret,
        )
        s, body, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid}/output")
        assert s == 200
        text = body["chunks"][0]["text"]
        assert "\x1b" not in text
        assert "[1;38;2;255;215;0m" not in text
        assert "[0m" not in text
        assert "[2J" not in text
        assert "[3A" not in text
        assert "╺─━━━━ Hermes ━━━━╸" in text
        assert "The previous batch had a ls issue." in text
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_ansi_strip_does_not_touch_audit_log():
    """The strip happens on the way OUT (GET), not on the way IN
    (POST). The audit_log keeps the raw chunk for debugging."""
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        raw = "[1;38;2;255;215;0mraw chunk[0m"
        _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            {"seq": 1, "text": raw, "stream": "stdout"},
            agent_id, secret,
        )
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.execute(
            "SELECT payload FROM audit_log "
            "WHERE task_id = ? AND event_type = 'agent.output_chunk' "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        )
        payload = cur.fetchone()[0]
        conn.close()
        assert raw in payload
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)
