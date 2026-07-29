/* Task Progress Monitor — frontend (T4, 2026-07-29).
 *
 * Powers real-time status badges + inline expand + side panel.
 * Polls /api/projects/{id}/tasks/{id}/status every 5s for each
 * running task, and /api/projects/{id}/tasks/running every 5s to
 * keep the side panel + task-row badges in sync.
 *
 * v1.1 (2026-07-29): added live output streaming. When the user
 * expands a running task, we start a 2s poller on
 * /api/projects/{id}/tasks/{id}/output?since=<id> and append
 * new chunks to a per-task <pre> block. Stderr is rendered in
 * a collapsible <details> section so the main view stays clean.
 *
 * API surface (window.taskProgress):
 *   init(projectId)         — call once after page load
 *   refreshAll()            — force-poll now (debug button)
 *
 * Data flow per tick (5s):
 *   1. fetch /tasks/running  → update side panel + per-row badges
 *   2. for each row currently visible with status=running, ALSO fetch
 *      /tasks/{id}/status  → update its badge with finer loop_status
 *      (slow / stuck / unknown) that the /running endpoint also
 *      returns, but we re-fetch the explicit per-task endpoint to
 *      keep things simple and resilient to ordering bugs.
 *
 * Inline expand:
 *   Click a task row → toggle a detail panel below it.
 *   Detail panel shows: loop_status badge, reason, duration,
 *   last_liveness_age, plus a Cancel button if cancellable.
 *
 * Side panel:
 *   Bottom-right slide-out. Toggle button in nav.
 *   Lists all running tasks with name + badge + age.
 *   Click a row → navigate to that project page.
 */
