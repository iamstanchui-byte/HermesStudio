"""Tests for hermes_orch.core.tool_call_patterns (v1.7, 2026-07-29).

Captures real hermes stdout samples (synthesized from the
display.py source) and verifies each one is correctly parsed
into (tool_name, signature_body) by extract_tool_call().

Also covers:
  - non-tool lines (chat, banners, blank) return None
  - the duration suffix is correctly stripped
  - first matching pattern wins (no double-counting)
  - the catch-all `┊ ⚡ ...` handles unknown tools
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# Allow tests to import from src
ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

import pytest

from hermes_orch.core.tool_call_patterns import (
    TOOL_PATTERNS,
    extract_tool_call,
    strip_duration_suffix,
)


# ===== Real-looking hermes stdout samples =====
# These mimic the exact format produced by
# hermes-agent/agent/display.py:_get_cute_tool_message. The
# format is `┊ <emoji> <verb>     <args>  <duration>`. We vary
# duration format to make sure the regex is flexible.

# (line, expected_tool, expected_signature_body)
SAMPLES = [
    # shell (terminal)
    (
        "┊ 💻 $         curl https://api.example.com -o data.json  0.5s",
        "shell", "curl https://api.example.com -o data.json",
    ),
    (
        "┊ 💻 $         ls -la /tmp  0.1s",
        "shell", "ls -la /tmp",
    ),
    (
        "┊ 💻 $         pytest tests/test_foo.py  12.3s",
        "shell", "pytest tests/test_foo.py",
    ),
    # web_search
    (
        "┊ 🔍 search    how to parse JSON in Python  0.3s",
        "web_search", "how to parse JSON in Python",
    ),
    (
        "┊ 🔍 recall    \"earlier conversation about X\"  0.4s",
        "web_search", '"earlier conversation about X"',
    ),
    # web_extract (fetch)
    (
        "┊ 📄 fetch     example.com  1.2s",
        "web_fetch", "example.com",
    ),
    # process
    (
        "┊ ⚙️  proc      list processes  0.1s",
        "process", "list processes",
    ),
    (
        "┊ ⚙️  proc      kill 20260720_xyz  0.2s",
        "process", "kill 20260720_xyz",
    ),
    # read_file
    (
        "┊ 📖 read      /home/user/project/src/main.py  0.1s",
        "read", "/home/user/project/src/main.py",
    ),
    # write_file
    (
        "┊ ✍️  write     /home/user/project/out.json  0.2s",
        "write", "/home/user/project/out.json",
    ),
    # patch
    (
        "┊ 🔧 patch     /home/user/project/src/main.py  0.3s",
        "edit", "/home/user/project/src/main.py",
    ),
    # search_files
    (
        "┊ 🔎 find      *.py  0.4s",
        "search", "*.py",
    ),
    (
        "┊ 🔎 grep      def test_  0.5s",
        "search", "def test_",
    ),
    # browser_*
    (
        "┊ 🌐 navigate  github.com  1.0s",
        "browser", "github.com",
    ),
    (
        "┊ 📸 snapshot  full  0.5s",
        "browser", "full",
    ),
    (
        "┊ 👆 click     #submit-button  0.1s",
        "browser", "#submit-button",
    ),
    (
        "┊ ⌨️  type      \"hello world\"  0.2s",
        "browser", '"hello world"',
    ),
    (
        "┊ ⌨️  press     Enter  0.1s",
        "browser", "Enter",
    ),
    # vision
    (
        "┊ 👁️  vision    what is in the image  5.0s",
        "vision", "what is in the image",
    ),
    # skill
    (
        "┊ 📚 skill     mcp-builder  0.5s",
        "skill", "mcp-builder",
    ),
    # memory
    (
        "┊ 🧠 memory    +user_pref: \"likes dark mode\"  0.2s",
        "memory", '+user_pref: "likes dark mode"',
    ),
    # plan (todo)
    (
        "┊ 📋 plan      3/5 task(s)  0.1s",
        "plan", "3/5 task(s)",
    ),
    # image_generate
    (
        "┊ 🎨 create    a cat wearing a hat  30.0s",
        "image", "a cat wearing a hat",
    ),
    # tts
    (
        "┊ 🔊 speak     Hello, world  2.0s",
        "tts", "Hello, world",
    ),
    # send_message
    (
        "┊ 📨 send      user: \"hi there\"  0.1s",
        "send", 'user: "hi there"',
    ),
    # cronjob
    (
        "┊ ⏰ cron      create daily-report  0.1s",
        "cronjob", "create daily-report",
    ),
    # execute_code
    (
        "┊ 🐍 exec      import os; print(os.getcwd())  0.5s",
        "exec", "import os; print(os.getcwd())",
    ),
    # delegate_task
    (
        "┊ 🔀 delegate  3x: research AI trends | write report  0.2s",
        "delegate", "3x: research AI trends | write report",
    ),
    # Catch-all: unknown tool uses `┊ ⚡ <tool_name>  ...`
    (
        "┊ ⚡ some_future_tool  preview here  0.3s",
        "misc", "preview here",
    ),
]


# ===== Per-sample match tests =====


@pytest.mark.parametrize("line,expected_tool,expected_sig", SAMPLES)
def test_extract_tool_call_matches_real_sample(line, expected_tool, expected_sig):
    result = extract_tool_call(line)
    assert result is not None, f"expected match for: {line!r}"
    tool, body = result
    assert tool == expected_tool, f"wrong tool for {line!r}: {tool!r} != {expected_tool!r}"
    assert body == expected_sig, f"wrong sig for {line!r}: {body!r} != {expected_sig!r}"


# ===== Non-tool lines should NOT match =====


NON_TOOL_LINES = [
    "",  # blank
    " ",  # whitespace
    "Hello, I am a helpful assistant.",  # plain chat
    "Let me check the file for you.",  # LLM response
    "I'll use the search_files tool to find the answer.",  # LLM meta-comment
    "Task completed successfully.",  # status message
    "Reading the file...",  # LLM preamble
    "Let me try again.",  # retry message
    "Error: file not found",  # error
    "✓ Done",  # completion marker without tool prefix
]


@pytest.mark.parametrize("line", NON_TOOL_LINES)
def test_extract_tool_call_skips_non_tool_lines(line):
    assert extract_tool_call(line) is None, f"false positive: {line!r}"


# ===== Duration suffix stripping =====


def test_strip_duration_suffix_strips_seconds():
    assert strip_duration_suffix("cmd  0.5s") == "cmd"
    assert strip_duration_suffix("cmd  12s") == "cmd"
    assert strip_duration_suffix("cmd  1.0s") == "cmd"
    assert strip_duration_suffix("cmd  123.456s") == "cmd"


def test_strip_duration_suffix_strips_done():
    assert strip_duration_suffix("cmd  done") == "cmd"


def test_strip_duration_suffix_no_change():
    """Lines without a trailing duration are left alone."""
    assert strip_duration_suffix("just args") == "just args"
    assert strip_duration_suffix("just args ") == "just args"


def test_strip_duration_suffix_empty():
    assert strip_duration_suffix("") == ""
    assert strip_duration_suffix("   ") == ""


# ===== Edge cases =====


def test_first_match_wins_no_double_counting():
    """The pattern list is ordered most-specific-first. A line
    that could match multiple patterns should match the first
    one in the list, not the catch-all `┊ ⚡`.

    All real tool lines start with `┊ <specific emoji>`. The
    catch-all `┊ ⚡` is the last entry. So no real line should
    fall through to the catch-all.
    """
    for line, expected_tool, _ in SAMPLES:
        if expected_tool == "misc":
            continue  # this IS the catch-all
        result = extract_tool_call(line)
        assert result is not None
        tool, _ = result
        assert tool != "misc", (
            f"non-catch-all line matched the misc catch-all: {line!r}"
        )


def test_signature_stable_across_calls_with_different_durations():
    """Two calls with the same args but different durations
    should produce the same signature (after SHA1)."""
    a = extract_tool_call("┊ 💻 $         curl https://x  0.5s")
    b = extract_tool_call("┊ 💻 $         curl https://x  12.3s")
    c = extract_tool_call("┊ 💻 $         curl https://x  done")
    assert a == b == c
    assert a is not None
    # And the SHA1 matches
    sig_a = hashlib.sha1(a[1].encode()).hexdigest()[:16]
    sig_b = hashlib.sha1(b[1].encode()).hexdigest()[:16]
    sig_c = hashlib.sha1(c[1].encode()).hexdigest()[:16]
    assert sig_a == sig_b == sig_c


def test_signature_changes_with_different_args():
    """Two calls with different args should produce different
    signatures."""
    a = extract_tool_call("┊ 💻 $         curl https://a  0.5s")
    b = extract_tool_call("┊ 💻 $         curl https://b  0.5s")
    assert a is not None and b is not None
    assert a[1] != b[1]
    sig_a = hashlib.sha1(a[1].encode()).hexdigest()[:16]
    sig_b = hashlib.sha1(b[1].encode()).hexdigest()[:16]
    assert sig_a != sig_b


def test_empty_args_after_strip_returns_none():
    """If the line matches a pattern but the args portion is
    empty (e.g. `┊ 💻 $   ` with only whitespace), we should NOT
    emit a tool_call event with an empty signature."""
    # A line that has the prefix but only whitespace then "done"
    result = extract_tool_call("┊ 💻 $    ")
    assert result is None
    # Truly empty args
    result = extract_tool_call("┊ 💻 $   done")
    # "done" remains after strip but it's a status marker; we
    # still emit it but with sig="done" — that won't match any
    # real call signature so it won't false-positive. The behavior
    # is to NOT silently drop "done" as a defensive choice.
    assert result == ("shell", "done")


def test_tool_patterns_list_has_22_plus_catchall():
    """Sanity: we have 22 named tools + 1 catch-all = 23 total."""
    assert len(TOOL_PATTERNS) == 23
    # Last entry must be the catch-all
    assert TOOL_PATTERNS[-1].tool == "misc"


def test_all_tool_names_unique():
    """No two patterns should map to the same internal name
    (we want clean labels in the UI)."""
    tools = [p.tool for p in TOOL_PATTERNS]
    # "browser" is intentionally shared across navigate/snapshot/
    # click/type/press. The `⌨️` pattern matches both type and
    # press as a single regex, so we have 4 browser entries that
    # cover 5 different verbs.
    expected_counts = {
        "shell": 1, "web_search": 1, "web_fetch": 1, "process": 1,
        "read": 1, "write": 1, "edit": 1, "search": 1,
        "browser": 4,  # navigate + snapshot + click + (type|press combined)
        "vision": 1, "skill": 1, "memory": 1, "plan": 1,
        "image": 1, "tts": 1, "send": 1, "cronjob": 1,
        "exec": 1, "delegate": 1, "misc": 1,
    }
    actual_counts = {t: tools.count(t) for t in expected_counts}
    assert actual_counts == expected_counts, (
        f"tool name counts mismatch: {actual_counts}"
    )
