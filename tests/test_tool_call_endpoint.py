"""Tests for the Looping Detection endpoint (v1.2, 2026-07-29).

Endpoint under test:
  POST /api/projects/{project_id}/tasks/{task_id}/tool-call
    (wrapper → server, HMAC-ish auth via X-Agent-Id matching
     task.assigned_agent_id, writes audit_log row)

We run against the live server (port 8765) and seed data via
sync sqlite3 — same approach as the v1.1 /output-chunk tests.
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


def _create_project(name: str | None = None) -> str:
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO projects (id, name, goal, state, coordinator_role, "
            "accept_criteria, deliverable_path, max_iterations, "
            "current_iteration, last_iteration_summary) "
            "VALUES (?, ?, ?, 'planned', '', '', '', 0, 0, '')",
            (pid, name or f"loop-test-{pid[-8:]}", "loop detection test"),
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
) -> tuple[str, str]:
    tid = f"t-{uuid.uuid4().hex[:8]}"
    if agent_id is None:
        agent_id = f"loop-test-agent-{uuid.uuid4().hex[:6]}"
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
        conn.execute("DELETE FROM audit_log WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()


# ===== POST /tool-call tests =====


def test_post_tool_call_happy_path():
    """Wrapper posts a tool-call event; server writes audit_log row
    and returns the new id."""
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/tool-call",
            body={"tool": "shell", "signature": "abc123"},
            headers={"X-Agent-Id": agent_id},
        )
        assert s == 200, f"{s} {body}"
        assert body["ok"] is True
        assert "id" in body and isinstance(body["id"], int)
    finally:
        _delete_project(pid)


def test_post_tool_call_401_missing_x_agent_id():
    pid = _create_project()
    tid, _ = _insert_task(pid)
    try:
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/tool-call",
            body={"tool": "shell", "signature": "x"},
        )
        assert s == 401
        assert "X-Agent-Id" in str(body)
    finally:
        _delete_project(pid)


def test_post_tool_call_403_wrong_agent():
    pid = _create_project()
    tid, _ = _insert_task(pid)
    try:
        s, _, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/tool-call",
            body={"tool": "shell", "signature": "x"},
            headers={"X-Agent-Id": "intruder"},
        )
        assert s == 403
    finally:
        _delete_project(pid)


def test_post_tool_call_404_unknown_task():
    pid = _create_project()
    try:
        s, _, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/t-fake/tool-call",
            body={"tool": "shell", "signature": "x"},
            headers={"X-Agent-Id": "any"},
        )
        assert s == 404
    finally:
        _delete_project(pid)


def test_post_tool_call_404_idor():
    pid_a = _create_project("A")
    pid_b = _create_project("B")
    tid, agent_id = _insert_task(pid_a)
    try:
        s, _, _ = _http(
            "POST",
            f"/api/projects/{pid_b}/tasks/{tid}/tool-call",
            body={"tool": "shell", "signature": "x"},
            headers={"X-Agent-Id": agent_id},
        )
        assert s == 404
    finally:
        _delete_project(pid_a)
        _delete_project(pid_b)


def test_post_tool_call_400_missing_tool():
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/tool-call",
            body={"signature": "x"},
            headers={"X-Agent-Id": agent_id},
        )
        assert s == 400
        assert "tool" in str(body).lower()
    finally:
        _delete_project(pid)


def test_post_tool_call_400_missing_signature():
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/tool-call",
            body={"tool": "shell"},
            headers={"X-Agent-Id": agent_id},
        )
        assert s == 400
        assert "signature" in str(body).lower()
    finally:
        _delete_project(pid)


def test_post_tool_call_long_tool_name_truncated():
    """A 1000-char tool name is truncated to 256 chars (defensive cap)."""
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/tool-call",
            body={"tool": "x" * 1000, "signature": "abc"},
            headers={"X-Agent-Id": agent_id},
        )
        assert s == 200
        # Read it back via DB to confirm truncation
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.execute(
            "SELECT json_extract(payload, '$.tool') FROM audit_log "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (tid,),
        )
        stored = cur.fetchone()[0]
        conn.close()
        assert len(stored) == 256
    finally:
        _delete_project(pid)


def test_post_tool_call_long_signature_truncated():
    """Signature capped at 64 chars."""
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/tool-call",
            body={"tool": "shell", "signature": "y" * 200},
            headers={"X-Agent-Id": agent_id},
        )
        assert s == 200
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.execute(
            "SELECT json_extract(payload, '$.signature') FROM audit_log "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (tid,),
        )
        stored = cur.fetchone()[0]
        conn.close()
        assert len(stored) == 64
    finally:
        _delete_project(pid)


def test_post_tool_call_drives_looping_detection_via_status_endpoint():
    """End-to-end: post 5 tool-call events with the same signature
    for a running task, then GET /status and verify loop_status ==
    'looping'."""
    pid = _create_project()
    tid, agent_id = _insert_task(pid)
    try:
        for i in range(5):
            s, _, _ = _http(
                "POST",
                f"/api/projects/{pid}/tasks/{tid}/tool-call",
                body={"tool": "shell", "signature": "loop_sig_xyz"},
                headers={"X-Agent-Id": agent_id},
            )
            assert s == 200
        # Now query status
        s, body, _ = _http(
            "GET", f"/api/projects/{pid}/tasks/{tid}/status"
        )
        assert s == 200
        assert body["loop_status"] == "looping"
        assert body["loop_reason"].startswith("looped")
        assert "shell" in body["loop_reason"]
    finally:
        _delete_project(pid)
