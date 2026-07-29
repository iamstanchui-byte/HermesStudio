"""Test utilities for HMAC-signed wrapper requests (v1.6+).

Provides:
  - signed_request(method, path, body, agent_id, secret) -> (status, body, headers)
  - register_test_agent(agent_id, secret) -> None  (INSERT into agents table)

The wrapper endpoints require:
  X-Agent-Id:    <agent id>
  X-Timestamp:   <unix epoch seconds>
  X-Signature:   <hex HMAC-SHA256 of method/path/body-sha256/timestamp>

The test helpers below compute the signature the same way the wrapper
does. Tests should call signed_request() for any wrapper-side endpoint
(output-chunk, tool-call, heartbeat, result, start, poll, files, etc.)
and plain http() for dashboard-side reads (no auth required).
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


BASE = "http://127.0.0.1:8765"
DB_PATH = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"


def _now_ts() -> str:
    return str(int(time.time()))


def _string_to_sign(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return f"{method.upper()}\n{path}\n{body_hash}\n{timestamp}".encode("utf-8")


def _sign(secret: str, method: str, path: str, body: bytes, timestamp: str) -> str:
    msg = _string_to_sign(method, path, body, timestamp)
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def signed_request(
    method: str,
    path: str,
    body: dict | None,
    agent_id: str,
    secret: str,
    *,
    extra_headers: dict | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict | list | str | None, dict]:
    """Make a wrapper-side HTTP request with valid HMAC headers.

    Returns (status, body_or_text, headers). body is parsed JSON when
    possible; falls back to text.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    ts = _now_ts()
    sig = _sign(secret, method, path, data or b"", ts)
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Id": agent_id,
        "X-Timestamp": ts,
        "X-Signature": sig,
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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


def register_test_agent(agent_id: str, secret: str) -> None:
    """Insert (or update) a row in `agents` for `agent_id` with the
    given hmac_secret. Idempotent: overwrites the secret on a re-run.

    Uses sync sqlite3 against the live DB. We bypass the HTTP
    register endpoint because that returns a fresh secret and we
    want to control the secret value for signing.
    """
    import hashlib as _h
    secret_hash = _h.sha256(secret.encode("utf-8")).hexdigest()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(str(DB_PATH))
    try:
        # Upsert: delete + insert to make this idempotent and simple
        conn.execute("DELETE FROM agent_profiles WHERE agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        conn.execute(
            "INSERT INTO agents (id, secret_hash, hmac_secret, status, "
            "created_at) VALUES (?, ?, ?, 'verified', ?)",
            (agent_id, secret_hash, secret, now),
        )
        conn.commit()
    finally:
        conn.close()


def unregister_test_agent(agent_id: str) -> None:
    """Remove the test agent row + any profile rows. Cleanup helper."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM agent_profiles WHERE agent_id = ?", (agent_id,))
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        conn.commit()
    finally:
        conn.close()


def make_test_agent(secret: str | None = None) -> tuple[str, str]:
    """Convenience: create a unique test agent + secret, register it.
    Returns (agent_id, secret). The caller is responsible for
    unregistering in teardown (use unregister_test_agent).
    """
    agent_id = f"test-hmac-{uuid.uuid4().hex[:8]}"
    if secret is None:
        secret = f"test-secret-{uuid.uuid4().hex}"
    register_test_agent(agent_id, secret)
    return agent_id, secret
