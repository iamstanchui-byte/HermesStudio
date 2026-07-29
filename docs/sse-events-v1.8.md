# Real-time SSE event stream (v1.8, 2026-07-29)

## Why

The Task Progress Monitor used to refresh its state by polling
`/api/projects/{id}/tasks/state` every 5 seconds. This works but has
two real costs:

1. **Wasted requests** — most polls return the same data (no change
   since the last tick). With 5s intervals and a typical project
   having 3-5 tasks, that's 12 req/min/project just to show "nothing
   changed" most of the time.
2. **Perceived lag** — when an agent finishes a task, the dashboard
   stays visually "running" for up to 5 seconds before the next poll
   catches the state transition. For a tool that watches 1-10
   running tasks in parallel, this lag adds up.

v1.8 swaps the polling for a long-lived SSE connection. The server
pushes events as they happen; the browser updates the UI in <100ms.

## Wire format

```
GET /api/projects/{id}/events
→ 200 OK
   Content-Type: text/event-stream; charset=utf-8
   Cache-Control: no-cache, no-transform
   X-Accel-Buffering: no

event: snapshot
id: 0
data: {"project_id": "proj-X", "tasks": [...], "count": N}

event: task.state_changed
data: {"task_id": "t-1", "status": "running"}

event: output.chunk
data: {"task_id": "t-1", "seq": 42, "stream": "stdout", "text": "..."}

: keepalive

```

### Event types

| Event                | Trigger                                  | Payload fields                                    |
|----------------------|------------------------------------------|---------------------------------------------------|
| `snapshot`           | Sent once on connect                     | `project_id, tasks[], count`                      |
| `task.state_changed` | Task transitions (start/result/cancel)   | `task_id, status, agent_id?`                      |
| `output.chunk`       | Wrapper posts a stdout/stderr chunk      | `task_id, seq, stream, text, id`                  |
| `tool.call`          | Wrapper posts a tool invocation          | `task_id, tool, signature, id`                    |
| `keepalive`          | Every 30s of inactivity (SSE comment)    | (no data; `: keepalive` is the SSE comment line)  |

The `snapshot` event has `id: 0`. Live events currently have no
`id` (deliberate — see "Trade-offs" below).

### Per-project scoping

The endpoint URL includes the project_id, and the server filters
publishes by project_id. Two browser tabs open on different
projects each have their own connection; an event on project A
never reaches a stream for project B.

## Architecture

```
┌─────────────────┐  publish_event  ┌────────────────┐  SSE format  ┌──────────────┐
│ /output-chunk   │ ───────────────→ │ core/sse.py    │ ───────────→ │ Browser tab  │
│ /tool-call      │                  │  _subscribers  │              │ (EventSource)│
│ /tasks/start    │                  │   dict[pid,    │              │              │
│ /tasks/result   │                  │     list[Queue]│              │ task_progress│
│ /tasks/cancel   │                  │                │              │     .js      │
│  (project-scope)│                  └────────────────┘              └──────────────┘
└─────────────────┘                         ▲
                                            │ subscribe()
                                            │ (async ctx mgr)
┌─────────────────┐                  ┌──────┴──────────┐
│ Browser opens   │  HTTP GET        │ /events stream  │
│ EventSource(url)│ ───────────────→ │  (per project)  │
└─────────────────┘                  └─────────────────┘
```

`src/hermes_orch/core/sse.py` is the in-process pub/sub. Per-project
subscriber lists, each subscriber holds a bounded `asyncio.Queue`
(maxsize=100). On overflow, the event is dropped for that slow
subscriber — they reconnect and pull a fresh snapshot.

## Client behavior

`src/hermes_orch/static/task_events.js` (new) defines
`window.orch.TaskEventStream`, a thin wrapper around `EventSource`:

- `new TaskEventStream(projectId, handlers)` — handlers is
  `{onSnapshot, onTaskStateChanged, onOutputChunk, onToolCall,
   onReconcile, onConnected, onDisconnected}`.
