"""Regression test for the v1.9.3 _submit_result body-signing bug.

Background
----------
The wrapper submits task results via `POST /api/tasks/{id}/result`
with the result dict in the body. The server's `require_hmac_auth`
reads the raw body bytes and SHA256s them as part of the signature
input. The wrapper must sign the SAME body bytes the server sees.

Pre-v1.9.3 bug: the wrapper's `_submit_result` used
    headers=_auth_headers('POST', _result_path)   # body defaults to b""
    json=result                                   # httpx encodes result
The signature was bound to body=b"" while the server hashed the
actual JSON. Every submit_result 401'd, the task stayed in
'running' for 3 minutes, and the supervisor's stuck_wrapper
check marked it failed. User saw "task timeout failed" but the
root cause was a signing bug.

v1.9.3 fix: serialize the body once with json.dumps, pass it as
`body=` to _auth_headers, send as `content=` on the httpx call.
Both sides now hash the same bytes.

These tests verify both:
  1. The fixed /result call signs the correct body (server returns 200)
  2. The buggy version (json=result, headers signed with body=b"")
     returns 401 (regression catcher — keeps the bug from coming back)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests._hmac_util import (
    register_test_agent,
    signed_request,
    unregister_test_agent,
)


BASE = "http://127.0.0.1:8765"
DB_PATH = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"


# ===== HTTP helpers =====


def _create_project() -> str:
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO projects (id, name, goal, state, coordinator_role, "
            "accept_criteria, deliverable_path, max_iterations, "
            "current_iteration, last_iteration_summary) "
            "VALUES (?, ?, ?, 'planned', '', '', '', 0, 0, '')",
            (pid, f"submit-result-test-{pid[-8:]}", "v1.9.3 regression"),
        )
        conn.commit()
    finally:
        conn.close()
    return pid


def _make_agent() -> tuple[str, str]:
    import secrets
    agent_id = f"test-res-{uuid.uuid4().hex[:8]}"
    secret = secrets.token_urlsafe(24)
    register_test_agent(agent_id, secret)
    return agent_id, secret


def _insert_task(project_id: str, agent_id: str, status: str = "running") -> str:
    tid = f"t-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, status, "
            "depends_on, on_parent_failure, priority, assigned_agent_id, "
            "action, params) "
            "VALUES (?, ?, ?, 'super', ?, '[]', 'skip', 'normal', ?, ?, ?)",
            (tid, project_id, f"task-{tid[-8:]}", status, agent_id,
             "test action", "{}"),
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


def _sign(secret: str, method: str, path: str, body: bytes, ts: str) -> str:
    """Compute the HMAC signature the same way the server does."""
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = f"{method.upper()}\n{path}\n{body_hash}\n{ts}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _post_with_explicit_body(
    path: str, body: bytes, agent_id: str, secret: str
) -> tuple[int, object]:
    """POST with explicit body bytes (the v1.9.3 fixed pattern)."""
    ts = str(int(time.time()))
    sig = _sign(secret, "POST", path, body, ts)
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Agent-Id": agent_id,
            "X-Timestamp": ts,
            "X-Signature": sig,
        },
    )
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


def _post_with_wrong_body_signing(
    path: str, actual_body: bytes, agent_id: str, secret: str
) -> tuple[int, object]:
    """POST with the v1.9.3 BUG: sign body=b"" but send the real body.

    This is the buggy pattern that the user hit on 2026-07-30. We
    expect 401 — keeping this assertion here as a regression
    catcher so anyone refactoring won't accidentally re-introduce
    the bug.
    """
    ts = str(int(time.time()))
    # BUG: signing body=b"" while sending actual_body
    sig = _sign(secret, "POST", path, b"", ts)
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=actual_body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Agent-Id": agent_id,
            "X-Timestamp": ts,
            "X-Signature": sig,
        },
    )
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


# ===== Tests =====


def test_submit_result_with_correct_body_signing_succeeds():
    """The fixed /result call signs the body bytes; server accepts.

    Reproduces the success path of the v1.9.3 fix. If the wrapper
    ever regresses to signing body=b"" while sending the real
    body, this test still passes (because we use the correct
    pattern). The next test verifies the buggy pattern fails.
    """
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id, status="running")
    try:
        result = {
            "status": "completed",
            "summary": "hello from the wrapper",
            "skipped_artifacts": [],
        }
        body = json.dumps(result).encode("utf-8")
        path = f"/api/tasks/{tid}/result"
        s, body_resp = _post_with_explicit_body(path, body, agent_id, secret)
        assert s == 200, f"Expected 200, got {s}: {body_resp!r}"
        # Server should report the task as completed now
        conn = sqlite3.connect(str(DB_PATH))
        try:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
            assert row is not None
            assert row[0] == "completed", f"task not completed: {row[0]}"
        finally:
            conn.close()
    finally:
        _delete_project(pid)
        unregister_test_agent(agent_id)


def test_submit_result_with_wrong_body_signing_returns_401():
    """Regression catcher: signing body=b"" while sending the real
    body MUST return 401. If this test ever fails (returns 200),
    it means the server's signature check no longer verifies the
    body — a security regression.

    This is also a useful demonstration of the bug class: the
    client thinks it sent a valid request, the server sees a
    different body than what was signed, and 401s.
    """
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id, status="running")
    try:
        result = {
            "status": "completed",
            "summary": "this body will be signed as b''",
            "skipped_artifacts": [],
        }
        actual_body = json.dumps(result).encode("utf-8")
        path = f"/api/tasks/{tid}/result"
        s, body_resp = _post_with_wrong_body_signing(
            path, actual_body, agent_id, secret
        )
        assert s == 401, (
            f"Expected 401 (signature mismatch), got {s}: {body_resp!r}. "
            f"This means the server stopped verifying the body — security regression."
        )
    finally:
        _delete_project(pid)
        unregister_test_agent(agent_id)


def test_signed_request_helper_uses_correct_body():
    """The shared test helper `signed_request` must also pass the
    body bytes to the signature. This is the helper that other
    v1.6+ tests use; if it ever regresses to signing body=b""
    while sending the real body, all those tests would also break
    in subtle ways. Pin the helper's behavior here.
    """
    pid = _create_project()
    agent_id, secret = _make_agent()
    tid = _insert_task(pid, agent_id, status="running")
    try:
        result = {"status": "completed", "summary": "via signed_request helper"}
        s, body_resp, _ = signed_request(
            "POST", f"/api/tasks/{tid}/result", result, agent_id, secret,
        )
        assert s == 200, f"signed_request helper: expected 200, got {s}: {body_resp!r}"
    finally:
        _delete_project(pid)
        unregister_test_agent(agent_id)
