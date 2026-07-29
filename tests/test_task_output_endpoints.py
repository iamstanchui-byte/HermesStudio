"""Tests for the Live Output Streaming endpoints (v1.1, 2026-07-29).

Endpoints under test:
  POST /api/projects/{project_id}/tasks/{task_id}/output-chunk
    (wrapper → server, HMAC-ish auth, audit_log write)
  GET  /api/projects/{project_id}/tasks/{task_id}/output?since=N
    (frontend → server, returns ordered chunks)

We run against the live server (port 8765) and seed data via
sync sqlite3 (same approach as T2/T3 tests). The auth model is
MVP-level (X-Agent-Id header must match task.assigned_agent_id).
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
) -> tuple[int, dict | list | str | None, dict]:
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


def _insert_task(
    project_id: str,
    *,
    status: str = "running",
    agent_id: str | None = None,
) -> str:
    """Insert a task with the given assigned_agent_id (default: a
    fake agent id so the POST auth check has something to match)."""
    tid = f"t-{uuid.uuid4().hex[:8]}"
    if agent_id is None:
        agent_id = f"test-agent-{uuid.uuid4().hex[:6]}"
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
    return tid, agent_id


def _delete_project(project_id: str) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
        conn.execute(
            "DELETE FROM audit_log WHERE project_id = ?", (project_id,)
        )
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()


# ===== POST /output-chunk tests =====


def test_post_chunk_happy_path():
    """The wrapper posts a stdout chunk; server writes audit_log row
    and returns the new id."""
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            body={"seq": 1, "text": "hello world", "stream": "stdout"},
            headers={"X-Agent-Id": agent_id},
        )
        assert s == 200, f"{s} {body}"
        assert body["ok"] is True
        assert body["seq"] == 1
        assert "id" in body and isinstance(body["id"], int)
    finally:
        _delete_project(pid)


def test_post_chunk_stderr_accepted():
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            body={"seq": 1, "text": "warning: foo", "stream": "stderr"},
            headers={"X-Agent-Id": agent_id},
        )
        assert s == 200
    finally:
        _delete_project(pid)


def test_post_chunk_401_missing_x_agent_id():
    pid = _create_project()
    tid, _ = _insert_task(pid)
    try:
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            body={"seq": 1, "text": "x", "stream": "stdout"},
        )
        assert s == 401
        assert "X-Agent-Id" in str(body)
    finally:
        _delete_project(pid)


def test_post_chunk_403_wrong_agent():
    """A wrapper for agent-A cannot post chunks for a task assigned
    to agent-B. This is the security guard against cross-agent
    log spoofing."""
    pid = _create_project()
    tid, _owner = _insert_task(pid)  # owner is auto-generated
    try:
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            body={"seq": 1, "text": "spoofed", "stream": "stdout"},
            headers={"X-Agent-Id": "some-other-agent"},
        )
        assert s == 403
    finally:
        _delete_project(pid)


def test_post_chunk_404_unknown_task():
    pid = _create_project()
    try:
        s, _, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/t-fake/output-chunk",
            body={"seq": 1, "text": "x", "stream": "stdout"},
            headers={"X-Agent-Id": "any"},
        )
        assert s == 404
    finally:
        _delete_project(pid)


def test_post_chunk_404_task_in_other_project():
    """IDOR guard: a chunk for a task in project A must not be
    accepted via project B's path."""
    pid_a = _create_project("A")
    pid_b = _create_project("B")
    tid, agent_id = _insert_task(pid_a)
    try:
        s, _, _ = _http(
            "POST",
            f"/api/projects/{pid_b}/tasks/{tid}/output-chunk",
            body={"seq": 1, "text": "x", "stream": "stdout"},
            headers={"X-Agent-Id": agent_id},
        )
        assert s == 404
    finally:
        _delete_project(pid_a)
        _delete_project(pid_b)


def test_post_chunk_400_missing_seq():
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            body={"text": "no seq", "stream": "stdout"},
            headers={"X-Agent-Id": agent_id},
        )
        assert s == 400
        assert "seq" in str(body).lower()
    finally:
        _delete_project(pid)


def test_post_chunk_400_invalid_stream():
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        s, _, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            body={"seq": 1, "text": "x", "stream": "wtf"},
            headers={"X-Agent-Id": agent_id},
        )
        assert s == 400
    finally:
        _delete_project(pid)


def test_post_chunk_64kb_text_truncated():
    """Defensive cap: a 100KB text gets truncated to 64KB so a
    misbehaving wrapper can't OOM the server."""
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        big = "x" * 100_000
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            body={"seq": 1, "text": big, "stream": "stdout"},
            headers={"X-Agent-Id": agent_id},
        )
        assert s == 200
        # Read it back via GET
        s, get_body, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid}/output")
        assert s == 200
        assert get_body["count"] == 1
        assert len(get_body["chunks"][0]["text"]) == 65536
    finally:
        _delete_project(pid)


# ===== GET /output tests =====


