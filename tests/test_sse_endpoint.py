"""Integration tests for the SSE event endpoint (v1.8, 2026-07-29).

Endpoint under test:
  GET /api/projects/{project_id}/events
    Long-lived text/event-stream. Server sends:
      1. Initial 'snapshot' event with current task states
      2. Live events as they happen (task.state_changed, output.chunk,
         tool.call)
      3. ': keepalive' heartbeat every 30s (not tested here — would
         take 30s+; verified by code review)

Uses httpx.AsyncClient to drive the streaming connection. The
existing test suite (test_task_output_endpoints.py,
test_tool_call_endpoint.py, etc.) all use the same pattern: hit
the live server on 127.0.0.1:8765.

Test design note: the SSE event bus is in-process. The test
process and the running server each have their own bus state.
Tests that need to trigger a server-side publish therefore hit
a real endpoint (e.g. POST /cancel) instead of calling
publish_event() from the test — that call would only update the
TEST process's bus, not the server's. Subscriber-count and
reset_for_tests are unit-test only (see test_sse_bus.py); they
don't reflect server state and are not used here.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio


BASE = "http://127.0.0.1:8765"
DB_PATH = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"


# ===== DB seed helpers =====


def _create_project() -> str:
    """Insert a fresh project row. Returns its id."""
    pid = f"proj-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO projects (id, name, goal, state, coordinator_role, "
            "accept_criteria, deliverable_path, max_iterations, "
            "current_iteration, last_iteration_summary) "
            "VALUES (?, 'sse-test', ?, 'planned', '', '', '', 0, 0, '')",
            (pid, "sse endpoint test"),
        )
        conn.commit()
    finally:
        conn.close()
    return pid


def _insert_task(project_id: str, *, status: str = "running", name: str | None = None) -> str:
    tid = f"t-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO tasks (id, project_id, name, agent_role, status, "
            "depends_on, on_parent_failure, priority) "
            "VALUES (?, ?, ?, 'super', ?, '[]', 'skip', 'normal')",
            (tid, project_id, name or f"sse-task-{tid[-8:]}", status),
        )
        conn.commit()
    finally:
        conn.close()
    return tid


def _delete_project(project_id: str) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
    finally:
        conn.close()


# ===== SSE parsing helper =====


class SseStream:
    """Background reader that parses an httpx SSE response into
    a queue of events. Tests `await stream.next()` to get the
    next event, or `stream.collect(n)` for a batch.

    Why a class + queue: httpx's aiter_lines consumes the stream,
    so we can only iterate it once. We spawn a single background
    task that reads forever and pushes parsed events into an
    asyncio.Queue, then tests can pull events one or many at a
    time without re-iterating the underlying stream.
    """

    def __init__(self, response, *, name: str = "sse"):
        self._response = response
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._name = name
        self.closed = False

    async def __aenter__(self):
        self._task = asyncio.create_task(self._read_loop(), name=f"reader-{self._name}")
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _read_loop(self):
        """Parse SSE wire format and push events to the queue.
        Handles multi-line `data:` (concatenated) per spec.
        """
        current: dict = {"data_lines": []}
        try:
            async for raw in self._response.aiter_lines():
                if not raw:
                    # Empty line = end of current event
                    if current.get("type") or current["data_lines"]:
                        data_str = "\n".join(current["data_lines"])
                        try:
                            data = json.loads(data_str) if data_str else None
                        except json.JSONDecodeError:
                            data = data_str
                        ev: dict = {
                            "type": current.get("type", "message"),
                            "data": data,
                        }
                        if "id" in current:
                            ev["id"] = current["id"]
                        await self._queue.put(ev)
                    current = {"data_lines": []}
                    continue
                if raw.startswith(":"):
                    # SSE comment (heartbeat, etc.) — we don't put
                    # these on the queue; tests can observe
                    # connection liveness implicitly via timeouts.
                    continue
                if raw.startswith("event:"):
                    current["type"] = raw[len("event:"):].strip()
                elif raw.startswith("id:"):
                    current["id"] = raw[len("id:"):].strip()
                elif raw.startswith("data:"):
                    current["data_lines"].append(raw[len("data:"):].lstrip())
        except (httpx.RemoteProtocolError, httpx.ReadError, asyncio.CancelledError):
            # Server closed (or we cancelled) — reader exits.
            return
        except Exception as e:
            # Surface unexpected errors so a test waiting on the
            # queue doesn't hang forever.
            await self._queue.put({"type": "__error__", "data": str(e)})
            return

    async def next(self, *, timeout: float = 2.0) -> dict | None:
        """Wait for the next event. Returns None on timeout."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return None

    async def collect(self, n: int, *, timeout: float = 2.0) -> list[dict]:
        """Wait for up to `n` events. Returns whatever arrived
        within the timeout (may be fewer than n)."""
        out: list[dict] = []
        try:
            async with asyncio.timeout(timeout):
                while len(out) < n:
                    ev = await self._queue.get()
                    out.append(ev)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        return out


