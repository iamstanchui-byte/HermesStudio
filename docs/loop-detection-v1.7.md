# Loop Detection v1.7 — Multi-tool (2026-07-29)

## Background

v1.2 added loop detection for **shell commands only**. The wrapper
watched hermes's stdout for lines matching `┊ 💻 $ <command>` and
emitted a `tool_call` event with `tool="shell"`, `signature=SHA1(command)[:16]`.
The server's `compute_loop_status` then flagged a task as "looping"
if the same (tool, signature) pair fired 5+ times in 60s.

This caught one common failure mode (curl polling a broken URL, ls'ing
a missing dir, etc.) but **missed all the other tools hermes uses**.
An agent stuck reading the same file, patching the same file, or
running the same search repeatedly would still look "ok" to the
monitor. The user has reported exactly this kind of stuck-but-loop
failure.

## Goal

Detect loops for **every tool hermes uses**, with per-tool thresholds
tuned to the natural call rate of each tool (a real agent might
read 20 files in a row but only runs the same shell command a
couple of times).

## Hermes tool display format

All tool calls in hermes's transcript use the pattern:

```
┊ <emoji> <verb>     <args>  <duration>
```

Confirmed by reading `agent/display.py:_get_cute_tool_message`:

| Tool | Emoji | Verb | Args format | Internal name |
|---|---|---|---|---|
| terminal | 💻 | `$` | `<command>` | shell |
| web_search | 🔍 | `search` | `<query>` | web_search |
| web_extract | 📄 | `fetch` | `<domain>` | web_fetch |
| process | ⚙️ | `proc` | `<action> <session_id>` | process |
| read_file | 📖 | `read` | `<path>` | read |
| write_file | ✍️ | `write` | `<path>` | write |
| patch | 🔧 | `patch` | `<path>` | edit |
| search_files | 🔎 | `find`/`grep` | `<pattern>` | search |
| browser_navigate | 🌐 | `navigate` | `<domain>` | browser |
| browser_snapshot | 📸 | `snapshot` | `full`/`compact` | browser |
| browser_click | 👆 | `click` | `<ref>` | browser |
| browser_type | ⌨️ | `type` | `<text>` | browser |
| browser_press | ⌨️ | `press` | `<key>` | browser |
| browser_vision | 👁️ | `vision` | `<question>` | vision |
| skill | 📚 | `skill` | `<label>` | skill |
| session_search | 🔍 | `recall` | `<query>` | web_search |
| memory | 🧠 | `memory` | `<action> <target>: <content>` | memory |
| todo | 📋 | `plan` | `<count>/<total>` | plan |
| image_generate | 🎨 | `create` | `<prompt>` | image |
| text_to_speech | 🔊 | `speak` | `<text>` | tts |
| vision_analyze | 👁️ | `vision` | `<question>` | vision |
| send_message | 📨 | `send` | `<target>: <message>` | send |
| cronjob | ⏰ | `cron` | `<action> <name>` | cronjob |
| execute_code | 🐍 | `exec` | `<first line of code>` | exec |
| delegate_task | 🔀 | `delegate` | `<goal>` | delegate |
| (catch-all) | ⚡ | `<tool_name>` | `<preview>` | misc |

The duration suffix is either `0.5s` / `12s` (numeric) or `done`
(completion marker without timing). We strip the trailing duration
before signing so two calls with different durations but same args
count as the same loop.

## Implementation

### Wrapper-side (`agent_cli.py`)

Replace the single `_TOOL_CALL_PATTERN` with a list of specs:

