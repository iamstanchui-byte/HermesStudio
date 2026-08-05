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
//   - onWorkflowPatchApplied: function(workflowId, diff) — called
//     after a successful apply_workflow_patch apply. Use this to
//     refresh the workflow editor (visual_workflow.js). The diff
//     arg is the {added, edited, removed} summary from the server.

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
    }

    // Default to CLOSED. User feedback (2026-08-01): the chat
    // auto-opening on every new project was annoying. The
    // existing localStorage key is still respected — users
    // who had it open keep seeing it open until they close
    // it once (which sets the key to '0' and overrides the
    // default). New visitors see the chat closed by default.
    // Discoverability is provided by the existing
    // #chat-toggle-btn in project.html (the floating "💬"
    // button at the bottom-right) — we don't render a
    // duplicate here.
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
        } else if (m.isError) {
            // v3.10.4 (2026-08-02): render actionable LLM errors as a
            // distinct red card. Without this, the user just saw a
            // tiny status-bar message that scrolled out of view; the
            // error context (rephrase, shorter context, etc.) was
            // hidden. Now it lives in the chat history alongside the
            // user's question, so they can re-read it after scrolling.
            div.className = 'bg-red-50 border border-red-300 rounded p-2 mr-8';
            div.innerHTML = `<div class="text-xs text-red-700 mb-1">⚠️ Chat error</div>
                <div class="whitespace-pre-wrap text-sm text-red-900">${escapeHtml(m.content)}</div>`;
        } else {
            div.className = 'bg-gray-50 border border-gray-200 rounded p-2 mr-8';
            let html = `<div class="text-xs text-gray-500 mb-1">Assistant</div>
                <div class="prose-sm">${renderChatContent(m.content)}</div>`;
            if (m.suggestions && m.suggestions.length) {
                html += `<div class="mt-2 space-y-1">${m.suggestions.map((s, i) =>
                    renderSuggestion(s, i, m.id)).join('')}</div>`;
            } else if (m.content && m.content.trim()) {
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
        } else if (type === 'create_plan_from_chat') {
            // v3.10.5 (2026-08-02): the chat LLM describes the plan
            // in prose; Apply = "generate the structured plan from
            // this conversation" (server calls /plan/from-llm with
            // the chat history as the goal). The chip is a one-click
            // "make this real" button.
            label = '✨ Create plan from this conversation';
            desc = s.description || 'Server will generate a plan from the chat history';
        } else if (type === 'apply_workflow_patch') {
            // v3.12.6 (Phase 2): incremental editing of an existing
            // workflow. The chatbox LLM emits a single suggestion
            // with all add/edit/remove sub-ops in one body. The
            // chip shows a compact +/-/~ summary; clicking Apply
            // shows a full diff modal before committing (so the
            // user can verify the patch).
            label = '🔧 Patch workflow';
            desc = _renderWorkflowPatchSummary(s);
        } else if (type === 'apply_plan_patch') {
            // v3.12.6 (Phase 4): incremental editing of the
            // project's plan_json. Same shape as workflow patch
            // (no workflow_id — the project is implicit). This is
            // the PREFERRED type for "add a step to the plan" on
            // an existing plan: it preserves all the other steps
            // the user already designed, instead of overwriting
            // the whole plan.
            label = '🔧 Patch plan';
            desc = _renderWorkflowPatchSummary(s);
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
            // v3.12.6 (Phase 2) context-aware editing: read the
            // currently-selected node from sessionStorage (set by
            // visual_plan.js / visual_workflow.js) and send it to
            // the server. The server injects it into the system
            // prompt as the FOCUS block so the LLM can target
            // suggestions at the selected step. Cleared on page
            // navigation (sessionStorage lifetime = tab session).
            const selectedNode = (() => {
                try {
                    const raw = sessionStorage.getItem(
                        'hermes_chat_selected_node'
                    );
                    return raw ? JSON.parse(raw) : null;
                } catch (e) { return null; }
            })();
            const r = await _fetchWithTimeout(
                `/api/projects/${PROJECT_ID}/chat`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        message: text,
                        selected_node: selectedNode,
                    }),
                },
                60000,
            );
            if (!r.ok) {
                const err = await r.json().catch(() => ({}));
                // v3.10.4 (2026-08-02): render the server's actionable
                // error message as a persistent assistant bubble, so
                // the user sees WHY the LLM didn't respond (think-only,
                // timeout, etc.) instead of the generic "(empty
                // response)" they'd see if we just appended the 200
                // path's `data.message || '(empty response)'`. The
                // server returns a 502 with a multi-line hint; we
                // render it as a quoted error card.
                const errText = _errDetailToString(err.detail, r.status);
                status.textContent = 'Error: ' + (errText.split('\n')[0] || 'see assistant message');
                status.className = 'text-xs text-red-600 mt-1 h-4';
                wrap.appendChild(renderChatMessage({
                    role: 'assistant',
                    content: '⚠️ ' + errText,
                    suggestions: [],
                    id: null,
                    isError: true,
                }));
                wrap.scrollTop = wrap.scrollHeight;
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
            const isCreateFromChat = suggestion.type === 'create_plan_from_chat';
            const isWorkflowPatch = suggestion.type === 'apply_workflow_patch';
            const isPlanPatch = suggestion.type === 'apply_plan_patch';
            const isAnyPatch = isWorkflowPatch || isPlanPatch;
            const stepCount = isPlan ? ((suggestion.plan && suggestion.plan.steps) || []).length : 0;
            // v3.12.6 (Phase 2 + 4): patches get a dedicated
            // diff modal instead of the plain confirm() dialog,
            // because the patch is non-trivial (may add/edit/remove
            // multiple steps) and we want the user to verify the
            // exact changes before committing. The same modal is
            // reused for workflow + plan patches (same shape).
            if (isAnyPatch) {
                const confirmed = await _showWorkflowPatchDiffModal(suggestion);
                if (!confirmed) return;
            } else {
                // v3.10.5 (2026-08-02): chat-driven plan creation. The
                // confirm message is friendlier and warns that the
                // server will generate the plan from the conversation.
                const confirmMsg = isPlan
                    ? `Apply this plan (${stepCount} step${stepCount === 1 ? '' : 's'})?`
                    : isCreateFromChat
                        ? `Create a plan from this conversation?\n\n` +
                          `The server will call the planner LLM with the ` +
                          `chat history as the goal and save the result as ` +
                          `the project's plan. This may take 10-20s.`
                        : `Apply this action?\n\n${JSON.stringify(suggestion, null, 2)}`;
                if (!confirm(confirmMsg)) return;
            }
            const status = document.getElementById('chat-status');
            status.textContent = isCreateFromChat
                ? 'Generating plan from conversation...'
                : 'Applying...';
            status.className = 'text-xs text-gray-500 mt-1 h-4';
            // v3.10.5: create_plan_from_chat can take longer (extra
            // LLM call). Bump the timeout for this type.
            const applyTimeout = isCreateFromChat ? 90000 : 30000;
            const ar = await _fetchWithTimeout(
                `/api/projects/${PROJECT_ID}/chat/apply`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({suggestion, message_id: messageId}),
                },
                applyTimeout,
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
            // v3.10.5 (2026-08-02): create_plan_from_chat returns
            // the generated plan with step_count; surface the count
            // so the user knows what they got.
            if (isCreateFromChat) {
                const sc = (adata && adata.step_count) || 0;
                status.textContent = `✓ Plan created (${sc} step${sc === 1 ? '' : 's'})`;
            } else if (isAnyPatch) {
                // v3.12.6 (Phase 2 + 4): patch summary
                const diff = (adata && adata.diff) || {};
                const na = (diff.added || []).length;
                const ne = (diff.edited || []).length;
                const nr = (diff.removed || []).length;
                const parts = [];
                if (na) parts.push(`+${na}`);
                if (ne) parts.push(`~${ne}`);
                if (nr) parts.push(`-${nr}`);
                const subj = isPlanPatch ? 'Plan' : 'Workflow';
                status.textContent = `✓ ${subj} patched (${parts.join(', ') || 'no change'})`;
            } else {
                status.textContent = 'Applied: ' + (adata.type || '?');
            }
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
            } else if (isAnyPatch) {
                // v3.12.6 (Phase 2 + 4): page hook so the visual
                // editor can refresh its drawflow without a full
                // page reload. The hook name differs by type:
                // onWorkflowPatchApplied for workflows (workflow
                // page), onPlanApplied for plans (plan page). Both
                // pages already register the appropriate hook
                // in their HTML template.
                const hooks = _getHooks();
                const hookName = isPlanPatch ? 'onPlanApplied' : 'onWorkflowPatchApplied';
                if (typeof hooks[hookName] === 'function') {
                    try {
                        if (isPlanPatch) {
                            hooks.onPlanApplied((adata && adata.plan) || {});
                        } else {
                            hooks.onWorkflowPatchApplied(
                                (adata && adata.workflow_id) || suggestion.workflow_id,
                                (adata && adata.diff) || {},
                            );
                        }
                    } catch (e) {
                        console.warn(hookName + ' hook failed:', e);
                    }
                } else {
                    setTimeout(() => location.reload(), 1500);
                }
            } else {
                // v3.10.5: for create_plan_from_chat, the plan was
                // just generated by the server. Reload so the user
                // sees the new plan in the project page UI.
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
        // v3.12.6 (Phase 2): workflow patch diff modal. Exposed for
        // tests and embedders that want to show the same modal
        // from a non-chatbox entry point.
        _renderWorkflowPatchSummary,
        _showWorkflowPatchDiffModal,
        // v3.12.6 (Phase 2): context-aware editing helpers. The
        // visual editor calls setSelectedNode() on selection and
        // clearSelectedNode() on deselect. The chatbox reads it
        // automatically on sendChatMessage().
        setSelectedNode,
        getSelectedNode,
        clearSelectedNode,
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

    // ========== v3.12.6 (Phase 2) workflow patch helpers ==========

    // v3.12.6 (Phase 2) context-aware editing: set / clear the
    // currently-selected node in sessionStorage. The chatbox reads
    // this on the next sendChatMessage() and includes it in the
    // request body. Call this from visual_plan.js / visual_workflow.js
    // when a step is selected or deselected.
    //
    // Example:
    //   chatbox.setSelectedNode({
    //     kind: "workflow_step",     // or "plan_step"
    //     workflow_id: "wf-abc...",
    //     step_name: "alpha",
    //     action: "fetch",
    //     agent_role: "researcher",
    //   });
    function setSelectedNode(node) {
        try {
            if (node && typeof node === 'object') {
                sessionStorage.setItem(
                    'hermes_chat_selected_node',
                    JSON.stringify(node)
                );
            } else {
                sessionStorage.removeItem('hermes_chat_selected_node');
            }
        } catch (e) {
            console.warn('setSelectedNode failed:', e);
        }
    }
    function getSelectedNode() {
        try {
            const raw = sessionStorage.getItem('hermes_chat_selected_node');
            return raw ? JSON.parse(raw) : null;
        } catch (e) { return null; }
    }
    function clearSelectedNode() {
        try {
            sessionStorage.removeItem('hermes_chat_selected_node');
        } catch (e) { /* noop */ }
    }

    // Compact summary for the suggestion chip. Returns an HTML
    // string like "+2, ~1, -1 (workflow wf-...)". Used in the
    // suggestion list (one-line description).
    function _renderWorkflowPatchSummary(suggestion) {
        const patch = suggestion.patch || {};
        const add = Array.isArray(patch.add) ? patch.add.length : 0;
        const edit = Array.isArray(patch.edit) ? patch.edit.length : 0;
        const remove = Array.isArray(patch.remove) ? patch.remove.length : 0;
        const parts = [];
        if (add) parts.push(`<span class="text-green-700">+${add}</span>`);
        if (edit) parts.push(`<span class="text-blue-700">~${edit}</span>`);
        if (remove) parts.push(`<span class="text-red-700">-${remove}</span>`);
        const summary = parts.length ? parts.join(', ') : '(empty patch)';
        const wfId = suggestion.workflow_id || '?';
        const wfShort = wfId.length > 16 ? wfId.slice(0, 13) + '…' : wfId;
        return `${summary} on <span class="font-mono">${escapeHtml(wfShort)}</span>`;
    }

    // Full diff modal for an apply_workflow_patch suggestion.
    // Shows each add/edit/remove in detail so the user can verify
    // the patch before clicking Apply. Returns a Promise that
    // resolves to true (Apply), false (Cancel), or null (closed).
    function _showWorkflowPatchDiffModal(suggestion) {
        return new Promise((resolve) => {
            const patch = suggestion.patch || {};
            const add = Array.isArray(patch.add) ? patch.add : [];
            const edit = Array.isArray(patch.edit) ? patch.edit : [];
            const remove = Array.isArray(patch.remove) ? patch.remove : [];
            const reason = suggestion.reason || '';

            // Render the 3 sub-sections
            const addHtml = add.length
                ? add.map(s => {
                    const name = escapeHtml(s.name || '(unnamed)');
                    const agent = escapeHtml(s.agent_role || '?');
                    const action = escapeHtml(s.action || '?');
                    const deps = (s.depends_on || []).map(d => escapeHtml(d));
                    return `<li class="font-mono text-xs py-1">
                        <span class="text-green-700">+</span>
                        <span class="font-semibold">${name}</span>
                        <span class="text-gray-500">[${agent}]</span>
                        <span class="text-gray-700">${action}</span>
                        ${deps.length ? `<span class="text-gray-400"> after=[${deps.join(', ')}]</span>` : ''}
                    </li>`;
                }).join('')
                : '<li class="text-gray-400 italic text-xs">(none)</li>';

            const editHtml = edit.length
                ? edit.map(e => {
                    const name = escapeHtml(e.name || '(unnamed)');
                    const p = e.patch || {};
                    const fields = Object.keys(p).map(k => {
                        const v = typeof p[k] === 'object' ? JSON.stringify(p[k]) : String(p[k]);
                        return `<div class="ml-3 text-xs">
                            <span class="font-mono text-blue-700">${escapeHtml(k)}</span>
                            <span class="text-gray-500">=</span>
                            <span class="text-gray-800">${escapeHtml(v)}</span>
                        </div>`;
                    }).join('');
                    return `<li class="py-1">
                        <span class="text-blue-700">~</span>
                        <span class="font-mono text-xs font-semibold">${name}</span>
                        <div class="border-l-2 border-blue-200 ml-2 pl-1 mt-0.5">${fields}</div>
                    </li>`;
                }).join('')
                : '<li class="text-gray-400 italic text-xs">(none)</li>';

            const removeHtml = remove.length
                ? remove.map(name =>
                    `<li class="font-mono text-xs py-1">
                        <span class="text-red-700">-</span>
                        <span class="line-through text-gray-600">${escapeHtml(name)}</span>
                    </li>`).join('')
                : '<li class="text-gray-400 italic text-xs">(none)</li>';

            // Build the modal
            const modal = document.createElement('div');
            modal.className = 'fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4';
            modal.innerHTML = `
                <div class="bg-white rounded-lg shadow-xl max-w-2xl w-full max-h-[85vh] flex flex-col">
                    <div class="px-4 py-3 border-b flex items-center justify-between">
                        <div>
                            <div class="text-sm font-semibold text-gray-800">
                                🔧 Workflow patch preview
                            </div>
                            <div class="text-xs text-gray-500 font-mono">
                                ${escapeHtml(suggestion.workflow_id || '?')}
                            </div>
                        </div>
                        <button data-act="cancel" class="text-gray-400 hover:text-gray-600 text-xl leading-none"
                            title="Close">&times;</button>
                    </div>
                    <div class="px-4 py-3 space-y-3 overflow-y-auto text-sm flex-1">
                        <div>
                            <div class="text-xs font-semibold text-green-700 mb-1">ADD (${add.length})</div>
                            <ul class="bg-green-50 border border-green-200 rounded p-2 space-y-0.5">${addHtml}</ul>
                        </div>
                        <div>
                            <div class="text-xs font-semibold text-blue-700 mb-1">EDIT (${edit.length})</div>
                            <ul class="bg-blue-50 border border-blue-200 rounded p-2 space-y-0.5">${editHtml}</ul>
                        </div>
                        <div>
                            <div class="text-xs font-semibold text-red-700 mb-1">REMOVE (${remove.length})</div>
                            <ul class="bg-red-50 border border-red-200 rounded p-2 space-y-0.5">${removeHtml}</ul>
                        </div>
                        ${reason ? `<div class="text-xs text-gray-500 italic border-t pt-2">Reason: ${escapeHtml(reason)}</div>` : ''}
                    </div>
                    <div class="px-4 py-3 border-t flex gap-2 justify-end">
                        <button data-act="cancel"
                            class="px-3 py-1 text-sm bg-gray-200 text-gray-800 rounded hover:bg-gray-300">
                            Cancel
                        </button>
                        <button data-act="apply"
                            class="px-3 py-1 text-sm bg-green-600 text-white rounded hover:bg-green-700"
                            ${(add.length + edit.length + remove.length) === 0 ? 'disabled' : ''}>
                            Apply patch
                        </button>
                    </div>
                </div>`;
            document.body.appendChild(modal);

            const cleanup = (result) => {
                document.body.removeChild(modal);
                resolve(result);
            };
            modal.addEventListener('click', (e) => {
                const act = e.target && e.target.dataset && e.target.dataset.act;
                if (act === 'apply') cleanup(true);
                else if (act === 'cancel') cleanup(false);
                else if (e.target === modal) cleanup(false); // backdrop
            });
        });
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
