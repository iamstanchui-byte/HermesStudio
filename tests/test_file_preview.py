"""Tests for the dashboard-side file preview endpoint (v1.9.3).

The wrapper-side `/api/projects/{id}/files/{path}` endpoint requires
HMAC (v1.6). The dashboard's HTML renders artifact links as plain
<a href> — the browser can't attach HMAC headers, so the user gets
401 in the popup. The `/file-preview/{path}` endpoint exists for
the dashboard (no auth). Same security model as every other dashboard
read endpoint (no session; project_id-in-URL is the only barrier).

These tests verify:
  1. The preview endpoint returns 200 + content for an existing file
  2. The preview endpoint returns 404 for a missing file
  3. The preview endpoint returns 400 for a directory
  4. The preview endpoint rejects path traversal (`..`)
  5. The HMAC'd /files/ endpoint still requires auth (regression catcher —
     we want both endpoints to coexist)
"""
from __future__ import annotations

import sqlite3
import uuid
import urllib.error
import urllib.request
from pathlib import Path

import pytest


BASE = "http://127.0.0.1:8765"
DB_PATH = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
PROJECTS_ROOT = Path("C:/Project/minimax code/hermes-project")


def _http(method: str, path: str) -> tuple[int, bytes, dict]:
    """Plain HTTP helper for dashboard-side reads (no auth)."""
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _create_test_project_with_file() -> tuple[str, str]:
    """Create a project + a small text file in projects_root.

    Returns (project_id, file_path_relative_to_project_dir).
    """
    pid = f"proj-preview-{uuid.uuid4().hex[:8]}"
    fname = "test-content.md"
    pdir = PROJECTS_ROOT / pid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / fname).write_text(
        "# v1.9.3 preview test\n\nHello from preview endpoint.\n",
        encoding="utf-8",
    )
    # Register in DB (some routes check the project exists)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO projects (id, name, goal, state, coordinator_role, "
            "accept_criteria, deliverable_path, max_iterations, "
            "current_iteration, last_iteration_summary) "
            "VALUES (?, ?, ?, 'planned', '', '', '', 0, 0, '')",
            (pid, "preview test", "preview endpoint test"),
        )
        conn.commit()
    finally:
        conn.close()
    return pid, fname


def _delete_test_project(pid: str) -> None:
    import shutil
    pdir = PROJECTS_ROOT / pid
    if pdir.exists():
        shutil.rmtree(pdir)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
        conn.commit()
    finally:
        conn.close()


# ===== tests =====


def test_preview_returns_200_for_existing_file():
    pid, fname = _create_test_project_with_file()
    try:
        s, body, headers = _http(
            "GET", f"/api/projects/{pid}/file-preview/{fname}"
        )
        assert s == 200, f"got {s} body={body[:200]!r}"
        assert b"v1.9.3 preview test" in body
        assert b"Hello from preview endpoint" in body
        # X-File-Path header echoes the requested path (used by browser
        # to show in the title bar / download dialog)
        assert headers.get("x-file-path") == fname
    finally:
        _delete_test_project(pid)


def test_preview_returns_404_for_missing_file():
    pid, _ = _create_test_project_with_file()
    try:
        s, body, _ = _http(
            "GET", f"/api/projects/{pid}/file-preview/does-not-exist.md"
        )
        assert s == 404, f"got {s}"
        assert b"File not found" in body
    finally:
        _delete_test_project(pid)


def test_preview_returns_400_for_directory():
    pid, _ = _create_test_project_with_file()
    try:
        # Create a subdirectory in the project folder; the endpoint
        # should reject it with 400 (it's not a file).
        (PROJECTS_ROOT / pid / "subdir").mkdir(exist_ok=True)
        s, body, _ = _http(
            "GET", f"/api/projects/{pid}/file-preview/subdir"
        )
        assert s == 400, f"got {s}"
        assert b"Not a file" in body
    finally:
        _delete_test_project(pid)


def test_preview_rejects_path_traversal():
    pid, _ = _create_test_project_with_file()
    try:
        # Attempt to escape via ../../. Endpoint must resolve and
        # verify the result is inside pdir. 400/403/404 all acceptable;
        # the important thing is NO 200 with content from outside.
        s, body, _ = _http(
            "GET",
            f"/api/projects/{pid}/file-preview/..%2F..%2Fwindows%2Fsystem.ini",
        )
        assert s != 200, f"got {s} — path traversal may have escaped!"
    finally:
        _delete_test_project(pid)


def test_hmac_endpoint_still_requires_auth():
    """Regression catcher: the /files/ endpoint must still require HMAC.

    The dashboard's /file-preview/ is a separate, no-auth endpoint.
    The wrapper's /files/ PUT/DELETE/GET (used for sync file upload) is
    HMAC'd. If a future refactor accidentally drops the auth on the
    HMAC endpoint, this test will catch it.
    """
    pid, _ = _create_test_project_with_file()
    try:
        s, body, _ = _http(
            "GET", f"/api/projects/{pid}/files/test-content.md"
        )
        assert s == 401, f"got {s} — HMAC endpoint lost its auth!"
        assert b"Missing auth headers" in body
    finally:
        _delete_test_project(pid)