# ===== Fixtures =====


@pytest_asyncio.fixture
async def project_with_tasks():
    """Create a project with one running task for SSE tests."""
    pid = _create_project()
    _insert_task(pid, status="running", name="alpha")
    _insert_task(pid, status="completed", name="beta")
    try:
        yield pid
    finally:
        _delete_project(pid)


@pytest_asyncio.fixture
async def two_projects():
    """Two projects, each with a running task, for per-project
    isolation tests."""
    pid_a = _create_project()
    pid_b = _create_project()
    tid_a = _insert_task(pid_a, status="running", name="proj-a-task")
    tid_b = _insert_task(pid_b, status="running", name="proj-b-task")
    try:
        yield {"pid_a": pid_a, "pid_b": pid_b, "tid_a": tid_a, "tid_b": tid_b}
    finally:
        _delete_project(pid_a)
        _delete_project(pid_b)


# ===== Tests =====


@pytest.mark.asyncio
async def test_sse_endpoint_returns_event_stream_content_type(project_with_tasks):
    """The endpoint advertises text/event-stream so the browser
    knows to use EventSource (not fetch + JSON parsing)."""
    pid = project_with_tasks
    async with httpx.AsyncClient(timeout=5.0) as client:
        async with client.stream("GET", f"{BASE}/api/projects/{pid}/events") as resp:
            assert resp.status_code == 200
            ct = resp.headers.get("content-type", "")
            assert ct.startswith("text/event-stream"), f"got: {ct!r}"
            async with SseStream(resp, name="ct") as stream:
                ev = await stream.next(timeout=2.0)
                assert ev is not None, "no events received"


@pytest.mark.asyncio
async def test_sse_endpoint_sends_snapshot_first(project_with_tasks):
    """The very first event must be 'snapshot' with the project's
    current task list — this is what the client uses as the
    initial state instead of doing a separate /tasks/state fetch.
    """
    pid = project_with_tasks
    async with httpx.AsyncClient(timeout=5.0) as client:
        async with client.stream("GET", f"{BASE}/api/projects/{pid}/events") as resp:
            async with SseStream(resp) as stream:
                snap = await stream.next(timeout=2.0)
    assert snap is not None, "no events"
    assert snap["type"] == "snapshot"
    assert snap["data"]["project_id"] == pid
    assert snap["data"]["count"] == 2
    task_names = sorted(t["name"] for t in snap["data"]["tasks"])
    assert task_names == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_sse_endpoint_snapshot_event_wire_format(project_with_tasks):
    """Snapshot event uses the canonical SSE wire format:
        event: snapshot
        id: 0
        data: <json>

    The browser's EventSource parses this into {type, lastEventId, data}.
    """
    pid = project_with_tasks
    async with httpx.AsyncClient(timeout=5.0) as client:
        async with client.stream("GET", f"{BASE}/api/projects/{pid}/events") as resp:
            # Read the first event (parsed form proves the wire format
            # was at least: 'event: snapshot', 'id: 0', 'data: {...}')
            async with SseStream(resp) as stream:
                ev = await stream.next(timeout=2.0)
    assert ev is not None
    assert ev["type"] == "snapshot"
    assert ev.get("id") == "0"
    assert isinstance(ev["data"], dict)
    assert "project_id" in ev["data"]
    assert "tasks" in ev["data"]


