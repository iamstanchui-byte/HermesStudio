"""SSE event bus (v1.8, 2026-07-29).

A minimal in-process pub/sub for pushing real-time events to
browser dashboards. The browser opens a long-lived
`/api/projects/{id}/events` connection (SSE), the server emits
events as the underlying state changes (output chunk arrives,
task state changes, etc.), and the browser EventSource applies
them without polling.

Design:
  - In-process only (single FastAPI process; no Redis/Kafka).
  - Per-project subscriber lists. Each subscriber holds a bounded
    `asyncio.Queue` (maxsize=100). On overflow, the event is dropped
    for that subscriber — the slow client should reconnect and
    fetch a fresh snapshot.
  - Lock-protected subscriber list. The lock is held briefly
    (just to add/remove a queue or copy the list); no I/O under
    lock.
  - `publish_event()` is the only entry point. Returns the number
    of subscribers that received the event (for observability).
  - `subscribe()` is an async context manager that registers a
    queue, yields it for the caller's consumer loop, and
    unregisters on exit (including cancellation).

We chose to use `dict[str, list[Queue]]` (not Redis pub/sub or
Kafka) because:
  - The orchestrator is single-process (uvicorn, no workers).
  - Local network, low event volume (< 100 events/sec even with
    50 watchers).
  - No serialization overhead vs. Redis.

For multi-process / multi-host deployment, swap `publish_event`
to write to Redis pub/sub or similar; the SSE endpoint would
subscribe to that channel.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


# project_id -> list of subscriber queues
_subscribers: dict[str, list[asyncio.Queue]] = {}
_lock = asyncio.Lock()


# Per-connection queue size. Slow consumers (e.g. browser tab in
# background) get events dropped after this. The EventSource
# reconnection logic will then fetch a fresh snapshot via the
# initial-state payload of the next connection.
_MAX_QUEUE_SIZE = 100


def _make_queue() -> asyncio.Queue:
    return asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)


async def publish_event(project_id: str, event_type: str, data: Any) -> int:
    """Push an event to all subscribers of project_id.

    Returns the number of subscribers that received it. Drops
    events for slow consumers (queue full); they should reconnect
    and pull a fresh snapshot.

    Fire-and-forget: never raises. Failures are logged.
    """
    event = {"type": event_type, "data": data}
    delivered = 0
    dropped = 0
    try:
        async with _lock:
            subs = list(_subscribers.get(project_id, []))
        for q in subs:
            try:
                q.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                dropped += 1
    except Exception as e:  # defensive: never crash the publisher
        logger.warning(f"publish_event({project_id}, {event_type}) failed: {e}")
    if dropped:
        logger.debug(
            f"publish_event({project_id}, {event_type}): "
            f"delivered={delivered} dropped={dropped}"
        )
    return delivered


@asynccontextmanager
async def subscribe(project_id: str) -> AsyncIterator[asyncio.Queue]:
    """Subscribe to events for a project.

    Usage:
        async with subscribe(project_id) as queue:
            while True:
                event = await queue.get()
                ...

    The queue is registered for the duration of the context
    manager. On exit (normal or via exception/cancellation), the
    queue is unregistered and any pending events are dropped.
    """
    q = _make_queue()
    async with _lock:
        _subscribers.setdefault(project_id, []).append(q)
    try:
        yield q
    finally:
        async with _lock:
            subs = _subscribers.get(project_id, [])
            try:
                subs.remove(q)
            except ValueError:
                pass
            if not subs:
                _subscribers.pop(project_id, None)


def subscriber_count(project_id: str) -> int:
    """How many active subscribers for project_id. Used by tests
    + observability. Lock-free read; may be slightly stale."""
    return len(_subscribers.get(project_id, []))


async def reset_for_tests() -> None:
    """Clear all subscribers. Tests use this to start clean."""
    async with _lock:
        _subscribers.clear()