(function () {
  'use strict';

  const POLL_MS = 5000;
  const STATUS_BADGE = {
    ok:      { glyph: '🟢', cls: 'bg-green-100 text-green-800',   label: 'ok' },
    slow:    { glyph: '🟡', cls: 'bg-yellow-100 text-yellow-800', label: 'slow' },
    stuck:   { glyph: '🔴', cls: 'bg-red-100 text-red-800',       label: 'stuck' },
    looping: { glyph: '🟣', cls: 'bg-purple-100 text-purple-800', label: 'looping' },
    unknown: { glyph: '⚪', cls: 'bg-gray-100 text-gray-700',      label: 'no signal' },
  };

  // === Module state ===
  let projectId = null;
  let runningCache = new Map();  // task_id → status dict
  let pollTimer = null;
  let sidePanelOpen = false;

  // === Public API ===
  window.taskProgress = {
    init(pid) {
      projectId = pid;
      // First paint: sync badges from whatever the server-rendered
      // HTML already shows. The polling loop below will keep them
      // fresh; this just avoids a 5s blank window on initial load.
      _seedFromDom();
      // Take over from base.html's 10s page-reload poller. We do
      // our own 5s polling for status updates and a 2s poller for
      // the live output of any expanded task. A full page reload
      // would wipe the user's expanded panels + scroll position +
      // any in-progress cancellations, so we ask base.html to
      // stand down via the public hook. The user can still hit the
      // auto-refresh toggle in the nav to force-reload if they
      // want a full refresh.
      if (typeof window.__orchPausePageRefresh === 'function') {
        window.__orchPausePageRefresh();
      }
      // Wire side-panel toggle (injected into base.html)
      const btn = document.getElementById('task-progress-toggle-btn');
      if (btn) btn.addEventListener('click', _toggleSidePanel);
      // Wire Cancel buttons (delegated so dynamically-rendered rows
      // get covered too)
      document.addEventListener('click', (e) => {
        if (e.target && e.target.matches('button[data-cancel-task]')) {
          e.stopPropagation();
          _cancelTask(e.target.getAttribute('data-cancel-task'));
        }
      });
      // Wire row clicks for inline expand (delegated)
      document.addEventListener('click', (e) => {
        const row = e.target && e.target.closest('[data-task-id]');
        if (!row) return;
        // Don't expand when the click is on a button inside the row
        if (e.target.closest('button, a, input, select, textarea')) return;
        _toggleExpand(row);
      });
      // Kick off polling
      _scheduleNext();
    },
    refreshAll() {
      return _pollOnce();
    },
  };

  // === Polling loop ===

  function _scheduleNext() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      try {
        await _pollOnce();
      } catch (err) {
        // Don't let one bad poll kill the loop
        console.warn('task_progress poll error:', err);
      } finally {
        _scheduleNext();
      }
    }, POLL_MS);
  }

  async function _pollOnce() {
    // v1.5: null projectId means workflow-level page (no
    // single project to scope to). Side panel shows running
    // tasks across ALL projects via /api/tasks/?status=running.
    const isCrossProject = !projectId;
    const _url = isCrossProject
      ? '/api/tasks/?status=running&exclude_archived_tasks=1'
      : `/api/projects/${encodeURIComponent(projectId)}/tasks/state`;
    // v1.3 hot-fix: poll /tasks/state (light shape covering all
    // statuses), not just /tasks/running. /tasks/running excludes
    // non-running tasks by definition, so a finished task's row
    // would never get a status update — its pill would say
    // "running" forever. /tasks/state fixes that by returning
    // every visible task with its current status.
    const r = await _fetchWithTimeout(
      _url,
      {},
      4000,
    );
    if (!r.ok) {
      // Don't blow up the page on a transient 5xx — just skip this tick.
      return;
    }
    const body = await r.json();
    const rawTasks = body.tasks || [];
    const allStates = isCrossProject
      ? rawTasks.map(_normalizeTaskToStateShape)
      : rawTasks;
    runningCache = new Map(allStates.map((t) => [t.task_id, t]));
    // Side panel: filter to running only (matches the old
    // /tasks/running endpoint behavior; non-running tasks are
    // not shown in the panel — their status appears in the row
    // pill via _applyTaskState).
    _renderSidePanel(allStates.filter((t) => t.status === 'running'));
    // Update every visible row on the page (covers both
    // currently-running and just-finished tasks so the row pill
    // text + class stays in sync with the server).
    _updateAllBadges();
  }


  // v1.5: normalize a /api/tasks/?status=running row (full Task
  // pydantic model dump) to the light shape used by the rest of
  // task_progress.js. Same shape as the per-project /tasks/state
  // endpoint so the downstream pipeline doesn't care which
  // source the data came from. loop_status / loop_reason are
  // not pre-computed for the cross-project list (that would
  // require N compute_loop_status calls server-side for every
  // poll) — the side panel doesn't use them and there's no row
  // on the workflow page to badge.
  function _normalizeTaskToStateShape(t) {
    return {
      task_id: t.id,
      project_id: t.project_id,
      name: t.name || '',
      status: t.status || '',
      agent_role: t.agent_role || '',
      loop_status: 'ok',
      loop_reason: t.status === 'running'
        ? 'liveness OK'
        : 'task is ' + (t.status || '?'),
      duration_s: 0,
      last_event_age_s: null,
      started_at: t.started_at,
      last_liveness_at: t.last_liveness_at,
    };
  }

  function _seedFromDom() {
    // Read initial loop_status from the data attribute the template
    // set (project.html renders t.loop_status as |default('ok')).
    document.querySelectorAll('[data-task-id][data-loop-status]').forEach((row) => {
      const ls = row.getAttribute('data-loop-status');
      _applyTaskState(row, {
        status: row.getAttribute('data-task-status') || '',
        loop_status: ls,
        loop_reason: row.getAttribute('data-loop-reason') || '',
        duration_s: parseInt(row.getAttribute('data-duration-s') || '0', 10),
        last_event_age_s: row.getAttribute('data-last-age') === ''
          ? null
          : parseInt(row.getAttribute('data-last-age') || '0', 10),
        task_id: row.getAttribute('data-task-id'),
      });
    });
    // Render side panel from any task_status cards already in DOM
    // (the side panel is hidden until first poll, so this is a
    // best-effort initial paint only).
    const initial = Array.from(
      document.querySelectorAll('[data-task-id][data-loop-status]')
    ).map((row) => ({
      task_id: row.getAttribute('data-task-id'),
      name: row.querySelector('[data-task-name]')?.textContent || '',
      loop_status: row.getAttribute('data-loop-status'),
      last_event_age_s: row.getAttribute('data-last-age') === ''
        ? null
        : parseInt(row.getAttribute('data-last-age') || '0', 10),
      project_id: projectId,
    }));
    if (initial.length) _renderSidePanel(initial);
  }

  // === State rendering ===
  //
  // v1.3 hot-fix: we now update BOTH the loop_status badge AND the
  // status pill (text + class). Pre-v1.3, the pill's text and color
  // stayed frozen on the initial server-render value forever — so
  // a task that transitioned running → done would visually still
  // look running. The status pill CSS classes match what
  // project.html renders server-side, so the in-place updates
  // look identical to a fresh page load.

  // Mirror of project.html's status-pill color map. Keep these in
  // sync with the Jinja template at templates/project.html.
  const STATUS_PILL_CLASS = {
    pending:  'bg-gray-100 text-gray-800',
    assigned: 'bg-gray-100 text-gray-800',
    running:  'bg-blue-100 text-blue-800',
    completed:'bg-green-100 text-green-800',
    failed:   'bg-red-100 text-red-800',
    skipped:  'bg-yellow-100 text-yellow-800',
    cancelled:'bg-gray-100 text-gray-800',
  };

  function _updateAllBadges() {
    document.querySelectorAll('[data-task-id]').forEach((row) => {
      const tid = row.getAttribute('data-task-id');
      const cached = runningCache.get(tid);
      if (cached) {
        _applyTaskState(row, cached);
        _updateExpandPanel(tid, cached);
      }
    });
  }

  // v1.3 hot-fix: keep the inline expand panel in sync with the
  // current task state. Without this, a task that transitions
  // running → done while the panel is open would visually still
  // show "Status: running" + the live streaming block, even
  // though the row pill above has already updated to "done".
  function _updateExpandPanel(tid, t) {
    const panel = document.querySelector(
      `[data-expand-for="${tid}"]`
    );
    if (!panel) return;
    // Update the four text fields. Skip the ones that don't
    // exist (e.g. the loop_status badge header is hidden for
    // terminal tasks, but the row updates handled that).
    const setText = (sel, val) => {
      const el = panel.querySelector(sel);
      if (el) el.textContent = val;
    };
    setText('[data-detail-status]', t.status || '?');
    setText(
      '[data-detail-reason]', t.loop_reason || ''
    );
    setText(
      '[data-detail-duration]',
      t.duration_s ? `${t.duration_s}s` : '—'
    );
    setText(
      '[data-detail-age]',
      t.last_event_age_s == null
        ? '—'
        : `${t.last_event_age_s}s ago`
    );
    // Update the loop_status badge in the panel header
    const loopBadge = panel.querySelector(
      '[data-detail-loop-badge]'
    );
    if (loopBadge) {
      const meta = STATUS_BADGE[t.loop_status] || STATUS_BADGE.unknown;
      loopBadge.className =
        'inline-flex items-center px-1.5 py-0.5 text-xs font-medium rounded ' +
        meta.cls;
      loopBadge.textContent = `${meta.glyph} ${meta.label}`;
    }
    // v1.3 hot-fix: if the task transitioned running → terminal
    // while the panel is open, replace the live streaming block
    // with a static "no streaming" note. The reverse transition
    // (terminal → running) is unusual but we handle it for
    // symmetry: rebuild the streaming block.
    const isTerminal = !['pending', 'assigned', 'running']
      .includes(t.status);
    const streamHost = panel.querySelector('[data-stream-host]');
    const terminalNote = panel.querySelector('[data-terminal-note]');
    if (isTerminal && streamHost && !terminalNote) {
      const note = document.createElement('div');
      note.setAttribute('data-terminal-note', '1');
      note.className = 'mt-3 text-xs text-gray-600 italic';
      note.textContent =
        `Task is ${t.status} — no streaming output will arrive. ` +
        `Final result is in the task row's "View result" link above.`;
      streamHost.replaceWith(note);
    } else if (!isTerminal && terminalNote && !streamHost) {
      // Rebuild a minimal streaming block. We don't try to
      // restore the previous chunks (those are lost when the
      // node is removed); the user can re-click the row to get
      // a fresh stream.
      const host = document.createElement('div');
      host.setAttribute('data-stream-host', tid);
      host.className = 'mt-3';
      host.innerHTML =
        `<div class="flex items-center justify-between mb-1">
          <div class="text-xs font-semibold text-gray-700">
            📺 Live output
            <span data-stream-status="${_escape(tid)}" class="text-gray-500 font-normal">starting…</span>
          </div>
        </div>
        <pre data-stream-stdout="${_escape(tid)}"
             class="bg-white border border-gray-200 rounded p-2 text-xs font-mono whitespace-pre-wrap max-h-64 overflow-y-auto"
             style="min-height: 2.5rem;">(waiting for output…)</pre>`;
      terminalNote.replaceWith(host);
      _startStreaming(tid, host);
    }
  }

  function _applyTaskState(row, t) {
    // 1. Update the status pill (text + class). This was the
    //    missing piece in v1 — the pill would stay frozen on the
    //    server-render value (usually "running") forever.
    const pill = row.querySelector('[data-status-pill]');
    if (pill) {
      const newCls = STATUS_PILL_CLASS[t.status] || STATUS_PILL_CLASS.pending;
      // Reset to a clean base, then add the per-status color.
      // Drop every status-* class so the new one takes effect
      // even when the row transitioned (e.g. running → done).
      const baseCls = 'px-2 py-0.5 text-xs rounded font-mono';
      pill.className = `${baseCls} ${newCls}`;
      pill.textContent = t.status || '?';
    }
    // 2. Update the loop_status badge. Only running tasks get
    //    one (a finished task's loop_status is "ok" + reason
    //    "task is X" — not interesting to badge).
    if (t.status !== 'running') {
      _removeLoopBadge(row);
      return;
    }
    const meta = STATUS_BADGE[t.loop_status] || STATUS_BADGE.unknown;
    let badge = row.querySelector('[data-loop-badge]');
    if (!badge) {
      badge = document.createElement('span');
      badge.setAttribute('data-loop-badge', '1');
      // Insert next to the existing status pill
      const statusPill = row.querySelector('[data-status-pill]');
      if (statusPill && statusPill.parentNode) {
        statusPill.parentNode.insertBefore(badge, statusPill.nextSibling);
      } else {
        // Fallback: just append to row
        row.appendChild(badge);
      }
    }
    badge.className =
      'ml-1 inline-flex items-center px-1.5 py-0.5 text-xs font-medium rounded ' +
      meta.cls;
    // v1.7: when looping, append the tool name + count to the badge
    // so the user sees "🟣 looping read x15" instead of just "🟣 looping".
    // The full reason (path/command) is in the tooltip on hover.
    if (t.loop_status === 'looping' && t.tool) {
      const count = t.repeat_count != null ? ` x${t.repeat_count}` : '';
      badge.textContent = `${meta.glyph} ${meta.label} ${t.tool}${count}`;
    } else {
      badge.textContent = `${meta.glyph} ${meta.label}`;
    }
    badge.title = t.loop_reason || meta.label;
  }

  function _removeLoopBadge(row) {
    const badge = row.querySelector('[data-loop-badge]');
    if (badge) badge.remove();
  }

  // === Inline expand ===

  function _toggleExpand(row) {
    const tid = row.getAttribute('data-task-id');
    const existing = row.nextElementSibling;
    if (existing && existing.getAttribute('data-expand-for') === tid) {
      // Collapsing: stop the streaming poller first so we don't leak
      // timers in the background
      _stopStreaming(tid);
      existing.remove();
      return;
    }
    // Remove any sibling expand for a different task (only one open
    // at a time — keeps the page tidy)
    let sib = row.nextElementSibling;
    while (sib && sib.hasAttribute('data-expand-for')) {
      const sibTid = sib.getAttribute('data-expand-for');
      if (sibTid) _stopStreaming(sibTid);
      sib.remove();
      sib = row.nextElementSibling;
    }
    const t = runningCache.get(tid) || _readRowData(row);
    const detail = _renderDetail(t);
    row.insertAdjacentElement('afterend', detail);
    // v1.1: start streaming the live output for this task. We poll
    // the /output endpoint every 2s. Skip the poller for terminal
    // tasks (v1.3 hot-fix) — the rendered panel already says "no
    // streaming output will arrive", so polling would just
    // return 0 chunks forever and waste cycles.
    const isTerminal = !['pending', 'assigned', 'running'].includes(t.status);
    if (!isTerminal) {
      _startStreaming(tid, detail);
    }
  }

  function _readRowData(row) {
    return {
      task_id: row.getAttribute('data-task-id'),
      status: row.getAttribute('data-task-status') || '',
      loop_status: row.getAttribute('data-loop-status') || 'unknown',
      loop_reason: row.getAttribute('data-loop-reason') || '',
      duration_s: parseInt(row.getAttribute('data-duration-s') || '0', 10),
      last_event_age_s: row.getAttribute('data-last-age') === ''
        ? null
        : parseInt(row.getAttribute('data-last-age') || '0', 10),
    };
  }

  function _renderDetail(t) {
    const wrap = document.createElement('div');
    wrap.setAttribute('data-expand-for', t.task_id);
    wrap.className = 'ml-4 mb-2 p-3 bg-gray-50 border border-gray-200 rounded text-sm';
    const ageStr = t.last_event_age_s == null
      ? '—'
      : `${t.last_event_age_s}s ago`;
    const durStr = t.duration_s ? `${t.duration_s}s` : '—';
    const meta = STATUS_BADGE[t.loop_status] || STATUS_BADGE.unknown;
    const cancellable = ['pending', 'assigned', 'running'].includes(t.status);
    const isTerminal = !cancellable;
    // v1.3 hot-fix: a finished task will never get new streaming
    // output, so the "Live output" panel with "(waiting for
    // output…)" was misleading. For terminal states we render a
    // static note instead and don't start a streaming poller.
    const liveSection = isTerminal ? `
      <div class="mt-3 text-xs text-gray-600 italic">
        Task is ${_escape(t.status)} — no streaming output will arrive.
        Final result is in the task row's "View result" link above.
      </div>
    ` : `
      <div class="mt-3" data-stream-host="${_escape(t.task_id)}">
        <div class="flex items-center justify-between mb-1">
          <div class="text-xs font-semibold text-gray-700">
            📺 Live output
            <span data-stream-status="${_escape(t.task_id)}" class="text-gray-500 font-normal">starting…</span>
          </div>
          <button data-stream-toggle="${_escape(t.task_id)}"
            class="text-xs text-blue-700 hover:text-blue-900"
            title="Pause / resume the 2s polling">⏸</button>
        </div>
        <pre data-stream-stdout="${_escape(t.task_id)}"
             class="bg-white border border-gray-200 rounded p-2 text-xs font-mono whitespace-pre-wrap max-h-64 overflow-y-auto"
             style="min-height: 2.5rem;">(waiting for output…)</pre>
        <details data-stream-stderr-wrap="${_escape(t.task_id)}" class="mt-1" hidden>
          <summary class="text-xs text-yellow-700 cursor-pointer">
            ⚠ stderr <span data-stream-stderr-count="${_escape(t.task_id)}">(0)</span>
          </summary>
          <pre data-stream-stderr="${_escape(t.task_id)}"
               class="bg-yellow-50 border border-yellow-200 rounded p-2 text-xs font-mono whitespace-pre-wrap max-h-48 overflow-y-auto mt-1"></pre>
        </details>
      </div>
    `;
    wrap.innerHTML = `
      <div class="flex items-center gap-2 mb-1">
        <span data-detail-loop-badge
          class="inline-flex items-center px-1.5 py-0.5 text-xs font-medium rounded ${meta.cls}">
          ${meta.glyph} ${meta.label}
        </span>
        <span data-detail-reason class="text-gray-600">${_escape(t.loop_reason || '')}</span>
      </div>
      <div class="grid grid-cols-2 gap-2 text-xs text-gray-700">
        <div>Status: <span data-detail-status class="font-mono">${_escape(t.status || '?')}</span></div>
        <div>Duration: <span data-detail-duration class="font-mono">${durStr}</span></div>
        <div>Last liveness: <span data-detail-age class="font-mono">${ageStr}</span></div>
        <div>Task: <span class="font-mono">${_escape(t.task_id || '')}</span></div>
      </div>
      ${cancellable ? `
        <div class="mt-2">
          <button data-cancel-task="${_escape(t.task_id)}"
            class="px-2 py-1 text-xs bg-yellow-100 text-yellow-800 rounded hover:bg-yellow-200"
            title="Cancel this task (frees the assigned profile, marks task as cancelled)">
            Cancel task
          </button>
        </div>` : ''}
      ${liveSection}
    `;
    return wrap;
  }

  // === Side panel ===

  function _toggleSidePanel() {
    sidePanelOpen = !sidePanelOpen;
    const panel = document.getElementById('task-progress-side-panel');
    if (panel) panel.style.display = sidePanelOpen ? 'block' : 'none';
  }

  function _renderSidePanel(tasks) {
    const list = document.getElementById('task-progress-side-panel-list');
    const empty = document.getElementById('task-progress-side-panel-empty');
    const count = document.getElementById('task-progress-side-panel-count');
    if (!list) return;
    if (count) count.textContent = String(tasks.length);
    if (!tasks.length) {
      list.innerHTML = '';
      if (empty) empty.style.display = 'block';
      return;
    }
    if (empty) empty.style.display = 'none';
    // Sort: stuck first (most concerning), then slow, unknown, ok
    // Sort: most concerning first. Looping > stuck > slow > unknown > ok
    // (a stuck wrapper is dead; a looping one is wasting tokens RIGHT NOW).
    const order = { looping: 0, stuck: 1, slow: 2, unknown: 3, ok: 4 };
    const sorted = [...tasks].sort(
      (a, b) => (order[a.loop_status] ?? 9) - (order[b.loop_status] ?? 9)
    );
    list.innerHTML = sorted.map((t) => {
      const meta = STATUS_BADGE[t.loop_status] || STATUS_BADGE.unknown;
      const ageStr = t.last_event_age_s == null
        ? '—'
        : `${t.last_event_age_s}s`;
      return `
        <div class="flex items-center justify-between gap-2 py-1 border-b border-gray-100 last:border-0"
             data-side-task-id="${_escape(t.task_id)}">
          <div class="flex-1 min-w-0">
            <div class="text-sm font-medium truncate" title="${_escape(t.name || t.task_id)}">${_escape(t.name || t.task_id)}</div>
            <div class="text-xs text-gray-500">${_escape(t.agent_role || '')}</div>
          </div>
          <span class="inline-flex items-center px-1.5 py-0.5 text-xs font-medium rounded ${meta.cls}">
            ${meta.glyph} ${meta.label}
          </span>
          <span class="text-xs text-gray-500 w-12 text-right">${ageStr}</span>
        </div>
      `;
    }).join('');
  }

  // === Cancel ===

  async function _cancelTask(taskId) {
    if (!taskId || !projectId) return;
    if (!confirm(`Cancel task ${taskId}?`)) return;
    try {
      const r = await _fetchWithTimeout(
        `/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/cancel`,
        { method: 'POST' },
        5000,
      );
      if (r.ok) {
        // Force an immediate refresh so the badge updates without
        // waiting for the next 5s tick.
        await _pollOnce();
      } else {
        const detail = await r.text();
        alert(`Cancel failed (${r.status}): ${detail}`);
      }
    } catch (err) {
      alert('Cancel error: ' + (err && err.message ? err.message : err));
    }
  }

  // === Helpers ===

  // === v1.1: Live output streaming ===

  // Per-task streaming state. Keyed by task_id.
  //   {since: last audit_log id seen, stdoutSeq: highest seq rendered,
  //    stderrSeq: highest seq rendered, paused: bool,
  //    timer: setTimeout handle, host: parent <div> element}
  const _streamers = new Map();
  const STREAM_POLL_MS = 2000;

  function _startStreaming(tid, hostEl) {
    // Idempotent: if a poller is already running for this task, just
    // make sure it's pointing at the (possibly new) host element
    let s = _streamers.get(tid);
    if (s) {
      s.host = hostEl;
      return;
    }
    s = {
      since: 0,
      stdoutSeq: 0,
      stderrCount: 0,
      paused: false,
      timer: null,
      host: hostEl,
    };
    _streamers.set(tid, s);
    // Wire pause toggle (delegated, in case the host is replaced)
    const toggle = hostEl.querySelector(`[data-stream-toggle="${tid}"]`);
    if (toggle) {
      toggle.addEventListener('click', () => {
        s.paused = !s.paused;
        toggle.textContent = s.paused ? '▶' : '⏸';
        toggle.title = s.paused ? 'Resume polling' : 'Pause polling';
        const status = hostEl.querySelector(`[data-stream-status="${tid}"]`);
        if (status) status.textContent = s.paused ? 'paused' : 'resumed';
      });
    }
    _scheduleNextStream(s, tid);
  }

  function _stopStreaming(tid) {
    const s = _streamers.get(tid);
    if (!s) return;
    if (s.timer) clearTimeout(s.timer);
    _streamers.delete(tid);
  }

  function _scheduleNextStream(s, tid) {
    s.timer = setTimeout(async () => {
      try {
        if (!s.paused) await _streamTick(s, tid);
      } catch (err) {
        const status = s.host && s.host.querySelector(
          `[data-stream-status="${tid}"]`
        );
        if (status) status.textContent = 'error: ' + (err && err.message || err);
      } finally {
        // Only reschedule if the streamer is still active (user may
        // have collapsed the panel while a tick was in flight).
        if (_streamers.get(tid) === s) _scheduleNextStream(s, tid);
      }
    }, STREAM_POLL_MS);
  }

  async function _streamTick(s, tid) {
    const r = await _fetchWithTimeout(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(tid)}/output?since=${s.since}`,
      {},
      STREAM_POLL_MS - 200,
    );
    if (!r.ok) {
      // 404 = task gone. Stop polling.
      if (r.status === 404) {
        _stopStreaming(tid);
        const status = s.host && s.host.querySelector(
          `[data-stream-status="${tid}"]`
        );
        if (status) status.textContent = 'task gone';
      }
      return;
    }
    const body = await r.json();
    const chunks = body.chunks || [];
    if (chunks.length) {
      for (const c of chunks) {
        _renderStreamChunk(s, tid, c);
      }
      s.since = body.next_since || s.since;
    }
    // Update status text (idle / streaming / stopped)
    const status = s.host && s.host.querySelector(
      `[data-stream-status="${tid}"]`
    );
    if (status) {
      if (chunks.length === 0) {
        status.textContent = 'idle (no new output)';
      } else {
        status.textContent = `+${chunks.length} chunk${chunks.length > 1 ? 's' : ''}`;
      }
    }
    // Auto-stop on terminal state: if the task is no longer running
    // and the server has nothing more to say, drop the poller so we
    // don't keep pinging a finished task.
    if (chunks.length === 0) {
      const t = runningCache.get(tid);
      if (t && !['pending', 'assigned', 'running'].includes(t.status)) {
        const totalAfterSince = s.stdoutSeq + s.stderrCount;
        if (status) status.textContent = `done (${totalAfterSince} chunks)`;
        // Keep polling for ~10s in case of late tail flush, then stop
        if (!s._stopAfter) s._stopAfter = Date.now() + 10000;
        if (Date.now() >= s._stopAfter) _stopStreaming(tid);
      } else {
        s._stopAfter = null;
      }
    } else {
      s._stopAfter = null;
    }
  }

  function _renderStreamChunk(s, tid, c) {
    if (c.stream === 'stderr') {
      // Per design: stderr is collapsed under a <details>. Increment
      // both the per-stream seq tracker (for ordering) and the count
      // (for the summary badge).
      s.stderrCount += 1;
      const pre = s.host && s.host.querySelector(
        `[data-stream-stderr="${tid}"]`
      );
      const wrap = s.host && s.host.querySelector(
        `[data-stream-stderr-wrap="${tid}"]`
      );
      const count = s.host && s.host.querySelector(
        `[data-stream-stderr-count="${tid}"]`
      );
      if (wrap) wrap.hidden = false;
      if (count) count.textContent = `(${s.stderrCount})`;
      if (pre) {
        // Replace the leading placeholder if this is the first chunk
        if (!pre.textContent && s.stderrCount === 1) pre.textContent = '';
        pre.textContent += c.text;
        // Auto-scroll to bottom
        pre.scrollTop = pre.scrollHeight;
      }
    } else {
      // Default to stdout
      s.stdoutSeq += 1;
      const pre = s.host && s.host.querySelector(
        `[data-stream-stdout="${tid}"]`
      );
      if (pre) {
        // First chunk: drop the "(waiting for output…)" placeholder
        if (pre.textContent === '(waiting for output…)') pre.textContent = '';
        pre.textContent += c.text;
        pre.scrollTop = pre.scrollHeight;
      }
    }
  }

  function _escape(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // Re-use the page-level helper if it exists (defined in base.html),
  // otherwise fall back to a local wrapper.
  function _fetchWithTimeout(url, opts, ms) {
    if (typeof window._fetchWithTimeout === 'function') {
      return window._fetchWithTimeout(url, opts, ms);
    }
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms);
    return fetch(url, { ...opts, signal: ctl.signal }).finally(() => clearTimeout(t));
  }
})();
