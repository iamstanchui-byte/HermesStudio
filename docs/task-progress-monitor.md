# Task Progress Monitor — Design (DRAFT for review, 2026-07-29)

## Status

**DRAFT for user review.** No code yet. After review and sign-off,
implementation in ~3 days.

## 1. Problem

When a task is `running`, the operator has no visibility into
*what the agent is doing right now*. They only see:
- Status icon (running)
- Start time / duration (e.g. "3m 12s")
- "no events for 12s" (raw last-event timestamp)

This makes it hard to tell:
- Is the agent making progress, or **stuck in a loop** (LLM calling
  the same tool over and over with no result)?
- Is it slow because the tool is slow, or because the agent is
  confused?
- Should the operator **intervene** (pause / cancel)?

The 2026-07-29 incident that motivated this: an LLM agent ran
for 12+ minutes calling `fetch_data` 7 times in a row, never
producing a result. Operator only found out after checking
manually. Tokens wasted: ~30k. Time wasted: 12m.

## 2. Goals (success criteria)

- Operator can tell **at a glance** if a running task is making
  progress, slow, stuck, or looping — within 30s of the problem
  starting.
- Operator can **intervene** in 1 click: pause or cancel a stuck
  task without opening SSH or a terminal.
- Loop detection is **server-side** (computed from the existing
  audit log) so it works without frontend changes for non-Dashboard
  clients (CLI, scripts).
- **No new infra** (no SSE / WebSocket). Polling is enough for
  the use case. (Could upgrade to SSE later if needed.)

## 3. Non-goals (out of scope for v1)

- Real-time LLM token streaming in the dashboard (separate feature
  for the chatbox; see docs/chatbox-plan-editor.md §3 Task 3).
- Predicting failures before they happen (just detection).
- Auto-pause when loop detected (operator must click; the
  human-in-loop is intentional).
- Loop detection across tasks (e.g. two tasks looping together).
  Per-task only for v1.

## 4. UX flow

### 4.1 Primary user stories

1. **"I want to know if my task is doing anything"**
   → Open project page. Each running task row shows a colored
   dot (🟢🟡🔴⚠️). Glance → done.

2. **"I think a task is stuck — let me see what it's doing"**
   → Click the task row. Inline expand shows the event timeline
   (last 20 events) with timestamps and tool names. If looping
   detected, an orange banner explains why.

3. **"I want to see all running tasks across the project"**
   → Click "📡 Live" button in the project header. Right side
   panel slides in showing a compact list of all running tasks
   with their loop-status badges.

4. **"I'm sure this task is looping — kill it"**
   → From the expanded view or live panel, click "Cancel".
   Confirmation dialog. Server sends cancel command to agent.
   Task moves to `cancelled` state.

### 4.2 State machine for the loop-status badge

```
                  [ok] <-> [slow] <-> [stuck]
                    |       |          |
                    v       v          v
                  [looping] ←--- (oscillates with ok during long runs)
```

**Transitions** (computed every poll, server-side):

| From | To | Trigger |
|---|---|---|
| any | `ok` | last event < 30s ago, no loop pattern |
| ok | `slow` | no event for 30-120s |
| slow | `ok` | new event arrives |
| slow | `stuck` | no event for > 120s |
| stuck | `slow` | new event arrives |
| ok / slow | `looping` | last 3 events are same tool call |
| looping | `ok` | new event is different tool / has a result |
| looping | `looping` | new event is same tool (counter++) |

The badge should be **sticky** within a poll cycle (don't
oscillate visibly as new events arrive). Server picks ONE status
per poll response.

## 5. Wireframes

### 5.1 Project page — task list (current, with new badges)