def test_get_output_404_unknown_task():
    pid = _create_project()
    try:
        s, _, _ = _http("GET", f"/api/projects/{pid}/tasks/t-fake/output")
        assert s == 404
    finally:
        _delete_project(pid)


def test_get_output_404_idor():
    pid_a = _create_project("A")
    pid_b = _create_project("B")
    tid, agent_id = _insert_task(pid_a)
    try:
        s, _, _ = _http("GET", f"/api/projects/{pid_b}/tasks/{tid}/output")
        assert s == 404
    finally:
        _delete_project(pid_a)
        _delete_project(pid_b)


def test_get_output_empty_when_no_chunks():
    pid = _create_project()
    tid, _ = _insert_task(pid)
    try:
        s, body, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid}/output")
        assert s == 200
        assert body["chunks"] == []
        assert body["count"] == 0
        assert body["next_since"] == 0
    finally:
        _delete_project(pid)


def test_get_output_returns_chunks_in_order():
    """3 chunks posted in order come back in the same order with
    increasing ids and seqs preserved."""
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        for i in range(1, 4):
            s, _, _ = _http(
                "POST",
                f"/api/projects/{pid}/tasks/{tid}/output-chunk",
                body={"seq": i, "text": f"line {i}\n", "stream": "stdout"},
                headers={"X-Agent-Id": agent_id},
            )
            assert s == 200
        s, body, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid}/output")
        assert s == 200
        assert body["count"] == 3
        seqs = [c["seq"] for c in body["chunks"]]
        assert seqs == [1, 2, 3]
        texts = [c["text"] for c in body["chunks"]]
        assert texts == ["line 1\n", "line 2\n", "line 3\n"]
        # ids are monotonically increasing
        ids = [c["id"] for c in body["chunks"]]
        assert ids == sorted(ids)
    finally:
        _delete_project(pid)


def test_get_output_since_filter():
    """since=N returns only chunks with id > N."""
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        for i in range(1, 5):
            _http(
                "POST",
                f"/api/projects/{pid}/tasks/{tid}/output-chunk",
                body={"seq": i, "text": f"x{i}", "stream": "stdout"},
                headers={"X-Agent-Id": agent_id},
            )
        s, body, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid}/output")
        assert s == 200
        all_ids = [c["id"] for c in body["chunks"]]
        # Take the second id as the cursor; only chunks AFTER it
        cursor = all_ids[1]
        s, body2, _ = _http(
            "GET", f"/api/projects/{pid}/tasks/{tid}/output?since={cursor}"
        )
        assert s == 200
        remaining = [c["id"] for c in body2["chunks"]]
        assert all(i > cursor for i in remaining)
        # And the count + next_since match
        assert body2["count"] == len(remaining)
        assert body2["next_since"] == (remaining[-1] if remaining else cursor)
    finally:
        _delete_project(pid)


def test_get_output_stdout_and_stderr_preserved():
    """Both streams come back with the right stream field."""
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            body={"seq": 1, "text": "out1", "stream": "stdout"},
            headers={"X-Agent-Id": agent_id},
        )
        _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/output-chunk",
            body={"seq": 1, "text": "err1", "stream": "stderr"},
            headers={"X-Agent-Id": agent_id},
        )
        s, body, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid}/output")
        assert s == 200
        streams = {c["stream"] for c in body["chunks"]}
        assert streams == {"stdout", "stderr"}
    finally:
        _delete_project(pid)


def test_get_output_scoped_to_task():
    """Chunks for task A don't show up under task B's output."""
    pid = _create_project()
    tid_a, agent_a = _insert_task(pid)
    tid_b, agent_b = _insert_task(pid)
    try:
        _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid_a}/output-chunk",
            body={"seq": 1, "text": "A1", "stream": "stdout"},
            headers={"X-Agent-Id": agent_a},
        )
        _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid_b}/output-chunk",
            body={"seq": 1, "text": "B1", "stream": "stdout"},
            headers={"X-Agent-Id": agent_b},
        )
        s, body_a, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid_a}/output")
        s, body_b, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid_b}/output")
        assert [c["text"] for c in body_a["chunks"]] == ["A1"]
        assert [c["text"] for c in body_b["chunks"]] == ["B1"]
    finally:
        _delete_project(pid)


def test_get_output_500_chunk_cap():
    """The GET endpoint caps at 500 chunks per request. The
    next_since returned lets the client paginate."""
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        # Insert 510 chunks (small text to keep request fast)
        for i in range(1, 511):
            _http(
                "POST",
                f"/api/projects/{pid}/tasks/{tid}/output-chunk",
                body={"seq": i, "text": str(i), "stream": "stdout"},
                headers={"X-Agent-Id": agent_id},
            )
        s, body, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid}/output")
        assert s == 200
        assert body["count"] == 500
        # next_since points at the last returned id, so the client
        # can continue paginating
        assert body["next_since"] == body["chunks"][-1]["id"]
    finally:
        _delete_project(pid)
