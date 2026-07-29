/* Task Progress Monitor — frontend (T4, 2026-07-29).
 *
 * Powers real-time status badges + inline expand + side panel.
 * Polls /api/projects/{id}/tasks/{id}/status every 5s for each
 * running task, and /api/projects/{id}/tasks/running every 5s to
 * keep the side panel + task-row badges in sync.
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
    if (!projectId) return;
    // 1. /tasks/running — drives the side panel AND updates all
    //    running-task badges in one round trip.
    const r = await _fetchWithTimeout(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/running`,
      {},
      4000,
    );
    if (!r.ok) {
      // Don't blow up the page on a transient 5xx — just skip this tick.
      return;
    }
    const body = await r.json();
    runningCache = new Map((body.tasks || []).map((t) => [t.task_id, t]));
    _renderSidePanel(body.tasks || []);
    // 2. Update every badge on the page (covers both task rows
    //    currently status=running in the DOM and any new ones the
    //    user has added since the last poll).
    _updateAllBadges();
  }

  function _seedFromDom() {
    // Read initial loop_status from the data attribute the template
    // set (project.html renders t.loop_status as |default('ok')).
    document.querySelectorAll('[data-task-id][data-loop-status]').forEach((row) => {
      const ls = row.getAttribute('data-loop-status');
      _applyBadge(row, {
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

  // === Badge rendering ===

  function _updateAllBadges() {
    document.querySelectorAll('[data-task-id]').forEach((row) => {
      const tid = row.getAttribute('data-task-id');
      const cached = runningCache.get(tid);
      if (cached) _applyBadge(row, cached);
    });
  }

  function _applyBadge(row, t) {
    // Only running tasks get a meaningful loop_status badge; done /
    // failed / etc. show the existing status badge unchanged.
    if (t.status !== 'running' && t.loop_status === 'ok') {
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
    badge.textContent = `${meta.glyph} ${meta.label}`;
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
      existing.remove();
      return;
    }
    // Remove any sibling expand for a different task (only one open
    // at a time — keeps the page tidy)
    let sib = row.nextElementSibling;
    while (sib && sib.hasAttribute('data-expand-for')) {
      sib.remove();
      sib = row.nextElementSibling;
    }
    const t = runningCache.get(tid) || _readRowData(row);
    const detail = _renderDetail(t);
    row.insertAdjacentElement('afterend', detail);
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
    wrap.innerHTML = `
      <div class="flex items-center gap-2 mb-1">
        <span class="inline-flex items-center px-1.5 py-0.5 text-xs font-medium rounded ${meta.cls}">
          ${meta.glyph} ${meta.label}
        </span>
        <span class="text-gray-600">${_escape(t.loop_reason || '')}</span>
      </div>
      <div class="grid grid-cols-2 gap-2 text-xs text-gray-700">
        <div>Status: <span class="font-mono">${_escape(t.status || '?')}</span></div>
        <div>Duration: <span class="font-mono">${durStr}</span></div>
        <div>Last liveness: <span class="font-mono">${ageStr}</span></div>
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
    const order = { stuck: 0, slow: 1, unknown: 2, ok: 3 };
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