- `.start()` — opens the EventSource, also kicks off a 30s
  reconcile poll as a safety net (calls `onReconcile`).
- `.stop()` — closes the connection and cancels the reconcile.

`task_progress.js` wires the handlers in `_openEventStream()`:

- `onSnapshot(data)` — populates `runningCache` with
  `data.tasks`, re-renders the side panel + every row badge. This
  replaces the first `/tasks/state` fetch (one less HTTP call on
  page load).
- `onTaskStateChanged` / `onToolCall` — trigger `_pollOnce()`.
  Loop status is server-computed; re-fetching keeps the badge in
  sync with the server's view.
- `onOutputChunk(data)` — appends to the active streamer for that
  task (if the user has the inline expand panel open). Bumps
  `s.since` so the 2s poller doesn't re-fetch the same chunk.
- `onReconcile()` — just calls `_pollOnce()`. 30s cadence; the
  safety net for events the browser missed (background tab, network
  blip, server restart).
- `onConnected` / `onDisconnected` — no-ops in production; could
  drive a "live / reconnecting" indicator in the UI.

## Why not WebSockets?

SSE is the right tool for this:
- One-way (server → browser) is exactly what the dashboard needs
  (commands go via the existing REST endpoints).
- Works over plain HTTP/1.1 — no upgrade handshake, no reverse-proxy
  configuration, no `ws://` URL handling.
- Browser `EventSource` auto-reconnects with exponential backoff.
- `text/event-stream` is debuggable with `curl`.

WebSockets are overkill here. They'd add complexity (upgrade
handshake, framing protocol) without any feature we need.

## Trade-offs

- **No `id` on live events** (snapshot has `id: 0`). This means the
  browser can't use `Last-Event-ID` to resume after a missed
  connection — the 30s reconcile poll is the resync mechanism.
  Adding per-event sequence IDs is a v2.x concern; we want to
  measure real usage patterns first.
- **In-process bus only.** Works for the current single-process
  uvicorn deployment. If we ever go multi-worker or multi-host,
  swap `core/sse.py` to use Redis pub/sub (the API would stay
  the same).
- **No client-side auth on the SSE endpoint.** Like other
  dashboard reads, the URL is the capability (project_id is
  effectively a secret). For productize, add session-cookie auth.
- **Slow subscribers get events dropped** (queue maxsize=100). The
  reconcile poll catches them up. This is the right tradeoff vs.
  blocking the publisher or growing memory unbounded.

## Performance

- 5s polling (v1.0-v1.7) → 30s reconcile + SSE push (v1.8):
  - **Idle project**: 12 req/min → 2 req/min (6x lower load).
  - **Active project (churning)**: roughly equivalent — SSE
    delivers state changes without polling, and the 30s tick
    still runs as a safety net.
  - **UI lag on state change**: up to 5s → <100ms (50x faster).

## Tests

- `tests/test_sse_bus.py` (14 unit tests): publish_event,
  subscribe lifecycle, per-project isolation, slow subscribers,
  exception safety, reset_for_tests.
- `tests/test_sse_endpoint.py` (10 integration tests): snapshot
  shape, live event delivery (via real `/cancel` to trigger
  server-side publish), 404 on unknown project, multiple
  connections, per-project wire isolation, disconnect safety,
  multiple events in order, snapshot `id: 0` invariant.

## Files

- `src/hermes_orch/core/sse.py` (new)
- `src/hermes_orch/api/projects.py` (events endpoint + publish_event hooks)
- `src/hermes_orch/api/tasks.py` (publish_event on start/result/cancel)
- `src/hermes_orch/static/task_events.js` (new — TaskEventStream class)
- `src/hermes_orch/static/task_progress.js` (wires _openEventStream)
- `src/hermes_orch/templates/project.html` (loads task_events.js)
- `tests/test_sse_bus.py` (new)
- `tests/test_sse_endpoint.py` (new)
- `scripts/_smoke_sse.py` (manual smoke test — not part of CI)
