// SSE event stream for real-time task updates (v1.8, 2026-07-29).
//
// Replaces the 5s polling in task_progress.js with a long-lived
// EventSource connection. The server pushes events as they happen:
//   - "snapshot"          initial task list (replaces the first /tasks/state fetch)
//   - "task.state_changed"  running -> done/failed/cancelled/etc
//   - "output.chunk"      new agent output (replaces polling /tasks/{id}/output?since=N)
//   - "tool.call"         new tool invocation (informational; loop_status recomputed on demand)
//
// EventSource handles reconnection automatically. We add a
// low-rate 30s polling tick as a drift-correction safety net
// (in case an event was dropped due to a slow client).
//
// Usage:
//   const stream = new TaskEventStream(projectId, handlers);
//   stream.start();
//   stream.stop();  // on page unload

(function () {
  "use strict";

  // Low-rate reconciliation poll. SSE handles realtime events;
  // this is just a "did we miss anything?" check. 30s is long
  // enough that we save 6x on request load vs the old 5s poll.
  const RECONCILE_POLL_MS = 30000;

  class TaskEventStream {
    constructor(projectId, handlers) {
      this.projectId = projectId;
      this.handlers = handlers || {};
      this.es = null;
      this.reconcileTimer = null;
      this.connected = false;
    }

    start() {
      const url = `/api/projects/${encodeURIComponent(this.projectId)}/events`;
      try {
        this.es = new EventSource(url);
      } catch (e) {
        console.error("[orch-events] failed to open EventSource", e);
        // Fall back to polling — the handlers should still work
        // via _pollOnce() called from task_progress.js
        this._startReconcile();
        return;
      }

      // v1.8: the server sends the initial snapshot in the first
      // event. We don't need a separate /tasks/state fetch.
      this.es.addEventListener("snapshot", (e) => {
        try {
          const data = JSON.parse(e.data);
          if (this.handlers.onSnapshot) this.handlers.onSnapshot(data);
        } catch (err) {
          console.warn("[orch-events] bad snapshot JSON", err);
        }
      });

      this.es.addEventListener("task.state_changed", (e) => {
        try {
          const data = JSON.parse(e.data);
          if (this.handlers.onTaskStateChanged)
            this.handlers.onTaskStateChanged(data);
        } catch (err) {
          console.warn("[orch-events] bad task.state_changed JSON", err);
        }
      });

      this.es.addEventListener("output.chunk", (e) => {
        try {
          const data = JSON.parse(e.data);
          if (this.handlers.onOutputChunk) this.handlers.onOutputChunk(data);
        } catch (err) {
          console.warn("[orch-events] bad output.chunk JSON", err);
        }
      });

      this.es.addEventListener("tool.call", (e) => {
        try {
          const data = JSON.parse(e.data);
          if (this.handlers.onToolCall) this.handlers.onToolCall(data);
        } catch (err) {
          console.warn("[orch-events] bad tool.call JSON", err);
        }
      });

      this.es.addEventListener("open", () => {
        this.connected = true;
        if (this.handlers.onConnected) this.handlers.onConnected();
      });

      this.es.addEventListener("error", (e) => {
        // EventSource auto-reconnects on transient errors. We
        // just log and let the browser handle it. If the
        // connection stays down for long, the reconcile poll
        // will catch up.
        this.connected = false;
        if (this.handlers.onDisconnected) this.handlers.onDisconnected(e);
        // No need to close/reopen manually — EventSource handles
        // exponential backoff reconnection.
      });

      // v1.8: also kick off the low-rate reconcile poll as a
      // safety net. If SSE drops events (browser backgrounder,
      // network blip, etc.) the reconcile keeps the UI in sync.
      this._startReconcile();
    }

    stop() {
      if (this.es) {
        this.es.close();
        this.es = null;
      }
      if (this.reconcileTimer) {
        clearInterval(this.reconcileTimer);
        this.reconcileTimer = null;
      }
      this.connected = false;
    }

    _startReconcile() {
      if (this.reconcileTimer) return;
      this.reconcileTimer = setInterval(() => {
        if (this.handlers.onReconcile) this.handlers.onReconcile();
      }, RECONCILE_POLL_MS);
    }
  }

  // Expose on the orch namespace (window.orch) so other modules
  // (e.g. task_progress.js) can construct an instance without
  // importing this file.
  window.orch = window.orch || {};
  window.orch.TaskEventStream = TaskEventStream;
})();