```
┌─────────────────────────────────────────────────────────────────┐
│ Project: chat-apply-test                                          │
│ Goal: monitor HK weather every hour, post to Slack on rain       │
│ Plan: 5 steps (rendered)                                         │
├─────────────────────────────────────────────────────────────────┤
│ Tasks                                                            │
│                                                                 │
│  Status   ID          Role    Start    Duration   Actions         │
│  ──────   ──────────  ─────   ──────   ────────   ───────         │
│  ● done   t-001      super   12:00    0.4s       [view]          │
│  ● done   t-002      super   12:00    1.2s       [view]          │
│  🟢 done  t-003      super   12:01    0.8s       [view]          │
│  🟡 run   t-004      super   12:05    3m 12s     [pause][cancel]│ ← running, 30s-2m
│  🟢 run   t-005      super   12:08    1m 45s     [view]          │ ← running, < 30s
│  ⚠ loop  t-006      super   12:09    12m 03s    [pause][cancel]│ ← LOOPING!
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

Badge legend:
- ● `done` (green)
- ● `running, ok` (green dot) — events flowing
- 🟡 `running, slow` (yellow) — 30s-2m no event
- 🔴 `running, stuck` (red) — > 2m no event
- ⚠️ `running, looping` (orange) — same tool 3+ times

### 5.2 Task row — inline expand (click to view details)

Before (collapsed):
```
│  🟡 run   t-004      super   12:05    3m 12s     [pause][cancel]│
```

After (expanded, click row to expand):
```
│  🟡 run   t-004      super   12:05    3m 12s     [pause][cancel]│
│    ┌─────────────────────────────────────────────────────────┐
│    │ t-004  Last events (last 20)                [view raw]   │
│    │ ─────────────────────────────────────────────────────── │
│    │ 12:05:18  start  → dispatched to super                 │
│    │ 12:05:19  tool: read_file     → projects/.../plan.md    │
│    │ 12:05:21  tool: read_file     → projects/.../facts.md   │
│    │ 12:05:22  tool: list_files    → projects/.../           │
│    │ 12:05:25  tool: read_file     → projects/.../notes.md  │
│    │ 12:05:28  tool: read_file     → projects/.../notes.md  │ ← suspicious: read same file twice
│    │ 12:05:31  tool: read_file     → projects/.../notes.md  │
│    │ 12:05:35  ⚠  last event 14s ago                          │
│    │ ─────────────────────────────────────────────────────── │
│    │ Status: 🟡 slow — last event 14s ago, no progress        │
│    │ Tools called (last 3): read_file (×3)                    │
│    │ [pause] [cancel] [open in agent]                        │
│    └─────────────────────────────────────────────────────────┘
```

For a true loop:
```
│  ⚠ loop  t-006      super   12:09    12m 03s    [pause][cancel]│
│    ┌─────────────────────────────────────────────────────────┐
│    │ ⚠ LOOPING DETECTED (2026-07-29 design)                   │
│    │                                                            │
│    │ Same tool 'fetch_data' called 7 times in the last 3m.    │
│    │ Each call returns 0 results. Agent may be confused.       │
│    │                                                            │
│    │ Last 10 events:                                             │
│    │   12:18:14  tool: fetch_data  (no result)                 │
│    │   12:18:10  tool: fetch_data  (no result)                 │
│    │   12:18:06  tool: fetch_data  (no result)                 │
│    │   12:18:02  tool: fetch_data  (no result)                 │
│    │   12:17:58  tool: fetch_data  (no result)                 │
│    │   12:17:54  tool: fetch_data  (no result)                 │
│    │   12:17:50  tool: fetch_data  (no result)                 │
│    │   12:17:46  start   (12m ago)                              │
│    │                                                            │
│    │ [pause] [cancel] [view raw log]                            │
│    └─────────────────────────────────────────────────────────┘
```

### 5.3 Live tasks side panel (right side, like the chatbox)

Triggered by clicking the "📡 Live" button in the project header.

```
┌──────────────────────────┐
│ 📡 Live tasks        ✕   │
├──────────────────────────┤
│ 2 running                │
├──────────────────────────┤
│ 🟡 t-004  super          │
│   slow · 3m 12s          │
│   [expand]               │
│                          │
│ ⚠ t-006  super           │
│   LOOP · 12m 03s         │
│   [expand] [cancel]      │
│                          │
├──────────────────────────┤
│ Auto-refresh: 5s         │
└──────────────────────────┘
```

Click a task → opens the same inline expand as in the project
page (or scrolls to it).

## 6. Loop detection algorithm (pseudocode)

Computed server-side per task, per poll. Stored in a per-task
in-memory cache (cleared on task completion).

```python
# Constants (tunable; conservative defaults)
SLOW_THRESHOLD_S = 30       # no event for 30s = slow
STUCK_THRESHOLD_S = 120     # no event for 2m = stuck
LOOP_WINDOW = 5             # look at last 5 events
LOOP_MIN_REPEAT = 3         # need 3+ same tool to call it a loop

