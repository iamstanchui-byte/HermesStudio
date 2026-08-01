# coding: utf-8
"""Regression test: wrapper applies pending profile configs BEFORE running tasks.

Background (v3.10.4, 2026-08-02):
  proj-e05e89e9 (analyst 2) hit a 1-line ordering bug in the wrapper
  daemon loop. The order was:

    1. _heartbeat() - get assigned tasks
    2. ThreadPoolExecutor runs tasks (hermes subprocess, 3+ min)
    3. _apply_pending_configs_inline() - poll + apply SOUL configs

  If a long-running task was active, step 3 was BLOCKED until the
  task pool exited. The supervisor's 30s dispatch timeout would fire
  before the wrapper got a chance to claim the new profile's pending
  config, and the new task would fail with:

    dispatch.soul_apply_failed: SOUL apply failed for profile
    <uuid> (role=X, cfg_id=<uuid>): SOUL apply timed out
    (status=pending)

  Real-world repro: super profile 38510a3e was running task
  365463c4 (research-hk-market-context, started 02:23:39). super-b
  config 89449e75 was queued at 02:23:35 — the wrapper was busy
  with 365463c4, never polled super-b, the 30s dispatch timeout
  fired at 02:24:05, compare-features (t-da3dc4ce) failed, and
  finalize-hk-view-report (t-5a7c0e13) was skipped as a downstream
  consequence. Two tasks lost to a one-line ordering bug.

Fix: move `_apply_pending_configs_inline()` BEFORE the task pool
(after the heartbeat + cleanup pass). The pre-tick block now runs
the config poll + apply before the workers start, so a long-running
task can never starve a new profile's SOUL apply.

This test reads the source of `start()` in agent_cli.py and asserts:
  1. The call to `_apply_pending_configs_inline()` appears in the
     MAIN daemon loop (not the bg heartbeat thread) BEFORE the
     `if assigned:` block that starts the ThreadPoolExecutor.
  2. The call to `_apply_pending_configs_inline()` is NOT in the
     "after the pool, before zombie sweep" position any more.
  3. A regression-marking comment is present so the next person who
     looks at this file sees why the order matters.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

AGENT_CLI_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "hermes_orch" / "agent_cli.py"
)


def _read_main_daemon_loop_body() -> str:
    """Read the MAIN daemon loop from agent_cli.py.

    The `def start(...)` function has TWO `while not stop_flag`
    blocks:
      1. The bg heartbeat thread loop (smaller, just calls
         _heartbeat_loop() and sleeps)
      2. The MAIN daemon loop (heartbeat -> apply configs -> run
         tasks in pool -> zombie sweep)

    We want the SECOND one. We identify it by finding the loop that
    contains `_apply_pending_configs_inline()` — the bg heartbeat
    loop doesn't call that.
    """
    src = AGENT_CLI_PATH.read_text(encoding="utf-8")
    m_start = re.search(r"^def start\(", src, re.MULTILINE)
    assert m_start, "def start( not found in agent_cli.py"
    after_start = src[m_start.end():]
    all_whiles = list(re.finditer(
        r"^(\s+)while not stop_flag\[.stop.\]:",
        after_start,
        re.MULTILINE,
    ))
    assert len(all_whiles) >= 2, (
        f"expected >=2 'while not stop_flag' in start() (bg heartbeat + "
        f"main loop), found {len(all_whiles)}"
    )
    # Pick the while whose body contains _apply_pending_configs_inline.
    # The bg loop is small (~10 lines) and only heartbeats + sleeps.
    chosen = None
    for w in all_whiles:
        # Take a chunk of ~1000 lines after this while and check
        # for the apply call. The main loop is ~300 lines so 1000
        # is plenty; the bg loop is <20 lines.
        chunk = after_start[w.end():w.end() + 50000]
        # Limit to same indentation
        while_indent = len(w.group(1))
        body_lines = []
        for line in chunk.split("\n"):
            if line.strip() == "":
                body_lines.append(line)
                continue
            leading = len(line) - len(line.lstrip())
            if leading <= while_indent:
                break
            body_lines.append(line)
        body = "\n".join(body_lines)
        if "_apply_pending_configs_inline" in body:
            chosen = (w, body, while_indent)
            break
    assert chosen, (
        "no `while not stop_flag` block in start() contains "
        "_apply_pending_configs_inline — the call may have been removed. "
        "If you're refactoring, keep at least one call in the main loop."
    )
    return chosen[1]


def test_config_apply_runs_before_task_pool():
    """The pre-task-pool config-apply call must exist (the fix)."""
    body = _read_main_daemon_loop_body()
    matches = list(re.finditer(
        r"^(\s+)_apply_pending_configs_inline\(\)\s*$",
        body,
        re.MULTILINE,
    ))
    assert len(matches) >= 1, (
        "no `_apply_pending_configs_inline()` call found in the main "
        "daemon loop. The v3.10.4 fix moved it to the pre-task-pool "
        "position; if you're refactoring, preserve the ordering — see "
        "the bug comment in the file."
    )
    # The task pool block `if assigned:` starts the workers; it must
    # come AFTER the apply call (the apply must finish first).
    pre_tick_call_idx = body.find("_apply_pending_configs_inline()")
    # Find ALL `if assigned:` blocks. The main loop has two:
    #   1. The first is a log message ("got N assigned task(s)")
    #   2. The second is the actual task pool with ThreadPoolExecutor
    # We want the second one (the one that creates a pool of workers).
    # Distinguish by checking for the `concurrent.futures` import that
    # only the real pool block has.
    pool_block_matches = list(re.finditer(
        r"^(\s+)if assigned:", body, re.MULTILINE
    ))
    assert pool_block_matches, (
        "task pool `if assigned:` block not found in main daemon loop body. "
        "If you refactored the loop, the test may need updating — make sure "
        "the call ordering is preserved."
    )
    pool_block_idx = None
    for m in pool_block_matches:
        # Look ahead in the body for the concurrent.futures import
        following = body[m.start():m.start() + 500]
        if "concurrent.futures" in following:
            pool_block_idx = m.start()
            break
    assert pool_block_idx is not None, (
        f"could not find the actual task pool `if assigned:` block "
        f"(with ThreadPoolExecutor). Found {len(pool_block_matches)} "
        f"`if assigned:` lines, none followed by `concurrent.futures`. "
        f"The fix requires the actual pool block to be AFTER the config "
        f"apply call. Check that the loop structure is preserved."
    )
    assert pre_tick_call_idx >= 0, "apply call not found in daemon loop body"
    assert pre_tick_call_idx < pool_block_idx, (
        f"_apply_pending_configs_inline() is at offset {pre_tick_call_idx} "
        f"but the task pool `if assigned:` block is at {pool_block_idx}. "
        f"The v3.10.4 fix requires the config apply to run BEFORE the task "
        f"pool. If you've moved it back, you'll reintroduce the bug where "
        f"long-running tasks starve new profile's SOUL apply."
    )


def test_config_apply_not_in_old_post_pool_position():
    """The OLD call site (after the task pool) must be gone. The old
    position was: after the `for f in futures: f.result()` loop, before
    the zombie-sweep block. The new code uses a comment marker instead
    of a real call.
    """
    body = _read_main_daemon_loop_body()
    # Find the futures loop end (`f.result()` for-loop)
    futures_end_idx = body.rfind("f.result()")
    assert futures_end_idx > 0, "f.result() for-loop not found"
    # Look for `_apply_pending_configs_inline()` AFTER the futures loop
    after_futures = body[futures_end_idx:]
    assert not re.search(
        r"^(\s+)_apply_pending_configs_inline\(\)\s*$",
        after_futures,
        re.MULTILINE,
    ), (
        "Found `_apply_pending_configs_inline()` call AFTER the task pool "
        "futures loop. The v3.10.4 fix moved this call to the pre-tick "
        "position. If you're refactoring, don't restore the call here — "
        "see the v3.10.4 comment in the file for the bug history."
    )


def test_regression_comment_present():
    """The file must have a v3.10.4 comment + the repro project ID."""
    src = AGENT_CLI_PATH.read_text(encoding="utf-8")
    m = re.search(
        r"#\s*v3\.10\.4.*?(?:BUGFIX|Bug|fix).*?(?:\n\s*#[^\n]+){3,}",
        src,
    )
    assert m, (
        "v3.10.4 BUGFIX comment not found in agent_cli.py. The pre-tick "
        "config-apply ordering is non-obvious — a future refactor might "
        "move it back. Keep a 3+ line comment explaining why, with the "
        "project ID (proj-e05e89e9) for grep-ability."
    )
    assert re.search(r"proj-e05e89e9", src), (
        "v3.10.4 fix comment doesn't mention the repro project "
        "(proj-e05e89e9). Add it for grep-ability — future debuggers "
        "search by project ID."
    )
