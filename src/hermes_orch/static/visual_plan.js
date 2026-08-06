// Visual plan editor JS — Phase C (2026-07-27).
//
// Wraps a drawflow canvas for editing a project's plan (plan.steps
// as drawflow nodes, depends_on as wires). Single source of truth
// for the plan is the in-memory `state.plan` object — every drawflow
// event (add/delete/wire) updates the model. Save = PUT the model
// as JSON. Generate tasks = POST to /plan/run (creates actual task
// rows from the plan).
//
// Conventions:
//   - node id "vp-{step_name}" (must be a valid CSS id; we kebab-case
//     the step name first to ensure that)
//   - node HTML is the same compact card style as visual_workflow.js
//     (cyan border, role pill, action line, hidden × delete button)
//   - side panel is hidden by default; opens on node click, closes
//     on click outside or after save
//   - all errors surface as a transient save banner (top-right)
//
// Phase C does NOT include:
//   - Auto-layout (defer to C+)
//   - Validation state badges (need /plan/validate endpoint)
//   - Min-map interactivity (basic visual only)
//   - Side panel for plan-level (name/description) — those inputs
//     live in the toolbar above the canvas (see HTML).

(function() {
    'use strict';

    // ===== Module-level state =====
    let _editor = null;             // drawflow instance
    // === v1.5.3: server-side visual_layout (2026-07-29) ===
    // Replaces v1.5's localStorage-based canvas persistence.
    // Matches the workflow_packages.visual_layout pattern: drawflow
    // node positions are stored on the plan document server-side
    // (so they survive reloads AND cross-device / cross-browser).
    // _plan.visual_layout is {step_name: {"x": <float>, "y": <float>}}.
    // Missing entries fall back to drawflow's default vertical
    // stack (50, 50 + i*120). The 1s debounce collapses drag bursts
    // into one PUT. We don't try to keep any client-side cache —
    // localStorage's only benefit was "reload recovery", and the
    // server now provides that for everyone.
    const VP_LAYOUT_DEBOUNCE_MS = 1000;
    let _vpLayoutSaveTimer = null;
    let _plan = {                   // the current plan (source of truth)
        version: '1.0',
        name: '',
        description: '',
        trigger: 'manual',
        variables: [],
        steps: [],
    };
    let _projectId = null;
    let _projectMaxIterations = 3;  // v3.10.10: cached at init, used by the Generate Tasks modal
    let _selectedNodeName = null;   // which step's details are shown in the side panel
    let _jsonMode = false;          // toggle between visual and JSON textarea
    // v3.14.0 (Phase 3 followup): count of unsaved step edits. The
    // top "Save" button shows this count so the user can see when
    // their in-memory edits haven't been persisted to the server
    // yet. Without this, users assumed "Save step" auto-saved to
    // the server, which it doesn't (saveStepEdits only updates
    // the in-memory _plan; server-side persistence is in
    // savePlan() which the user must trigger explicitly). Reset
    // to 0 after each successful savePlan().
    let _dirtyStepCount = 0;
    // Map from step name -> drawflow's INTERNAL node id (the
    // counter it uses as the key in its data map, e.g. '1' / '2'
    // for the first two nodes, NOT the name we passed to addNode
    // and NOT the DOM id 'node-1' / 'node-2'). drawflow's
    // addConnection looks nodes up by this internal id (it does
    // NOT use the name, and it does NOT use the DOM id). So we
    // capture the internal id right after addNode by scanning
    // the module's data map for the node whose `.name` matches
    // the one we just added. Rebuilt on every renderAllSteps.
    const _internalIdByStepName = new Map();

    // kebab-case validator (must match server-side). Centralized
    // here so the visual editor gives the same error as the API.
    const KEBAB_RE = /^[a-z0-9][a-z0-9-]*$/;

    // ===== v2.2 (2026-07-30): Undo/Redo + Copy/Paste =====
    // Mirror of visual_workflow.js. The plan editor didn't have
    // these before, so a lot of accidental deletions / typo renames
    // were unrecoverable. History snapshots cover the whole _plan
    // object (steps + variables + visual_layout + name + description);
    // any mutation that touches _plan calls _checkpoint() at the
    // top so undo can revert. Ctrl+Z / Ctrl+Y work everywhere,
    // including in text fields (matches workflow editor + most
    // native editors). Ctrl+C / Ctrl+V only fire when focus is
    // NOT in a text field, so users can still copy-paste within
    // the side panel form freely.
    const _history = {
        undoStack: [],          // [{label, snapshot: <deep-copy _plan>}]
        redoStack: [],
        maxSize: 50,
        _isRendering: false,    // suppress checkpoints during renderAllSteps
        _isApplyingPatch: false, // suppress during undo/redo
    };
    let _clipboard = null;      // {step: <deep-copy>, ts: <number>}
    // Drag detection so dblclick on a moved card doesn't open the
    // side panel (we use the same 8px threshold as visual_workflow.js).
    const _DRAG_THRESHOLD_PX = 8;
    let _mouseDownPos = null;

    // ===== DOM helpers =====
    function $(id) { return document.getElementById(id); }
    function showBanner(msg, kind) {
        const el = $('vp-save-banner');
        if (!el) return;
        el.textContent = msg;
        el.className = 'vp-save-banner show ' + (kind || 'success');
        setTimeout(() => { el.className = 'vp-save-banner ' + (kind || 'success'); }, 2200);
    }
    function escapeHtml(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // ===== v2.2: Undo/Redo helpers =====
    // Deep-copy the whole _plan object (steps + variables + layout +
    // name + description). We don't try to be selective — 50 entries
    // at ~2-10KB each is < 1MB of memory, well within the editor's
    // lifetime budget.
    function _snapshot() {
        return { plan: JSON.parse(JSON.stringify(_plan)) };
    }
    function _restoreSnapshot(snap) {
        _plan = snap.plan;
    }
    function _checkpoint(label) {
        if (_history._isRendering || _history._isApplyingPatch) return;
        _history.undoStack.push({ label, snapshot: _snapshot() });
        if (_history.undoStack.length > _history.maxSize) {
            _history.undoStack.shift();
        }
        // Any new edit invalidates the redo stack (standard editor behavior).
        _history.redoStack = [];
        _updateHistoryButtons();
    }
    function _undo() {
        if (_history.undoStack.length === 0) return;
        const entry = _history.undoStack.pop();
        // Capture current state for the redo stack BEFORE we restore
        // the snapshot. Same pattern as visual_workflow.js.
        _history.redoStack.push({ label: entry.label, snapshot: _snapshot() });
        _history._isApplyingPatch = true;
        try {
            _restoreSnapshot(entry.snapshot);
            _renderAll();
        } finally {
            _history._isApplyingPatch = false;
        }
        _updateHistoryButtons();
        showBanner('Undid: ' + entry.label, 'success');
    }
    function _redo() {
        if (_history.redoStack.length === 0) return;
        const entry = _history.redoStack.pop();
        _history.undoStack.push({ label: entry.label, snapshot: _snapshot() });
        _history._isApplyingPatch = true;
        try {
            _restoreSnapshot(entry.snapshot);
            _renderAll();
        } finally {
            _history._isApplyingPatch = false;
        }
        _updateHistoryButtons();
        showBanner('Redid: ' + entry.label, 'success');
    }
    function _updateHistoryButtons() {
        const undoBtn = document.getElementById('vp-undo-btn');
        const redoBtn = document.getElementById('vp-redo-btn');
        if (undoBtn) {
            undoBtn.disabled = _history.undoStack.length === 0;
            undoBtn.title = _history.undoStack.length === 0
                ? 'Nothing to undo (Ctrl+Z)'
                : 'Undo: ' + _history.undoStack[_history.undoStack.length - 1].label + ' (Ctrl+Z)';
        }
        if (redoBtn) {
            redoBtn.disabled = _history.redoStack.length === 0;
            redoBtn.title = _history.redoStack.length === 0
                ? 'Nothing to redo (Ctrl+Y)'
                : 'Redo: ' + _history.redoStack[_history.redoStack.length - 1].label + ' (Ctrl+Y)';
        }
    }
    // Re-render the whole plan UI after a snapshot restore. Wraps
    // renderAllSteps + minimap + form fields so _isRendering is
    // set correctly (suppresses any unintended checkpoint).
    function _renderAll() {
        _history._isRendering = true;
        try {
            renderAllSteps();
            updateMinimap();
            const nameInput = document.getElementById('vp-plan-name');
            const descInput = document.getElementById('vp-plan-description');
            if (nameInput) nameInput.value = _plan.name || '';
            if (descInput) descInput.value = _plan.description || '';
            // If the previously selected step was renamed/deleted,
            // close the side panel so it doesn't show stale form
            // values for a step that no longer exists.
            if (_selectedNodeName) {
                const stillExists = _plan.steps.some(s => s.name === _selectedNodeName);
                if (!stillExists) closeSidePanel();
            }
        } finally {
            _history._isRendering = false;
        }
    }

    // ===== v2.2: Copy / Paste step =====
    // Single-step clipboard (overwritten on each copy). Paste
    // appends a deep-copy of the step with a unique -copy / -copy-N
    // suffix to the name. We don't try to auto-wire anything (the
    // user can drag if they want a similar depends_on pattern).
    function _copySelectedStep() {
        if (!_selectedNodeName) {
            // v3.10.8: instead of showing a banner (which used to
            // be paired with `e.preventDefault()` in the keydown
            // handler and silently broke Ctrl+C text-copy), return
            // a sentinel value so the keydown handler can decide
            // whether to preventDefault. If we return false, the
            // browser keeps its default Ctrl+C behavior — the user
            // can copy selected text on the page (step names, the
            // chatbox output, anything else) without the editor
            // hijacking the keypress.
            return false;
        }
        const step = _plan.steps.find(s => s.name === _selectedNodeName);
        if (!step) {
            showBanner('Selected card is stale (not in _plan.steps). Re-render to refresh.', 'error');
            return false;
        }
        _clipboard = {
            step: JSON.parse(JSON.stringify(step)),
            ts: Date.now(),
        };
        showBanner('Copied step "' + step.name + '". Click another card or an empty spot, then Ctrl+V (or click Paste) to clone.', 'success');
        return true;
    }
    function _pasteClipboard() {
        if (!_clipboard) {
            // v3.10.9: return false so the keydown handler knows
            // to skip preventDefault — the browser keeps its
            // default Ctrl+V.
            return false;
        }
        // Find a unique name. Try `<name>-copy`, `<name>-copy-2`, ...
        // Cap at 1000 so we don't loop forever.
        const baseName = _clipboard.step.name;
        const candidateBase = baseName.replace(/-copy(-\d+)?$/, '') + '-copy';
        let candidate = candidateBase;
        let n = 2;
        const taken = new Set(_plan.steps.map(s => s.name));
        while (taken.has(candidate)) {
            if (n > 999) {
                showBanner('Cannot paste: name collision cap reached (1000 -copy variants)', 'error');
                return false;
            }
            candidate = candidateBase + '-' + n;
            n += 1;
        }
        // Deep copy again so two consecutive pastes don't share
        // nested objects (params_template in particular).
        const newStep = JSON.parse(JSON.stringify(_clipboard.step));
        newStep.name = candidate;
        // Wipe depends_on + feedback_to so the pasted step starts
        // "loose" — the user wires it up explicitly. Auto-wiring
        // would silently re-create the source's intent.
        newStep.depends_on = [];
        newStep.feedback_to = [];
        _checkpoint('Paste step (as "' + candidate + '")');
        _plan.steps.push(newStep);
        renderAllSteps();
        updateMinimap();
        // Open the side panel on the new step so the user can
        // rename / tweak before clicking Apply.
        openSidePanel(candidate);
        showBanner('Pasted as "' + candidate + '". Edits stay in memory until you click Apply.', 'success');
        return true;
    }

    // ===== v2.2: Global keyboard shortcuts =====
    // Bound once on init() (idempotent). Handles:
    //   Ctrl+Z            = undo
    //   Ctrl+Y / Ctrl+Shift+Z = redo
    //   Ctrl+C            = copy selected step (only if a card is
    //                        selected; otherwise let the browser
    //                        do its default text-copy)
    //   Ctrl+V            = paste step (only if the editor's
    //                        clipboard has a step; otherwise let
    //                        the browser do its default text-paste)
    //   Escape            = close side panel
    //
    // v3.10.8 (2026-08-02): undo/redo now ALSO skip text fields
    // (input / textarea / contenteditable). Previously they fired
    // everywhere — which meant Cmd+Z inside the chatbox textarea
    // (the project page has chatbox docked at the bottom) was
    // swallowed by the plan editor's undo and the user could not
    // undo their chat text. The chatbox is part of the same page
    // so its keydown bubbles up to this document-level handler.
    // Letting the native textarea undo work is the expected UX
    // (Cmd+Z inside a text field should always be the field's own
    // undo). The plan editor's undo still works on the canvas and
    // in the side-panel read-only / non-text controls.
    //
    // v3.10.9 (2026-08-02): Ctrl+C / Ctrl+V no longer always
    // preventDefault. The handler used to call e.preventDefault()
    // unconditionally, which meant if no card was selected and
    // the user pressed Ctrl+C to copy text on the page (e.g. a
    // step name in the canvas, the chatbox output, an error
    // message), the editor hijacked the keypress and the browser
    // never copied anything. Fix: the copy/paste handlers return
    // a boolean (true = handled, false = nothing to do). When
    // false, we DON'T call e.preventDefault(), so the browser
    // keeps its default Ctrl+C / Ctrl+V behavior and the user
    // can copy/paste text normally.
    function _bindGlobalShortcuts() {
        if (window._vpShortcutsBound) return;
        window._vpShortcutsBound = true;
        document.addEventListener('keydown', (e) => {
            const ctrl = e.ctrlKey || e.metaKey;
            if (!ctrl) {
                // Escape: close whichever modal/sheet is open (top-most
                // wins). v3.8.0: JSON modal + Save-as-workflow modal
                // were added; same pattern as closeSidePanel — only
                // acts on the visible one so we coexist with other
                // Esc handlers the user may have installed.
                if (e.key === 'Escape') {
                    const jsonOv = document.getElementById('vp-json-modal-overlay');
                    if (jsonOv && !jsonOv.classList.contains('hidden')) {
                        closeJsonModal();
                        e.preventDefault();
                        return;
                    }
                    const gtOv = document.getElementById('vp-generate-tasks-overlay');
                    if (gtOv && !gtOv.classList.contains('hidden')) {
                        // v3.10.10: Generate Tasks modal also closes
                        // on Esc, same pattern as JSON + Save as
                        // workflow modals.
                        closeGenerateTasksModal();
                        e.preventDefault();
                        return;
                    }
                    const sawOv = document.getElementById('vp-save-as-workflow-overlay');
                    if (sawOv && !sawOv.classList.contains('hidden')) {
                        closeSaveAsWorkflowModal();
                        e.preventDefault();
                        return;
                    }
                    const sp = document.getElementById('vp-side-panel');
                    if (sp && !sp.classList.contains('hidden')) {
                        closeSidePanel();
                        e.preventDefault();
                    }
                }
                return;
            }
            const key = e.key.toLowerCase();
            const tag = (e.target && e.target.tagName || '').toLowerCase();
            const isTextField = tag === 'input' || tag === 'textarea' || (e.target && e.target.isContentEditable);
            // v3.10.8: Undo / Redo defer to the native text-field
            // undo when focus is in a text field. Outside text
            // fields, they act on the plan editor history.
            if (isTextField) {
                return;  // let the browser handle Ctrl+Z/Y/C/V
            }
            // Undo / Redo — work everywhere except text fields
            if (key === 'z' && !e.shiftKey) {
                e.preventDefault();
                _undo();
            } else if ((key === 'z' && e.shiftKey) || key === 'y') {
                e.preventDefault();
                _redo();
            } else if (key === 'c' && !e.shiftKey) {
                // v3.10.9: only preventDefault if we actually
                // handled the copy. If no card is selected,
                // return false from _copySelectedStep() and let
                // the browser do its default Ctrl+C so the user
                // can copy text on the page.
                const handled = _copySelectedStep();
                if (handled) e.preventDefault();
            } else if (key === 'v' && !e.shiftKey) {
                // Same pattern for paste. If the editor has no
                // step in its clipboard, fall through to the
                // browser's default Ctrl+V (which the text-field
                // guard above already handles for inputs).
                const handled = _pasteClipboard();
                if (handled) e.preventDefault();
            }
        });
    }

    // ===== Init =====
    // v3.9.0 (Phase 2 UX): SOUL-preset cache. The plan editor needs
    // to know which step.agent_roles have a project_soul_presets row
    // (so the pill on each card can render green vs gray). We fetch
    // /api/projects/{id}/plan/presets once, cache for `ttl_s`
    // seconds, and re-render the canvas when the cache fills so the
    // pills appear without forcing the user to wait. On a cache miss
    // after TTL expiry (the user keeps the editor open for a long
    // time and saves a new preset) the next re-render triggers a
    // background re-fetch. The cache key is per-project so switching
    // projects (via Save as workflow + load) doesn't leak state.
    let _presetCache = null;        // {project_id, presets, fetched_at, ttl_s}
    const _PRESET_TTL_DEFAULT_S = 30;

    async function _loadPresets(force) {
        if (!_projectId) return;
        const now = Date.now() / 1000;
        if (!force && _presetCache
            && _presetCache.project_id === _projectId
            && (now - _presetCache.fetched_at) < (_presetCache.ttl_s || _PRESET_TTL_DEFAULT_S)) {
            return _presetCache.presets;
        }
        try {
            const r = await fetch('/api/projects/' + encodeURIComponent(_projectId) + '/plan/presets',
                { credentials: 'same-origin' });
            if (!r.ok) {
                // 404 (project gone) or 5xx — leave the cache empty
                // so the pill falls back to "unbound" (gray). The
                // next render will retry.
                _presetCache = { project_id: _projectId, presets: [], fetched_at: now, ttl_s: _PRESET_TTL_DEFAULT_S };
                return [];
            }
            const body = await r.json();
            _presetCache = {
                project_id: _projectId,
                presets: (body && body.presets) || [],
                fetched_at: now,
                ttl_s: (body && body.ttl_seconds) || _PRESET_TTL_DEFAULT_S,
            };
            return _presetCache.presets;
        } catch (e) {
            // Network error — leave the cache empty, fall through
            // to "unbound" pill until the next render. Don't surface
            // a banner for a background fetch; the user can save
            // and reload if they need the freshest data.
            _presetCache = { project_id: _projectId, presets: [], fetched_at: now, ttl_s: _PRESET_TTL_DEFAULT_S };
            return [];
        }
    }

    // Build a role_name -> "bound"|"unbound" lookup for the canvas
    // render. A role is "bound" if ANY preset has that role_name
    // (multiple profiles can have the same role; presence of one
    // is enough to mark the step as bound). Empty agent_role is
    // never bound (no preset can match the empty string by design).
    function _presetBoundMap(presets) {
        const m = {};
        for (const p of presets || []) {
            const rn = p && p.role_name;
            if (!rn) continue;
            m[rn] = true;
        }
        return m;
    }

    function init() {
        const wrap = $('vp-wrap');
        if (!wrap) return;
        _projectId = wrap.getAttribute('data-project-id');
        // v3.10.10: read the project's current max_iterations so
        // the Generate Tasks modal can pre-fill the loop-back cap
        // input. Default 3 if the attribute is missing (matches
        // WorkflowRunBody default). 0 is a legitimate "disable"
        // value; the modal handles it.
        const maxIterAttr = parseInt(wrap.getAttribute('data-project-max-iterations') || '3', 10);
        _projectMaxIterations = isNaN(maxIterAttr) ? 3 : maxIterAttr;
        const rawJson = wrap.getAttribute('data-plan-json') || '';
        if (rawJson && rawJson !== '') {
            try { _plan = JSON.parse(rawJson); }
            catch (e) {
                console.error('Failed to parse initial plan JSON:', e);
                _plan = { version: '1.0', name: '', description: '', trigger: 'manual', variables: [], steps: [] };
            }
        }
        // Initialize drawflow. drawflow needs the wrap to exist
        // BEFORE new Drawflow(wrap).start() is called. We construct
        // on a child div because drawflow injects an inner canvas.
        const canvasEl = $('vp-canvas');
        if (!canvasEl) {
            console.error('vp-canvas element not found');
            return;
        }
        // drawflow 0.0.59: new Drawflow(parent, options)
        // The parent is the wrap; drawflow adds its own child div
        // for the canvas. We add a CSS class so we can target it
        // in our styles.
        // eslint-disable-next-line no-undef
        _editor = new Drawflow(canvasEl, {
            reroute: true,    // auto-reroute connections around nodes
            zoom: 1.0,
            zoom_max: 1.6,
            zoom_min: 0.5,
        });
        _editor.start();
        // v1.5.3: apply any persisted visual_layout from the server
        // (set in the previous savePlan() call) onto the drawflow
        // nodes' x/y attributes. drawflow renders nodes at the
        // position given in their `data` object, so this is a one-
        // line write per step.
        // v1.5.3.2: _applyPlanVisualLayout was called here but the
        // data map is empty at init time (no nodes added yet). The
        // actual position restoration now happens in renderAllSteps
        // (each step's x/y is passed to addNode from visual_layout).
        // Keeping the helper around as a safety net / future use.
        // v1.5.3: when the user drags a card, update _plan.visual_layout
        // so the next savePlan() persists the new position. The PUT
        // happens in savePlan() — we don't auto-save on every drag.
        _editor.on('nodeMoved', _capturePlanVisualLayout);
        // Wire the connection lifecycle to _plan.steps[i].depends_on
        // (mirrors visual_workflow.js's pattern). Without this, the
        // user drags a wire on the canvas, drawflow stores it in its
        // internal data map, but _plan.steps[i].depends_on is never
        // updated — so on Save, the empty depends_on is sent and the
        // wire disappears on reload. drawflow 0.0.59 fires
        // 'connectionCreated' with payload { output_id, input_id,
        // output_class, input_class }; payload ids are INTERNAL
        // numeric ids (e.g. "3"), NOT "node-3" or step names.
        _editor.on('connectionCreated', (connection) => {
            _onConnectionCreated(connection);
        });
        // drawflow 0.0.59 does NOT fire a connectionRemoved event
        // when the user removes a wire (only connectionCreated on
        // add). Patch _editor.removeConnection to call our handler
        // manually with the same shape.
        if (_editor.removeConnection && !_editor._vpRemoveConnPatched) {
            const _origRemove = _editor.removeConnection.bind(_editor);
            _editor.removeConnection = (outputId, inputId, outputClass, inputClass) => {
                _origRemove(outputId, inputId, outputClass, inputClass);
                _onConnectionRemoved({
                    output_id: outputId, input_id: inputId,
                    output_class: outputClass, input_class: inputClass,
                });
            };
            _editor._vpRemoveConnPatched = true;
        }
        // nodeRemoved: drawflow fires this when a card is deleted
        // (Delete key, removeNodeId, or our X button calling
        // removeNodeId). Payload is the NUMERIC id string. We use
        // it to scrub the step from _plan.steps so the data stays
        // in sync even if the caller forgot to update _plan first.
        _editor.on('nodeRemoved', (numericId) => {
            _onNodeRemoved(numericId);
        });
        // DEBUG: expose for inspection in dev tools / tests
        if (typeof window !== 'undefined') {
            window.__vp_editor = _editor;
            window.__vp_plan = _plan;
        }
        // Pre-fill plan name/description inputs
        $('vp-plan-name').value = _plan.name || '';
        $('vp-plan-description').value = _plan.description || '';
        // Render initial nodes
        renderAllSteps();
        updateMinimap();
        // v3.9.0 (Phase 2 UX): kick off the preset fetch in the
        // background so the SOUL pill on each card gets a bound vs
        // unbound color. We render once above (pills default to
        // "unbound" / gray), then re-render once the fetch resolves
        // so bound pills switch to green. The user sees a brief
        // gray-then-green flash on first paint — acceptable because
        // the page-load fetch is sub-50ms on local LAN and the user
        // is reading the plan name, not the pill, in that window.
        // We don't await — keep init() synchronous so the editor
        // becomes interactive immediately. The re-render is gated
        // by `_history._isRendering` to avoid creating an
        // undo-stack entry for the background re-render.
        _loadPresets(false).then(() => {
            if (!_editor) return;  // user navigated away before fetch
            _history._isRendering = true;
            try { renderAllSteps(); }
            finally { _history._isRendering = false; }
        });
        // v2.2: bind global keyboard shortcuts (Ctrl+Z/Y/C/V, Escape)
        // and initialize the undo/redo button states (both disabled
        // at start because undo stack is empty).
        _bindGlobalShortcuts();
        // v3.12.5: bind the 4-template palette chips in the toolbar
        // (search / analyze / audit / write). Idempotent — guarded
        // by window._vpChipsBound so re-init doesn't double-bind.
        _bindPaletteChips();
        _bindSaveMenuOutsideClick();
        _updateHistoryButtons();
    }

    // ===== Render: build the in-memory plan from the canvas =====
    // We don't need this — drawflow is the canvas state, and our
    // _plan model is the source of truth. The canvas is REBUILT
    // from _plan whenever the user clicks "Apply JSON to canvas"
    // or when we want to re-render after a delete-all.

    // ===== Connection / node lifecycle helpers =====
    // drawflow 0.0.59 stores nodes in
    //   _editor.drawflow.drawflow.Home.data[internalId]
    // where each entry has fields: name (we set to "vp-" + step.name),
    // class, html, inputs, outputs, pos_x, pos_y. The keys are
    // STRING numbers ("1", "2", ...). Walk this map to translate
    // between step names and internal ids.
    function _dataMap() {
        if (!_editor || !_editor.drawflow || !_editor.drawflow.drawflow) return null;
        const modules = _editor.drawflow.drawflow;
        // The first module is "Home" by default; we don't hardcode
        // it in case a future config has a different default module.
        const moduleName = Object.keys(modules)[0];
        return moduleName ? modules[moduleName].data : null;
    }
    function _stepNameFromInternalId(internalId) {
        const data = _dataMap();
        if (!data) return null;
        const node = data[internalId];
        if (!node || !node.name) return null;
        const m = /^vp-(.+)$/.exec(node.name);
        return m ? m[1] : null;
    }
    function _internalIdFromStepName(name) {
        const data = _dataMap();
        if (!data) return null;
        for (const k of Object.keys(data)) {
            if (data[k] && data[k].name === 'vp-' + name) return k;
        }
        return null;
    }
    // drawflow's connectionCreated payload uses internal numeric ids
    // for output_id and input_id. We translate to step names and
    // append to target.depends_on. This is what was missing before
    // 2026-07-27 — without it, dragging a wire updated drawflow's
    // data but not _plan, so Save sent depends_on=[] and the wire
    // disappeared on reload.
    //
    // v1.9.4 (FLIPPED 2026-07-30 in v2.0): route by output_class.
    //   output_1 (chain)    → target.depends_on += [source]
    //                         (the dependent step lists what it waits for)
    //   output_2 (loop-back)→ source.feedback_to += [target]
    //                         (the FAILING step lists its recovery steps)
    //
    // v2.0 (FLIPPED) explanation: feedback_to is now on the FAILING
    // step (matches the standard on_failure pattern in AWS Step
    // Functions, Airflow, Temporal). A wire from A to B with the
    // red handle means: "if A fails, re-run B". The data lives on A
    // (the failing step), not B (the recovery step). depends_on
    // stays on the dependent step (the "downstream" end of the
    // chain) because that's the natural English reading too:
    // B.depends_on = [A] = "B depends on A".
    //
    // Self-references (target == source) are silently dropped — a
    // step can't loop back to itself.
    function _onConnectionCreated(connection) {
        const sourceInternal = connection.output_id;
        const targetInternal = connection.input_id;
        const sourceName = _stepNameFromInternalId(sourceInternal);
        const targetName = _stepNameFromInternalId(targetInternal);
        if (!sourceName || !targetName) return;
        // Self-reference is a no-op. The plan runner also drops
        // these but dropping here too means we never write the
        // dangling ref to _plan, so Save round-trips clean.
        if (sourceName === targetName) return;
        const outputClass = connection.output_class || 'output_1';
        if (outputClass === 'output_2') {
            // v2.0: feedback_to lives on the SOURCE (failing step)
            const source = _plan.steps.find(s => s.name === sourceName);
            if (!source) return;
            if (!Array.isArray(source.feedback_to)) source.feedback_to = [];
            if (!source.feedback_to.includes(targetName)) {
                source.feedback_to.push(targetName);
            }
        } else {
            // depends_on lives on the TARGET (dependent step) — unchanged
            const target = _plan.steps.find(s => s.name === targetName);
            if (!target) return;
            if (!Array.isArray(target.depends_on)) target.depends_on = [];
            if (!target.depends_on.includes(sourceName)) {
                target.depends_on.push(sourceName);
            }
        }
    }
    // Mirror of _onConnectionCreated for the patched removeConnection.
    // drawflow doesn't fire a "connectionRemoved" event, so we wrap
    // removeConnection (above in init) to call this manually.
    // v2.0: feedback_to is removed from the SOURCE (failing step).
    function _onConnectionRemoved(connection) {
        const sourceInternal = connection.output_id;
        const targetInternal = connection.input_id;
        const sourceName = _stepNameFromInternalId(sourceInternal);
        const targetName = _stepNameFromInternalId(targetInternal);
        if (!sourceName || !targetName) return;
        const outputClass = connection.output_class || 'output_1';
        if (outputClass === 'output_2') {
            // v2.0: feedback_to is on the SOURCE
            const source = _plan.steps.find(s => s.name === sourceName);
            if (!source || !Array.isArray(source.feedback_to)) return;
            source.feedback_to = source.feedback_to.filter(n => n !== targetName);
        } else {
            // depends_on is on the TARGET — unchanged
            const target = _plan.steps.find(s => s.name === targetName);
            if (!target || !Array.isArray(target.depends_on)) return;
            target.depends_on = target.depends_on.filter(n => n !== sourceName);
        }
    }
    // drawflow fires nodeRemoved when a card is removed (Delete key,
    // our X button via removeNodeId, or any removeNode call). Payload
    // is the numeric id as a string. We scrub the step from _plan
    // so the in-memory model matches what's on canvas.
    // v1.9.4: also scrub from any other step's feedback_to list
    // so a deleted step doesn't leave dangling references.
    function _onNodeRemoved(numericId) {
        const name = _stepNameFromInternalId(String(numericId));
        if (!name) return;
        _plan.steps = _plan.steps.filter(s => s.name !== name);
        for (const s of _plan.steps) {
            if (Array.isArray(s.depends_on)) {
                s.depends_on = s.depends_on.filter(n => n !== name);
            }
            if (Array.isArray(s.feedback_to)) {
                s.feedback_to = s.feedback_to.filter(n => n !== name);
            }
        }
        if (_selectedNodeName === name) closeSidePanel();
    }

    // ===== Step rendering =====
    function nodeHtml(step) {
        // Compact card. No skill in the plan editor side (skill is
        // available in the side panel for advanced users).
        // v1.9.4: feedback_to IS supported (it lives in the plan
        // model and the side panel, like depends_on).
        // The .vp-node-header flex wrapper + <span> name element is
        // INTENTIONALLY copied from the workflow page's _stepToCardHtml
        // (see visual_workflow.js). Reason: chromium's headless font
        // rasterizer renders text inside a flex container / inline
        // element correctly, but renders an equivalent <div> at the
        // same font/family/size as faded light gray (darkest pixel
        // 230 vs the correct 17). Verified on 2026-07-27 with a
        // side-by-side Playwright pixel scan. Keep this structure
        // in sync with the workflow page's card HTML.
        const rolePill = step.agent_role
            ? `<span class="vp-node-role">${escapeHtml(step.agent_role)}</span>`
            : `<span class="vp-node-role" style="opacity:0.5">any</span>`;
        // v3.9.0 (Phase 2 UX): SOUL binding pill. "🎯" emoji + the
        // role_name, colored by whether a project_soul_presets row
        // exists. bound = green (preset exists, will apply on
        // dispatch), unbound = gray (orch server will auto-populate
        // on first dispatch). The pill sits in the same .vp-node-header
        // row as the role pill so the user can scan role + SOUL state
        // at a glance. data-soul-bound is set so Playwright / e2e
        // tests can assert the visual state without re-parsing the
        // CSS class.
        //
        // The bound map is built from _presetCache (populated by
        // _loadPresets at init time). On a fresh page load the cache
        // is empty so all pills render as "unbound" briefly; the
        // background fetch completes in <50ms and the init()
        // wrapper re-renders to swap them to "bound" where
        // applicable. We keep the API of nodeHtml synchronous so
        // the renderAllSteps re-render doesn't need to await.
        //
        // Bug history: an earlier version used `\\'` (backslash +
        // apostrophe) inside the single-quoted "unbound" title
        // string. The `\\` becomes a single backslash, and the
        // trailing `'` closes the string early — leaving
        // `s default_soul...` as invalid JS source. Symptom was
        // "Missing } in template expression" (the browser's way of
        // saying the template literal's ${...} expression was
        // unclosed because the syntax error was at the inner
        // string boundary). We use `&apos;` HTML entity in the
        // attribute value instead — safer than escape-sequence
        // gymnastics.
        let soulPill = '';
        if (step.agent_role) {
            const boundMap = _presetBoundMap(_presetCache && _presetCache.presets);
            const isBound = !!boundMap[step.agent_role];
            const cls = isBound ? 'bound' : 'unbound';
            const label = isBound
                ? `🎯 SOUL: ${step.agent_role}`
                : `🎯 auto-SOUL`;
            // For the unbound case, surface the LLM-drafted
            // `default_soul` (set by the chat planner when the
            // role has no preset yet — v3.10.0 "both" mode). We
            // truncate to ~200 chars for the tooltip; the full
            // text is shown in the SOUL preview modal that opens
            // on click (see `openSoulPreview`).
            let tip;
            if (isBound) {
                tip = 'A SOUL preset is bound to this role — the orch server will apply it on dispatch.';
            } else if (step.default_soul && step.default_soul.trim()) {
                const preview = step.default_soul.trim();
                const short = preview.length > 200
                    ? preview.slice(0, 200).replace(/\s+/g, ' ').trim() + '…'
                    : preview;
                tip = 'No preset yet — the LLM drafted this default_soul at plan time. ' +
                    'Click to view the full text.\n\n' + short;
            } else {
                tip = 'No preset yet — the orch server will auto-populate a SOUL on first dispatch ' +
                    '(from a generic role template). Click to add one.';
            }
            // data-soul-default holds the full text for the
            // preview modal. JSON-encode so quote/newline chars
            // survive attribute embedding, then HTML-escape the
            // JSON itself for the surrounding attribute quotes.
            // (visual_plan.js only has `escapeHtml`; the
            // from-template modal in project.html uses
            // `escapeHtmlAttr` but they're identical — both
            // escape & < > " '. We use escapeHtml here.)
            const dataDefault = step.default_soul
                ? escapeHtml(JSON.stringify(step.default_soul))
                : '';
            const onclick = isBound
                ? ''
                : ' onclick="window.VP_SOUL_PREVIEW && window.VP_SOUL_PREVIEW(this)"';
            soulPill = `<span class="vp-node-soul ${cls}" data-soul-bound="${isBound ? '1' : '0'}" data-soul-default='${dataDefault}' title="${escapeHtml(tip)}"${onclick}>${escapeHtml(label)}</span>`;
        }
        const action = step.action
            ? `<div class="vp-node-action">${escapeHtml(step.action)}</div>`
            : '';
        // v3.14.0 (Phase 3): human_approval visual marker — mirror
        // the same yellow border + ⏸ label used in visual_workflow
        // (see _stepToCardHtml in visual_workflow.js). The
        // .vp-node-approval class is added to the card wrapper,
        // CSS in visual_plan.html gives it the yellow border.
        const isApproval = step.type === 'human_approval';
        const approvalClass = isApproval ? ' vp-node-approval' : '';
        const approvalLabel = isApproval
            ? `<div class="vp-node-approval-label">⏸ human approval</div>`
            : '';
        // v1.9.4: also show a small loop-back indicator when the
        // step has any feedback_to wires. Red color matches the
        // wire so the user can see at a glance "this step listens
        // for failures".
        const depsHtml = (step.depends_on && step.depends_on.length)
            ? `<div class="vp-node-deps" style="color:#6b7280;font-size:10px;margin-top:3px">← ${step.depends_on.length} dep${step.depends_on.length === 1 ? '' : 's'}</div>`
            : '';
        const fbHtml = (step.feedback_to && step.feedback_to.length)
            ? `<div class="vp-node-fb" style="color:#dc2626;font-size:10px;margin-top:1px;font-weight:500">↻ ${step.feedback_to.length} loop-back${step.feedback_to.length === 1 ? '' : 's'}</div>`
            : '';
        return `
            <div class="vp-node${approvalClass}" data-step-name="${escapeHtml(step.name)}">
                <button class="vp-node-delete" data-node-name="${escapeHtml(step.name)}" title="Delete step">×</button>
                <div class="vp-node-header">
                    <span class="vp-node-name">${escapeHtml(step.name)}</span>
                    ${rolePill}
                    ${soulPill}
                </div>
                ${action}
                ${approvalLabel}
                ${depsHtml}
                ${fbHtml}
            </div>
        `;
    }

    function addNodeToCanvas(step, x, y) {
        // drawflow 0.0.59: editor.addNode(name, inputs, outputs, posx, posy, class, data, html)
        // The 6th arg is the class applied to the .drawflow-node
        // wrapper. We use 'vp-node' so the .vp-node CSS in the
        // template applies (white card with cyan border, etc.).
        // Without this, drawflow's default style (cyan #0ff bg,
        // white text) leaks through and the card looks washed-out.
        // v1.9.4: each node now has 1 input + 2 outputs:
        //   - output_1 = chain (depends_on, default gray wire)
        //   - output_2 = loop-back (feedback_to, red dashed wire)
        // The visual_workflow.html CSS targets .output_2 for the
        // red dashed styling; the same CSS lives in visual_plan.html.
        // eslint-disable-next-line no-undef
        _editor.addNode(
            'vp-' + step.name,     // node name (stored as `name` on
                                   // the node data, but NOT used as
                                   // the key in drawflow's data map
                                   // — see below)
            1,                      // inputs
            2,                      // outputs (output_1 chain, output_2 loop-back)
            x, y,
            'vp-node',              // class applied to .drawflow-node
            { stepName: step.name }, // node data (custom field)
            nodeHtml(step),         // inner HTML
        );
        // After addNode, drawflow has stored the new node in its
        // data map under an INTERNAL counter id (NOT the name we
        // passed, NOT the DOM id 'node-N'). addConnection uses
        // these internal ids. drawflow's getModuleFromNodeId looks
        // up by data key (counter), not by name, so we have to
        // walk the data map ourselves. There should only be one
        // module ('Home' by default) but we don't hardcode that.
        for (const moduleName of Object.keys(_editor.drawflow.drawflow)) {
            const data = _editor.drawflow.drawflow[moduleName].data;
            for (const k of Object.keys(data)) {
                if (data[k].name === 'vp-' + step.name) {
                    _internalIdByStepName.set(step.name, k);
                    return;
                }
            }
        }
    }

    function wireDepsForStep(step) {
        // addConnection uses the INTERNAL id (the data-map key
        // for the node), NOT the name and NOT the DOM id. We
        // captured these in addNodeToCanvas.
        if (!step.depends_on) return;
        for (const depName of step.depends_on) {
            const sourceId = _internalIdByStepName.get(depName);
            const targetId = _internalIdByStepName.get(step.name);
            if (!sourceId || !targetId) {
                // Forward ref — the dep hasn't been added yet (or
                // this step was just added and we don't have its
                // internal id yet). renderAllSteps retries after
                // a tick.
                continue;
            }
            // WORKAROUND: drawflow 0.0.59's addConnection silently
            // no-ops on a fresh state (we confirmed by reading its
            // source: the if-cond evaluates correctly, the push
            // should fire, but the live array length stays 0).
            // Manual push works reliably. So we do the data push
            // + SVG creation by hand instead.
            _addWireManually(sourceId, targetId, 'output_1');
        }
    }

    // v1.9.4 (FLIPPED 2026-07-30 in v2.0): in the NEW semantic,
    // feedback_to is on the FAILING step (the source), not the
    // target. So when loading from data, we iterate EACH step as
    // a potential source and for each target in its feedback_to,
    // draw wire from this step → target (this step is the failing
    // step; the target is the recovery step). This is the inverse
    // of depends_on's wire (where the target is the dependent).
    //
    // Uses output_2 (the second output handle on each card) so
    // the CSS can render the red dashed style. The wire direction
    // (source → target) is the same as depends_on; only the data
    // placement differs.
    function wireFeedbackForStep(step) {
        if (!step.feedback_to) return;
        for (const targetName of step.feedback_to) {
            // v2.0: source is THIS step (the failing one), target
            // is the one we want to re-run when this fails.
            const sourceId = _internalIdByStepName.get(step.name);
            const targetId = _internalIdByStepName.get(targetName);
            if (!sourceId || !targetId) continue;
            // Self-ref is dropped at the run step too, but skip the
            // wire here so the user doesn't see a confusing wire
            // that goes nowhere.
            if (targetName === step.name) continue;
            _addWireManually(sourceId, targetId, 'output_2');
        }
    }

    // Manually add a connection: push to the data map + create the
    // SVG path. Mirrors what drawflow 0.0.59's addConnection does,
    // but the in-library version silently no-ops (see wireDepsForStep
    // comment). Tested by: doing the same push by hand in the dev
    // console — array length goes 0 -> 1 as expected.
    //
    // v1.9.4: outputClass is the SOURCE output class — 'output_1'
    // (depends_on) or 'output_2' (feedback_to). The target input
    // class is always 'input_1' (we only have one input per card).
    // The SVG gets the output class stamped on it so the CSS can
    // color depends_on gray and feedback_to red dashed.
    function _addWireManually(sourceId, targetId, outputClass) {
        if (!_editor) return;
        outputClass = outputClass || 'output_1';
        try {
            // Push to data: source's outputs.<outputClass>.connections
            const sourceData = _editor.drawflow.drawflow.Home.data[sourceId];
            const targetData = _editor.drawflow.drawflow.Home.data[targetId];
            if (!sourceData || !targetData) return;
            const sourceOut = sourceData.outputs[outputClass];
            const targetIn = targetData.inputs.input_1;
            if (!sourceOut || !targetIn) return;
            // Skip if already connected (check both classes — a
            // step can in theory have both depends_on and feedback_to
            // to the same source, which is OK, but two output_1
            // wires or two output_2 wires would be a drawflow bug).
            for (const c of sourceOut.connections) {
                if (c.node == targetId && c.output == 'input_1') return;
            }
            sourceOut.connections.push({node: targetId.toString(), output: 'input_1'});
            targetIn.connections.push({node: sourceId.toString(), input: outputClass});
            // Create the SVG path (drawflow uses SVG for wires)
            if (_editor.precanvas && _editor.module === 'Home') {
                const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
                const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
                path.classList.add("main-path");
                path.setAttributeNS(null, "d", "");
                svg.classList.add("connection");
                svg.classList.add("node_in_node-" + targetId);
                svg.classList.add("node_out_node-" + sourceId);
                svg.classList.add(outputClass);  // output_1 or output_2 — CSS hook
                svg.classList.add("input_1");
                svg.appendChild(path);
                _editor.precanvas.appendChild(svg);
                // Update the wire geometry (drawflow's updateConnectionNodes)
                if (typeof _editor.updateConnectionNodes === 'function') {
                    _editor.updateConnectionNodes("node-" + sourceId);
                    _editor.updateConnectionNodes("node-" + targetId);
                }
            }
            // Fire the event for any listeners
            if (typeof _editor.dispatch === 'function') {
                _editor.dispatch("connectionCreated", {
                    output_id: sourceId, input_id: targetId,
                    output_class: outputClass, input_class: "input_1",
                });
            }
        } catch (e) {
            // swallow — drawflow throws if either node is gone
        }
    }

    function renderAllSteps() {
        if (!_editor) return;
        // Clear existing (also resets drawflow's internal counter,
        // so subsequent node ids start back from 1)
        _editor.clear();
        // Reset our step name -> internal id map; rebuild as we
        // add nodes (each addNodeToCanvas populates it)
        _internalIdByStepName.clear();
        // v1.5.3.2 (2026-07-29): was placing every step on a hardcoded
        // grid (baseX + col*dx, baseY + row*dy) regardless of the
        // persisted visual_layout. That meant save + reload always
        // snapped the cards back to the default grid — the user
        // observed this as "drag -> save -> reload -> positions reset".
        // The fix: read _plan.visual_layout[step.name] first; fall back
        // to the default grid for steps added since the last save
        // (no entry in the layout). Capture side already writes the
        // new pos to _plan.visual_layout on every nodeMoved, so the
        // very next savePlan() persists it.
        const layout = _plan.visual_layout || {};
        const baseX = 100, baseY = 100;
        const dx = 280, dy = 130;
        for (let i = 0; i < _plan.steps.length; i++) {
            const step = _plan.steps[i];
            const saved = layout[step.name];
            if (saved && typeof saved.x === 'number' && typeof saved.y === 'number') {
                addNodeToCanvas(step, saved.x, saved.y);
            } else {
                const col = i % 3;
                const row = Math.floor(i / 3);
                addNodeToCanvas(step, baseX + col * dx, baseY + row * dy);
            }
        }
        // Wire deps. We retry once after a tick to handle forward
        // refs (a step depending on a step that hasn't been added
        // yet because of position ordering).
        for (const step of _plan.steps) wireDepsForStep(step);
        // v1.9.4: also wire feedback_to (loop-back, red dashed).
        // Same retry-once-after-tick pattern.
        for (const step of _plan.steps) wireFeedbackForStep(step);
        // v1.9.4: add a `title` attribute to each card's two
        // output handles so the user can tell them apart. drawflow
        // auto-creates .output_1 and .output_2 divs inside the
        // .drawflow-node but doesn't add title attrs; we do it
        // here. Hint: output_1 is the chain edge, output_2 is the
        // loop-back edge.
        _annotateOutputHandles();
        setTimeout(() => {
            for (const step of _plan.steps) wireDepsForStep(step);
            for (const step of _plan.steps) wireFeedbackForStep(step);
        }, 50);
    }

    // v1.9.4 (v2.0 updated 2026-07-30): stamp `title` attributes
    // on each card's two output handles so the user can tell them
    // apart without a legend tour. Mirrors visual_workflow.js's
    // _annotateOutputHandles.
    //   output_1 (chain, normal):     adds target.depends_on += [this]
    //   output_2 (loop-back, red dashed):
    //                                 adds this.feedback_to += [target]
    //                                 ("if I fail, re-run target")
    function _annotateOutputHandles() {
        try {
            const wrap = document.getElementById('vp-canvas-wrap') || document;
            for (const el of wrap.querySelectorAll('.drawflow-node')) {
                const o1 = el.querySelector('.output_1');
                const o2 = el.querySelector('.output_2');
                if (o1 && !o1.title) o1.title = 'chain (target depends on this)';
                if (o2 && !o2.title) o2.title = 'loop-back (if I fail, re-run target)';
            }
        } catch (e) {
            // best-effort, don't break the canvas if drawflow's
            // internal class names change in a future version
        }
    }

    // ===== Step CRUD =====
    // v3.12.5: 4-template palette (search / analyze / audit / write),
    // mirroring visual_workflow.js so the UX is consistent across
    // both editors. The bare `+ Add step` button (which produced a
    // step with all-empty fields) is gone — every new step now starts
    // with a sensible `action` so the user can immediately tell what
    // the step is supposed to do. Fields like `agent_role` stay
    // empty because they depend on the user's runtime choices; the
    // side panel is opened automatically so the user can fill them
    // in without an extra click.
    const _PALETTE_ACTIONS = {
        search:  'fetch_url',
        analyze: 'summarize',
        audit:   'audit_check',
        write:   'write_output',
        // v3.14.0 (Phase 3): human_approval palette chip — adds a
        // step with type="human_approval" pre-set. The action is
        // descriptive ("manual_review") for display; the runtime
        // recognizes the step by its type, not its action. The
        // `approval` sub-object is configured in the side panel
        // after the step is added (we don't pre-fill a default
        // summary_template because that's user-specific copy).
        human_approval: 'manual_review',
    };
    function _newStepFromTemplate(tmpl) {
        const action = _PALETTE_ACTIONS[tmpl] || '';
        // Pick a unique name by appending a numeric suffix, so the
        // user can click the same chip multiple times in a row and
        // get `search-1`, `search-2`, ... without conflicts.
        const used = new Set(_plan.steps.map(s => s.name));
        let n = 1;
        let name = `${tmpl}-${n}`;
        while (used.has(name)) {
            n += 1;
            name = `${tmpl}-${n}`;
        }
        const step = {
            name,
            agent_role: '',
            action: action,
            skill: '',
            tool: '',
            required_capability: '',
            depends_on: [],
            feedback_to: [],  // v1.9.4: loop-back field
            params_template: {},
            output_path: '',
        };
        // v3.14.0 (Phase 3): human_approval chip sets type
        // explicitly. Without this, the step would default to
        // do_task and the supervisor would dispatch an agent
        // task instead of creating an inbox approval. We don't
        // set default_soul on a human_approval step (no agent
        // runs it).
        if (tmpl === 'human_approval') {
            step.type = 'human_approval';
        }
        return step;
    }
    function _addStepFromChip(tmpl) {
        const newStep = _newStepFromTemplate(tmpl);
        // Chain mode: depends_on = [last step's name]. Skip if no
        // prior step (first chip click on an empty plan). Same shape
        // as visual_workflow.js:1559-1600.
        const lastStep = _plan.steps.length > 0
            ? _plan.steps[_plan.steps.length - 1]
            : null;
        if (lastStep) {
            newStep.depends_on = [lastStep.name];
        }
        // v2.2: checkpoint BEFORE the push so undo restores the
        // pre-add state.
        _checkpoint('Add step "' + newStep.name + '"');
        _plan.steps.push(newStep);
        // Place below the lowest existing node, same logic as the
        // old addStep() helper.
        const lastIdx = _plan.steps.length - 1;
        addNodeToCanvas(newStep, 100 + (lastIdx % 3) * 280, 100 + Math.floor(lastIdx / 3) * 130);
        // Chain mode: draw the wire from the last step to the new
        // one. wireDepsForStep reads the internal-id map populated
        // by addNodeToCanvas (for both the source and target), so
        // it works whether the source step was added via chip or
        // via the initial plan_json render. The old bare
        // + Add step button didn't draw any wire (depends_on was
        // always []), so chip flow is strictly better.
        wireDepsForStep(newStep);
        // v2.2: auto-select the new step so the user can immediately
        // Ctrl+C → Ctrl+V to clone, or dblclick to edit. We also
        // open the side panel so the user sees what fields still
        // need filling (agent_role, params_template, etc.) without
        // a second click.
        _selectedNodeName = newStep.name;
        openSidePanel(newStep.name);
        updateMinimap();
        showBanner(
            `Added step: ${newStep.name}` +
            (lastStep ? ` (auto-wired from ${lastStep.name}).` : '.'),
            'success'
        );
    }
    function _bindPaletteChips() {
        // v3.12.5: bind the 4-template palette chips in the toolbar.
        // Idempotent — guarded by a window flag so a re-init (e.g. after
        // SPA navigation) doesn't double-bind.
        if (window._vpChipsBound) return;
        window._vpChipsBound = true;
        document.querySelectorAll('.vp-palette-chip').forEach((chip) => {
            chip.addEventListener('click', () => {
                const tmpl = chip.dataset.template;
                _addStepFromChip(tmpl);
            });
        });
    }

    function deleteStepByName(name) {
        _checkpoint('Delete step "' + name + '"');  // v2.2: undo support
        // Remove from plan model (in-memory). The nodeRemoved event
        // listener we registered in init() will fire when drawflow
        // actually removes the card, and that listener will run
        // ANOTHER filter pass — that's a no-op the second time
        // because the step is already gone. We do the filter here
        // first so the in-memory model is correct even if removeNodeId
        // throws (e.g. on a stale step name).
        _plan.steps = _plan.steps.filter(s => s.name !== name);
        // Scrub from any other step's depends_on AND feedback_to
        // (v1.9.4) so a deleted step doesn't leave dangling refs.
        for (const s of _plan.steps) {
            if (s.depends_on) s.depends_on = s.depends_on.filter(d => d !== name);
            if (s.feedback_to) s.feedback_to = s.feedback_to.filter(d => d !== name);
        }
        // Remove the card from the canvas. drawflow expects the
        // DOM id (e.g. "node-3"), NOT the step name. We look up
        // the internal id by walking the data map (set up in
        // addNodeToCanvas). removeNodeId is the public API; it
        // fires the 'nodeRemoved' event which we listen to.
        if (_editor) {
            const internalId = _internalIdFromStepName(name);
            if (internalId != null) {
                try { _editor.removeNodeId('node-' + internalId); }
                catch (e) { /* may already be removed */ }
            }
        }
        // Clear side panel if it was showing this step
        if (_selectedNodeName === name) closeSidePanel();
    }

    function deleteSelectedStep() {
        if (!_selectedNodeName) return;
        if (!confirm(`Delete step "${_selectedNodeName}"?\n\nThis also removes it from any other step's depends_on.`)) return;
        deleteStepByName(_selectedNodeName);
        showBanner('Step deleted', 'success');
    }

    // ===== Side panel =====
    function openSidePanel(stepName) {
        const step = _plan.steps.find(s => s.name === stepName);
        if (!step) return;
        _selectedNodeName = stepName;
        // v3.12.6 (Phase 3): context-aware editing. Write the
        // selected step to sessionStorage so the chatbox LLM sees
        // it in the FOCUS block and can target proposals at this
        // step. The plan editor doesn't have apply_plan_patch
        // yet (deferred to Phase 4 per spec §13), so the LLM uses
        // this context for prose + update_plan targeting only.
        if (window.chatbox && typeof window.chatbox.setSelectedNode === 'function') {
            window.chatbox.setSelectedNode({
                kind: 'plan_step',
                project_id: _projectId,
                step_name: step.name,
                action: step.action || '',
                agent_role: step.agent_role || '',
                required_capability: step.required_capability || '',
                skill: step.skill || '',
                depends_on: Array.isArray(step.depends_on) ? step.depends_on : [],
            });
        }
        $('vp-side-title').textContent = 'Step: ' + stepName;
        $('vp-f-name').value = step.name;
        $('vp-f-action').value = step.action || '';
        $('vp-f-role').value = step.agent_role || '';
        // v3.14.0 (Phase 3): type field — default to "do_task" for
        // legacy steps that don't have the field set yet. The server
        // already defaults this, but populating it client-side gives
        // a clearer UX.
        $('vp-f-type').value = step.type || 'do_task';
        $('vp-f-capability').value = step.required_capability || '';
        $('vp-f-skill').value = step.skill || '';
        $('vp-f-output').value = step.output_path || '';
        $('vp-f-params').value = JSON.stringify(step.params_template || {}, null, 2);
        $('vp-f-deps').value = (step.depends_on || []).join(', ');
        // v1.9.4: feedback_to (loop-back). Same comma-separated
        // format as depends_on; user can also drag a red dashed
        // wire from the card's output_2 handle.
        $('vp-f-feedback-to').value = (step.feedback_to || []).join(', ');
        // Hide the inline error from any prior failed save.
        const errEl = $('vp-f-error');
        if (errEl) { errEl.classList.add('hidden'); errEl.textContent = ''; }
        $('vp-side-panel').classList.remove('hidden');
        // Highlight the selected node visually. drawflow wraps the
        // node in <div id="node-vp-{name}" class="parent-node
        // drawflow-node vp-node ...">. The .vp-node class is the one
        // we apply via addNode's classoverride; we just need to
        // query the .drawflow-node element by its id.
        document.querySelectorAll('.vp-node').forEach(n => n.classList.remove('selected'));
        const nodeEl = document.getElementById('node-vp-' + stepName);
        if (nodeEl) nodeEl.classList.add('selected');
    }

    function closeSidePanel() {
        _selectedNodeName = null;
        // v3.12.6 (Phase 3): clear the chatbox FOCUS context so the
        // next chat message doesn't carry a stale step reference.
        if (window.chatbox && typeof window.chatbox.clearSelectedNode === 'function') {
            window.chatbox.clearSelectedNode();
        }
        $('vp-side-panel').classList.add('hidden');
        document.querySelectorAll('.vp-node').forEach(n => n.classList.remove('selected'));
    }

    function saveStepEdits() {
        if (!_selectedNodeName) return;
        _checkpoint('Edit step "' + _selectedNodeName + '"');  // v2.2: undo support
        const step = _plan.steps.find(s => s.name === _selectedNodeName);
        if (!step) return;
        const newName = ($('vp-f-name').value || '').trim();
        if (!newName) { _showSideError('Name required'); return; }
        if (!KEBAB_RE.test(newName)) {
            _showSideError('Name must be kebab-case (lowercase letters, digits, hyphens)');
            return;
        }
        // If name changed, check uniqueness + update refs
        if (newName !== step.name) {
            if (_plan.steps.some(s => s.name === newName)) {
                _showSideError('A step with that name already exists');
                return;
            }
            const oldName = step.name;
            step.name = newName;
            // Update depends_on AND feedback_to (v1.9.4) in other steps
            for (const s of _plan.steps) {
                if (s.depends_on) {
                    s.depends_on = s.depends_on.map(d => d === oldName ? newName : d);
                }
                if (s.feedback_to) {
                    s.feedback_to = s.feedback_to.map(d => d === oldName ? newName : d);
                }
            }
            // Update the canvas node id
            const oldId = 'vp-' + oldName;
            const newId = 'vp-' + newName;
            // drawflow 0.0.59 doesn't have a public rename API;
            // the easiest reliable way is to remove + re-add the
            // node with the new id, preserving position.
            try { _editor.removeNode(oldId); } catch (e) {}
            // Re-render the whole canvas (preserves deps via the
            // model). Simple, robust. For 10+ step plans this
            // becomes expensive; Phase C+ will add a smarter
            // rename path.
            renderAllSteps();
        }
        step.action = $('vp-f-action').value.trim();
        step.agent_role = $('vp-f-role').value.trim();
        // v3.14.0 (Phase 3): persist the type field. Use null when
        // it equals the default to keep plan_json minimal (matches
        // the server's behavior of defaulting missing type to
        // "do_task"). This keeps round-tripped plans looking clean.
        const newType = ($('vp-f-type').value || 'do_task').trim();
        if (newType && newType !== 'do_task') {
            step.type = newType;
        } else {
            delete step.type;
        }
        step.required_capability = $('vp-f-capability').value.trim();
        step.skill = $('vp-f-skill').value.trim();
        step.output_path = $('vp-f-output').value.trim();
        // params: parse JSON, fall back to empty dict
        const paramsRaw = $('vp-f-params').value.trim();
        if (paramsRaw) {
            try { step.params_template = JSON.parse(paramsRaw); }
            catch (e) { _showSideError('Params must be valid JSON: ' + e.message); return; }
        } else {
            step.params_template = {};
        }
        // depends_on: comma-separated, trimmed
        const depsRaw = $('vp-f-deps').value.trim();
        step.depends_on = depsRaw
            ? depsRaw.split(',').map(s => s.trim()).filter(s => s)
            : [];
        // v1.9.4: feedback_to (loop-back). Same format. Self-refs
        // are dropped silently — a step can't loop back to itself.
        const fbRaw = $('vp-f-feedback-to').value.trim();
        step.feedback_to = fbRaw
            ? fbRaw.split(',').map(s => s.trim()).filter(s => s && s !== step.name)
            : [];
        // Re-render the canvas (to update the node card content
        // + the wires from the new depends_on / feedback_to).
        renderAllSteps();
        // Re-open the side panel on the same step (render cleared
        // the selection).
        openSidePanel(step.name);
        // v3.14.0 (Phase 3 followup): mark the plan as having
        // unsaved step changes. The top "Save" button shows the
        // count (e.g. "Save (1 change)"). Without this, users
        // assumed "Save step" persisted to the server — it only
        // updates the in-memory _plan model. The server-side
        // persistence happens in savePlan() which the user must
        // trigger explicitly. This was a recurring confusion
        // reported during Phase 3 testing.
        _dirtyStepCount = (_dirtyStepCount || 0) + 1;
        _updateSaveDirtyIndicator();
        showBanner('Step saved (click top "Save" to persist)', 'success');
    }

    // v3.14.0 (Phase 3 followup): helper to update the top "Save"
    // button with the number of unsaved step changes. Reset to 0
    // after a successful savePlan().
    function _updateSaveDirtyIndicator() {
        const btn = $('vp-save-btn');
        if (!btn) return;
        const n = _dirtyStepCount || 0;
        if (n > 0) {
            btn.textContent = `💾 Save (${n} change${n === 1 ? '' : 's'})`;
            btn.classList.add('bg-amber-600', 'hover:bg-amber-700');
            btn.classList.remove('bg-cyan-600', 'hover:bg-cyan-700');
            btn.title = `${n} unsaved step change${n === 1 ? '' : 's'} — click to persist to server`;
        } else {
            btn.textContent = '💾 Save';
            btn.classList.remove('bg-amber-600', 'hover:bg-amber-700');
            btn.classList.add('bg-cyan-600', 'hover:bg-cyan-700');
            btn.title = 'Persist the current plan to the server';
        }
    }

    // v1.9.4: side-panel inline error display. Mirrors
    // visual_workflow.html's #vf-edit-error. Used for soft errors
    // (validation failures) that don't justify a top-of-page
    // banner. The error sticks until the next successful save
    // (openSidePanel hides it).
    function _showSideError(msg) {
        const errEl = $('vp-f-error');
        if (!errEl) {
            // Fallback: no error element in DOM (older template),
            // use the banner.
            showBanner(msg, 'error');
            return;
        }
        errEl.textContent = msg;
        errEl.classList.remove('hidden');
    }

    // ===== Min-map (basic) =====
    function updateMinimap() {
        const minimap = $('vp-minimap');
        const empty = $('vp-minimap-empty');
        if (!minimap) return;
        if (_plan.steps.length === 0) {
            // Show empty placeholder
            if (empty) empty.style.display = 'block';
            // Remove any existing canvas clone
            const oldClone = minimap.querySelector('.vp-minimap-inner');
            if (oldClone) oldClone.remove();
            return;
        }
        if (empty) empty.style.display = 'none';
        // Phase C basic minimap: just show a count badge per step
        // (no actual canvas clone — the .vp-canvas-wrap child is
        // too complex to clone safely). For Phase C+ we'll do a
        // proper scaled mini-canvas.
        const inner = document.createElement('div');
        inner.className = 'vp-minimap-inner';
        inner.style.padding = '6px';
        inner.style.fontSize = '9px';
        inner.style.color = '#6b7280';
        inner.style.lineHeight = '1.6';
        inner.innerHTML = _plan.steps.map((s, i) =>
            `<div>${i + 1}. ${escapeHtml(s.name)}</div>`
        ).join('');
        // Replace existing
        const oldClone = minimap.querySelector('.vp-minimap-inner');
        if (oldClone) oldClone.remove();
        minimap.appendChild(inner);
    }

    // ===== Save / Generate / Validate =====
    async function savePlan() {
        // Refresh plan model from the toolbar inputs
        _plan.name = $('vp-plan-name').value.trim();
        _plan.description = $('vp-plan-description').value.trim();
        // depends_on is kept in sync by the connectionCreated /
        // connectionRemoved listeners we registered in init() — no
        // need to walk the drawflow data map here. (The old
        // syncDepsFromCanvas() did this walk and had a wrong path,
        // which is why wires were silently lost on Save before
        // 2026-07-27.)
        try {
            const r = await fetch('/api/projects/' + _projectId + '/plan', {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({plan: _plan}),
            });
            if (!r.ok) {
                const d = await r.json().catch(() => ({}));
                showBanner('Save failed: ' + _errDetailToString(d.detail, r.status), 'error');
                return;
            }
            // v3.9.0 (Phase 2 UX): the plan may have a brand-new
            // step.agent_role that needs a fresh preset lookup
            // (e.g. user added a "reviewer" step to a project that
            // has no reviewer preset yet). Invalidate the cache so
            // the next render fetches the new state. We don't
            // re-render here — the user-visible state didn't change
            // (pills are still "unbound" until they save a preset on
            // the project page); the next drag/edit will re-render
            // with the fresh data.
            _presetCache = null;
            // v3.14.0 (Phase 3 followup): clear the dirty-step
            // counter on successful save. The button reverts to
            // its default "Save" label and the unsaved-changes
            // highlight goes away.
            _dirtyStepCount = 0;
            _updateSaveDirtyIndicator();
            showBanner('Plan saved (' + _plan.steps.length + ' step(s))', 'success');
        } catch (e) {
            showBanner('Network error: ' + e.message, 'error');
        }
    }

    // Keep syncDepsFromCanvas around as a defensive backup in case
    // a connection was created/removed before our listeners were
    // bound (race during init). It's not used by savePlan() anymore
    // — the connection lifecycle is the source of truth. If we ever
    // need to forcibly resync (e.g. after a JSON import), call this.
    function syncDepsFromCanvas() {
        if (!_editor) return;
        const data = _dataMap();
        if (!data) return;
        for (const step of _plan.steps) {
            const internalId = _internalIdFromStepName(step.name);
            if (internalId == null) { step.depends_on = []; continue; }
            const node = data[internalId];
            if (!node || !node.outputs) { step.depends_on = []; continue; }
            const deps = [];
            for (const outKey of Object.keys(node.outputs)) {
                const out = node.outputs[outKey];
                if (!out || !out.connections) continue;
                for (const conn of out.connections) {
                    if (conn.node == null) continue;
                    const sourceName = _stepNameFromInternalId(String(conn.node));
                    if (sourceName) deps.push(sourceName);
                }
            }
            step.depends_on = deps;
        }
    }

    async function generateTasks() {
        if (_plan.steps.length === 0) {
            showBanner('Plan has no steps. Add some first.', 'error');
            return;
        }
        // Save first so the operator's latest plan is on the server
        // before we open the confirm modal. If save fails, we don't
        // show the modal (the user needs to fix the save error first).
        await savePlan();
        // v3.10.10 (2026-08-02): open the Generate Tasks modal
        // instead of the bare `confirm()` dialog. The modal lets
        // the operator set the loop-back cap (max_iterations) so
        // any step.feedback_to on the plan actually fires. Mirrors
        // the workflow Run modal's "Loop-back cap" field. See
        // _doGenerateTasks() below for the actual fetch.
        openGenerateTasksModal();
    }

    // ===== v3.10.10 (2026-08-02): Generate Tasks modal =====
    // Replaces the v2.2 `confirm()` dialog. Lets the operator
    // set the loop-back cap at Generate-task time so any
    // step.feedback_to on the plan actually fires. Without
    // this UI, projects default to max_iterations=0 and the
    // supervisor's _maybe_loop_back returns False fast — the
    // red dashed wires in the visual builder would silently
    // no-op.
    function openGenerateTasksModal() {
        const ov = $('vp-generate-tasks-overlay');
        if (!ov) return;
        // Update the "what this does" copy to reflect the current
        // step count. The modal template uses a span with id
        // vp-generate-tasks-step-count.
        const cnt = $('vp-generate-tasks-step-count');
        if (cnt) cnt.textContent = String(_plan.steps.length);
        // Pre-fill the loop-back cap with the project's current
        // value. The operator can override; we send whatever they
        // type to the server. Default 3 (matches WorkflowRunBody
        // default and the workflow Run modal's placeholder).
        const input = $('vp-generate-tasks-max-iter');
        if (input) {
            // Pre-fill with the project's value. If it's 0, the
            // operator sees the warning text; they can type 3+
            // to enable.
            input.value = String(_projectMaxIterations || 0);
        }
        // Show a small note if the current value is 0 (warning)
        // so the operator knows the implications.
        const cur = $('vp-generate-tasks-current');
        if (cur) {
            if (!_projectMaxIterations) {
                cur.textContent =
                    '⚠ Currently 0 — feedback_to wires will be no-ops unless you raise this to 3+ before submitting.';
                cur.classList.remove('hidden');
                cur.classList.add('text-amber-700');
            } else {
                cur.textContent = 'Currently ' + _projectMaxIterations + ' for this project.';
                cur.classList.remove('hidden');
                cur.classList.remove('text-amber-700');
            }
        }
        // Clear any previous error
        const err = $('vp-generate-tasks-error');
        if (err) { err.classList.add('hidden'); err.textContent = ''; }
        // Show submit button, hide spinner
        const sub = $('vp-generate-tasks-submit');
        if (sub) sub.disabled = false;
        const sp = $('vp-generate-tasks-spinner');
        if (sp) sp.classList.add('hidden');
        ov.classList.remove('hidden');
    }
    function closeGenerateTasksModal() {
        const ov = $('vp-generate-tasks-overlay');
        if (ov) ov.classList.add('hidden');
    }
    async function submitGenerateTasks() {
        const input = $('vp-generate-tasks-max-iter');
        if (!input) return;
        // Validate. Empty / NaN / negative → 0 (explicit disable,
        // same as the workflow Run modal allows).
        let maxIter = parseInt(input.value, 10);
        if (isNaN(maxIter) || maxIter < 0) maxIter = 0;
        if (maxIter > 99) maxIter = 99;  // defensive cap; the server
                                        // doesn't enforce this but a
                                        // sane UI shouldn't accept
                                        // 99999.
        // Show the spinner, disable the submit button
        const sub = $('vp-generate-tasks-submit');
        if (sub) sub.disabled = true;
        const sp = $('vp-generate-tasks-spinner');
        if (sp) sp.classList.remove('hidden');
        try {
            await _doGenerateTasks({max_iterations: maxIter});
            // _doGenerateTasks handles success (banner + redirect)
            // and most failures inline. If it didn't redirect, the
            // modal is still open — close it on success.
            // (Failure path keeps the modal open so the operator
            // can retry with a different cap.)
            const ov = $('vp-generate-tasks-overlay');
            // _doGenerateTasks redirects on success via setTimeout
            // (1500ms). Detect by checking the banner class —
            // simpler: just close after the await returns and let
            // the success banner speak. If _doGenerateTasks throws
            // / returns early without redirecting, the operator
            // sees the banner and we re-enable the modal.
            if (ov && !ov.classList.contains('hidden')) {
                // Give the banner a beat, then re-enable on failure
                setTimeout(() => {
                    if (ov && !ov.classList.contains('hidden')) {
                        // still open → failure path
                        if (sub) sub.disabled = false;
                        if (sp) sp.classList.add('hidden');
                    }
                }, 800);
            }
        } catch (e) {
            const err = $('vp-generate-tasks-error');
            if (err) { err.classList.remove('hidden'); err.textContent = 'Network error: ' + e.message; }
            if (sub) sub.disabled = false;
            if (sp) sp.classList.add('hidden');
        }
    }

    // _doGenerateTasks: the actual /plan/run call. Extracted so
    // both the modal submit AND the (legacy) reset-retry path can
    // call it. Passes max_iterations in the body so the operator's
    // chosen cap is applied to the project before tasks are
    // dispatched.
    async function _doGenerateTasks({max_iterations}) {
        try {
            const r = await fetch('/api/projects/' + _projectId + '/plan/run', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    archive_existing: true,
                    name_suffix: '',
                    max_iterations: max_iterations,
                }),
            });
            if (!r.ok) {
                const d = await r.json().catch(() => ({}));
                const detail = _errDetailToString(d.detail, r.status);
                showBanner('Generate failed: ' + detail, 'error');
                // Per user feedback 2026-07-28: the plan editor
                // used to fail with "Cannot run plan on a
                // terminal-state project" and the user had no
                // UI affordance to fix it (had to SQL-update or
                // create a new project). Offer a one-click
                // "Reset to planned" right here.
                if (typeof detail === 'string' && detail.includes('terminal-state project')) {
                    if (confirm(
                        'This project is in a terminal state (' + detail + ').\n\n' +
                        'Reset to "planned" so you can re-run with the new plan steps?\n\n' +
                        '(Existing tasks will be kept in the DB as archived; the plan\n' +
                        'is preserved; this just clears the completion flag.)'
                    )) {
                        try {
                            const rr = await fetch('/api/projects/' + _projectId + '/plan/reset', {
                                method: 'POST',
                            });
                            const rj = await rr.json().catch(() => ({}));
                            if (!rr.ok) {
                                showBanner('Reset failed: ' + _errDetailToString(rj.detail, rr.status), 'error');
                                return;
                            }
                            showBanner('Reset to planned. Retrying...', 'success');
                            // Retry the run with the same max_iterations.
                            const r2 = await fetch('/api/projects/' + _projectId + '/plan/run', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({
                                    archive_existing: true,
                                    name_suffix: '',
                                    max_iterations: max_iterations,
                                }),
                            });
                            if (!r2.ok) {
                                const d2 = await r2.json().catch(() => ({}));
                                showBanner('Generate failed: ' + _errDetailToString(d2.detail, r2.status), 'error');
                                return;
                            }
                            const d2 = await r2.json();
                            // v3.10.4: clarify that the user must
                            // click [▶ Run] on the project page to
                            // dispatch. Previously this banner
                            // implied auto-dispatch ("going to
                            // project...") which was misleading
                            // after the no-auto-dispatch change.
                            showBanner('Generated ' + d2.tasks_created + ' task(s). Click [▶ Run] on the project page to dispatch.', 'success');
                            closeGenerateTasksModal();
                            setTimeout(() => { location.href = '/projects/' + _projectId; }, 1500);
                            return;
                        } catch (e2) {
                            showBanner('Reset retry error: ' + e2.message, 'error');
                            return;
                        }
                    }
                }
                return;
            }
            const d = await r.json();
            // v3.10.4: clarify that the user must click [▶ Run]
            // on the project page to dispatch. Previously this
            // banner implied auto-dispatch which was misleading
            // after the no-auto-dispatch change.
            showBanner('Generated ' + d.tasks_created + ' task(s). Click [▶ Run] on the project page to dispatch.', 'success');
            closeGenerateTasksModal();
            setTimeout(() => { location.href = '/projects/' + _projectId; }, 1500);
        } catch (e) {
            showBanner('Network error: ' + e.message, 'error');
            throw e;  // let the modal handler re-enable the form
        }
    }

    // NOTE: validatePlan() was removed in 2026-08-07. The button
    // was redundant — the Save button (savePlan → PUT /api/projects/{id}/plan)
    // does the authoritative server-side validation, and the
    // client-side check was a strict subset of what the server
    // catches. Plus the alert() dialog was jarring vs. the
    // inline error elements used everywhere else in the UI.
    // If a fast-feedback "validate as you type" UX is wanted
    // later, re-add it with inline errors (mirror the visual_workflow
    // editor's #vf-edit-error pattern) and a new button label.

    // ===== JSON mode toggle =====
    // v3.8.0: the "Edit JSON" button now opens a modal overlay
    // (#vp-json-modal-overlay in visual_plan.html) instead of toggling
    // a hidden bottom-of-page div. The user feedback was that the
    // bottom-of-page form was too easy to miss — operators would
    // click "Text" and not see anything happen because the form
    // appeared below the canvas fold. A modal makes the JSON editor
    // immediately visible.
    //
    // toggleJsonMode() is kept as a no-op for backward compat with
    // any old bookmarks / test scripts that still call it (the old
    // "Text" button was wired to it). The new button calls
    // openJsonModal() directly.
    function toggleJsonMode() {
        // Backward-compat shim: open the modal instead of toggling
        // a removed bottom-of-page form. Old button (now removed from
        // the toolbar) called this; new "Edit JSON" calls openJsonModal.
        openJsonModal();
    }

    function openJsonModal() {
        // Sync the textarea with the current plan state so the user
        // sees what's on the canvas (or makes targeted edits and
        // clicks "Apply JSON to canvas" to push changes back).
        $('vp-json-textarea').value = JSON.stringify(_plan, null, 2);
        // Clear any previous error
        const errEl = $('vp-json-error');
        if (errEl) { errEl.classList.add('hidden'); errEl.textContent = ''; }
        $('vp-json-modal-overlay').classList.remove('hidden');
    }

    function closeJsonModal() {
        $('vp-json-modal-overlay').classList.add('hidden');
    }

    function applyJsonToCanvas() {
        try {
            const newPlan = JSON.parse($('vp-json-textarea').value);
            // Minimal validation (the server is the real authority)
            if (!newPlan.steps) { _showJsonError('Plan must have a steps array'); return; }
            _plan = newPlan;
            $('vp-plan-name').value = _plan.name || '';
            $('vp-plan-description').value = _plan.description || '';
            renderAllSteps();
            updateMinimap();
            showBanner('JSON applied to canvas', 'success');
            closeJsonModal();
        } catch (e) {
            _showJsonError('Invalid JSON: ' + e.message);
        }
    }

    function _showJsonError(msg) {
        const errEl = $('vp-json-error');
        if (!errEl) { showBanner(msg, 'error'); return; }
        errEl.textContent = msg;
        errEl.classList.remove('hidden');
    }

    function copyCanvasToJson() {
        $('vp-json-textarea').value = JSON.stringify(_plan, null, 2);
        showBanner('Canvas → JSON copied', 'success');
    }

    // ===== v3.8.0: Save plan as workflow =====
    // Opens a modal that asks for a workflow name + optional
    // description, then POSTs to /api/projects/{id}/plan/to-workflow.
    // The LLM generalizes concrete values into {{var}} placeholders.
    // On success, redirect to /workflows/{new_id}.
    function openSaveAsWorkflowModal() {
        // Pre-fill the name input with the plan name (kebab-case it
        // if it isn't already). Operator can override.
        const planName = (_plan && _plan.name) ? _plan.name : '';
        const suggested = (planName || (window._VP_PROJECT_ID || 'workflow') + '-template')
            .toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '');
        $('vp-save-as-workflow-name').value = suggested;
        $('vp-save-as-workflow-description').value =
            (_plan && _plan.description) ? _plan.description : '';
        // Reset status
        const status = $('vp-save-as-workflow-status');
        if (status) { status.classList.add('hidden'); status.textContent = ''; }
        $('vp-save-as-workflow-spinner').classList.add('hidden');
        $('vp-save-as-workflow-submit').disabled = false;
        $('vp-save-as-workflow-overlay').classList.remove('hidden');
        // Focus the name field for quick keyboard entry
        setTimeout(() => $('vp-save-as-workflow-name').focus(), 50);
    }

    function closeSaveAsWorkflowModal() {
        $('vp-save-as-workflow-overlay').classList.add('hidden');
    }

    // 2026-08-07: Save dropdown menu (the arrow next to the main
    // Save button). Toggles visibility; closes on outside click.
    // The dropdown contains the v3.8.0 "Save as workflow" action
    // (moved out of the toolbar to save horizontal space).
    function toggleSaveMenu(event) {
        // Stop propagation so the outside-click handler below
        // doesn't immediately close the menu we just opened.
        if (event) event.stopPropagation();
        const menu = $('vp-save-menu');
        if (menu) menu.classList.toggle('open');
    }
    function closeSaveMenu() {
        const menu = $('vp-save-menu');
        if (menu) menu.classList.remove('open');
    }
    function _bindSaveMenuOutsideClick() {
        // Idempotent (guarded by a window flag) so re-init (e.g.
        // after SPA navigation) doesn't double-bind.
        if (window._vpSaveMenuBound) return;
        window._vpSaveMenuBound = true;
        document.addEventListener('click', (e) => {
            const menu = $('vp-save-menu');
            const group = e.target.closest('.vp-save-group');
            if (!menu || !menu.classList.contains('open')) return;
            if (group) return;  // click inside the save group = handled by the button
            menu.classList.remove('open');
        });
        // Esc closes the menu (standard dropdown UX).
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeSaveMenu();
        });
    }

    async function submitSaveAsWorkflow() {
        const nameEl = $('vp-save-as-workflow-name');
        const descEl = $('vp-save-as-workflow-description');
        const name = (nameEl.value || '').trim();
        const description = (descEl.value || '').trim();
        const status = $('vp-save-as-workflow-status');
        const submit = $('vp-save-as-workflow-submit');
        const spinner = $('vp-save-as-workflow-spinner');

        // Defensive validation (the input has pattern= but Safari
        // sometimes lets bad input through on form submit)
        if (!name || !/^[a-z0-9][a-z0-9-]*$/.test(name)) {
            status.textContent = 'Name must be kebab-case (lowercase letters, digits, hyphens; start with letter or digit).';
            status.classList.remove('hidden');
            return;
        }

        submit.disabled = true;
        spinner.classList.remove('hidden');
        status.classList.add('hidden');

        const projectId = window._VP_PROJECT_ID;
        if (!projectId) {
            status.textContent = 'Internal: project id not in window._VP_PROJECT_ID.';
            status.classList.remove('hidden');
            submit.disabled = false;
            spinner.classList.add('hidden');
            return;
        }

        try {
            const r = await _fetchWithTimeout(
                `/api/projects/${projectId}/plan/to-workflow`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name, description}),
                },
                90_000  // LLM synthesis can take 30-60s
            );
            const j = await r.json();
            if (r.status !== 200 && r.status !== 201) {
                throw new Error(_errDetailToString(j.detail, r.status));
            }
            // Close the modal, then redirect to the new workflow
            closeSaveAsWorkflowModal();
            showBanner('Workflow created: ' + (j.name || name), 'success');
            setTimeout(() => {
                window.location.href = '/workflows/' + j.id;
            }, 800);
        } catch (e) {
            status.textContent = e.message || String(e);
            status.classList.remove('hidden');
            submit.disabled = false;
            spinner.classList.add('hidden');
        }
    }

    // ===== Event wiring =====
    // drawflow 0.0.59 fires 'nodeSelected' on click and 'nodeRemoved'
    // on delete. We listen on the parent wrap.
    function wireCanvasEvents() {
        const wrap = $('vp-canvas');
        if (!wrap) return;
        // ===== v2.2: behavior change — dblclick opens, click-empty closes =====
        // Before v2.2 a single click on a card opened the side panel
        // (which was jarring — the user was often just dragging).
        // v2.2 (matching visual_workflow.js's behavior) is:
        //   - mousedown on a card: remember position (for drag detection)
        //   - click on a card: just visual select (drawflow's built-in)
        //   - click on the × button: delete (with confirm)
        //   - click on the wrap's empty area: close the side panel
        //   - dblclick on a card: open the side panel for editing
        // The drag detection ensures a drag-reposition (movement >
        // 8px between mousedown and dblclick) doesn't open the panel.
        // Track mousedown on the wrap (card or empty). We only need
        // the position when the mousedown is on a card, since only
        // card-dblclicks need drag detection.
        wrap.addEventListener('mousedown', (ev) => {
            const nodeEl = ev.target.closest('.vp-node');
            _mouseDownPos = nodeEl
                ? { x: ev.clientX, y: ev.clientY }
                : null;
        });
        // Click handler: delete button OR close-panel-on-empty.
        wrap.addEventListener('click', function(ev) {
            // Delete button: .vp-node-delete
            const del = ev.target.closest('.vp-node-delete');
            if (del) {
                ev.stopPropagation();
                const name = del.getAttribute('data-node-name');
                if (name) {
                    if (confirm('Delete step "' + name + '"?')) {
                        deleteStepByName(name);
                        showBanner('Step deleted', 'success');
                    }
                }
                return;
            }
            // Click on the wrap's empty area (not on a card AND
            // not inside the side panel) → close the side panel if
            // it's open. Reverts the form per closeSidePanel.
            // Defensive: ignore clicks whose target is inside the
            // side panel, otherwise the Apply/Cancel click bubbles
            // up to wrap, sees target isn't a card, and closes the
            // panel right after applyEdit re-opened it.
            const sp = document.getElementById('vp-side-panel');
            if (sp && sp.contains(ev.target)) return;
            const nodeInner = ev.target.closest('.vp-node[data-step-name]');
            if (!nodeInner) {
                if (sp && !sp.classList.contains('hidden')) {
                    closeSidePanel();
                }
            }
            // Click on a card: no-op here (drawflow already handles
            // selection visuals via .selected class). The user
            // double-clicks to actually open the side panel.
        });
        // dblclick: open side panel. Same drag-detection as
        // visual_workflow.js (skip if movement > 8px between
        // mousedown and dblclick).
        wrap.addEventListener('dblclick', (ev) => {
            const nodeInner = ev.target.closest('.vp-node[data-step-name]');
            if (!nodeInner) return;
            const stepName = nodeInner.dataset.stepName;
            if (!stepName) return;
            if (_mouseDownPos) {
                const dx = ev.clientX - _mouseDownPos.x;
                const dy = ev.clientY - _mouseDownPos.y;
                if (Math.sqrt(dx * dx + dy * dy) > _DRAG_THRESHOLD_PX) {
                    return;
                }
            }
            openSidePanel(stepName);
        });
    }

    // ===== LLM-generate-plan (Phase D, 2026-07-27) =====
    // The LLM produces a plan_json (design-time), not tasks. The
    // user reviews in the canvas, then clicks Save (persist) or
    // Run (materialize + dispatch). This replaces the old
    // project-page "Generate plan" that wrote tasks directly.
    function openGeneratePlanModal() {
        const status = $('vp-generate-status');
        if (status) status.textContent = '';
        const ta = $('vp-generate-goal');
        if (ta) ta.value = '';
        const overlay = $('vp-generate-modal-overlay');
        if (overlay) overlay.classList.remove('hidden');
        // focus the textarea after the modal opens
        setTimeout(() => { if (ta) ta.focus(); }, 50);
    }
    function closeGeneratePlanModal() {
        const overlay = $('vp-generate-modal-overlay');
        if (overlay) overlay.classList.add('hidden');
    }
    async function generatePlanFromLlm() {
        const goalEl = $('vp-generate-goal');
        const goal = (goalEl && goalEl.value || '').trim();
        const status = $('vp-generate-status');
        if (status) {
            status.textContent = 'Asking LLM (60-120s)...';
            status.className = 'text-sm text-gray-500 ml-2';
        }
        try {
            const r = await _fetchWithTimeout(
                `/api/projects/${_projectId}/plan/from-llm`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({goal: goal}),
                },
                480_000,  // 8 min — LLM can be slow for long Chinese prompts
                            // + complex plans. The server-side planner
                            // has its own 60s timeout and falls back to
                            // mock on LLM failure, so a "real" timeout
                            // here usually means the planner is in a
                            // queue/retry loop on the LLM provider side.
            );
            // v3.5.2: defensive JSON parsing. The fetch() itself
            // succeeded (got a Response object), but the body might
            // not be JSON if a proxy/load-balancer or FastAPI's
            // default 500 handler returned HTML. Without this guard
            // the user sees the cryptic "Unexpected token 'I', 'Inter
            // nal S'... is not valid JSON" nested-exception error.
            // Try to parse JSON; on failure, surface the raw body
            // (first 300 chars) so the user sees what actually came
            // back from the server.
            const _rawText = await r.text();
            let j = null;
            try {
                j = _rawText ? JSON.parse(_rawText) : {};
            } catch (_parseErr) {
                // Body isn't JSON. Build a minimal {"detail": "..."}
                // shape so the existing _errDetailToString path works.
                const _snippet = (_rawText || '').slice(0, 300)
                    .replace(/\s+/g, ' ').trim();
                j = {detail: `non-JSON response (status ${r.status}, ` +
                    `body: ${_snippet}${_rawText && _rawText.length > 300 ? '…' : ''})`};
            }
            if (!r.ok) {
                throw new Error(_errDetailToString(j.detail, r.status));
            }
            // The endpoint returns a ProjectPlanResponse. Load the
            // returned plan into the editor state and re-render.
            _plan = j.plan || {version: '1.0', name: '', description: '', trigger: 'manual', variables: [], steps: []};
            // Pre-fill name/description inputs
            $('vp-plan-name').value = _plan.name || '';
            $('vp-plan-description').value = _plan.description || '';
            // Close the modal and re-render
            closeGeneratePlanModal();
            renderAllSteps();
            if (status) status.textContent = '';
            showBanner(
                `LLM drafted ${(_plan.steps || []).length} step(s). Review and Save or Run.`,
                'success'
            );
        } catch (e) {
            if (status) {
                status.textContent = 'Failed: ' + (e.message || String(e));
                status.className = 'text-sm text-red-600 ml-2';
            }
        }
    }

    // v1.5.3 + v1.5.3.1: write _plan.visual_layout[step_name] =
    // {x, y} when a drawflow node is moved. v1.5.3 originally
    // tried to read the moved node from the callback arg, but
    // drawflow 0.0.59's `nodeMoved` event does NOT pass a node
    // arg — the callback is just `() => void`. We now walk
    // drawflow's internal node map (same pattern as
    // visual_workflow.js's _captureAllNodePositions at
    // src/hermes_orch/static/visual_workflow.js:605) to read
    // each node's current pos_x / pos_y. The actual server PUT
    // happens in savePlan() — this just updates the in-memory
    // _plan.
    function _capturePlanVisualLayout() {
        if (!_editor || !_plan || !_plan.steps) return;
        if (!_editor.drawflow || !_editor.drawflow.drawflow) return;
        const stepNames = new Set(_plan.steps.map((s) => s.name));
        if (!_plan.visual_layout) _plan.visual_layout = {};
        const modules = Object.keys(_editor.drawflow.drawflow);
        for (const modName of modules) {
            const mod = _editor.drawflow.drawflow[modName];
            if (!mod || !mod.data) continue;
            for (const nodeId of Object.keys(mod.data)) {
                const node = mod.data[nodeId];
                if (!node || !node.data) continue;
                // step name: visual_plan stores it in `data.stepName`,
                // visual_workflow stores it in `data.name`. Accept
                // either (defensive against future consolidation).
                const stepName = node.data.stepName || node.data.name;
                if (!stepName || !stepNames.has(stepName)) continue;
                if (typeof node.pos_x === 'number'
                    && typeof node.pos_y === 'number') {
                    _plan.visual_layout[stepName] = {
                        x: node.pos_x,
                        y: node.pos_y,
                    };
                }
            }
        }
    }
    // v1.5.3 + v1.5.3.1: on init, walk the current plan and apply
    // the persisted x/y onto each drawflow node's pos_x / pos_y
    // so the canvas renders the saved layout. (The first version
    // of this tried _editor.getNodeFromId(step.name) but the
    // arg there is drawflow's INTERNAL numeric id, not the
    // step name — so it always threw and silently skipped. We
    // now walk the node map the same way _capturePlanVisualLayout
    // does, which works because drawflow stores each node's
    // data.name = step name.) Missing entries (steps added
    // since the last save) keep their default drawflow position.
    function _applyPlanVisualLayout() {
        if (!_editor || !_plan || !_plan.steps) return;
        if (!_editor.drawflow || !_editor.drawflow.drawflow) return;
        const layout = _plan.visual_layout || {};
        const modules = Object.keys(_editor.drawflow.drawflow);
        for (const modName of modules) {
            const mod = _editor.drawflow.drawflow[modName];
            if (!mod || !mod.data) continue;
            for (const nodeId of Object.keys(mod.data)) {
                const node = mod.data[nodeId];
                if (!node || !node.data) continue;
                // Same step-name field fallback as capture above
                const stepName = node.data.stepName || node.data.name;
                const pos = layout[stepName];
                if (pos && typeof pos.x === 'number' && typeof pos.y === 'number') {
                    node.pos_x = pos.x;
                    node.pos_y = pos.y;
                }
            }
        }
    }

    // ===== Public API (window.vp) =====
    window.vp = {
        // v3.12.5: addStep is removed — the 4-template palette chips
        // in the toolbar (`_bindPaletteChips`) cover this use case
        // with a sensible `action` pre-fill. The chips don't need a
        // window.vp entry because they're bound on init and the
        // handlers call internal helpers directly.
        savePlan: savePlan,
        generateTasks: generateTasks,
        // v3.8.0: Edit JSON is now a modal (openJsonModal/closeJsonModal)
        // — toggleJsonMode is kept as a backward-compat shim that
        // delegates to openJsonModal().
        toggleJsonMode: toggleJsonMode,
        openJsonModal: openJsonModal,
        closeJsonModal: closeJsonModal,
        applyJsonToCanvas: applyJsonToCanvas,
        copyCanvasToJson: copyCanvasToJson,
        // v3.8.0: Save as workflow
        openSaveAsWorkflowModal: openSaveAsWorkflowModal,
        closeSaveAsWorkflowModal: closeSaveAsWorkflowModal,
        submitSaveAsWorkflow: submitSaveAsWorkflow,
        // 2026-08-07: Save dropdown (the arrow next to the main
        // Save button). The dropdown contains "Save as workflow";
        // the main button still calls savePlan().
        toggleSaveMenu: toggleSaveMenu,
        closeSaveMenu: closeSaveMenu,
        saveStepEdits: saveStepEdits,
        deleteSelectedStep: deleteSelectedStep,
        // v2.2 (2026-07-30): Undo / Redo / Copy / Paste — mirror the
        // workflow template editor. See _checkpoint / _clipboard
        // above for the data model. Toolbar buttons + keyboard
        // shortcuts also bind to these via _bindGlobalShortcuts().
        undo: _undo,
        redo: _redo,
        copyStep: _copySelectedStep,
        pasteStep: _pasteClipboard,
        openGeneratePlanModal: openGeneratePlanModal,
        closeGeneratePlanModal: closeGeneratePlanModal,
        generatePlanFromLlm: generatePlanFromLlm,
        // v3.10.10 (2026-08-02): Generate Tasks modal — replaces the
        // bare `confirm()` dialog with a proper modal that lets
        // the operator set the loop-back cap (max_iterations).
        // Mirrors the workflow Run modal's "Loop-back cap" field.
        openGenerateTasksModal: openGenerateTasksModal,
        closeGenerateTasksModal: closeGenerateTasksModal,
        submitGenerateTasks: submitGenerateTasks,
        // v3.9.0 (Phase 2 UX): expose preset-cache hooks so e2e
        // tests can wait for the cache to fill and re-render. The
        // cache is module-private otherwise (no need for app code
        // to read it). `loadPresets(force)` returns the presets
        // array (cached or fresh-fetched). `getPresetCache()` is
        // a sync accessor for tests that need to assert the cache
        // state without awaiting a fetch.
        loadPresets: _loadPresets,
        getPresetCache: function() { return _presetCache; },
        // Per 2026-07-28: called by the "Apply workflow" modal when
        // the plan editor is the host page. Appends the supplied
        // step dicts to _plan.steps, re-renders the canvas so
        // drawflow wires appear for depends_on, and re-points the
        // toolbar form fields at the new plan. No HTTP call here
        // — the user still has to click 💾 Save to persist. We
        // pick the (silent) re-render over a page reload so the
        // user can keep editing the imported steps in place.
        importSteps: function(newSteps) {
            if (!Array.isArray(newSteps) || newSteps.length === 0) return 0;
            // Dedupe by name: if the plan already has a step with
            // the same name, rename the new one with -2, -3 suffix.
            const used = new Set(_plan.steps.map(s => s.name));
            for (const s of newSteps) {
                if (!s.name) continue;
                let n = s.name, i = 2;
                while (used.has(n)) n = s.name + '-' + (i++);
                if (n !== s.name) s.name = n;
                used.add(s.name);
            }
            _plan.steps = _plan.steps.concat(newSteps);
            renderAllSteps();
            $('vp-plan-name').value = _plan.name || '';
            $('vp-plan-description').value = _plan.description || '';
            return newSteps.length;
        },
        // Phase 1.5 (2026-07-29): replace the entire plan from
        // an external source (e.g. chatbox apply). Unlike
        // importSteps which appends, this REPLACES _plan and
        // re-renders. Used by the chatbox onPlanApplied hook so
        // the visual canvas reflects the LLM-applied plan
        // without a page reload.
        loadPlan: function(newPlan) {
            if (!newPlan || typeof newPlan !== 'object') return false;
            // Ensure required fields with safe defaults
            _plan = {
                version: newPlan.version || '1.0',
                name: newPlan.name || '',
                description: newPlan.description || '',
                trigger: newPlan.trigger || 'manual',
                variables: Array.isArray(newPlan.variables) ? newPlan.variables : [],
                steps: Array.isArray(newPlan.steps) ? newPlan.steps : [],
            };
            renderAllSteps();
            $('vp-plan-name').value = _plan.name || '';
            $('vp-plan-description').value = _plan.description || '';
            // Clear the unsaved-changes banner (a fresh plan from
            // the server is already saved, so there's nothing to
            // save until the user edits it).
            try { window.dispatchEvent(new CustomEvent('vp:plan-loaded')); } catch (e) {}
            return true;
        },
    };

    // ===== SOUL preview modal (v3.10.0) =====
    // Click on the "🎯 auto-SOUL" pill on a step card to see the
    // full default_soul text the LLM drafted at plan time. The
    // pill itself only fits ~5 chars in the title attribute;
    // longer SOULs need a real modal. Bound below as
    // `window.VP_SOUL_PREVIEW` so the inline `onclick` on each
    // pill (added in nodeHtml) can call it.
    //
    // v3.10.0: in the "both" mode, the LLM writes a default_soul
    // for every step whose role has no preset yet. The user
    // needs a way to read that text — it's the persona the agent
    // will adopt on first dispatch. The modal also lets them
    // copy it to clipboard (for editing into a SOUL template
    // later).
    function _openSoulPreview(pillEl) {
        if (!pillEl) return;
        // data-soul-default is JSON-encoded (so the value is
        // safe to embed in an HTML attribute even when the
        // default_soul contains quotes, newlines, etc.).
        let raw = '';
        try {
            raw = pillEl.getAttribute('data-soul-default') || '';
            if (raw) raw = JSON.parse(raw);
        } catch (e) {
            raw = '';
        }
        const role = (pillEl.closest('.drawflow-node')?.querySelector('.vp-node-role')?.textContent
            || pillEl.parentElement?.querySelector('.vp-node-role')?.textContent
            || '?').trim();
        const hasSoul = !!(raw && raw.trim());
        // Build the modal markup. Lazy-create the element so the
        // visual_plan.html template doesn't need a new block.
        let overlay = document.getElementById('vp-soul-preview-overlay');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'vp-soul-preview-overlay';
            overlay.className = 'hidden fixed inset-0 z-50 flex items-center justify-center';
            overlay.style.backgroundColor = 'rgba(0,0,0,0.5)';
            overlay.innerHTML = `
<div class="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-2xl w-full mx-4 max-h-[80vh] flex flex-col" onclick="event.stopPropagation()">
    <div class="px-5 py-3 border-b flex items-center justify-between flex-shrink-0">
        <h2 class="text-base font-semibold text-gray-800 dark:text-gray-100">
            🎯 default_soul — <span id="vp-soul-preview-role" class="font-mono"></span>
        </h2>
        <button type="button" id="vp-soul-preview-close"
                class="text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 text-2xl leading-none"
                aria-label="Close">×</button>
    </div>
    <div class="p-5 overflow-y-auto flex-1">
        <p id="vp-soul-preview-blurb" class="text-xs text-gray-500 dark:text-gray-400 mb-3"></p>
        <textarea id="vp-soul-preview-text" readonly
                  class="block w-full border rounded p-3 text-sm font-mono whitespace-pre-wrap"
                  style="min-height: 200px;"></textarea>
    </div>
    <div class="px-5 py-3 border-t flex items-center justify-end gap-2 flex-shrink-0">
        <button type="button" id="vp-soul-preview-copy"
                class="px-3 py-1.5 bg-gray-100 text-gray-700 rounded text-sm hover:bg-gray-200">
            Copy
        </button>
        <button type="button" id="vp-soul-preview-ok"
                class="px-3 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700">
            Close
        </button>
    </div>
</div>`;
            document.body.appendChild(overlay);
            // Wire close handlers (idempotent — only the first call
            // actually attaches since the element persists).
            overlay.addEventListener('click', (ev) => {
                if (ev.target === overlay) overlay.classList.add('hidden');
            });
            document.getElementById('vp-soul-preview-close').onclick = () => overlay.classList.add('hidden');
            document.getElementById('vp-soul-preview-ok').onclick = () => overlay.classList.add('hidden');
            document.getElementById('vp-soul-preview-copy').onclick = () => {
                const ta = document.getElementById('vp-soul-preview-text');
                if (!ta) return;
                ta.select();
                try {
                    const ok = document.execCommand('copy');
                    const btn = document.getElementById('vp-soul-preview-copy');
                    const orig = btn.textContent;
                    btn.textContent = ok ? 'Copied!' : 'Copy failed';
                    setTimeout(() => { btn.textContent = orig; }, 1500);
                } catch (e) { /* clipboard not available */ }
            };
            // Esc closes the modal.
            document.addEventListener('keydown', (ev) => {
                if (ev.key === 'Escape' && !overlay.classList.contains('hidden')) {
                    overlay.classList.add('hidden');
                }
            });
        }
        // Populate the modal contents.
        document.getElementById('vp-soul-preview-role').textContent = role;
        const blurb = document.getElementById('vp-soul-preview-blurb');
        const ta = document.getElementById('vp-soul-preview-text');
        if (hasSoul) {
            blurb.textContent = 'The LLM drafted this default_soul at plan time. ' +
                'The orch server will write it to the agent\u2019s SOUL.md on first dispatch ' +
                '(via _ensure_soul_preset in dispatch_step). You can also save it as a SOUL template ' +
                'for reuse in other projects.';
            ta.value = raw;
        } else {
            blurb.textContent = 'No default_soul was set by the LLM for this step. The orch server ' +
                'will use a generic role template on first dispatch. Click "SOUL presets" on the project ' +
                'page to add a real preset for this role.';
            ta.value = '';
        }
        overlay.classList.remove('hidden');
    }
    // Expose for the inline onclick on each pill.
    window.VP_SOUL_PREVIEW = _openSoulPreview;

    // ===== Bootstrap =====
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() { init(); wireCanvasEvents(); });
    } else {
        init(); wireCanvasEvents();
    }
})();