def compute_loop_status(task_id: str, now: float) -> dict:
    recent = audit_log.where(task_id=task_id).order_by(ts desc).limit(LOOP_WINDOW)
    if not recent:
        return {"status": "unknown", "reason": "no events yet"}
    
    last_event = recent[0]
    last_event_age = now - last_event.ts
    task_started = task.started_at
    duration = now - task_started
    
    # 1. Stuck: no event for > 2 min
    if last_event_age > STUCK_THRESHOLD_S:
        return {
            "status": "stuck",
            "reason": f"no events for {int(last_event_age)}s",
            "duration_s": int(duration),
            "last_event_age_s": int(last_event_age),
        }
    
    # 2. Loop: same tool 3+ times consecutively (most recent)
    recent_tool_calls = [e for e in recent if e.kind == "tool_call"]
    if len(recent_tool_calls) >= LOOP_MIN_REPEAT:
        last_n_tools = [e.tool for e in recent_tool_calls[:LOOP_MIN_REPEAT]]
        if len(set(last_n_tools)) == 1 and last_n_tools[0]:
            return {
                "status": "looping",
                "reason": f"tool '{last_n_tools[0]}' called {LOOP_MIN_REPEAT}+ times",
                "tool": last_n_tools[0],
                "repeat_count": sum(
                    1 for e in recent_tool_calls
                    if e.tool == last_n_tools[0]
                ),
            }
    
    # 3. Slow: no event for 30-120s
    if last_event_age > SLOW_THRESHOLD_S:
        return {
            "status": "slow",
            "reason": f"no event for {int(last_event_age)}s",
            "duration_s": int(duration),
        }
    
    # 4. Ok
    return {
        "status": "ok",
        "reason": "events flowing",
        "duration_s": int(duration),
    }
