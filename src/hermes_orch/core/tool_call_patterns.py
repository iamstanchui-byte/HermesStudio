"""Hermes tool-call line patterns (v1.7, 2026-07-29).

Hermes's transcript uses one of these formats for every tool call:

    ┊ <emoji> <verb>     <args>  <duration>

The complete list of (emoji, verb, internal_name) tuples was
copied from `agent/display.py:_get_cute_tool_message` in the
hermes-agent source. When hermes adds a new tool, we add a row
here AND (optionally) bump its threshold in
`loop_status.TOOL_LOOP_THRESHOLDS`.

Two helpers exposed:
  - extract_tool_call(line) -> (tool_name, signature_body) | None
  - TOOL_PATTERNS: the regex list (useful for tests / inspection)

The wrapper uses `extract_tool_call` to detect tool calls as
hermes writes them to stdout. The server's compute_loop_status
then queries the resulting `agent.tool_call` events to flag
tasks that are looping on the same tool with the same args.
"""
from __future__ import annotations

import re
from typing import NamedTuple


class ToolPattern(NamedTuple):
    regex: re.Pattern[str]
    tool: str           # internal name used in audit_log + loop_status
    sig_group: int      # which capture group in regex holds the signature body


# v1.7 — 22 tool patterns + 1 catch-all.
# Order: most specific first. A line matches the first regex whose
# shape it fits. The catch-all `┊ ⚡ <tool>  ...` is last so any
# future hermes tool still produces a tool_call event.
TOOL_PATTERNS: list[ToolPattern] = [
    ToolPattern(re.compile(r"┊\s*💻\s+\$\s+(.+)"),                "shell",      1),
    ToolPattern(re.compile(r"┊\s*🔍\s+(?:search|recall)\s+(.+)"), "web_search", 1),
    ToolPattern(re.compile(r"┊\s*📄\s+fetch\s+(.+)"),              "web_fetch",  1),
    ToolPattern(re.compile(r"┊\s*⚙️\s+proc\s+(.+)"),                "process",    1),
    ToolPattern(re.compile(r"┊\s*📖\s+read\s+(.+)"),                "read",       1),
    ToolPattern(re.compile(r"┊\s*✍️\s+write\s+(.+)"),               "write",      1),
    ToolPattern(re.compile(r"┊\s*🔧\s+patch\s+(.+)"),               "edit",       1),
    ToolPattern(re.compile(r"┊\s*🔎\s+(?:find|grep)\s+(.+)"),      "search",     1),
    ToolPattern(re.compile(r"┊\s*🌐\s+navigate\s+(.+)"),            "browser",    1),
    ToolPattern(re.compile(r"┊\s*📸\s+snapshot\s+(.+)"),            "browser",    1),
    ToolPattern(re.compile(r"┊\s*👆\s+click\s+(.+)"),               "browser",    1),
    ToolPattern(re.compile(r"┊\s*⌨️\s+(?:type|press)\s+(.+)"),     "browser",    1),
    ToolPattern(re.compile(r"┊\s*👁️\s+vision\s+(.+)"),             "vision",     1),
    ToolPattern(re.compile(r"┊\s*📚\s+skill\s+(.+)"),               "skill",      1),
    ToolPattern(re.compile(r"┊\s*🧠\s+memory\s+(.+)"),              "memory",     1),
    ToolPattern(re.compile(r"┊\s*📋\s+plan\s+(.+)"),                "plan",       1),
    ToolPattern(re.compile(r"┊\s*🎨\s+create\s+(.+)"),              "image",      1),
    ToolPattern(re.compile(r"┊\s*🔊\s+speak\s+(.+)"),               "tts",        1),
    ToolPattern(re.compile(r"┊\s*📨\s+send\s+(.+)"),                "send",       1),
    ToolPattern(re.compile(r"┊\s*⏰\s+cron\s+(.+)"),                "cronjob",    1),
    ToolPattern(re.compile(r"┊\s*🐍\s+exec\s+(.+)"),                "exec",       1),
    ToolPattern(re.compile(r"┊\s*🔀\s+delegate\s+(.+)"),            "delegate",   1),
    # Catch-all: `┊ ⚡ <tool_name>  <preview>  <duration>`.
    # Future hermes tools fall here.
    ToolPattern(re.compile(r"┊\s*⚡\s+\S+\s+(.+)"),                "misc",       1),
]


# Trailing duration stripper. Matches the optional `<duration>`
# field at the end of every tool-call line. Examples:
#   "...  0.5s"     -> strip
#   "...  12s"      -> strip
#   "...  1.0s"     -> strip
#   "...  done"     -> strip
#   "..." (no dur)  -> no change
_DURATION_SUFFIX_RE = re.compile(r"\s+(?:\d+(?:\.\d+)?s|done)\s*$")


def strip_duration_suffix(args: str) -> str:
    """Remove the trailing duration from a captured args string.

    The signature should be stable across calls with the same
    inputs but different durations, so we strip the duration
    before hashing. Empty/whitespace-only results are returned
    as the empty string.
    """
    return _DURATION_SUFFIX_RE.sub("", args).strip()


def extract_tool_call(line: str) -> tuple[str, str] | None:
    """Match a hermes tool-call line and return (tool_name, signature_body).

    Returns None if the line is not a recognized tool call (e.g.
    a chat message, a banner line, a blank line, or a "preparing"
    / "completed" status message that hermes emits between calls).

    The signature body is the args portion with the trailing
    duration stripped. The caller is expected to SHA1 this for
    storage in the audit_log.
    """
    for pat in TOOL_PATTERNS:
        m = pat.regex.search(line)
        if m:
            raw = m.group(pat.sig_group)
            body = strip_duration_suffix(raw)
            if not body:
                # Matched a tool but args is empty (e.g. `┊ 💻 $  done`).
                # Not a real call; skip.
                return None
            return (pat.tool, body)
    return None
