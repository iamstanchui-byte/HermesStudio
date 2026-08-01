// Phase 1.5 (2026-07-29): chatbox-as-plan-editor, shared module.
// Used by both project.html (side panel on the project page) and
// visual_plan.html (side panel on the drawflow canvas). Provides
// the per-project chat panel behavior: history load, send, apply
// suggestion, 409 conflict UI, reformat fallback.
//
// Required on the page that includes this file:
//   - A <div id="chat-panel"> (see project.html for markup)
//   - window.PROJECT_ID or window.CHATBOX_PROJECT_ID set
//   - _fetchWithTimeout and _errDetailToString defined (base.html)
//   - escapeHtml defined (base.html)
//
// Optional hooks (set on window.ChatboxHooks before init):
//   - onPlanApplied: function(plan) — called after a successful
//     update_plan apply. Use this to refresh the visual canvas
//     or any other UI that reflects the plan.

(function () {
    'use strict';

    // The project id for this chat. Falls back to PROJECT_ID for
    // backward compat with the original project.html embed.
    const PROJECT_ID = (typeof window.CHATBOX_PROJECT_ID === 'string'
        && window.CHATBOX_PROJECT_ID)
        || (typeof window.PROJECT_ID === 'string' && window.PROJECT_ID)
        || '';

    if (!PROJECT_ID) {
        console.warn('chatbox.js: no PROJECT_ID set; chat will not work');
        return;
    }

    // Optional hooks (re-read on each call so the host page can
    // set them up after this script loads).
    function _getHooks() {
        return window.ChatboxHooks || {};
    }

    // ========== state ==========

    const _chatStateKey = 'chatOpen:' + PROJECT_ID;
    let _chatPanelOpen = false;

    // ========== panel open/close ==========

    function toggleChatPanel() {
        const panel = document.getElementById('chat-panel');
        if (!panel) return;
        _chatPanelOpen = !_chatPanelOpen;
        if (_chatPanelOpen) {
            panel.classList.remove('hidden');
            try { localStorage.setItem(_chatStateKey, '1'); } catch (e) {}
            if (document.getElementById('chat-messages').children.length <= 1) {
                loadChatHistory();
            }
            setTimeout(() => {
                const input = document.getElementById('chat-input');
                if (input) input.focus();
            }, 50);
        } else {
            panel.classList.add('hidden');
            try { localStorage.setItem(_chatStateKey, '0'); } catch (e) {}
        }
        renderFloatingToggle();
    }

    // Floating "💬" button — replaces the auto-open behavior with
    // a discoverable-but-not-intrusive trigger. Always renders a
    // single button at the bottom-right of the page; visible
    // only when the chat panel is closed (so the user can open
    // it), hidden when the panel is open (the panel has its own
    // X / "Close" affordance at the top).
    //
    // The button is added to <body> (not the panel) so it survives
    // panel re-renders and works on pages where the chat panel
    // is lazy-mounted. Idempotent: calling this twice just updates
    // the same element's hidden state.
    function renderFloatingToggle() {
        let btn = document.getElementById('chat-floating-toggle');
        if (!btn) {
            btn = document.createElement('button');
            btn.id = 'chat-floating-toggle';
            btn.type = 'button';
            btn.setAttribute('aria-label', 'Open Orchestrator assistant');
            btn.title = 'Open Orchestrator assistant';
            btn.className = 'fixed bottom-4 right-4 z-40 w-12 h-12 rounded-full ' +
                'bg-blue-600 hover:bg-blue-700 text-white text-xl ' +
                'shadow-lg flex items-center justify-center transition-colors';
            btn.innerHTML = '💬';
            btn.onclick = function () { toggleChatPanel(); };
            document.body.appendChild(btn);
        }
        btn.classList.toggle('hidden', _chatPanelOpen);
    }

    // Default to CLOSED. User feedback (2026-08-01): the chat
    // auto-opening on every new project was annoying. The
    // existing localStorage key is still respected — users
    // who had it open keep seeing it open until they close
    // it once (which sets the key to '0' and overrides the
    // default). New visitors see the chat closed by default.
    // Discoverability is provided by the floating "💬" button
    // we render below; that button is always visible at the
    // bottom-right of the page when the panel is closed.
    (function _restoreChatState() {
        let saved = '0';
        try { saved = localStorage.getItem(_chatStateKey) || '0'; } catch (e) {}
        if (saved === '1') {
            const panel = document.getElementById('chat-panel');
            if (panel) {
                panel.classList.remove('hidden');
                _chatPanelOpen = true;
                setTimeout(() => {
                    if (document.getElementById('chat-messages')
                        && document.getElementById('chat-messages').children.length <= 1) {
                        loadChatHistory();
                    }
                }, 200);
            }
        }
        renderFloatingToggle();
    })();

    // ========== history ==========

    async function loadChatHistory() {
        try {
            const r = await _fetchWithTimeout(`/api/projects/${PROJECT_ID}/chat`, {}, 10000);
            if (!r.ok) {
                const err = await r.json().catch(() => ({}));
                document.getElementById('chat-messages').innerHTML =
                    `<div class="text-red-600 text-xs">Failed to load history: ${_errDetailToString(err.detail, r.status)}</div>`;
                return;
            }
            const data = await r.json();
            renderChatMessages(data.messages || []);
        } catch (e) {
            if (e.name !== 'AbortError') {
                document.getElementById('chat-messages').innerHTML =
                    `<div class="text-red-600 text-xs">Load error: ${e.message}</div>`;
            }
        }
    }

    function renderChatMessages(messages) {
        const wrap = document.getElementById('chat-messages');
        if (!wrap) return;
        if (!messages.length) {
            wrap.innerHTML = `<div class="text-center text-gray-400 italic py-8 text-xs">
                Describe your goal in plain language. The assistant drafts a plan; you refine conversationally, then click Apply to write it. Click "Run" on the dashboard to dispatch.
            </div>`;
            return;
        }
        wrap.innerHTML = '';
        for (const m of messages) {
            wrap.appendChild(renderChatMessage(m));
        }
        wrap.scrollTop = wrap.scrollHeight;
    }

    // ========== message rendering ==========

    function renderChatMessage(m) {
        const div = document.createElement('div');
        if (m.role === 'user') {
            div.className = 'bg-blue-50 border border-blue-200 rounded p-2 ml-8';
            div.innerHTML = `<div class="text-xs text-gray-500 mb-1">You</div>
                <div class="whitespace-pre-wrap">${escapeHtml(m.content)}</div>`;
        } else {
            div.className = 'bg-gray-50 border border-gray-200 rounded p-2 mr-8';
            let html = `<div class="text-xs text-gray-500 mb-1">Assistant</div>
                <div class="prose-sm">${renderChatContent(m.content)}</div>`;
            if (m.suggestions && m.suggestions.length) {
                html += `<div class="mt-2 space-y-1">${m.suggestions.map((s, i) =>
                    renderSuggestion(s, i, m.id)).join('')}</div>`;
            } else {
                // LLM-fooling pattern #9: LLM sometimes describes the
                // action in text without a JSON block. Show a Reformat
                // button that asks the LLM to wrap the action in JSON
                // (auto-triggers a follow-up chat with the same intent).
                html += `<div class="mt-2">
                    <button onclick="window.chatbox.reformatLastAssistant()"
                        class="text-xs px-2 py-0.5 bg-amber-100 text-amber-800 border border-amber-300 rounded hover:bg-amber-200"
                        title="Ask the assistant to reformat its last response as structured actions (one-click retry with stronger instruction)">
                        ✨ Reformat as actions
                    </button>
                </div>`;
            }
            div.innerHTML = html;
        }
        return div;
    }

    // Render chat message content with markdown-lite. Handles
    // ```text``` fenced code blocks (used by the server-side DAG
    // renderer to embed a plan as monospace). Returns safe HTML.
    function renderChatContent(text) {
        if (!text) return '';
        const parts = [];
        const re = /```text\n([\s\S]*?)\n```/g;
        let last = 0;
        let m;
        while ((m = re.exec(text)) !== null) {
            if (m.index > last) {
                parts.push({kind: 'text', body: text.slice(last, m.index)});
            }
            parts.push({kind: 'code', body: m[1]});
            last = m.index + m[0].length;
        }
        if (last < text.length) {
            parts.push({kind: 'text', body: text.slice(last)});
        }
        if (parts.length === 0) {
            return `<div class="whitespace-pre-wrap">${escapeHtml(text)}</div>`;
        }
        return parts.map(p => {
            if (p.kind === 'code') {
                return `<pre class="font-mono text-xs bg-white border border-gray-200 rounded p-2 my-2 overflow-x-auto whitespace-pre">${escapeHtml(p.body)}</pre>`;
            }
            return `<div class="whitespace-pre-wrap">${escapeHtml(p.body)}</div>`;
        }).join('');
    }

    // ========== suggestion rendering ==========

    function renderSuggestion(s, idx, messageId) {
        const type = s.type || 'unknown';
        let label, desc;
        if (type === 'create_task') {
            label = `➕ Add task: ${s.name || s.action || '(unnamed)'}`;
            desc = `agent=${s.agent_role || '?'}, action=${s.action || '?'}` +
                (s.depends_on && s.depends_on.length ? `, after=[${s.depends_on.join(', ')}]` : '');
        } else if (type === 'run') {
            label = '▶️ Run project';
            desc = s.note || 'Dispatch all pending tasks';
        } else if (type === 'replan') {
            label = '✨ Replan';
            desc = `goal: ${(s.goal || '').slice(0, 80)}${(s.goal || '').length > 80 ? '…' : ''}`;
        } else if (type === 'update_plan') {
            // Phase 1 (2026-07-28): chatbox-as-plan-editor. The LLM
            // edits the project plan, not tasks. The DAG is already
            // shown in the message body (server pre-renders it as a
            // ```text``` block); the chip is just a one-click apply.
            const steps = (s.plan && s.plan.steps) || [];
            const stepCount = steps.length;
            const planName = (s.plan && s.plan.name) || 'unnamed';
            label = `📋 Update plan: ${planName}`;
            desc = `${stepCount} step${stepCount === 1 ? '' : 's'}` +
                (s.if_match ? '' : ' (new plan, no prior state)');
        } else {
            label = `? ${type}`;
            desc = JSON.stringify(s).slice(0, 80);
        }
        return `<div class="flex items-center justify-between gap-2 bg-white border border-gray-300 rounded px-2 py-1 text-xs">
            <div class="flex-1 min-w-0">
                <div class="font-medium text-gray-800">${escapeHtml(label)}</div>
                <div class="text-gray-500 truncate" title="${escapeHtml(desc)}">${escapeHtml(desc)}</div>
            </div>
            <button onclick="window.chatbox.applySuggestion(${messageId || 0}, ${idx}, event)"
                class="px-2 py-0.5 bg-green-600 text-white rounded text-xs hover:bg-green-700 shrink-0">
                Apply
            </button>
        </div>`;
    }

    function appendChatElement(el) {
        const wrap = document.getElementById('chat-messages');
        if (!wrap) return;
        const placeholder = wrap.querySelector('.italic.py-8');
        if (placeholder) placeholder.remove();
        wrap.appendChild(el);
        wrap.scrollTop = wrap.scrollHeight;
    }

    // ========== reformat fallback (LLM-fooling pattern #9) ==========

    async function reformatLastAssistant() {
        const status = document.getElementById('chat-status');
        status.textContent = 'Reformatting...';
        status.className = 'text-xs text-gray-500 mt-1 h-4';
        const allBubbles = Array.from(document.querySelectorAll('#chat-messages > div'));
        const lastUser = allBubbles.reverse().find(d => d.classList.contains('bg-blue-50'));
        if (!lastUser) {
            status.textContent = 'No prior user message to reformat';
            status.className = 'text-xs text-red-600 mt-1 h-4';
            return;
        }
        const text = lastUser.querySelector('.whitespace-pre-wrap')?.textContent || '';
        if (!text) {
            status.textContent = 'Could not read user message text';
            status.className = 'text-xs text-red-600 mt-1 h-4';
            return;
        }
        try {
            const r = await _fetchWithTimeout(
                `/api/projects/${PROJECT_ID}/chat/reformat`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: text}),
                },
                60000,
            );
            const data = await r.json().catch(() => ({}));
            if (!r.ok) {
                status.textContent = 'Reformat failed: ' + _errDetailToString(data.detail, r.status);
                status.className = 'text-xs text-red-600 mt-1 h-4';
                return;
            }
            const wrap = document.getElementById('chat-messages');
            wrap.appendChild(renderChatMessage({
                role: 'assistant',
                content: data.message || '(empty)',
                suggestions: data.suggestions || [],
                id: data.message_id,
            }));
            wrap.scrollTop = wrap.scrollHeight;
            status.textContent = 'Reformatted';
            status.className = 'text-xs text-green-600 mt-1 h-4';
        } catch (e) {
            const reason = e.name === 'AbortError' ? 'timed out (>60s)' : e.message;
            status.textContent = 'Error: ' + reason;
            status.className = 'text-xs text-red-600 mt-1 h-4';
        }
    }

    // ========== send / apply ==========

    async function sendChatMessage() {
        const input = document.getElementById('chat-input');
        const text = input.value.trim();
        if (!text) return;
        const sendBtn = document.getElementById('chat-send-btn');
        const status = document.getElementById('chat-status');
        input.value = '';
        sendBtn.disabled = true;
        status.textContent = 'Sending...';
        const wrap = document.getElementById('chat-messages');
        if (wrap.querySelector('.italic')) wrap.innerHTML = '';
        wrap.appendChild(renderChatMessage({role: 'user', content: text}));
        wrap.scrollTop = wrap.scrollHeight;
        try {
            const r = await _fetchWithTimeout(
                `/api/projects/${PROJECT_ID}/chat`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: text}),
                },
                60000,
            );
            if (!r.ok) {
                const err = await r.json().catch(() => ({}));
                status.textContent = 'Error: ' + _errDetailToString(err.detail, r.status);
                status.className = 'text-xs text-red-600 mt-1 h-4';
                loadChatHistory();
                return;
            }
            const data = await r.json();
            status.textContent = 'Sent';
            status.className = 'text-xs text-green-600 mt-1 h-4';
            wrap.appendChild(renderChatMessage({
                role: 'assistant',
                content: data.message || '(empty response)',
                suggestions: data.suggestions || [],
                id: data.message_id,
            }));
            wrap.scrollTop = wrap.scrollHeight;
        } catch (e) {
            const reason = e.name === 'AbortError' ? 'request timed out (>60s)' : e.message;
            status.textContent = 'Error: ' + reason;
            status.className = 'text-xs text-red-600 mt-1 h-4';
        } finally {
            sendBtn.disabled = false;
            input.focus();
        }
    }

    async function applySuggestion(messageId, suggestionIdx, ev) {
        const btn = ev ? (ev.currentTarget || ev.target) : null;
        try {
            const r = await _fetchWithTimeout(`/api/projects/${PROJECT_ID}/chat`, {}, 10000);
            if (!r.ok) return;
            const data = await r.json();
            const msg = (data.messages || []).find(m => m.id === messageId);
            if (!msg || !msg.suggestions || !msg.suggestions[suggestionIdx]) {
                alert('Suggestion no longer available. Reopen the chat to refresh.');
                return;
            }
            const suggestion = msg.suggestions[suggestionIdx];
            const isPlan = suggestion.type === 'update_plan';
            const stepCount = isPlan ? ((suggestion.plan && suggestion.plan.steps) || []).length : 0;
            const confirmMsg = isPlan
                ? `Apply this plan (${stepCount} step${stepCount === 1 ? '' : 's'})?`
                : `Apply this action?\n\n${JSON.stringify(suggestion, null, 2)}`;
            if (!confirm(confirmMsg)) return;
            const status = document.getElementById('chat-status');
            status.textContent = 'Applying...';
            status.className = 'text-xs text-gray-500 mt-1 h-4';
            const ar = await _fetchWithTimeout(
                `/api/projects/${PROJECT_ID}/chat/apply`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({suggestion, message_id: messageId}),
                },
                30000,
            );
            const adata = await ar.json().catch(() => {});
            // 409 = optimistic lock conflict. The plan was
            // modified by someone/something else between the LLM
            // reading it and you applying. Show a 3-way merge UI
            // (Phase 2, 2026-07-29): the server's current plan +
            // your draft, with two resolution paths.
            if (ar.status === 409) {
                status.textContent = '⚠ Plan was modified externally — choose resolution';
                status.className = 'text-xs text-amber-700 mt-1 h-4';
                if (btn) {
                    btn.disabled = true;
                    btn.textContent = 'Conflict';
                    btn.classList.remove('bg-green-600', 'hover:bg-green-700');
                    btn.classList.add('bg-amber-500', 'cursor-not-allowed');
                }
                const detail = (adata && adata.detail) || {};
                const serverPlan = detail.current_plan || null;
                const conflictMsg = detail.error
                    || 'plan was modified since the assistant read it';
                // Build the merge UI inline next to the Apply button
                const mergeBox = document.createElement('div');
                mergeBox.className = 'text-xs mt-2 p-2 bg-amber-50 border border-amber-300 rounded space-y-2';
                mergeBox.innerHTML = `
                    <div class="font-semibold text-amber-800">⚠ Conflict: ${escapeHtml(conflictMsg)}</div>
                    <div class="grid grid-cols-2 gap-2 mt-2">
                        <div class="bg-white border border-gray-300 rounded p-2">
                            <div class="font-medium text-gray-700 mb-1">📥 Server's current plan</div>
                            ${_summarizePlan(serverPlan)}
                        </div>
                        <div class="bg-white border border-gray-300 rounded p-2">
                            <div class="font-medium text-gray-700 mb-1">📤 Your draft</div>
                            ${_summarizePlan(suggestion.plan)}
                        </div>
                    </div>
                    <div class="flex gap-2 mt-2">
                        <button data-action="use-server"
                            class="flex-1 px-2 py-1 bg-amber-600 text-white rounded text-xs hover:bg-amber-700"
                            title="Discard your draft; the chat will use the server's current plan as the new starting point">
                            Use server's plan
                        </button>
                        <button data-action="force-apply"
                            class="flex-1 px-2 py-1 bg-red-600 text-white rounded text-xs hover:bg-red-700"
                            title="Ignore the conflict; overwrite the server's plan with your draft (no merge)">
                            Force my draft
                        </button>
                    </div>
                `;
                // Wire up the buttons
                mergeBox.querySelector('button[data-action="use-server"]')
                    .addEventListener('click', () => {
                        _resolveConflictUseServer(msg, serverPlan, status);
                    });
                mergeBox.querySelector('button[data-action="force-apply"]')
                    .addEventListener('click', () => {
                        _resolveConflictForceApply(suggestion, msg.id, status, btn);
                    });
                if (btn && btn.parentElement && btn.parentElement.parentElement) {
                    btn.parentElement.parentElement.appendChild(mergeBox);
                }
                return;
            }
            if (!ar.ok) {
                status.textContent = 'Apply failed: ' + _errDetailToString(adata.detail, ar.status);
                status.className = 'text-xs text-red-600 mt-1 h-4';
                return;
            }
            status.textContent = 'Applied: ' + (adata.type || '?');
            status.className = 'text-xs text-green-600 mt-1 h-4';
            if (btn) {
                btn.disabled = true;
                btn.textContent = '✓ Applied';
            }
            // Phase 1.5 (2026-07-29): call the onPlanApplied hook
            // if set. Visual plan editor uses this to refresh the
            // drawflow canvas without a full page reload. Other
            // pages can just location.reload() to get fresh data.
            if (isPlan && suggestion.plan) {
                const hooks = _getHooks();
                if (typeof hooks.onPlanApplied === 'function') {
                    try {
                        hooks.onPlanApplied(suggestion.plan);
                    } catch (e) {
                        console.warn('onPlanApplied hook failed:', e);
                    }
                } else {
                    // No hook registered — fall back to page reload
                    setTimeout(() => location.reload(), 1500);
                }
            } else {
                // Default: reload page after a short pause so the
                // user sees the success indicator.
                setTimeout(() => location.reload(), 1500);
            }
        } catch (e) {
            const reason = e.name === 'AbortError' ? 'timed out' : e.message;
            document.getElementById('chat-status').textContent = 'Error: ' + reason;
            document.getElementById('chat-status').className = 'text-xs text-red-600 mt-1 h-4';
        }
    }

    async function clearChat() {
        if (!confirm('Clear all chat history for this project? This cannot be undone.')) return;
        try {
            const r = await _fetchWithTimeout(
                `/api/projects/${PROJECT_ID}/chat/clear`,
                {method: 'POST'},
                10000,
            );
            if (r.ok) {
                document.getElementById('chat-messages').innerHTML =
                    `<div class="text-center text-gray-400 italic py-8 text-xs">Chat cleared.</div>`;
            } else {
                const err = await r.json().catch(() => ({}));
                alert('Clear failed: ' + _errDetailToString(err.detail, r.status));
            }
        } catch (e) {
            alert('Error: ' + e.message);
        }
    }

    // ========== public API ==========

    window.chatbox = {
        // Public functions called from inline onclick handlers
        toggleChatPanel,
        sendChatMessage,
        applySuggestion,
        clearChat,
        reformatLastAssistant,
        loadChatHistory,
        // Public helpers (for tests and embedders)
        renderChatMessage,
        renderChatContent,
        renderSuggestion,
        renderChatMessages,
        // Phase 2 (2026-07-29): 3-way merge helpers. Exposed for tests
        // and for the embedder to call directly if needed.
        _summarizePlan,
        _resolveConflictUseServer,
        _resolveConflictForceApply,
    };

    // ========== 3-way merge helpers (Phase 2, 2026-07-29) ==========

    // Render a brief summary of a plan: name, step count, top step
    // names. Used in the conflict resolution box (small, scannable).
    function _summarizePlan(plan) {
        if (!plan || typeof plan !== 'object') {
            return '<div class="text-gray-400 italic">(no plan)</div>';
        }
        const steps = Array.isArray(plan.steps) ? plan.steps : [];
        const name = plan.name || '(unnamed)';
        const html = [];
        html.push(`<div class="font-mono text-xs text-gray-600">${escapeHtml(name)}</div>`);
        html.push(`<div class="text-gray-500 text-xs mb-1">${steps.length} step${steps.length === 1 ? '' : 's'}</div>`);
        if (steps.length > 0) {
            const preview = steps.slice(0, 5).map(s => {
                const n = (typeof s === 'object' && s.name) || '?';
                return `<li class="font-mono text-xs">${escapeHtml(n)}</li>`;
            }).join('');
            const more = steps.length > 5
                ? `<li class="text-gray-400 italic text-xs">+ ${steps.length - 5} more...</li>`
                : '';
            html.push(`<ul class="list-disc list-inside pl-1 space-y-0.5">${preview}${more}</ul>`);
        }
        return html.join('');
    }

    // Conflict resolution: "Use server's plan" — discard the user's
    // draft. The chat's in-memory view of the plan (if any host
    // page maintains one) is updated via the ChatboxHooks.onPlanLoaded
    // hook so the UI reflects the server's state. The user can then
    // ask the LLM to redo the edit on top of the server's plan.
    function _resolveConflictUseServer(originalMsg, serverPlan, status) {
        // Call the onPlanLoaded hook if set (host page can refresh
        // its in-memory state). The hook is optional — if not set,
        // we just inform the user.
        const hooks = _getHooks();
        if (serverPlan && typeof hooks.onPlanLoaded === 'function') {
            try { hooks.onPlanLoaded(serverPlan); } catch (e) {
                console.warn('onPlanLoaded hook failed:', e);
            }
        }
        if (status) {
            status.textContent = '✓ Discarded your draft; chat will use server\u2019s plan';
            status.className = 'text-xs text-green-600 mt-1 h-4';
        }
        // Close the merge box and replace it with a system-style
        // note that the user can act on next.
        const mergeBox = document.querySelector('[data-action="use-server"]')?.closest('.bg-amber-50');
        if (mergeBox) {
            mergeBox.innerHTML = `<div class="text-amber-800">
                <div class="font-semibold mb-1">✓ Using server's plan</div>
                <div class="text-xs">Your draft was discarded. The assistant's view of the plan is now in sync with the server. Ask the LLM to redo the edit on top if needed.</div>
            </div>`;
        }
    }

    // Conflict resolution: "Force my draft" — re-apply with if_match
    // omitted (the server treats null if_match as "no prior state"
    // and skips the lock check, effectively overwriting).
    async function _resolveConflictForceApply(suggestion, messageId, status, btn) {
        if (status) {
            status.textContent = 'Force-applying...';
            status.className = 'text-xs text-gray-500 mt-1 h-4';
        }
        // Build a new suggestion with no if_match
        const force = {
            type: 'update_plan',
            plan: suggestion.plan,
            if_match: null,
        };
        try {
            const ar2 = await _fetchWithTimeout(
                `/api/projects/${PROJECT_ID}/chat/apply`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({suggestion: force, message_id: messageId}),
                },
                30000,
            );
            const adata2 = await ar2.json().catch(() => {});
            if (!ar2.ok) {
                if (status) {
                    status.textContent = 'Force apply failed: '
                        + _errDetailToString(adata2.detail, ar2.status);
                    status.className = 'text-xs text-red-600 mt-1 h-4';
                }
                return;
            }
            if (status) {
                status.textContent = '✓ Forced — your plan overwrote the server\u2019s';
                status.className = 'text-xs text-green-600 mt-1 h-4';
            }
            if (btn) {
                btn.disabled = true;
                btn.textContent = '✓ Forced';
            }
            // Trigger hook for host page (visual editor refresh)
            if (suggestion.plan) {
                const hooks = _getHooks();
                if (typeof hooks.onPlanApplied === 'function') {
                    try { hooks.onPlanApplied(suggestion.plan); }
                    catch (e) { console.warn('onPlanApplied hook failed:', e); }
                } else {
                    setTimeout(() => location.reload(), 1500);
                }
            }
        } catch (e) {
            if (status) {
                status.textContent = 'Force apply error: '
                    + (e.name === 'AbortError' ? 'timed out' : e.message);
                status.className = 'text-xs text-red-600 mt-1 h-4';
            }
        }
    }
})();