@pytest.mark.asyncio
async def test_sse_endpoint_404_for_unknown_project():
    """An SSE request for a non-existent project must 404
    (don't open a stream for a project the user shouldn't see)."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        async with client.stream(
            "GET", f"{BASE}/api/projects/proj-does-not-exist/events"
        ) as resp:
            assert resp.status_code == 404
            body = await resp.aread()
            assert b"not found" in body.lower() or b"404" in body


@pytest.mark.asyncio
async def test_sse_endpoint_delivers_live_publish(project_with_tasks):
    """After the snapshot, an event published server-side is
    delivered to the connected client within ~100ms.
    This is the core SSE feature: real-time push instead of polling.

    Implementation note: the test process has its OWN event bus
    (separate from the server's). To trigger a server-side
    publish, we hit a real endpoint that publishes — the
    project-scoped cancel endpoint, which is what the dashboard
    itself uses.
    """
    pid = project_with_tasks
    # Find the running task from the fixture
    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute(
            "SELECT id FROM tasks WHERE project_id = ? AND status = 'running'",
            (pid,),
        ).fetchone()
        tid = row[0]
    finally:
        conn.close()
    async with httpx.AsyncClient(timeout=5.0) as client:
        async with client.stream("GET", f"{BASE}/api/projects/{pid}/events") as resp:
            async with SseStream(resp) as stream:
                # 1. Snapshot first
                snap = await stream.next(timeout=2.0)
                assert snap and snap["type"] == "snapshot"
                # 2. Trigger a server-side publish by cancelling
                #    the running task. This is the same path the
                #    dashboard's "Cancel" button uses.
                async with httpx.AsyncClient(timeout=5.0) as action_client:
                    r = await action_client.post(
                        f"{BASE}/api/projects/{pid}/tasks/{tid}/cancel"
                    )
                    assert r.status_code == 200, f"cancel failed: {r.text}"
                # 3. The next event should be the live one
                ev = await stream.next(timeout=2.0)
                assert ev is not None, "live event not received"
                assert ev["type"] == "task.state_changed"
                assert ev["data"]["task_id"] == tid
                assert ev["data"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_sse_endpoint_two_connections_both_get_event(project_with_tasks):
    """Two browser tabs open on the same project: both receive
    the live event. (Per-project broadcast, not first-wins.)"""
    pid = project_with_tasks
    # We need TWO cancellable running tasks for this test (one
    # cancel per stream; both streams should see both events).
    extra_tid = _insert_task(pid, status="running", name="gamma")
    conn = sqlite3.connect(str(DB_PATH))
    try:
        first = conn.execute(
            "SELECT id FROM tasks WHERE project_id = ? AND status = 'running' "
            "ORDER BY rowid LIMIT 1",
            (pid,),
        ).fetchone()[0]
    finally:
        conn.close()
    async with httpx.AsyncClient(timeout=5.0) as c1, httpx.AsyncClient(timeout=5.0) as c2:
        async with c1.stream("GET", f"{BASE}/api/projects/{pid}/events") as r1, \
                   c2.stream("GET", f"{BASE}/api/projects/{pid}/events") as r2:
            async with SseStream(r1, name="c1") as s1, SseStream(r2, name="c2") as s2:
                # Drain snapshots
                snap1 = await s1.next(timeout=2.0)
                snap2 = await s2.next(timeout=2.0)
                assert snap1 and snap1["type"] == "snapshot"
                assert snap2 and snap2["type"] == "snapshot"
                # Trigger server-side publish via cancel
                async with httpx.AsyncClient(timeout=5.0) as action_client:
                    r = await action_client.post(
                        f"{BASE}/api/projects/{pid}/tasks/{extra_tid}/cancel"
                    )
                    assert r.status_code == 200, f"cancel failed: {r.text}"
                # Both streams should see the task.state_changed
                e1, e2 = await asyncio.gather(
                    s1.next(timeout=2.0),
                    s2.next(timeout=2.0),
                )
                assert e1 and e1["type"] == "task.state_changed"
                assert e2 and e2["type"] == "task.state_changed"
                assert e1["data"]["task_id"] == extra_tid
                assert e2["data"]["task_id"] == extra_tid
                assert e1["data"]["status"] == "cancelled"
                assert e2["data"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_sse_endpoint_disconnect_does_not_crash_server(project_with_tasks):
    """When the client disconnects abruptly (close context
    manager), the server's event generator must not raise.
    We verify indirectly by: (1) making a connection, (2)
    disconnecting mid-stream, (3) confirming the server can
    still serve a fresh request after the disconnect.
    """
    pid = project_with_tasks
    # 1 + 2: open and close a connection
    async with httpx.AsyncClient(timeout=5.0) as client:
        async with client.stream("GET", f"{BASE}/api/projects/{pid}/events") as resp:
            async with SseStream(resp) as stream:
                ev = await stream.next(timeout=2.0)
                assert ev is not None
            # SseStream context exits here (reader cancelled)
        # stream() context exits here (TCP closed)
    await asyncio.sleep(0.2)  # let server's cleanup run
    # 3: fresh request must still work — proves no lingering state
    async with httpx.AsyncClient(timeout=5.0) as client:
        async with client.stream("GET", f"{BASE}/api/projects/{pid}/events") as resp:
            assert resp.status_code == 200
            async with SseStream(resp) as stream:
                ev = await stream.next(timeout=2.0)
                assert ev and ev["type"] == "snapshot"


@pytest.mark.asyncio
async def test_sse_endpoint_publish_to_other_project_does_not_leak(two_projects):
    """Cancelling a task in project B must not appear in project
    A's SSE stream. Per-project isolation on the wire (not just
    in the bus) — verified via real server-side actions.
    """
    pid_a = two_projects["pid_a"]
    tid_b = two_projects["tid_b"]
    async with httpx.AsyncClient(timeout=5.0) as client:
        async with client.stream("GET", f"{BASE}/api/projects/{pid_a}/events") as resp:
            async with SseStream(resp) as stream:
                snap = await stream.next(timeout=2.0)
                assert snap is not None
                # Cancel a task in project B (different project)
                async with httpx.AsyncClient(timeout=5.0) as action:
                    r = await action.post(
                        f"{BASE}/api/projects/{two_projects['pid_b']}/tasks/{tid_b}/cancel"
                    )
                    assert r.status_code == 200
                # Project A's stream should NOT receive the event
                ev = await stream.next(timeout=0.5)
                assert ev is None, f"unexpected event from other project: {ev}"


@pytest.mark.asyncio
async def test_sse_endpoint_delivers_multiple_live_events(project_with_tasks):
    """3 server-side publishes in quick succession should all
    arrive in order. (FIFO per-subscriber queue.) Triggers each
    via a separate task cancel — the only easy way to publish
    server-side from a test without an LLM in the loop.
    """
    pid = project_with_tasks
    # Create 3 more running tasks to cancel. 1 already exists from
    # the fixture, so we need 2 more (cancelling 3 total = 3 events).
    extra_tids = [
        _insert_task(pid, status="running", name=f"gamma-{i}")
        for i in range(3)
    ]
    async with httpx.AsyncClient(timeout=5.0) as client:
        async with client.stream("GET", f"{BASE}/api/projects/{pid}/events") as resp:
            async with SseStream(resp) as stream:
                snap = await stream.next(timeout=2.0)
                assert snap and snap["type"] == "snapshot"
                # Cancel 3 tasks in sequence
                async with httpx.AsyncClient(timeout=5.0) as action:
                    for tid in extra_tids:
                        r = await action.post(
                            f"{BASE}/api/projects/{pid}/tasks/{tid}/cancel"
                        )
                        assert r.status_code == 200, f"cancel failed for {tid}: {r.text}"
                # Collect 3 events
                evs = await stream.collect(3, timeout=3.0)
                assert len(evs) == 3, f"got {len(evs)} events, expected 3"
                # Each event corresponds to one of the cancelled tasks
                received_tids = {ev["data"]["task_id"] for ev in evs}
                assert received_tids == set(extra_tids), (
                    f"mismatched tids: got {received_tids}, expected {set(extra_tids)}"
                )
                # All are task.state_changed with status=cancelled
                for ev in evs:
                    assert ev["type"] == "task.state_changed"
                    assert ev["data"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_sse_endpoint_snapshot_has_event_id_zero(project_with_tasks):
    """The snapshot event must include `id: 0` so EventSource
    can use it for Last-Event-ID tracking. (Live events don't
    have an id yet — that's a v2.x concern; the 30s reconcile
    poll handles drift correction in v1.8.)
    """
    pid = project_with_tasks
    async with httpx.AsyncClient(timeout=5.0) as client:
        async with client.stream("GET", f"{BASE}/api/projects/{pid}/events") as resp:
            async with SseStream(resp) as stream:
                snap = await stream.next(timeout=2.0)
    assert snap is not None
    assert snap["type"] == "snapshot"
    assert snap.get("id") == "0", f"snapshot should have id=0, got {snap.get('id')!r}"
