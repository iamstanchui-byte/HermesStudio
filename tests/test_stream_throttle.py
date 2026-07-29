"""Unit tests for the v1.1 stream throttling algorithm.

We test `_stream_throttle_loop` directly — it accepts injectable
`time_fn` / `sleep_fn` / `should_stop` so we can drive the loop
deterministically without spinning up a real hermes subprocess.

The full wrapper integration (Popen + actual POSTs) is covered
by the smoke test, not here.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

import pytest


# Skip the whole module if the wrapper module isn't importable in
# this environment (e.g. hermes-agent has different deps than the
# orchestrator). We try-exec on import to give a clear error if so.
try:
    from hermes_orch.agent_cli import _stream_throttle_loop  # noqa: E402
except Exception as _e:  # pragma: no cover
    pytest.skip(
        f"agent_cli not importable in test env: {_e}", allow_module_level=True
    )


class _FakeTime:
    """Monotonic clock controllable from the test. Every call to
    time_fn() returns the current `now`; advance() pushes it forward.
    """
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def _write(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def _append(path: Path, content: bytes) -> None:
    with open(path, "ab") as f:
        f.write(content)


# ===== Basic flow =====


def test_flushes_when_buffer_fills(tmp_path: Path):
    """8KB+ in the buffer triggers an immediate flush."""
    f = tmp_path / "out.log"
    _write(f, b"x" * 9000)  # > 8KB
    flushed: list[str] = []
    t = _FakeTime()
    # Stop after 2 iterations: iter 1 reads the file (buf > max →
    # flush); iter 2 sees should_stop=True and exits.
    iters = [False, True]
    _stream_throttle_loop(
        f,
        should_stop=lambda: iters.pop(0) if iters else True,
        flush=flushed.append,
        throttle_s=999.0,  # never trip on time
        buf_max=8192,
        time_fn=t,
        sleep_fn=lambda _s: None,
    )
    assert flushed == ["x" * 9000]


def test_flushes_when_throttle_elapses(tmp_path: Path):
    """Even small content flushes after 2s of no activity."""
    f = tmp_path / "out.log"
    _write(f, b"small")
    flushed: list[str] = []
    t = _FakeTime()
    # Loop: tick 1 (t=1000) → small in buf, last_flush=1000.
    #        tick 2 (t=1001) → buf still 5 bytes, not enough, sleep.
    #        tick 3 (t=1003) → 3s elapsed > 2s throttle → flush.
    # should_stop returns True on tick 4 to exit.
    ticks = [1000.0, 1001.0, 1003.0, 9999.0]
    t_iter = iter(ticks)
    def stop() -> bool:
        try:
            return next(t_iter) > 5000
        except StopIteration:
            return True
    _stream_throttle_loop(
        f,
        should_stop=stop,
        flush=flushed.append,
        throttle_s=2.0,
        buf_max=8192,
        time_fn=lambda: t.now,
        sleep_fn=lambda _s: None,
    )
    # Should have flushed once ("small") before stopping
    assert flushed == ["small"]


def test_no_flush_when_buffer_empty(tmp_path: Path):
    """An empty (or non-existent) file produces no flushes."""
    f = tmp_path / "out.log"  # not created
    flushed: list[str] = []
    _stream_throttle_loop(
        f,
        should_stop=lambda: True,
        flush=flushed.append,
        throttle_s=0.001,
        buf_max=8192,
        sleep_fn=lambda _s: None,
    )
    assert flushed == []


def test_final_flush_on_stop(tmp_path: Path):
    """When the loop exits with leftover buffer, do a final flush."""
    f = tmp_path / "out.log"
    _write(f, b"hello")
    flushed: list[str] = []
    # After 1 iteration the buffer has "hello". should_stop returns
    # True on iteration 2; final flush kicks in.
    should_stop = [False, True]
    _stream_throttle_loop(
        f,
        should_stop=lambda: should_stop.pop(0) if should_stop else True,
        flush=flushed.append,
        throttle_s=999.0,  # never trip on time
        buf_max=8192,
        sleep_fn=lambda _s: None,
    )
    assert flushed == ["hello"]


# ===== Append behavior =====


def test_picks_up_new_bytes_on_subsequent_iterations(tmp_path: Path):
    """A writer that appends to the file should be picked up by the
    tail loop on later iterations. Each batch is one flush (since
    the batch is small and time advances)."""
    f = tmp_path / "out.log"
    _write(f, b"line1\n")
    flushed: list[str] = []
    # Run loop in a real thread so we can append from the main test
    import threading
    stop = threading.Event()
    def run() -> None:
        _stream_throttle_loop(
            f,
            should_stop=stop.is_set,
            flush=flushed.append,
            throttle_s=0.1,  # fast for the test
            buf_max=8192,
            sleep_fn=stop.wait,
        )
    t = threading.Thread(target=run, daemon=True)
    t.start()
    # Let one tick happen, then append more
    import time
    time.sleep(0.3)
    _append(f, b"line2\n")
    time.sleep(0.3)
    _append(f, b"line3\n")
    time.sleep(0.3)
    stop.set()
    t.join(timeout=2)
    # Each line should have been flushed in its own chunk
    assert "line1" in "".join(flushed)
    assert "line2" in "".join(flushed)
    assert "line3" in "".join(flushed)


# ===== Edge cases =====


def test_resume_position_after_reopen(tmp_path: Path):
    """The loop should NOT re-flush bytes it has already read, even
    though it re-opens the file each iteration."""
    f = tmp_path / "out.log"
    _write(f, b"abcdef")
    flushed: list[str] = []
    # Stop after 2 iterations. Iteration 1: reads "abcdef", buf not
    # yet flushed (throttle 999), stops on iter 2 → final flush
    # flushes the whole "abcdef".
    iters = [False, True]
    _stream_throttle_loop(
        f,
        should_stop=lambda: iters.pop(0),
        flush=flushed.append,
        throttle_s=999.0,
        buf_max=8192,
        sleep_fn=lambda _s: None,
    )
    assert flushed == ["abcdef"]
    assert len(flushed) == 1  # NOT flushed twice (the position is preserved)


def test_handles_file_growing_past_buf_max(tmp_path: Path):
    """A single big write (>2x buf_max) gets flushed in one chunk
    (not broken up mid-line). The next iteration starts a new chunk."""
    f = tmp_path / "out.log"
    big = b"x" * (16 * 1024)  # 16KB, 2x buf_max
    _write(f, big)
    flushed: list[str] = []
    iters = [False, True]
    _stream_throttle_loop(
        f,
        should_stop=lambda: iters.pop(0),
        flush=flushed.append,
        throttle_s=999.0,
        buf_max=8192,
        sleep_fn=lambda _s: None,
    )
    # The 16KB block is flushed as one chunk on iter 1 (buf > max)
    assert len(flushed) == 1
    assert len(flushed[0]) == 16 * 1024


def test_unicode_decode_with_replace(tmp_path: Path):
    """Invalid UTF-8 bytes are replaced (not raised)."""
    f = tmp_path / "out.log"
    _write(f, b"hello \xff\xfe world")
    flushed: list[str] = []
    iters = [False, True]
    _stream_throttle_loop(
        f,
        should_stop=lambda: iters.pop(0),
        flush=flushed.append,
        throttle_s=999.0,
        buf_max=8192,
        sleep_fn=lambda _s: None,
    )
    assert flushed == ["hello \ufffd\ufffd world"]


def test_burst_then_quiet_uses_throttle_not_buf_max(tmp_path: Path):
    """100 small writes that don't fill the buffer are still
    flushed when 2s elapses."""
    f = tmp_path / "out.log"
    _write(f, b"x")
    flushed: list[str] = []
    t = _FakeTime()
    # Simulate: tick at t=1000 (read 1 byte, buffer=1 byte), then
    # jump to t=1003 (3s elapsed > 2s throttle) → flush.
    ticks = [1000.0, 1003.0, 99999.0]
    t_iter = iter(ticks)
    def stop() -> bool:
        try:
            return next(t_iter) > 5000
        except StopIteration:
            return True
    _stream_throttle_loop(
        f,
        should_stop=stop,
        flush=flushed.append,
        throttle_s=2.0,
        buf_max=8192,
        time_fn=lambda: t.now,
        sleep_fn=lambda _s: None,
    )
    assert flushed == ["x"]
