"""Tests for the in-process SSE event bus (v1.8, 2026-07-29).

Covers src/hermes_orch/core/sse.py:
  - publish_event delivers to all subscribers of a project
  - subscribe() async ctx mgr registers + unregisters on exit
  - Per-project isolation: events on project A don't reach subs on B
  - Slow subscribers get events dropped when queue full (maxsize=100)
  - subscriber_count + reset_for_tests for test isolation
  - publish_event never raises (defensive)

Why unit tests (no FastAPI server): the bus is pure async with
no DB or HTTP. Integration with the SSE endpoint is tested in
test_sse_endpoint.py.
"""
from __future__ import annotations

import asyncio
import pytest
import pytest_asyncio

from hermes_orch.core import sse
from hermes_orch.core.sse import (
    publish_event,
    reset_for_tests,
    subscribe,
    subscriber_count,
)


# pytest-asyncio config: per-test event loop so bus state doesn't
# leak between tests. The bus uses module-level state, so we
# reset between tests in the fixture below.
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _clean_bus():
    """Reset the bus before AND after every test so test order
    doesn't matter and no test leaves dangling subscribers."""
    await reset_for_tests()
    yield
    await reset_for_tests()


async def test_publish_to_no_subscribers_is_noop():
    """publish_event with no subscribers returns 0 delivered
    and doesn't raise. Real-world: dashboard not open yet."""
    delivered = await publish_event("proj-x", "task.state_changed", {"x": 1})
    assert delivered == 0
    assert subscriber_count("proj-x") == 0


async def test_subscribe_yields_queue_and_receives_events():
    """Basic path: subscribe, publish, receive."""
    async with subscribe("proj-a") as q:
        assert subscriber_count("proj-a") == 1
        await publish_event("proj-a", "task.state_changed", {"status": "running"})
        event = await asyncio.wait_for(q.get(), timeout=1.0)
        assert event == {
            "type": "task.state_changed",
            "data": {"status": "running"},
        }


async def test_subscribe_unregisters_on_exit():
    """Exit the ctx mgr (normal flow) -> subscriber count drops."""
    async with subscribe("proj-a"):
        assert subscriber_count("proj-a") == 1
    assert subscriber_count("proj-a") == 0


async def test_subscribe_unregisters_on_exception():
    """Exit via exception -> subscriber count still drops.
    Critical for cleanup if the consumer loop crashes."""
    try:
        async with subscribe("proj-a"):
            assert subscriber_count("proj-a") == 1
            raise ValueError("simulated consumer crash")
    except ValueError:
        pass
    assert subscriber_count("proj-a") == 0


async def test_subscribe_unregisters_on_cancellation():
    """Exit via task cancellation -> subscriber still cleaned up.
    Important when the SSE client disconnects (server cancels
    the consumer coroutine)."""
    async def consumer():
        async with subscribe("proj-a"):
            assert subscriber_count("proj-a") == 1
            await asyncio.sleep(60)  # will be cancelled

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)  # let it register
    assert subscriber_count("proj-a") == 1
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Give the cleanup a moment
    await asyncio.sleep(0.05)
    assert subscriber_count("proj-a") == 0


async def test_multiple_subscribers_all_receive():
    """Two tabs open on the same project: both get the event."""
    async with subscribe("proj-a") as q1:
        async with subscribe("proj-a") as q2:
            assert subscriber_count("proj-a") == 2
            delivered = await publish_event(
                "proj-a", "output.chunk", {"text": "hello"}
            )
            assert delivered == 2
            e1 = await asyncio.wait_for(q1.get(), timeout=1.0)
            e2 = await asyncio.wait_for(q2.get(), timeout=1.0)
            assert e1["data"]["text"] == "hello"
            assert e2["data"]["text"] == "hello"


async def test_projects_are_isolated():
    """Event on project A is NOT delivered to project B's subs."""
    async with subscribe("proj-a") as qa:
        async with subscribe("proj-b") as qb:
            await publish_event("proj-a", "tool.call", {"x": 1})
            # qa gets it
            ea = await asyncio.wait_for(qa.get(), timeout=1.0)
            assert ea["data"]["x"] == 1
            # qb does NOT get it
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(qb.get(), timeout=0.1)