```python
_TOOL_PATTERNS = [
    # (compiled_regex, tool_name, sig_group_idx)
    (re.compile(r"┊\s*💻\s+\$\s+(.+)"),                "shell",      1),
    (re.compile(r"┊\s*🔍\s+(?:search|recall)\s+(.+)"), "web_search", 1),
    (re.compile(r"┊\s*📄\s+fetch\s+(.+)"),              "web_fetch",  1),
    (re.compile(r"┊\s*⚙️\s+proc\s+(.+)"),                "process",    1),
    (re.compile(r"┊\s*📖\s+read\s+(.+)"),                "read",       1),
    (re.compile(r"┊\s*✍️\s+write\s+(.+)"),               "write",      1),
    (re.compile(r"┊\s*🔧\s+patch\s+(.+)"),               "edit",       1),
    (re.compile(r"┊\s*🔎\s+(?:find|grep)\s+(.+)"),      "search",     1),
    (re.compile(r"┊\s*🌐\s+navigate\s+(.+)"),            "browser",    1),
    (re.compile(r"┊\s*📸\s+snapshot\s+(.+)"),            "browser",    1),
    (re.compile(r"┊\s*👆\s+click\s+(.+)"),               "browser",    1),
    (re.compile(r"┊\s*⌨️\s+(?:type|press)\s+(.+)"),     "browser",    1),
    (re.compile(r"┊\s*👁️\s+vision\s+(.+)"),             "vision",     1),
    (re.compile(r"┊\s*📚\s+skill\s+(.+)"),               "skill",      1),
    (re.compile(r"┊\s*🧠\s+memory\s+(.+)"),              "memory",     1),
    (re.compile(r"┊\s*📋\s+plan\s+(.+)"),                "plan",       1),
    (re.compile(r"┊\s*🎨\s+create\s+(.+)"),              "image",      1),
    (re.compile(r"┊\s*🔊\s+speak\s+(.+)"),               "tts",        1),
    (re.compile(r"┊\s*📨\s+send\s+(.+)"),                "send",       1),
    (re.compile(r"┊\s*⏰\s+cron\s+(.+)"),                "cronjob",    1),
    (re.compile(r"┊\s*🐍\s+exec\s+(.+)"),                "exec",       1),
    (re.compile(r"┊\s*🔀\s+delegate\s+(.+)"),            "delegate",   1),
    # Catch-all: any `┊ ⚡ <tool>  ...` line
    (re.compile(r"┊\s*⚡\s+\S+\s+(.+)"),                "misc",       1),
]
```

The tail loop tries each spec in order; first match wins. The
captured group is the args portion, which we strip of trailing
duration and SHA1 to get the signature.

### Server-side (`loop_status.py`)

Per-tool thresholds (each captures "this is a real loop, not normal
behavior"):

```python
TOOL_LOOP_THRESHOLDS = {
    "shell":      5,   # same command 5x = real loop
    "edit":       5,   # same patch repeated = real loop
    "write":      5,   # same write repeated = real loop
    "search":     8,   # some search iteration is normal
    "web_search": 8,
    "web_fetch":  10,  # polling a slow page is normal
    "read":       15,  # reading multiple files is normal
    "browser":    10,
    "skill":      5,
    "process":    8,
    "memory":     12,  # memory writes are normal
    "plan":       15,  # todo planning is normal
    "image":      5,
    "tts":        5,
    "vision":     5,
    "send":       10,
    "cronjob":    5,
    "exec":       5,
    "delegate":   5,
    "misc":       5,
}
DEFAULT_LOOP_THRESHOLD = 5
```

`_detect_loop` now uses `TOOL_LOOP_THRESHOLDS.get(tool, DEFAULT_LOOP_THRESHOLD)`
instead of the old single `LOOP_MIN_REPEATS=5` constant.

The `LOOP_MIN_REPEATS` constant is kept as a fallback for tools
not in the dict.

### UI

No change. The reason text in the looping badge is:
```
looped {count} times: {tool}
```

With the new tool names ("edit", "search", "read" etc.) the UI
just shows the right thing. The "tool" field on `LoopStatus`
already carries the tool name from v1.2.

## False-positive guard

The biggest risk: a real agent might legitimately call `read_file`
on 10-20 different files in a row. The threshold of 15 (same
file, same path) is conservative — it would only fire on a real
loop where the agent re-reads the same file over and over.

If we get false-positive reports in production, the fix is to bump
the per-tool threshold (no schema change needed). The dict is
loaded from a constant so a hot-reload isn't possible; bumping
requires a server restart.

## Migration

No data migration. The existing `agent.tool_call` events continue
to work (the SQL GROUP BY on (tool, signature) is unchanged). Old
events with `tool="shell"` and the new `tool="edit"` etc. live
side by side; the new thresholds only apply to the new tool names.

The old `_TOOL_CALL_PATTERN` regex is removed (replaced by
`_TOOL_PATTERNS` list). The behavior is backward-compatible for
shell detection but expanded for the new tools.
