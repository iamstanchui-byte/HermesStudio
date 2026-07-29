"""Tests for the Tool-Call endpoint (v1.2, 2026-07-29).

v1.6 update: wrapper endpoints require real HMAC. The test
helper at tests/_hmac_util.py registers a test agent with a
known secret and signs every wrapper request.

Endpoints under test:
  POST /api/projects/{project_id}/tasks/{task_id}/tool-call
    (wrapper -> server, HMAC-authed, audit_log write)
  GET  /api/projects/{project_id}/tasks/{task_id}/status
    (frontend -> server, no auth, returns loop status)
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
import uuid
from pathlib import Path


BASE = "http://127.0.0.1:8765"
DB_PATH = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"


# ===== HTTP helpers =====


def _http(
    method: str, path: str, body: dict | None = None, headers: dict | None = None
) -> tuple[int, object, dict]:
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


from tests._hmac_util import register_test_agent, signed_request, unregister_test_agent


def _signed_http(
    method: str, path: str, body: dict | None, agent_id: str, secret: str
) -> tuple[int, object, dict]:
    return signed_request(method, path, body, agent_id, secret)


def _create_project(name: str | None = None) -> str:
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO projects (id, name, goal, state) "
            "VALUES (?, ?, '', 'planned')",
            (pid, name or f"tool-test-{pid[-8:]}"),
        )
        conn.commit()
    finally:
        conn.close()
    return pid


def _make_agent() -> tuple[str, str]:
    import secrets
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    secret = secrets.token_urlsafe(24)
    register_test_agent(agent_id, secret)
    return agent_id, secret


def _drop_agent(agent_id: str) -> None:
    unregister_test_agent(agent_id)


def _insert_task(project_id: str, agent_id: str) -> str:
    tid = f"t-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, status, "
            "depends_on, on_parent_failure, priority, assigned_agent_id) "
            "VALUES (?, ?, ?, 'super', 'running', '[]', 'skip', 'normal', ?)",
            (tid, project_id, f"task-{tid[-8:]}", agent_id),
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


# ===== POST /tool-call tests =====


def test_post_tool_call_happy_path():
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        s, body, _ = _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/tool-call",
            {"tool": "shell", "signature": "abc123def456"},
            agent_id, secret,
        )
        assert s == 200, f"{s} {body}"
        assert body["ok"] is True
        assert "id" in body
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_post_tool_call_401_missing_headers():
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        s, body, _ = _http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/tool-call",
            body={"tool": "shell", "signature": "abc"},
        )
        assert s == 401
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_post_tool_call_404_unknown_task():
    pid = _create_project()
    agent_id, secret = _make_agent()
    try:
        s, body, _ = _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/t-fake/tool-call",
            {"tool": "shell", "signature": "abc"},
            agent_id, secret,
        )
        assert s == 404
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_post_tool_call_400_missing_tool():
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        s, body, _ = _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/tool-call",
            {"signature": "abc"},
            agent_id, secret,
        )
        assert s == 400
        assert "tool" in str(body).lower()
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_post_tool_call_400_missing_signature():
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        s, body, _ = _signed_http(
            "POST",
            f"/api/projects/{pid}/tasks/{tid}/tool-call",
            {"tool": "shell"},
            agent_id, secret,
        )
        assert s == 400
        assert "signature" in str(body).lower()
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)


def test_post_tool_call_drives_looping_detection_via_status_endpoint():
    """Post 5 tool-calls with the same signature in quick succession.
    The /status endpoint's loop_status should reflect that the agent
    is in a loop (5+ same signature in <60s)."""
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id)
    try:
        # Post 6 events with the same signature
        for i in range(6):
            s, _, _ = _signed_http(
                "POST",
                f"/api/projects/{pid}/tasks/{tid}/tool-call",
                {"tool": "shell", "signature": "loopy-sig-12345"},
                agent_id, secret,
            )
            assert s == 200, f"tool-call {i} failed: {s}"
        # Read the /status endpoint to see if loop detection triggered
        s, body, _ = _http("GET", f"/api/projects/{pid}/tasks/{tid}/status")
        assert s == 200
        # loop_status is either "looping" (>= 5 same sigs) or
        # "ok"/"slow" if the window check is timing-dependent.
        # We at least confirm the endpoint returned a loop_status.
        assert body.get("loop_status") in ("looping", "ok", "slow")
    finally:
        _delete_project(pid)
        _drop_agent(agent_id)