async def test_publish_returns_count_of_receivers():
    """The return value of publish_event equals the number of
    subscribers that successfully received the event."""
    async with subscribe("proj-a") as q1:
        assert await publish_event("proj-a", "x", {}) == 1
        async with subscribe("proj-a") as q2:
            assert await publish_event("proj-a", "x", {}) == 2
        # q2 left; back to 1
        assert await publish_event("proj-a", "x", {}) == 1
        # drain q1 so it doesn't fill up below
        await q1.get()


async def test_slow_subscriber_drops_events():
    """When a subscriber's queue is full, new events for THAT
    subscriber are dropped (the bus keeps going for other subs).
    Queue maxsize=100 -> 101st event drops.

    This is the "browser tab in background" case: events arrive
    faster than the consumer can process them. The bus must not
    block the publisher.
    """
    queue_maxsize = 100
    # Create a slow subscriber that never reads.
    async with subscribe("proj-a") as slow_q:
        # Also a fast subscriber so we can verify the bus still works.
        async with subscribe("proj-a") as fast_q:
            # Saturate the slow queue without draining.
            # put_nowait raises QueueFull when full.
            for i in range(queue_maxsize):
                slow_q.put_nowait({"type": "filler", "data": {"i": i}})
            assert slow_q.full()
            # Now publish. The slow one will get a QueueFull and
            # be dropped; the fast one gets the event normally.
            delivered = await publish_event(
                "proj-a", "task.state_changed", {"status": "running"}
            )
            assert delivered == 1  # only fast_q got it
            # Fast subscriber sees the event
            ev = await asyncio.wait_for(fast_q.get(), timeout=1.0)
            assert ev["data"]["status"] == "running"


async def test_publish_never_raises():
    """publish_event catches its own exceptions. We test by
    monkey-patching the internal list to be broken — publishing
    must still complete without raising."""
    # Break the internal state
    orig = sse._subscribers
    sse._subscribers = None  # type: ignore[assignment]
    try:
        # Should not raise even though _subscribers is None
        delivered = await publish_event("proj-x", "test", {"x": 1})
        assert delivered == 0
    finally:
        sse._subscribers = orig


async def test_subscriber_count_zero_for_unknown_project():
    """No subs ever registered for a project -> count is 0."""
    assert subscriber_count("proj-never-seen") == 0


async def test_subscriber_count_per_project():
    """subscriber_count is per-project, not global."""
    async with subscribe("proj-a"):
        async with subscribe("proj-a"):
            async with subscribe("proj-b"):
                assert subscriber_count("proj-a") == 2
                assert subscriber_count("proj-b") == 1
                assert subscriber_count("proj-c") == 0


async def test_reset_for_tests_clears_all_state():
    """reset_for_tests wipes every project's subscribers."""
    async with subscribe("proj-a"):
        async with subscribe("proj-b"):
            assert subscriber_count("proj-a") == 1
            assert subscriber_count("proj-b") == 1
    await reset_for_tests()
    assert subscriber_count("proj-a") == 0
    assert subscriber_count("proj-b") == 0


async def test_event_payload_arbitrary_shape():
    """The bus doesn't introspect the payload — it's a passthrough.
    Future event types with different shapes all work."""
    payloads = [
        {"type": "snapshot", "data": {"tasks": [], "count": 0}},
        {"type": "output.chunk", "data": {"task_id": "t-1", "text": "x"}},
        {"type": "tool.call", "data": {"tool": "shell", "sig": "abc"}},
        {"type": "task.state_changed", "data": {"status": "completed"}},
    ]
    async with subscribe("proj-a") as q:
        for p in payloads:
            await publish_event("proj-a", p["type"], p["data"])
        for expected in payloads:
            ev = await asyncio.wait_for(q.get(), timeout=1.0)
            assert ev == expected