```

The `recent_tool_calls[:LOOP_MIN_REPEAT]` slice takes the MOST
RECENT 3 tool calls (not the first 3). This catches "agent is
currently looping" rather than "agent looped earlier but
recovered".

## 7. API contract

### 7.1 GET /api/projects/{id}/tasks/{task_id}/status

Returns the live status of a single task. Designed to be polled
every 5s by the dashboard.

**Response** (200):
```json
{
  "task_id": "t-006",
  "project_id": "proj-abc",
  "status": "running",                    // running | done | failed | cancelled
  "duration_s": 723,
  "last_event": {
    "ts": "2026-07-29T12:18:14Z",
    "kind": "tool_call",
    "tool": "fetch_data",
    "summary": "fetch_data (no result)"
  },
  "last_event_age_s": 47,
  "loop_status": "looping",                // ok | slow | stuck | looping | unknown
  "loop_reason": "tool 'fetch_data' called 3+ times",
  "tools_recent": ["fetch_data", "fetch_data", "fetch_data"],
  "events": [                              // last 10
    {"ts": "...", "kind": "tool_call", "tool": "fetch_data", "summary": "..."},
    {"ts": "...", "kind": "tool_call", "tool": "fetch_data", "summary": "..."},
    ...
  ]
}
```

**Response** (404 if task not found):
```json
{"detail": "Task not found: t-006"}
```

### 7.2 GET /api/projects/{id}/tasks/running

Returns a compact list of all currently-running tasks. For the
side panel.

**Response** (200):
```json
{
  "running": [
    {
      "task_id": "t-004",
      "agent_role": "super",
      "started_at": "2026-07-29T12:05:18Z",
      "duration_s": 192,
      "last_event_age_s": 14,
      "loop_status": "slow",
      "loop_reason": "no event for 14s"
    },
    {
      "task_id": "t-006",
      "agent_role": "super",
      "started_at": "2026-07-29T12:09:11Z",
      "duration_s": 540,
      "last_event_age_s": 47,
      "loop_status": "looping",
      "loop_reason": "tool 'fetch_data' called 3+ times"
    }
  ],
  "count": 2,
  "computed_at": "2026-07-29T12:11:30Z"
}
```

### 7.3 POST /api/projects/{id}/tasks/{task_id}/cancel

Cancel a running task. Sends a cancel command to the agent
(via the existing task cancellation mechanism — see
`/api/tasks/{id}/cancel` already in `tasks.py`).

**Request body**: `{}`

**Response** (200):
```json
{
  "task_id": "t-006",
  "status": "cancelled",
  "previous_status": "running",
  "cancelled_at": "2026-07-29T12:12:00Z"
}
```

**Response** (409 if not running):
```json
{"detail": "Cannot cancel task in state 'done'"}
```

### 7.4 POST /api/projects/{id}/tasks/{task_id}/pause (v1.1, not v1)

Pause + later resume. Skipped for v1 to ship faster; v1 only has
cancel. Documented here so we don't forget.

## 8. UI changes (frontend)

### 8.1 Existing templates to modify

- `src/hermes_orch/templates/project.html` — task list table
  (add loop badge column, inline expand, "📡 Live" button in
  header)
- `src/hermes_orch/templates/base.html` — load new JS file
  `task_progress.js`

### 8.2 New files

- `src/hermes_orch/static/task_progress.js` — vanilla JS for
  polling + inline expand + side panel (no framework; matches
  the existing `chatbox.js` style).
- `src/hermes_orch/templates/_live_tasks_panel.html` — Jinja
  partial for the side panel (like `_workflow_actions.html`).

### 8.3 CSS additions

Append to `base.html` styles:
```css
.task-row[data-loop-status="looping"] {
    background-color: rgb(254 243 199);  /* amber-100 */
    border-left: 3px solid rgb(217 119 6);
}
.task-row[data-loop-status="stuck"] {
    background-color: rgb(254 226 226);  /* red-100 */
    border-left: 3px solid rgb(220 38 38);
}
.loop-pulse {
    display: inline-block;
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    animation: loop-pulse 2s ease-in-out infinite;
}
@keyframes loop-pulse {
    0%, 100% { opacity: 0.6; }
    50% { opacity: 1; }
}
```

## 9. Tests

### 9.1 Unit tests (server-side)

- `test_loop_status.py` — pure function tests:
  - empty events → `unknown`
  - recent event (< 30s old) → `ok`
  - 30-120s gap → `slow`
  - > 120s gap → `stuck`
  - last 3 events same tool → `looping`
  - 2 of last 3 same tool → `ok`
  - 3 of last 4 same tool, last one different → `ok` (only most
    recent 3 count)
  - loop after 4 ok events → `looping`
  - recovery from loop (different tool) → `ok`

### 9.2 Integration tests

- `test_task_status_endpoint.py`:
  - 200 with proper structure for a running task
  - 404 for nonexistent task
  - 200 with empty events array for freshly-started task
- `test_running_tasks_endpoint.py`:
  - returns only running tasks (filters done / failed / cancelled)
  - correct loop_status for various event patterns
- `test_cancel_endpoint.py`:
  - 200 cancels a running task
  - 409 if task is done
  - 404 if task not found
  - audit log records the cancel

### 9.3 E2E (manual smoke)

- Run a task that intentionally loops (mock LLM that returns the
  same tool call 5x)
- Verify dashboard shows ⚠️ looping badge within 5s
- Verify clicking "Cancel" stops the task

## 10. Implementation order

| # | Task | Effort | Risk |
|---|---|---|---|
| 1 | `compute_loop_status` function + unit tests | 0.5 day | low |
| 2 | GET /tasks/{id}/status + /tasks/running endpoints + tests | 1 day | low |
| 3 | POST /tasks/{id}/cancel endpoint + tests | 0.5 day | med (agent comm) |
| 4 | Frontend: inline expand + loop badge + side panel | 1 day | low |
| 5 | Manual smoke test on real running task | 0.5 day | low |
| **Total** | | **3.5 days** | |

## 11. Open questions for the user

Please confirm before implementation:

1. **Thresholds**: 30s for `slow`, 120s for `stuck`. Are these
   right? Or should I make them config-driven (per project / per
   task)?

2. **Cancel mechanism**: v1 uses the existing
   `POST /api/tasks/{id}/cancel` (in `tasks.py`). Is this the
   right behavior, or do you want a "softer" cancel that asks
   the agent to gracefully stop (e.g. set a "cancel_requested"
   flag the agent checks between tool calls)?

3. **Pause (v1.1)**: skip for v1, OK?

4. **Side panel location**: right (same as chatbox) or left
   (would conflict with the existing project layout)?

5. **Polling interval**: 5s default. Configurable in user
   settings? Or hardcode?

6. **Loop definition**: I picked "last 3 events are same tool".
   Alternative: "5 events in 60s with <2 distinct tools". Which
   is more meaningful?

7. **Visual treatment of `stuck` vs `looping`**: I picked
   red (stuck) and orange (looping). Different enough? Or both
   should be red?
