/* Visual workflow builder — Phase 1 skeleton (2026-07-24).
 *
 * User-stated design: Q2 (a) drawflow + Q3 (a) third "loop-back
 * handle" (Phase 2 adds the red dashed arrow). This skeleton ships
 * the canvas + render + save + add-step palette stub + side-panel
 * read-only viewer. Drag-reorder, drag-wire, and side-panel edit
 * are Phase 1.1 / 1.2 / 1.3.
 *
 * Public API (window.visualBuilder):
 *   init()             — bind to the canvas + render initial state
 *   save()             — PUT the current step_template back to the API
 *   openSidePanel(nodeId)  — show step details in the side panel
 *   closeSidePanel()   — hide the side panel
 *   toggleJsonForm()   — show/hide the raw JSON form (Q4 c)
 *
 * The shape of step_template in the DB:
 *   [ {name, agent_role, action, depends_on, params_template,
 *      output_path, skill, feedback_to}, ... ]
 *
 * Drawflow's internal model is different (modules of nodes, each
 * with input/output "connections"). We translate step_template
 * <-> drawflow data on each render.
 */

(function () {
    'use strict';

    // ---- module state ----
    let _editor = null;          // drawflow Editor instance
    let _workflowId = null;      // workflow id from the page data attribute
    let _stepTemplate = [];      // canonical list of step dicts
    let _variables = [];         // canonical list of variable dicts
    // Phase 2.5 (2026-07-26): {step_name: {x, y}} — persisted card
    // positions. Read from the DB on init, updated in-memory on every
    // drag (nodeMoved listener), sent back on Save. Visual-only;
    // the runner never reads this. Missing entries fall back to the
    // default vertical stack (50, 50 + i*120). Orphan entries (e.g.
    // a renamed step) are harmless — we just ignore them on render.
    let _visualLayout = {};
    let _selectedNodeId = null;  // drawflow node id (e.g. "node-3") of the focused card
    // Track mousedown position so we can distinguish a pure click
    // (no movement → open the side panel) from a drag (movement > 5px
    // → leave the card where the user dropped it, do not auto-open).
    // Phase 1.1 (drag-to-reorder) will replace this with real
    // reorder logic that ACTUALLY moves the card in _stepTemplate.
    let _mouseDownPos = null;
    // 8px threshold for "is this a drag?" Detected at click time by
    // comparing the recorded mousedown position to the current click
    // position. < 8px movement = treat as a click (open side panel).
    // >= 8px = treat as a drag (drop the card, do not open).
    // The threshold is intentionally lenient — a few px of mouse
    // jitter during a click should NOT skip opening the side panel.
    const _DRAG_THRESHOLD_PX = 8;

    // ===== v2.1 (2026-07-30): Undo / Redo =====
    // Snapshot-based history. Before any state-mutating user action
    // (add step, delete step, edit step, add wire, remove wire,
    // change variable), we call _checkpoint("label") which deep-
    // copies the current state and pushes it onto _undoStack. Undo
    // pops + restores; redo re-applies the inverse direction.
    //
    // Why snapshot (not patch-diff): workflows are 10-50 steps with
    // nested params_template. A 50-step snapshot is ~100-200KB;
    // 50 snapshots = ~5-10MB. Cheap, simple, and avoids the
    // patch-inverse bugs that bit the migration script in v2.0.
    //
    // _isRendering is set during _render() to suppress history
    // pushes from drawflow's connectionRemoved / nodeRemoved events
    // that fire as a side effect of the canvas clear+rebuild.
    const _history = {
        undoStack: [],          // [{label, snapshot}] — most recent at end
        redoStack: [],          // [{label, snapshot}]
        maxSize: 50,            // cap depth so memory stays bounded
        _isRendering: false,    // set true during _render() to suppress
        _isApplyingPatch: false, // set true during undo/redo to suppress
    };
    function _snapshot() {
        return {
            stepTemplate: JSON.parse(JSON.stringify(_stepTemplate)),
            variables: JSON.parse(JSON.stringify(_variables || [])),
            visualLayout: JSON.parse(JSON.stringify(_visualLayout || {})),
        };
    }
    function _restoreSnapshot(snap) {
        _stepTemplate = snap.stepTemplate;
        _variables = snap.variables;
        _visualLayout = snap.visualLayout;
    }
    function _checkpoint(label) {
        if (_history._isRendering || _history._isApplyingPatch) return;
        // Deep-copy on push so a later mutation doesn't mutate the
        // historical snapshot.
        _history.undoStack.push({label, snapshot: _snapshot()});
        if (_history.undoStack.length > _history.maxSize) {
            _history.undoStack.shift();
        }
        _history.redoStack = [];
        _updateHistoryButtons();
    }
    function _undo() {
        if (_history.undoStack.length === 0) return;
        const entry = _history.undoStack.pop();
        // Capture current state for the redo stack BEFORE we restore
        _history.redoStack.push({label: entry.label, snapshot: _snapshot()});
        _history._isApplyingPatch = true;
        try {
            _restoreSnapshot(entry.snapshot);
            _render();
        } finally {
            _history._isApplyingPatch = false;
        }
        _updateHistoryButtons();
        _showBanner('Undid: ' + entry.label, 'success');
    }
    function _redo() {
        if (_history.redoStack.length === 0) return;
        const entry = _history.redoStack.pop();
        _history.undoStack.push({label: entry.label, snapshot: _snapshot()});
        _history._isApplyingPatch = true;
        try {
            _restoreSnapshot(entry.snapshot);
            _render();
        } finally {
            _history._isApplyingPatch = false;
        }
        _updateHistoryButtons();
        _showBanner('Redid: ' + entry.label, 'success');
    }
    function _updateHistoryButtons() {
        const undoBtn = document.getElementById('vf-undo-btn');
        const redoBtn = document.getElementById('vf-redo-btn');
        if (undoBtn) {
            undoBtn.disabled = _history.undoStack.length === 0;
            undoBtn.title = _history.undoStack.length === 0
                ? 'Nothing to undo'
                : 'Undo: ' + _history.undoStack[_history.undoStack.length - 1].label
                + ' (Ctrl+Z)';
        }
        if (redoBtn) {
            redoBtn.disabled = _history.redoStack.length === 0;
            redoBtn.title = _history.redoStack.length === 0
                ? 'Nothing to redo'
                : 'Redo: ' + _history.redoStack[_history.redoStack.length - 1].label
                + ' (Ctrl+Y)';
        }
    }
    // Keyboard shortcuts: Ctrl+Z = undo, Ctrl+Y or Ctrl+Shift+Z = redo.
    // We bind to the document and skip when focus is in a text
    // input (so the user's typing isn't hijacked).
    //
    // v3.10.8 (2026-08-02): actually skip text fields (the original
    // comment claimed this but the code never did). Without this,
    // Ctrl+Z inside the chatbox textarea or any side-panel form
    // field was swallowed by the editor undo and the user couldn't
    // undo their typed text. Copy/paste was already text-field-aware
    // (see _bindCopyPasteShortcuts below) — undo/redo was the
    // missing piece. Mirrors the same fix in visual_plan.js.
    document.addEventListener('keydown', (e) => {
        const ctrl = e.ctrlKey || e.metaKey;
        if (!ctrl) return;
        // Skip text fields so the native textarea/input undo works.
        const tag = (e.target && e.target.tagName || '').toLowerCase();
        const isTextField = tag === 'input' || tag === 'textarea'
            || (e.target && e.target.isContentEditable);
        if (isTextField) return;
        const key = e.key.toLowerCase();
        // Ctrl+Z (no shift) = undo; Ctrl+Shift+Z or Ctrl+Y = redo
        if (key === 'z' && !e.shiftKey) {
            e.preventDefault();
            _undo();
        } else if ((key === 'z' && e.shiftKey) || key === 'y') {
            e.preventDefault();
            _redo();
        }
    });
    // ===== v2.1: Copy / Paste step =====
    // Module-level clipboard (just one step at a time — pasting
    // twice produces step-copy-1, step-copy-2 with unique names).
    // We hold the deep-copied step OBJECT (not just a name) so
    // paste can recreate the step verbatim with only a name
    // collision to resolve.
    let _clipboard = null;       // {step: <obj>, ts: <number>}
    function _copySelectedStep() {
        if (!_selectedNodeId) {
            // v3.10.9: return false so the keydown handler can
            // skip preventDefault — let the browser do its default
            // Ctrl+C text-copy when no card is selected. Previously
            // the keydown handler called e.preventDefault()
            // unconditionally, so the user couldn't copy selected
            // text on the page (e.g. step name in the canvas)
            // while the editor's keydown was active.
            return false;
        }
        const step = _findStepByNodeId(_selectedNodeId);
        if (!step) {
            _showBanner('Selected card not in step_template (stale drawflow state)', 'error');
            return false;
        }
        // Deep copy so subsequent edits to the original don't mutate
        // the clipboard. The pasted step will get its own unique
        // name (with a -copy or -copy-N suffix) so it doesn't
        // collide with the source.
        _clipboard = {
            step: JSON.parse(JSON.stringify(step)),
            ts: Date.now(),
        };
        _showBanner('Copied step "' + step.name + '". Click another card and press Ctrl+V (or click Paste) to clone.', 'success');
        return true;
    }
    function _pasteClipboard() {
        if (!_clipboard) {
            // v3.10.9: same pattern as _copySelectedStep — return
            // false so the keydown handler skips preventDefault.
            return false;
        }
        // Find a unique name. Try `<name>-copy`, `<name>-copy-2`,
        // ... up to a reasonable cap so we don't loop forever.
        const baseName = _clipboard.step.name;
        const candidateBase = baseName.replace(/-copy(-\d+)?$/, '') + '-copy';
        let candidate = candidateBase;
        let n = 2;
        const taken = new Set(_stepTemplate.map((s) => s.name));
        while (taken.has(candidate)) {
            if (n > 999) {
                _showBanner('Cannot paste: name collision cap reached (1000 -copy variants)', 'error');
                return false;
            }
            candidate = candidateBase + '-' + n;
            n += 1;
        }
        // Deep copy again so two consecutive pastes don't share a
        // reference to the same nested objects (especially
        // params_template, which is a dict we don't want them to
        // mutate together).
        const newStep = JSON.parse(JSON.stringify(_clipboard.step));
        newStep.name = candidate;
        // v2.1: checkpoint BEFORE the push so undo restores to
        // pre-paste state.
        _checkpoint('Paste step (as "' + candidate + '")');
        _stepTemplate.push(newStep);
        // Re-render so the new card appears on canvas.
        _render();
        // After re-render, the new step's node is on the canvas.
        // We don't auto-wire anything (the user can drag if they
        // want the same edge pattern) but we open the side panel
        // on it so the user can rename / tweak before clicking Save.
        const nodeId = _findNodeIdByStepName(candidate, _getAllNodes());
        if (nodeId) {
            openSidePanel(nodeId);
        }
        _showBanner('Pasted as "' + candidate + '". Edits stay in memory until you click Save.', 'success');
        return true;
    }
    // Ctrl+C / Ctrl+V keyboard shortcuts. We avoid hijacking the
    // browser's native copy/paste when focus is in a text input
    // (so users can still copy-paste within side-panel fields).
    //
    // v3.10.9 (2026-08-02): also bail out (no preventDefault) when
    // no card is selected for copy, or no step in the editor
    // clipboard for paste. Otherwise the editor swallows Ctrl+C
    // and the user can't copy text on the page (e.g. the canvas
    // step labels, an error message). The copy/paste handlers
    // return a boolean (true = handled, false = nothing to do);
    // only preventDefault when we actually did the work.
    function _bindCopyPasteShortcuts() {
        if (window._vfCopyPasteBound) return;
        window._vfCopyPasteBound = true;
        document.addEventListener('keydown', (e) => {
            const ctrl = e.ctrlKey || e.metaKey;
            if (!ctrl) return;
            // Skip if focus is in a text input / textarea — let
            // the user copy-paste within the field naturally.
            const tag = (e.target && e.target.tagName || '').toLowerCase();
            const isTextField = tag === 'input' || tag === 'textarea' || (e.target && e.target.isContentEditable);
            if (isTextField) return;
            if (e.key.toLowerCase() === 'c' && !e.shiftKey) {
                const handled = _copySelectedStep();
                if (handled) e.preventDefault();
            } else if (e.key.toLowerCase() === 'v' && !e.shiftKey) {
                const handled = _pasteClipboard();
                if (handled) e.preventDefault();
            }
        });
    }
    // ===== v2.1: JSON import / export (workflow template) =====
    // Export: download step_template + variables as a single
    // .workflow.json file. The file is a self-contained template
    // that can be re-imported into another workflow (or the same
    // one after a destructive edit). Useful for sharing patterns
    // between operators and for "save my work" before risky edits.
    //
    // The exported shape intentionally OMITS the workflow_id +
    // workflow name (those are server-assigned on import). We keep
    // the version + description so the importer can detect a
    // schema-version mismatch and refuse to apply a too-old / too-
    // new template. step_template + variables are the payload.
    function _exportWorkflowAsJson() {
        const payload = {
            // v2.1 export format. Bump `format_version` whenever
            // the JSON shape changes so the importer can do a
            // forward/backward check.
            format_version: '2.1',
            // Free-text comment the user can leave before saving.
            // The importer reads but doesn't require it.
            description: '',
            step_template: _stepTemplate,
            variables: _variables || [],
        };
        // Pretty-print so the file is human-readable. JSON.parse
        // round-trip is forgiving on whitespace.
        const text = JSON.stringify(payload, null, 2);
        const blob = new Blob([text], {type: 'application/json'});
        const url = URL.createObjectURL(blob);
        // Filename: `workflow-{name-or-id}-{YYYYMMDD-HHMM}.json`.
        // We use a timestamp so consecutive exports don't collide
        // in the user's Downloads folder.
        const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
        const fname = 'workflow-' + (_workflowId || 'template') + '-' + ts + '.json';
        const a = document.createElement('a');
        a.href = url;
        a.download = fname;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        // Defer revoke to give the browser time to start the
        // download; otherwise the click might race with revoke.
        setTimeout(() => URL.revokeObjectURL(url), 4000);
        _showBanner('Exported ' + fname + ' (' + _stepTemplate.length + ' steps, ' + (_variables || []).length + ' variables)', 'success');
    }
    // Import: read a workflow.json file (same shape as export),
    // validate, and replace the in-memory step_template +
    // variables. The user must click Save to persist. We do NOT
    // auto-validate the imported plan against the agent roles
    // (the Save endpoint does that). We DO check the format_version
    // and the top-level shape so a clearly-bad file is rejected
    // up-front with a readable error.
    function _importWorkflowFromJson(file) {
        if (!file) return;
        if (file.size > 2 * 1024 * 1024) {
            // 2 MB cap. Reasonable for a workflow (50 steps +
            // variables + descriptions). Bigger files are likely
            // either accidentally a DB export or a malicious
            // upload. Reject early.
            _showBanner('Import failed: file too large (' + Math.round(file.size / 1024) + ' KB > 2 MB cap)', 'error');
            return;
        }
        const reader = new FileReader();
        reader.onload = (e) => {
            let data;
            try {
                data = JSON.parse(e.target.result);
            } catch (err) {
                _showBanner('Import failed: not valid JSON (' + err.message + ')', 'error');
                return;
            }
            // Schema check
            if (!data || typeof data !== 'object' || Array.isArray(data)) {
                _showBanner('Import failed: top-level must be a JSON object', 'error');
                return;
            }
            if (typeof data.format_version !== 'string') {
                _showBanner('Import failed: missing format_version (is this a workflow template?)', 'error');
                return;
            }
            // v2.1 supports format_version = "2.1" exactly. We
            // accept minor 2.x but warn; reject 1.x / 3.x.
            const fv = data.format_version;
            const major = parseInt(fv.split('.')[0], 10);
            if (isNaN(major) || major !== 2) {
                _showBanner('Import failed: format_version=' + fv + ' is not 2.x (this editor only accepts 2.x templates)', 'error');
                return;
            }
            if (!Array.isArray(data.step_template)) {
                _showBanner('Import failed: step_template must be an array', 'error');
                return;
            }
            // v2.1: checkpoint BEFORE the replace so undo can
            // restore the pre-import state. Save() also has its
            // own checkpoint; together they make the import +
            // subsequent save fully undoable.
            _checkpoint('Import workflow template (' + file.name + ')');
            _stepTemplate = data.step_template;
            _variables = Array.isArray(data.variables) ? data.variables : [];
            _render();
            _showBanner('Imported ' + data.step_template.length + ' steps from ' + file.name + '. Review + click Save to persist.', 'success');
        };
        reader.onerror = () => {
            _showBanner('Import failed: could not read file', 'error');
        };
        reader.readAsText(file);
    }
    // ===== v2.1: Mini-map =====
    // A small fixed div in the bottom-right corner that shows all
    // step positions in the canvas. Click a dot to scroll the
    // canvas to that step. We re-render the mini-map after every
    // _render() (which is called on add/delete/move/undo/redo)
    // so it stays in sync.
    //
    // Why roll our own vs. drawflow-plugin-minimap: the plugin
    // adds 50KB+ of JS for canvas-rendering. Our canvas has 5-30
    // steps, so a simple SVG of <circle> elements is enough and
    // ~50 lines. If we later need to zoom or show a live
    // execution indicator (Phase 4+), revisit the plugin.
    function _renderMinimap() {
        const map = document.getElementById('vf-minimap');
        if (!map) return;
        const nodes = _getAllNodes();
        const names = Object.keys(nodes);
        // Compute bounding box of all step positions. If no
        // nodes, show an empty placeholder and bail.
        if (names.length === 0) {
            map.innerHTML = '<div class="vf-minimap-empty" style="font-size:9px;color:#9ca3af;text-align:center;padding:8px;">no steps</div>';
            return;
        }
        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (const k of names) {
            const n = nodes[k];
            if (!n || typeof n.pos_x !== 'number' || typeof n.pos_y !== 'number') continue;
            // drawflow stores absolute positions; the .drawflow-node
            // wrapper is ~160px wide × ~50px tall by default, plus
            // the user can resize. Use 200x80 as a conservative
            // card size so the bbox of dots covers the visible card
            // area, not just the dot positions.
            const cardW = 200, cardH = 80;
            minX = Math.min(minX, n.pos_x);
            minY = Math.min(minY, n.pos_y);
            maxX = Math.max(maxX, n.pos_x + cardW);
            maxY = Math.max(maxY, n.pos_y + cardH);
        }
        if (!isFinite(minX)) {
            map.innerHTML = '<div class="vf-minimap-empty" style="font-size:9px;color:#9ca3af;text-align:center;padding:8px;">no positions</div>';
            return;
        }
        // Add a small padding around the bbox so dots aren't at
        // the very edge of the minimap div.
        const padX = 30, padY = 20;
        const bboxW = (maxX - minX) + 2 * padX;
        const bboxH = (maxY - minY) + 2 * padY;
        // Scale: fit the bbox into the minimap's 200x120 box
        // (matches the CSS .vf-minimap size). preserveAspectRatio
        // so the map isn't squished.
        const mapW = 200, mapH = 120;
        const scale = Math.min(mapW / bboxW, mapH / bboxH);
        // Dots: one per step. Color: gray for regular, red if
        // feedback_to is set (so the operator sees "this step has
        // a loop-back" at a glance). Tooltip = step name.
        const dots = [];
        for (const k of names) {
            const n = nodes[k];
            if (!n || typeof n.pos_x !== 'number') continue;
            const step = _stepTemplate.find((s) => s.name === (n.data && n.data.name));
            const cx = ((n.pos_x - minX + padX) * scale).toFixed(1);
            const cy = ((n.pos_y - minY + padY) * scale).toFixed(1);
            const isFeedback = step && Array.isArray(step.feedback_to) && step.feedback_to.length > 0;
            const fill = isFeedback ? '#dc2626' : '#6b7280';
            const name = (n.data && n.data.name) || k;
            // Clicking a dot scrolls the main canvas so this card
            // is roughly centered. We use scrollIntoView on the
            // matching .drawflow-node DOM element.
            dots.push(
                '<circle cx="' + cx + '" cy="' + cy + '" r="4" fill="' + fill + '" '
                + 'data-step-name="' + _escapeAttr(name) + '" '
                + 'class="vf-minimap-dot" style="cursor:pointer;" '
                + 'title="' + _escapeAttr(name) + '">'
                + '</circle>'
            );
        }
        const svgW = (mapW).toFixed(0);
        const svgH = (mapH).toFixed(0);
        map.innerHTML =
            '<svg width="' + svgW + '" height="' + svgH + '" '
            + 'viewBox="0 0 ' + mapW + ' ' + mapH + '" '
            + 'style="display:block;background:#f9fafb;border-radius:4px;">'
            + dots.join('')
            + '</svg>';
        // Wire clicks: each dot scrolls the main canvas to the
        // matching card. One delegated handler (not one per dot)
        // so adding 50 dots doesn't add 50 listeners.
        const svg = map.querySelector('svg');
        if (svg) {
            svg.addEventListener('click', (e) => {
                const t = e.target;
                if (!t || !t.dataset || !t.dataset.stepName) return;
                const stepName = t.dataset.stepName;
                // Find the .drawflow-node element with this step
                // name. drawflow's id is "node-<num>" (numeric) so
                // we walk all .drawflow-node elements and check
                // their inner data-step-name.
                const cardEls = document.querySelectorAll('.drawflow-node [data-step-name]');
                for (const el of cardEls) {
                    if (el.dataset.stepName === stepName) {
                        // scrollIntoView with center alignment; the
                        // wrap is the scrollable container.
                        const card = el.closest('.drawflow-node');
                        if (card && card.scrollIntoView) {
                            card.scrollIntoView({behavior: 'smooth', block: 'center', inline: 'center'});
                        }
                        return;
                    }
                }
            });
        }
    }
    // Minimal HTML attribute escape for the minimap dot tooltips
    // (step names can contain hyphens / underscores but not
    // quotes; we still escape defensively).
    function _escapeAttr(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }
    // ===== v2.1: Parameter hints (LLM suggest var types) =====
    // Calls POST /api/workflows/{id}/suggest-vars and merges the
    // suggestions into _variables. The backend does a LOCAL
    // extraction of {{var}} placeholders + infers type from
    // literal values in the same step's params (no LLM call —
    // see the backend docstring for the algorithm + future LLM
    // extension). User can review each suggestion before applying.
    async function _suggestAndMergeVars() {
        if (!_workflowId) {
            _showBanner('Cannot suggest: workflow id missing (refresh the page)', 'error');
            return;
        }
        // v2.1: checkpoint BEFORE the merge so undo can restore
        // the pre-suggestion variable list.
        _checkpoint('Suggest variables from LLM');
        try {
            const r = await fetch('/api/workflows/' + encodeURIComponent(_workflowId) + '/suggest-vars', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: '{}',
            });
            if (!r.ok) {
                const errBody = await r.json().catch(() => ({}));
                _showBanner('Suggest failed: ' + (errBody.detail || r.status), 'error');
                return;
            }
            const data = await r.json();
            const suggestions = data.suggestions || [];
            if (suggestions.length === 0) {
                _showBanner('No {{var}} placeholders found in any step. Add placeholders in params_template, then try again.', 'info');
                return;
            }
            // Merge into _variables. For each suggestion:
            //   - If already defined, skip (or update if the existing
            //     type is "string" and we have a better type)
            //   - If not defined, add a new entry
            if (!Array.isArray(_variables)) _variables = [];
            const byName = new Map(_variables.map((v) => [v.name, v]));
            let added = 0;
            let updated = 0;
            for (const s of suggestions) {
                if (byName.has(s.name)) {
                    const existing = byName.get(s.name);
                    // Conservative update: only update the type
                    // if the existing one is the default "string"
                    // and we inferred something more specific.
                    if ((existing.type || "string") === "string" && s.type && s.type !== "string") {
                        existing.type = s.type;
                        updated += 1;
                    }
                } else {
                    _variables.push({
                        name: s.name,
                        type: s.type || "string",
                        default: s.default || "",
                        description: s.description || "",
                        required: true,
                    });
                    byName.set(s.name, _variables[_variables.length - 1]);
                    added += 1;
                }
            }
            // Re-render the JSON form (where the variables textbox
            // lives) so the user sees the new variables.
            const ta = document.getElementById('vf-variables-json');
            if (ta) ta.value = JSON.stringify(_variables, null, 2);
            // Re-render canvas (in case the JS auto-adds anything
            // for new placeholders; the existing _autoAddVariablesFromParams
            // is a no-op here but we keep the call for consistency).
            _render();
            _showBanner('Suggested ' + suggestions.length + ' variable(s); added ' + added + ', updated ' + updated + '. Review in the JSON form, then Save.', 'success');
        } catch (e) {
            _showBanner('Suggest failed: ' + e.message, 'error');
        }
    }
    // ===== end v2.1 Parameter hints =====

    // ---- DOM lookup ----
    function _canvas() { return document.getElementById('vf-canvas'); }
    function _sidePanel() { return document.getElementById('vf-side-panel'); }
    // v3.8.0: was `#vf-json-form` (the bottom-of-page div, removed).
    // The JSON editor is now a modal — toggleJsonForm() opens it,
    // closeJsonModal() hides it. We keep _jsonForm() returning the
    // modal overlay so the save() path's "is JSON form open?" check
    // still works (same truthy semantics — classList.contains('hidden')
    // is the new test).
    function _jsonForm() { return document.getElementById('vf-json-modal-overlay'); }
    function _saveBanner() { return document.getElementById('vf-save-banner'); }
    function _jsonFormIsOpen() {
        const f = _jsonForm();
        return f && !f.classList.contains('hidden');
    }

    // ---- helpers ----
    function _showBanner(text, kind) {
        const b = _saveBanner();
        b.textContent = text;
        b.className = `vf-save-banner show ${kind || 'success'}`;
        setTimeout(() => { b.className = 'vf-save-banner'; }, 2400);
    }

    // Extract all `{{var_name}}` placeholders from a string.
    // Returns an array of unique names. Whitespace inside braces
    // is trimmed. Non-identifier characters in the name are kept
    // (the validator will reject them with a clear message).
    function _extractPlaceholders(text) {
        if (typeof text !== 'string' || !text) return [];
        const out = new Set();
        const re = /\{\{\s*([^{}]+?)\s*\}\}/g;
        let m;
        while ((m = re.exec(text)) !== null) {
            out.add(m[1].trim());
        }
        return Array.from(out);
    }

    // Recursively walk a value and return all `{{var}}` placeholders.
    // Handles string leaves, dict leaves, and list elements. Skips
    // nulls and primitives. This catches placeholders in nested
    // params_template values like {"a": "{{x}}", "b": ["{{y}}"]}.
    function _collectPlaceholdersInValue(v, out) {
        out = out || new Set();
        if (v == null) return out;
        if (typeof v === 'string') {
            for (const n of _extractPlaceholders(v)) out.add(n);
        } else if (Array.isArray(v)) {
            for (const item of v) _collectPlaceholdersInValue(item, out);
        } else if (typeof v === 'object') {
            for (const val of Object.values(v)) {
                _collectPlaceholdersInValue(val, out);
            }
        }
        return out;
    }

    // Auto-add any `{{var}}` placeholders found in step_template's
    // params_template to _variables (so the validator accepts the
    // save). Returns the list of newly added variable names.
    //
    // This is "do the right thing" for semi-tech users: they type
    // `{{my_var}}` in a step's params, click Apply, and the system
    // silently creates the variable. No separate "Variables"
    // section to learn.
    //
    // Default type is "string" with required=true. The user can
    // edit the type / default / description via the JSON form or
    // a future Variables UI.
    function _autoAddVariablesFromParams() {
        if (!Array.isArray(_variables)) _variables = [];
        const existing = new Set(_variables.map((v) => v.name));
        const placeholders = new Set();
        for (const step of _stepTemplate) {
            if (step && step.params_template) {
                _collectPlaceholdersInValue(step.params_template, placeholders);
            }
        }
        const added = [];
        for (const name of placeholders) {
            if (!existing.has(name)) {
                _variables.push({
                    name,
                    type: 'string',
                    description: `Auto-added for {{${name}}}`,
                    required: true,
                });
                added.push(name);
            }
        }
        return added;
    }

    function _stepToCardHtml(step) {
        // Escape any HTML in the step name/role/action so a malicious
        // LLM-generated name can't inject markup. step.name etc.
        // should already be kebab-case ASCII, but defensive.
        const esc = (s) => String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
        const skill = step.skill
            ? `<div class="vf-node-skill">skill: ${esc(step.skill)}</div>`
            : '';
        // Tag the wrapper div with data-step-name so the click handler
        // can look up the step directly from the DOM without having
        // to know drawflow's internal module/container/numeric-id
        // structure (which varies by version and is not documented).
        return `
            <div class="vf-node" data-step-name="${esc(step.name || '')}">
                <button type="button" class="vf-node-delete" title="Delete step (or press Delete key when selected)">&times;</button>
                <div class="vf-node-header">
                    <span class="vf-node-name">${esc(step.name || '(unnamed)')}</span>
                    <span class="vf-node-role">${esc(step.agent_role || '?')}</span>
                </div>
                <div class="vf-node-action">${esc(step.action || '?')}</div>
                ${skill}
            </div>
        `;
    }

    function _clearConnections(editor) {
        // Remove all existing connections so a re-render doesn't
        // duplicate them. We walk all nodes via _getAllNodes() so
        // we don't have to know drawflow's internal structure.
        const nodes = _getAllNodes();
        for (const node of Object.values(nodes)) {
            if (node && node.outputs) {
                Object.keys(node.outputs).forEach((outKey) => {
                    const out = node.outputs[outKey];
                    if (out && out.connections) {
                        out.connections = [];
                    }
                });
            }
        }
    }

    function _render() {
        // v2.1: set the _isRendering flag so drawflow's
        // connectionRemoved / nodeRemoved events (which fire as a
        // side effect of _editor.clear()) don't push unwanted
        // entries to the undo stack. Cleared in the finally.
        _history._isRendering = true;
        try {
            const r = _renderInner();
            // v2.1: refresh the mini-map so it stays in sync
            // with the new state. Done inside the try so the
            // flag is true here too (we don't want a cascade of
            // mini-map redraws to push undo entries).
            _renderMinimap();
            return r;
        } finally {
            _history._isRendering = false;
        }
    }
    function _renderInner() {
        const wrap = _canvas();
        // Pass the wrap itself (not an inner div) so drawflow injects
        // its DOM as a direct child of the wrap. The side panel is
        // a sibling of drawflow's injected div, both inside the wrap;
        // the side panel uses position: absolute so it floats over
        // the canvas (z-index 10) without affecting layout.
        if (!wrap) {
            console.error('visual_workflow: vf-canvas wrap not found');
            return;
        }
        // First-time init: build the editor
        if (!_editor) {
            console.log('visual_workflow: init Drawflow on wrap', wrap);
            try {
                _editor = new Drawflow(wrap);
                _editor.reroute = true;
                _editor.start();
                console.log('visual_workflow: Drawflow started OK');
            } catch (e) {
                console.error('visual_workflow: Drawflow init failed:', e);
                _showInitError(e);
                return;
            }
            // Phase 1.1: bind nodeMoved listener once. After every
            // drag, sync _stepTemplate order to match drawflow's
            // current Y positions, AND re-compute all connection
            // paths (drawflow only updates the source side of
            // connections for the dragged node). Bind the listener
            // ONLY here (not in every _render) so we don't stack
            // up duplicates.
            // Phase 2.5 (2026-07-26): also persist the new (x, y)
            // into _visualLayout so the position survives reload.
            // The user still has to click Save to push it to the
            // server; this is the in-memory mirror.
            _editor.on('nodeMoved', () => {
                _captureAllNodePositions();
                _reorderStepTemplateByPosition();
                requestAnimationFrame(_recomputeAllConnectionPaths);
            });
            // Phase 1.2: bind connectionCreated listener. When the
            // user drags an output handle to an input handle, add
            // the source step's name to the target step's
            // depends_on. drawflow 0.0.59 fires this event with
            // payload { output_id, input_id, output_class,
            // input_class }.
            _editor.on('connectionCreated', (connection) => {
                _onConnectionCreated(connection);
            });
            // drawflow 0.0.59 fires nodeRemoved when the user
            // deletes a card (Delete key, removeNodeId, etc.).
            // Payload is the NUMERIC id string (e.g. "3") — NOT
            // the "node-3" form. We use it to remove the step from
            // _stepTemplate so the delete actually persists.
            _editor.on('nodeRemoved', (numericId) => {
                _onNodeRemoved(numericId);
            });
            // drawflow 0.0.59 does NOT fire a connectionRemoved event
            // when the user removes a wire. We patch
            // _editor.removeConnection to call _onConnectionRemoved
            // ourselves with the same shape.
            if (!_editor._vfRemoveConnectionPatched) {
                const _origRemove = _editor.removeConnection.bind(_editor);
                _editor.removeConnection = function (
                    sourceId, targetId, sourceOutput, targetInput
                ) {
                    _origRemove(sourceId, targetId, sourceOutput, targetInput);
                    _onConnectionRemoved({
                        output_id: sourceId,
                        input_id: targetId,
                        output_class: sourceOutput,
                        input_class: targetInput,
                    });
                };
                _editor._vfRemoveConnectionPatched = true;
            }
        }
        // (unreachable: the `_editor` block always sets _editor)
        // Always clear and re-render from canonical state
        _editor.clear();
        _clearConnections(_editor);

        // Lay out steps. Phase 2.5: if a saved position exists for this
        // step in _visualLayout, use it; otherwise fall back to the
        // default vertical stack (50, 50 + i*120). This is the difference
        // between "refresh resets everything" and "cards stay where I
        // left them" — the whole point of persisting visual_layout.
        const defaultX = 50, defaultY = 50, defaultDy = 120;
        try {
            _stepTemplate.forEach((step, i) => {
                const html = _stepToCardHtml(step);
                const data = {
                    name: step.name,
                    role: step.agent_role,
                    action: step.action,
                };
                const saved = _visualLayout[step.name];
                const posX = (saved && typeof saved.x === 'number') ? saved.x : defaultX;
                const posY = (saved && typeof saved.y === 'number') ? saved.y : (defaultY + i * defaultDy);
                // drawflow 0.0.59 addNode signature:
                //   addNode(name, n_inputs, n_outputs, posx, posy, classoverride, data, html)
                // The inputs/outputs args are NUMBERS (count of
                // connection points), not class names. drawflow
                // auto-generates class names "input_1", "input_2",
                // ... and "output_1", "output_2", ... on the
                // rendered elements. We pass 1 each so each card
                // has one input handle (left) and TWO output handles
                // (right): output_1 for normal depends_on chain, and
                // output_2 for the loop-back (feedback_to) edge —
                // styled red dashed so the two kinds are visually
                // distinct. CSS in visual_workflow.html targets the
                // .output_2 class for the loop-back style.
                _editor.addNode(
                    step.name,         // 1: name
                    1,                 // 2: number of inputs
                    2,                 // 3: number of outputs (1 normal + 1 loop-back)
                    posX,              // 4: posx (saved or default)
                    posY,              // 5: posy (saved or default)
                    'vf-node',         // 6: classoverride
                    data,              // 7: data (attached to node)
                    html,              // 8: html content
                );
            });
        } catch (e) {
            console.error('visual_workflow: addNode failed:', e);
            _showInitError(e);
            return;
        }

        // Phase 2 (v2.0 updated 2026-07-30): add tooltips to the
        // two output handles so the user can tell them apart.
        // drawflow creates the .output_1 and .output_2 divs
        // inside each .drawflow-node but doesn't add title
        // attributes; we do it here after addNode returns.
        //   output_1 (chain, normal):     adds target.depends_on += [this]
        //   output_2 (loop-back, red dashed):
        //                                 adds this.feedback_to += [target]
        //                                 ("if I fail, re-run target")
        try {
            for (const el of wrap.querySelectorAll('.drawflow-node')) {
                const o1 = el.querySelector('.output_1');
                const o2 = el.querySelector('.output_2');
                if (o1) o1.title = 'chain (target depends on this)';
                if (o2) o2.title = 'loop-back (if I fail, re-run target)';
            }
        } catch (e) {
            console.warn('tooltip assignment failed (non-fatal):', e.message);
        }

        // Wire depends_on: for each step B with deps ["A","C"], find
        // node A and C, and connect A's output -> B's input.
        // The 3rd and 4th args of addConnection are the OUTPUT and
        // INPUT class names, which must match what we passed to
        // addNode (output_1 / input_1, the default).
        try {
            // Build a name -> numeric id map from the DOM we just
            // created. Each card has data-step-name on the inner
            // .vf-node div (set in _stepToCardHtml). The outer
            // .drawflow-node has the DOM id like "node-1".
            const nameToNumericId = {};
            for (const el of wrap.querySelectorAll('.drawflow-node')) {
                const inner = el.querySelector('[data-step-name]');
                const name = inner ? inner.dataset.stepName : null;
                if (name && el.id.startsWith('node-')) {
                    nameToNumericId[name] = el.id.replace('node-', '');
                }
            }
            _stepTemplate.forEach((step) => {
                const targetNumeric = nameToNumericId[step.name];
                if (!targetNumeric) return;
                // Phase 2 (v2.0 FLIPPED 2026-07-30):
                //   - depends_on: data on the TARGET (the dependent
                //     step). Draw wire from each dep → this step.
                //     (unchanged)
                //   - feedback_to: data on the SOURCE (the failing
                //     step). Draw wire from this step → each recovery
                //     step. (v2.0: was the opposite)
                // The _onConnectionCreated handler does NOT need to
                // be re-fired on initial render (the data is
                // already in _stepTemplate), so we don't pass
                // a flag to skip the event — we just rely on
                // the handler's "already there" dedup check.
                (step.depends_on || []).forEach((depName) => {
                    const sourceNumeric = nameToNumericId[depName];
                    if (!sourceNumeric) return;
                    try {
                        _editor.addConnection(
                            sourceNumeric, targetNumeric,
                            'output_1', 'input_1',
                        );
                    } catch (e) {
                        console.warn(`addConnection(chain ${sourceNumeric}->${targetNumeric}) failed:`, e.message);
                    }
                });
                // v2.0: source is THIS step (failing), target is
                // each name in feedback_to (the recovery steps).
                (step.feedback_to || []).forEach((recoveryName) => {
                    const recoveryNumeric = nameToNumericId[recoveryName];
                    if (!recoveryNumeric) return;
                    try {
                        _editor.addConnection(
                            targetNumeric, recoveryNumeric,
                            'output_2', 'input_1',
                        );
                    } catch (e) {
                        console.warn(`addConnection(loop-back ${targetNumeric}->${recoveryNumeric}) failed:`, e.message);
                    }
                });
            });
        } catch (e) {
            console.error('visual_workflow: depends_on wiring failed:', e);
        }
        // Force re-compute of all connection paths after every render.
        // Without this, drawflow's SVG path `d` attribute may keep
        // STALE endpoint positions from before the re-render,
        // making edges look "misaligned" (curving to a phantom
        // location while the cards sit at their new positions).
        //
        // We use our own _recomputeAllConnectionPaths() instead of
        // drawflow's updateConnectionNodes, because:
        //  - updateConnectionNodes only updates paths where the
        //    given node is the SOURCE (not the TARGET).
        //  - drawflow's updateConnection(x, y) is a drag handler
        //    that crashes when called outside a drag.
        // Our helper reads the current handle positions via
        // getBoundingClientRect and sets each path's d-attr using
        // drawflow's createCurvature — same math drawflow uses
        // internally, but applied to BOTH endpoints of EVERY path.
        requestAnimationFrame(() => {
            _recomputeAllConnectionPaths();
        });

        // Click + double-click handlers:
        //   - Single click on card: just select (drawflow already does
        //     this; we don't open the side panel, to avoid accidental
        //     opens when the user is dragging).
        //   - Double click on card: open the side panel.
        //   - Click on wrap's blank area: close the side panel (which
        //     reverts the form per closeSidePanel's contract).
        // We track mousedown so a drag (movement > 8px) does NOT
        // trigger dblclick — drawflow's default behavior is to fire
        // click after drag-end, and dblclick can fire on a drag-end
        // if the user clicks twice in quick succession. Skipping on
        // movement > threshold makes drag-to-reorder unambiguous.
        if (!wrap._vfClickBound) {
            wrap.addEventListener('mousedown', (ev) => {
                const nodeEl = ev.target.closest('.drawflow-node');
                _mouseDownPos = nodeEl
                    ? { x: ev.clientX, y: ev.clientY }
                    : null;
            });
            wrap.addEventListener('click', (ev) => {
                // Click on the wrap's blank area (not a card) →
                // close the side panel if it's open. Reverts the
                // form via closeSidePanel.
                //
                // IMPORTANT: ignore clicks whose target is inside
                // the side panel (or any other non-canvas element).
                // Otherwise the click event that fires on the
                // Apply/Cancel button bubbles up to the wrap, sees
                // that the target is NOT a card, and closes the
                // panel right after applyEdit re-opened it.
                const sp = _sidePanel();
                if (sp && sp.contains(ev.target)) return;
                // Also ignore clicks on the Add Step palette (lives
                // outside the canvas wrap but the click still
                // bubbles through the document).
                if (ev.target.closest('.vf-palette-chip')) return;
                const stepEl = ev.target.closest('[data-step-name]');
                if (!stepEl) {
                    if (sp.classList.contains('open')) {
                        closeSidePanel();
                    }
                }
            });
            wrap.addEventListener('dblclick', (ev) => {
                const stepEl = ev.target.closest('[data-step-name]');
                const nodeEl = ev.target.closest('.drawflow-node');
                if (!stepEl || !nodeEl) return;
                // Was this a drag (movement > 8px)? If so, the user
                // intended to reposition the card, not view its
                // details. Skip opening the side panel.
                if (_mouseDownPos) {
                    const dx = ev.clientX - _mouseDownPos.x;
                    const dy = ev.clientY - _mouseDownPos.y;
                    if (Math.sqrt(dx * dx + dy * dy) > _DRAG_THRESHOLD_PX) {
                        return;
                    }
                }
                window.visualBuilder.openSidePanel(nodeEl.id);
            });
            // Phase 1.7: explicit "×" delete button. Click → call
            // drawflow's removeNodeId (which fires nodeRemoved,
            // which our listener handles to update _stepTemplate).
            // Bound on the wrap so it survives re-renders.
            if (!wrap._vfDeleteBound) {
                wrap.addEventListener('click', (ev) => {
                    const btn = ev.target.closest('.vf-node-delete');
                    if (!btn) return;
                    ev.stopPropagation();
                    const nodeEl = btn.closest('.drawflow-node');
                    if (!nodeEl || !_editor) return;
                    // Drawflow expects id like "node-1" for removeNodeId
                    _editor.removeNodeId(nodeEl.id);
                });
                wrap._vfDeleteBound = true;
            }
            wrap._vfClickBound = true;
        }
    }

    function _showInitError(err) {
        // Render a visible error inside the canvas wrap so the user
        // can see WHY no cards appeared, without opening F12.
        const wrap = _canvas();
        if (!wrap) return;
        const msg = document.createElement('div');
        msg.style.cssText = 'padding: 16px; background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; border-radius: 6px; margin: 16px; font-size: 12px; font-family: monospace;';
        msg.innerHTML = `
            <strong>Visual builder failed to initialize.</strong><br>
            ${(err && err.message) ? err.message : err}<br><br>
            Open F12 → Console for the full stack trace. The most common
            cause is the drawflow CDN not loading — check the Network tab.
        `;
        wrap.appendChild(msg);
    }

    function _findStepByNodeId(nodeId) {
        // Look up the step via the data-step-name attribute on the
        // .vf-node div inside the .drawflow-node wrapper. We tag every
        // card in _stepToCardHtml so we don't have to know drawflow's
        // internal module/container structure (which differs by
        // version). nodeId is the DOM id of the .drawflow-node
        // wrapper (e.g. "node-3") — we walk its descendants to find
        // the data attribute.
        if (!_editor) return null;
        const el = document.getElementById(nodeId);
        if (el) {
            const stepEl = el.querySelector('[data-step-name]');
            if (stepEl && stepEl.dataset.stepName) {
                return _stepTemplate.find((s) => s.name === stepEl.dataset.stepName) || null;
            }
        }
        // Fallback: walk all drawflow nodes by data field
        const node = _getAllNodes()[nodeId];
        if (node && node.data && node.data.name) {
            return _stepTemplate.find((s) => s.name === node.data.name) || null;
        }
        return null;
    }

    // Walk drawflow's internal state, returning all nodes as a flat
    // { nodeId: node } map. drawflow 0.0.59 stores nodes as
    // `editor.drawflow[moduleName][containerName]["data"][numericId]`.
    // We don't know the module/container names, so we walk all
    // sub-objects. The "data" key is special — it's the level
    // that holds the actual nodes. We skip non-data sub-objects.
    function _getAllNodes() {
        const out = {};
        if (!_editor || !_editor.drawflow) return out;
        for (const moduleName of Object.keys(_editor.drawflow)) {
            const module = _editor.drawflow[moduleName];
            if (!module || typeof module !== 'object') continue;
            for (const containerName of Object.keys(module)) {
                const container = module[containerName];
                if (!container || typeof container !== 'object') continue;
                for (const level2Key of Object.keys(container)) {
                    if (level2Key !== 'data') continue;  // skip non-data
                    const dataLevel = container.data;
                    if (!dataLevel || typeof dataLevel !== 'object') continue;
                    for (const id of Object.keys(dataLevel)) {
                        out[`node-${id}`] = dataLevel[id];
                    }
                }
            }
        }
        return out;
    }

    // Phase 1.1: re-derive the canonical _stepTemplate order from
    // drawflow's current Y positions. Called from the nodeMoved
    // listener after every drag. This does NOT re-render the canvas
    // — the cards stay where the user dropped them, but the order
    // in _stepTemplate is now what was dragged. On the next save,
    // the new order is persisted. On the next page load, _render
    // will place cards in the new order at (50, 170, 290, ...).
    //
    // IMPORTANT: depends_on references step names. If the user
    // reorders such that a step's depends_on is now AFTER it, the
    // workflow becomes invalid. The validator on save catches this
    // (a step's depends_on can only reference earlier steps). We
    // don't pre-validate here — let the server-side check run.
    function _reorderStepTemplateByPosition() {
        if (!_editor) return;
        const items = [];
        const nodes = _getAllNodes();
        for (const [domId, node] of Object.entries(nodes)) {
            // Get step name from DOM data attribute (more reliable
            // than drawflow's nested data structure)
            const el = document.getElementById(domId);
            if (!el) continue;
            const stepName = el.dataset.stepName;
            if (!stepName) continue;
            items.push({
                name: stepName,
                pos_y: typeof node.pos_y === 'number' ? node.pos_y : 0,
            });
        }
        // Stable sort by Y so equal Y (rare) keeps insertion order
        items.sort((a, b) => a.pos_y - b.pos_y);
        const nameToStep = new Map(_stepTemplate.map((s) => [s.name, s]));
        const newOrder = items
            .map((it) => nameToStep.get(it.name))
            .filter((s) => s !== undefined);
        if (newOrder.length === _stepTemplate.length && newOrder.length > 0) {
            _stepTemplate = newOrder;
        }
    }

    // Phase 2.5 (2026-07-26): snapshot every visible card's current
    // (x, y) into _visualLayout, keyed by step name. Called from
    // nodeMoved (after every drag) so the in-memory state always
    // mirrors the canvas. The next Save pushes it to the server.
    //
    // We iterate drawflow's internal node map (via getNodesFromName or
    // module scan) and read pos_x / pos_y off each node. Steps that
    // exist in _stepTemplate but not on canvas (race during re-render)
    // are skipped — their last-known position is kept.
    function _captureAllNodePositions() {
        if (!_editor || !_stepTemplate || _stepTemplate.length === 0) return;
        const stepNames = new Set(_stepTemplate.map((s) => s.name));
        const layout = _visualLayout && typeof _visualLayout === 'object' ? _visualLayout : {};
        // Walk every module in drawflow (we use only module 'Home' —
        // the visual editor is single-module; multi-module is a future
        // feature). For each node whose data.name matches a step,
        // capture its current (x, y).
        const modules = _editor.drawflow ? Object.keys(_editor.drawflow.drawflow) : [];
        for (const modName of modules) {
            const mod = _editor.drawflow.drawflow[modName];
            if (!mod || !mod.data) continue;
            for (const nodeId of Object.keys(mod.data)) {
                const node = mod.data[nodeId];
                if (!node || !node.data) continue;
                const stepName = node.data.name;
                if (!stepName || !stepNames.has(stepName)) continue;
                if (typeof node.pos_x === 'number' && typeof node.pos_y === 'number') {
                    layout[stepName] = { x: Math.round(node.pos_x), y: Math.round(node.pos_y) };
                }
            }
        }
        _visualLayout = layout;
    }

    // Phase 1.2 + drag: re-compute all connection SVG path d-attrs
    // using the CURRENT handle positions. This fixes drawflow 0.0.59's
    // stale-path bug:
    //   - drawflow.updateConnectionNodes(nodeId) only updates paths
    //     where the given node is the SOURCE. Paths where the node is
    //     a TARGET keep their old d-attr (which may be from a stale
    //     render or from before another card was dragged).
    //   - When the user drags a card, drawflow's mousemove handler
    //     updates the source side of all connections, but the target
    //     side stays at its last computed position.
    //   - On an initial render, addConnection internally calls
    //     updateConnectionNodes, but if the addNode loop is still
    //     mutating the DOM, getBoundingClientRect may return stale
    //     values and the computed path d-attr becomes wrong from
    //     the start. The next render (or any drag) re-uses that
    //     wrong d-attr.
    //
    // Solution: for each <svg.connection> in the container, find
    // the source and target node's input/output handle via DOM,
    // compute current screen-relative positions, and set the path's
    // `d` attribute using drawflow's internal createCurvature.
    // Called from nodeMoved (after every drag) AND from the rAF
    // block in _render (after initial render settles).
    function _recomputeAllConnectionPaths() {
        if (!_editor || !_editor.precanvas || !_editor.container) return;
        const pre = _editor.precanvas;
        const preRect = pre.getBoundingClientRect();
        const curvature = (typeof _editor.curvature === 'number') ? _editor.curvature : 0.5;
        const zoom = _editor.zoom || 1;
        const ux = (pre.clientWidth * zoom) ? (pre.clientWidth / (pre.clientWidth * zoom)) : 1;
        const uy = (pre.clientHeight * zoom) ? (pre.clientHeight / (pre.clientHeight * zoom)) : 1;
        const svgs = _editor.container.querySelectorAll('svg.connection');
        let updated = 0;
        svgs.forEach(svg => {
            const classes = (svg.getAttribute('class') || '').split(/\s+/);
            let sourceId = null, targetId = null, outputClass = null, inputClass = null;
            for (const cls of classes) {
                if (cls.startsWith('node_in_')) targetId = cls.slice(8);
                else if (cls.startsWith('node_out_')) sourceId = cls.slice(9);
                else if (cls.startsWith('output_')) outputClass = cls;
                else if (cls.startsWith('input_')) inputClass = cls;
            }
            if (!sourceId || !targetId || !outputClass || !inputClass) return;
            const sourceEl = _editor.container.querySelector('#node-' + sourceId + ' .' + outputClass);
            const targetEl = _editor.container.querySelector('#node-' + targetId + ' .' + inputClass);
            if (!sourceEl || !targetEl) return;
            const sr = sourceEl.getBoundingClientRect();
            const tr = targetEl.getBoundingClientRect();
            const srcX = sourceEl.offsetWidth / 2 + (sr.x - preRect.x) * ux;
            const srcY = sourceEl.offsetHeight / 2 + (sr.y - preRect.y) * uy;
            const tgtX = targetEl.offsetWidth / 2 + (tr.x - preRect.x) * ux;
            const tgtY = targetEl.offsetHeight / 2 + (tr.y - preRect.y) * uy;
            try {
                const d = _editor.createCurvature(srcX, srcY, tgtX, tgtY, curvature, "openclose");
                const path = svg.querySelector('path.main-path');
                if (path) {
                    path.setAttributeNS(null, 'd', d);
                    updated++;
                }
            } catch (e) {
                // Suppress per-connection errors
            }
        });
        return updated;
    }

    // Look up a step name from a drawflow connection endpoint.
    // drawflow passes either a numeric id (in older API) or the
    // full node object. We try both.
    function _getConnectionEndpointName(endpoint) {
        if (endpoint == null) return null;
        if (typeof endpoint === 'object') {
            // Could be a node with .data.name, or a {id, ...} ref
            if (endpoint.data && endpoint.data.name) return endpoint.data.name;
            if (endpoint.name) return endpoint.name;
        }
        if (typeof endpoint === 'number' || typeof endpoint === 'string') {
            const idStr = String(endpoint);
            const el = document.getElementById(`node-${idStr}`);
            if (el) {
                const stepEl = el.querySelector('[data-step-name]');
                if (stepEl && stepEl.dataset.stepName) {
                    return stepEl.dataset.stepName;
                }
            }
        }
        return null;
    }

    // Normalize a connection event/payload to a { source, target,
    // sourceClass, targetClass } shape. drawflow 0.0.59 uses
    // { output_id, input_id, output_class, input_class }; older
    // versions used { sourceId, targetId, sourceClass, targetClass }.
    function _normalizeConnection(conn) {
        const sourceId = conn.output_id !== undefined ? conn.output_id : conn.sourceId;
        const targetId = conn.input_id !== undefined ? conn.input_id : conn.targetId;
        const sourceClass = conn.output_class !== undefined ? conn.output_class : conn.sourceClass;
        const targetClass = conn.input_class !== undefined ? conn.input_class : conn.targetClass;
        return { sourceId, targetId, sourceClass, targetClass };
    }

    // Phase 1.2 + Phase 2 (FLIPPED 2026-07-30 in v2.0): when the
    // user wires two cards, route the wire to the right field
    // based on the source's output class:
    //   output_1 (chain, normal):     target.depends_on += [source]
    //   output_2 (loop-back, red dashed):
    //                                 source.feedback_to += [target]
    //                                 (v2.0: field is on FAILING step)
    //
    // v2.0 (FLIPPED) explanation: feedback_to is now on the FAILING
    // step (matches the standard on_failure pattern in AWS Step
    // Functions, Airflow, Temporal). A wire from A to B with the
    // red handle means: "if A fails, re-run B". The data lives on
    // A (the failing step), not B (the recovery step). depends_on
    // stays on the dependent step (the "downstream" end of the
    // chain) because that's the natural English reading too:
    // B.depends_on = [A] = "B depends on A".
    //
    // The visual wire from output_2 is red dashed so the two edge
    // kinds are distinguishable at a glance.
    //
    // We do NOT add a self-reference. Drawing an edge from A
    // back to A is a common user mistake; we silently ignore
    // it (the server-side validator would reject it on save
    // anyway, but we drop the in-memory state too so the next
    // save doesn't try to persist a forbidden ref).
    function _onConnectionCreated(connection) {
        const { sourceId, targetId, sourceClass } = _normalizeConnection(connection);
        const sourceName = _getConnectionEndpointName(sourceId);
        const targetName = _getConnectionEndpointName(targetId);
        if (!sourceName || !targetName) {
            console.warn('connectionCreated: missing source/target name', connection);
            return;
        }
        if (sourceName === targetName) {
            console.warn('connectionCreated: ignored self-reference', sourceName);
            return;
        }
        // v2.0: depends_on on the TARGET (dependent), feedback_to
        // on the SOURCE (failing). Different data placement.
        const isFeedback = sourceClass === 'output_2';
        const edgeLabel = isFeedback ? 'loop-back' : 'chain';
        if (isFeedback) {
            // v2.0: source.feedback_to += [target]
            const source = _stepTemplate.find((s) => s.name === sourceName);
            if (!source) {
                console.warn('connectionCreated: source step not in template', sourceName);
                return;
            }
            if (!Array.isArray(source.feedback_to)) source.feedback_to = [];
            if (!source.feedback_to.includes(targetName)) {
                // v2.1: checkpoint BEFORE the data change so undo
                // restores the pre-wire state (no loop-back).
                _checkpoint('Add loop-back: ' + sourceName + ' → ' + targetName);
                source.feedback_to.push(targetName);
                _showBanner(
                    `Wired ${sourceName} --${edgeLabel}--> ${targetName} (if ${sourceName} fails, re-run ${targetName}). Click Save to persist.`,
                    'success',
                );
            }
        } else {
            // depends_on: data on the TARGET (unchanged)
            const target = _stepTemplate.find((s) => s.name === targetName);
            if (!target) {
                console.warn('connectionCreated: target step not in template', targetName);
                return;
            }
            if (!Array.isArray(target.depends_on)) target.depends_on = [];
            if (!target.depends_on.includes(sourceName)) {
                // v2.1: checkpoint BEFORE the data change.
                _checkpoint('Add chain: ' + sourceName + ' → ' + targetName);
                target.depends_on.push(sourceName);
                _showBanner(
                    `Wired ${sourceName} --${edgeLabel}--> ${targetName}. Click Save to persist.`,
                    'success',
                );
            }
        }
    }

    // Phase 1.2 (v2.0 updated for flipped feedback_to): when the
    // user removes a wire, remove the source's name from the
    // target's depends_on, OR remove the target's name from the
    // source's feedback_to.
    // NOTE: drawflow 0.0.59 does NOT fire a connectionRemoved event
    // when the user removes a wire (only connectionCreated fires on
    // add). We wrap _editor.removeConnection in the init block to
    // call this handler manually.
    function _onConnectionRemoved(connection) {
        const { sourceId, targetId, sourceClass } = _normalizeConnection(connection);
        const sourceName = _getConnectionEndpointName(sourceId);
        const targetName = _getConnectionEndpointName(targetId);
        if (!sourceName || !targetName) return;
        const isFeedback = sourceClass === 'output_2';
        const edgeLabel = isFeedback ? 'loop-back' : 'chain';
        if (isFeedback) {
            // v2.0: feedback_to is on the SOURCE
            const source = _stepTemplate.find((s) => s.name === sourceName);
            if (!source || !Array.isArray(source.feedback_to)) return;
            const i = source.feedback_to.indexOf(targetName);
            if (i >= 0) {
                // v2.1: checkpoint BEFORE the splice.
                _checkpoint('Remove loop-back: ' + sourceName + ' → ' + targetName);
                source.feedback_to.splice(i, 1);
                _showBanner(
                    `Unwired ${sourceName} -/-> ${targetName} (${edgeLabel}). Click Save to persist.`,
                    'success',
                );
            }
        } else {
            // depends_on: data on the TARGET (unchanged)
            const target = _stepTemplate.find((s) => s.name === targetName);
            if (!target || !Array.isArray(target.depends_on)) return;
            const i = target.depends_on.indexOf(sourceName);
            if (i >= 0) {
                // v2.1: checkpoint BEFORE the splice.
                _checkpoint('Remove chain: ' + sourceName + ' → ' + targetName);
                target.depends_on.splice(i, 1);
                _showBanner(
                    `Unwired ${sourceName} -/-> ${targetName} (${edgeLabel}). Click Save to persist.`,
                    'success',
                );
            }
        }
    }

    // Phase 1.7 (2026-07-25): when the user deletes a card (Delete
    // key, the X button, or removeNodeId), remove the step from
    // _stepTemplate. drawflow 0.0.59 fires nodeRemoved with the
    // NUMERIC id as payload (e.g. "3", not "node-3"). We look up
    // the step by walking the DOM for the now-detached (or
    // just-removed) card's data-step-name attribute. We also
    // scrub the deleted step's name from any other step's
    // depends_on / feedback_to lists, so the workflow stays
    // consistent (a step that referenced the deleted one would
    // be invalid).
    function _onNodeRemoved(numericId) {
        // drawflow's id is "node-N". Look up the step name BEFORE
        // the DOM element is gone (we have a brief window — the
        // event fires synchronously during removeNodeId).
        const idStr = String(numericId);
        const wrapperEl = document.getElementById('node-' + idStr);
        let removedName = null;
        if (wrapperEl) {
            const stepEl = wrapperEl.querySelector('[data-step-name]');
            if (stepEl && stepEl.dataset.stepName) {
                removedName = stepEl.dataset.stepName;
            }
        }
        // Fallback: walk the _stepTemplate and find the one whose
        // data-step-name matches. We use the in-memory template
        // since it was in sync with drawflow at last render.
        if (!removedName) {
            for (const s of _stepTemplate) {
                if (s && s.name) {
                    // Heuristic: if we can't find the DOM, try to
                    // match the most recently-active step. But
                    // this is rare; usually the DOM is still there.
                    removedName = s.name;
                    break;
                }
            }
        }
        if (!removedName) return;
        // v3.12.6 (Phase 3): if the deleted step is the one the
        // chatbox FOCUS is set on, clear the FOCUS so the next
        // chat message doesn't reference a step that no longer
        // exists. Read the selection from sessionStorage because
        // _selectedNodeId may not be set (the user can delete
        // a card without opening its side panel first).
        if (window.chatbox) {
            const sel = window.chatbox.getSelectedNode && window.chatbox.getSelectedNode();
            if (sel && sel.kind === 'workflow_step' && sel.step_name === removedName) {
                window.chatbox.clearSelectedNode();
            }
        }
        // v2.1: checkpoint BEFORE the splice + scrub. The snapshot
        // captures the full step (with all fields) + all
        // depends_on / feedback_to references, so undo restores
        // the step in its original position with original wires.
        _checkpoint('Delete step "' + removedName + '"');
        // Remove from _stepTemplate
        const idx = _stepTemplate.findIndex((s) => s.name === removedName);
        if (idx >= 0) {
            _stepTemplate.splice(idx, 1);
        }
        // Scrub the removed name from any other step's
        // depends_on / feedback_to lists.
        for (const s of _stepTemplate) {
            if (Array.isArray(s.depends_on)) {
                s.depends_on = s.depends_on.filter((n) => n !== removedName);
            }
            if (Array.isArray(s.feedback_to)) {
                s.feedback_to = s.feedback_to.filter((n) => n !== removedName);
            }
        }
        // Close the side panel if it was showing this step
        if (_selectedNodeId === 'node-' + idStr) {
            closeSidePanel();
        }
        // Re-compute connection paths (the deleted node's wires
        // are gone too; the remaining paths may need a refresh
        // to settle the layout).
        requestAnimationFrame(_recomputeAllConnectionPaths);
        _showBanner(
            `Deleted step: ${removedName}. Click Save to persist.`,
            'success',
        );
    }

    // ---- public API ----
    function init() {
        const wrap = _canvas();
        if (!wrap) {
            console.error('visual_workflow: vf-canvas not found');
            return;
        }
        _workflowId = wrap.dataset.workflowId;
        try {
            _stepTemplate = JSON.parse(wrap.dataset.stepTemplate || '[]');
        } catch (e) {
            console.error('visual_workflow: bad step_template JSON', e);
            _stepTemplate = [];
        }
        try {
            _variables = JSON.parse(wrap.dataset.variables || '[]');
        } catch (e) {
            console.error('visual_workflow: bad variables JSON', e);
            _variables = [];
        }
        // Phase 2.5: load saved card positions. Default {} means
        // every step falls back to the vertical-stack layout. Any
        // orphan (renamed/deleted step) entries are kept — harmless.
        try {
            _visualLayout = JSON.parse(wrap.dataset.visualLayout || '{}');
        } catch (e) {
            console.error('visual_workflow: bad visual_layout JSON', e);
            _visualLayout = {};
        }
        if (!_visualLayout || typeof _visualLayout !== 'object') {
            _visualLayout = {};
        }
        _render();
        _bindGlobalShortcuts();
        // v2.1: bind Ctrl+C / Ctrl+V for copy / paste step. Done
        // once on init (guarded by the _vfCopyPasteBound flag).
        _bindCopyPasteShortcuts();
        // v2.1: initialize the Undo/Redo button disabled state.
        // _render() above will have set isRendering=true during
        // the rebuild; now that we're back in normal mode, refresh
        // the buttons to match the (empty) undo / redo stacks.
        _updateHistoryButtons();
        // Phase 1.4 (2026-07-25): click a palette chip to add a
        // step + auto-place at the next Y slot + auto-wire to
        // the last step (chain mode). Most workflows are linear
        // pipelines (search → analyze → write), so chain mode
        // covers the common case. If the user wants tree/branch,
        // they can re-wire after the chip click (the new card
        // starts with depends_on=[last] but the user can
        // edit the panel to clear or add more).
        //
        // The auto-wire fires drawflow's addConnection which
        // triggers our connectionCreated listener (Phase 1.2)
        // which adds the source name to target.depends_on. We
        // also pre-set depends_on BEFORE addConnection so the
        // /save PATCH is correct even if the addConnection
        // event fires before the listener is bound (race).
        document.querySelectorAll('.vf-palette-chip').forEach((chip) => {
            chip.addEventListener('click', () => {
                const tmpl = chip.dataset.template;
                const newStep = _newStepFromTemplate(tmpl);
                // Pre-wire: depends_on = [last step's name]
                // (chain mode). Skip if no prior step.
                const lastStep = _stepTemplate.length > 0
                    ? _stepTemplate[_stepTemplate.length - 1]
                    : null;
                if (lastStep) {
                    newStep.depends_on = [lastStep.name];
                }
                // v2.1: checkpoint BEFORE the push so undo restores
                // the pre-add state. _render() inside the undo path
                // will re-render the canvas from the restored data.
                _checkpoint('Add step "' + newStep.name + '"');
                _stepTemplate.push(newStep);
                _render();
                // After _render, drawflow has the new card but
                // not the visual edge. Add it explicitly so the
                // user sees the chain immediately.
                if (lastStep) {
                    const allNodes = _getAllNodes();
                    const targetId = _findNodeIdByStepName(newStep.name, allNodes);
                    const sourceId = _findNodeIdByStepName(lastStep.name, allNodes);
                    if (targetId && sourceId) {
                        try {
                            _editor.addConnection(sourceId, targetId, 'output_1', 'input_1');
                        } catch (e) {
                            // Duplicate or other addConnection issue
                            // (non-fatal; depends_on is already set)
                            console.warn('auto-wire addConnection failed:', e.message);
                        }
                    }
                }
                _showBanner(
                    `Added step: ${newStep.name}` +
                    (lastStep ? ` (auto-wired from ${lastStep.name}). Click Save to persist.` : '. Click Save to persist.'),
                    'success'
                );
            });
        });
    }

    // Find drawflow's node id (e.g. "node-7") for a step name.
    // Used by auto-wire after a render where the new cards are
    // already on the canvas but the connection hasn't been drawn
    // yet.
    function _findNodeIdByStepName(stepName, allNodes) {
        for (const [id, node] of Object.entries(allNodes)) {
            if (node && node.data && node.data.name === stepName) {
                return id;
            }
        }
        return null;
    }

    function _newStepFromTemplate(tmpl) {
        // 4 palette templates. The base shape is the same; the action
        // and skill differ. We pick a unique name by appending a
        // numeric suffix (collision-checked against existing names).
        const baseNames = {
            search:  { action: 'fetch_url',     skill: '' },
            analyze: { action: 'summarize',     skill: '' },
            audit:   { action: 'audit_check',   skill: '' },
            write:   { action: 'write_output',  skill: '' },
        };
        const cfg = baseNames[tmpl] || { action: 'do_thing', skill: '' };
        let n = 1;
        let name = `${tmpl}-${n}`;
        while (_stepTemplate.some((s) => s.name === name)) {
            n += 1;
            name = `${tmpl}-${n}`;
        }
        return {
            name,
            agent_role: 'win-agent01',
            action: cfg.action,
            depends_on: [],
            params_template: {},
            output_path: `out/${name}.json`,
            skill: cfg.skill,
        };
    }

    function openSidePanel(nodeId) {
        _selectedNodeId = nodeId;
        // The data-step-name attribute is on the inner .vf-node div
        // (set in _stepToCardHtml), not on the drawflow wrapper.
        // Walk all descendants to find it.
        const wrapperEl = document.getElementById(nodeId);
        const stepEl = wrapperEl ? wrapperEl.querySelector('[data-step-name]') : null;
        let step = null;
        if (stepEl && stepEl.dataset.stepName) {
            step = _stepTemplate.find((s) => s.name === stepEl.dataset.stepName) || null;
        }
        // Fallback: use the older _findStepByNodeId path
        if (!step) step = _findStepByNodeId(nodeId);
        if (!step) {
            console.warn('visual_workflow: no step for node', nodeId);
            return;
        }
        // v3.12.6 (Phase 3): context-aware editing. Write the
        // selected step to sessionStorage so the chatbox LLM can
        // see it in its system prompt and target apply_workflow_patch
        // suggestions at this step. No-op if the chatbox isn't
        // loaded (e.g. standalone workflow without a source project).
        if (window.chatbox && typeof window.chatbox.setSelectedNode === 'function') {
            window.chatbox.setSelectedNode({
                kind: 'workflow_step',
                workflow_id: _workflowId,
                step_name: step.name,
                action: step.action || '',
                agent_role: step.agent_role || '',
                depends_on: Array.isArray(step.depends_on) ? step.depends_on : [],
            });
        }
        _refreshEditFormFromTemplate(step);
        _sidePanel().classList.add('open');
    }

    // Phase 1.4 polish (2026-07-25): pull the form-input population
    // out of openSidePanel so closeSidePanel can call it too. Cancel /
    // ESC / blank-canvas click should REVERT the form to the current
    // step_template state, not just close the panel — otherwise
    // typed-but-not-applied values linger in the inputs and surface
    // the next time the user re-opens the same card. This makes Apply
    // the only way to commit, and Cancel/ESC the only ways to revert
    // (per the user's UX request).
    function _refreshEditFormFromTemplate(step) {
        if (!step) {
            const sel = _selectedNodeId ? _findStepByNodeId(_selectedNodeId) : null;
            if (!sel) return;
            step = sel;
        }
        document.getElementById('vf-edit-name').value = step.name || '';
        document.getElementById('vf-edit-role').value = step.agent_role || '';
        document.getElementById('vf-edit-action').value = step.action || '';
        document.getElementById('vf-edit-deps').value =
            Array.isArray(step.depends_on) ? step.depends_on.join(', ') : '';
        document.getElementById('vf-edit-skill').value = step.skill || '';
        document.getElementById('vf-edit-output-path').value = step.output_path || '';
        document.getElementById('vf-edit-feedback-to').value =
            Array.isArray(step.feedback_to) ? step.feedback_to.join(', ') : '';
        document.getElementById('vf-edit-params').value = JSON.stringify(
            step.params_template || {}, null, 2
        );
        // Clear any previous error message
        const errEl = document.getElementById('vf-edit-error');
        if (errEl) {
            errEl.textContent = '';
            errEl.classList.add('hidden');
        }
    }

    function closeSidePanel() {
        // Revert: restore the form inputs to the current step_template
        // state so any typed-but-not-applied values are dropped. Only
        // Apply should commit; Cancel / ESC / blank-canvas click
        // should be pure revert.
        if (_selectedNodeId) {
            _refreshEditFormFromTemplate();
        }
        _selectedNodeId = null;
        // v3.12.6 (Phase 3): clear the chatbox FOCUS context so the
        // next chat message doesn't carry a stale step reference.
        if (window.chatbox && typeof window.chatbox.clearSelectedNode === 'function') {
            window.chatbox.clearSelectedNode();
        }
        _sidePanel().classList.remove('open');
    }

    // Show a validation error inside the side panel so the user
    // sees the issue without opening F12. Called from applyEdit
    // when a field is invalid.
    function _showEditError(msg) {
        const el = document.getElementById('vf-edit-error');
        if (!el) return;
        el.textContent = msg;
        el.classList.remove('hidden');
    }

    function applyEdit() {
        if (!_selectedNodeId) {
            _showEditError('No card selected');
            return;
        }
        // _editor.drawflow.drawflow[nodeId] does NOT work — drawflow's
        // internal state is nested (module → container → numeric id).
        // Use _getAllNodes() which walks the full structure.
        const node = _getAllNodes()[_selectedNodeId];
        if (!node) {
            _showEditError('Selected card no longer exists (drawflow state mismatch)');
            return;
        }
        const step = _findStepByNodeId(_selectedNodeId);
        if (!step) {
            _showEditError('Selected card not in step_template (drag or apply broke the link)');
            return;
        }
        // Read all fields
        const newName = document.getElementById('vf-edit-name').value.trim();
        const newRole = document.getElementById('vf-edit-role').value.trim();
        const newAction = document.getElementById('vf-edit-action').value.trim();
        const depsStr = document.getElementById('vf-edit-deps').value;
        const newDeps = depsStr
            .split(',')
            .map((s) => s.trim())
            .filter((s) => s.length > 0);
        const newSkill = document.getElementById('vf-edit-skill').value.trim();
        const newOutput = document.getElementById('vf-edit-output-path').value.trim();
        const fbStr = document.getElementById('vf-edit-feedback-to').value;
        const newFeedbackTo = fbStr
            .split(',')
            .map((s) => s.trim())
            .filter((s) => s.length > 0);
        const paramsRaw = document.getElementById('vf-edit-params').value;
        let newParams;
        try {
            newParams = paramsRaw.trim() === '' ? {} : JSON.parse(paramsRaw);
        } catch (e) {
            _showEditError('Invalid JSON in params_template: ' + e.message);
            return;
        }
        if (!newParams || typeof newParams !== 'object' || Array.isArray(newParams)) {
            _showEditError('params_template must be a JSON object');
            return;
        }

        // Validate name (kebab-case, ≤ 40 chars, unique)
        if (!/^[a-z0-9][a-z0-9-]*$/.test(newName) || newName.length > 40) {
            _showEditError(`Invalid name: must be kebab-case (lowercase, digits, dashes), ≤ 40 chars`);
            return;
        }
        // Uniqueness: not used by any other step
        const oldName = step.name;
        if (newName !== oldName &&
            _stepTemplate.some((s) => s.name === newName)) {
            _showEditError(`Name "${newName}" already used by another step`);
            return;
        }
        if (!newRole) {
            _showEditError('agent_role is required');
            return;
        }
        if (!newAction) {
            _showEditError('action is required');
            return;
        }
        if (newSkill && !/^[a-z0-9][a-z0-9-]*$/.test(newSkill)) {
            _showEditError('skill must be kebab-case (lowercase, digits, dashes)');
            return;
        }

        // Validate depends_on: each must reference a step in the
        // template. We don't enforce "earlier step" here — the
        // server-side validator does. We just check the names exist.
        const allNames = new Set(_stepTemplate.map((s) => s.name));
        for (const dep of newDeps) {
            if (!allNames.has(dep)) {
                _showEditError(`depends_on references "${dep}" which is not a step in this workflow`);
                return;
            }
        }
        // Self-reference in depends_on is fine (the workflow will
        // wait for itself = deadlock, but we let the user shoot
        // themselves in the foot here — the supervisor's
        // _find_ready_tasks will mark it as never-satisfiable).
        // Validate feedback_to: same, but must reference a step
        // that is NOT this step (loop-back to self = no-op, but
        // we drop silently at run-workflow anyway).
        for (const ft of newFeedbackTo) {
            if (!allNames.has(ft)) {
                _showEditError(`feedback_to references "${ft}" which is not a step in this workflow`);
                return;
            }
            if (ft === newName) {
                _showEditError(`feedback_to cannot reference itself ("${ft}")`);
                return;
            }
        }

        // All validations passed. Commit the edits to the in-memory
        // step_template. The user must still click "Save" to persist.
        const oldNameLocal = step.name;
        // v2.1: checkpoint BEFORE the field assignment + reference
        // rewrite. The snapshot captures the old name + all field
        // values + all depends_on / feedback_to references that
        // pointed to the old name, so undo restores everything.
        _checkpoint('Edit step "' + oldNameLocal + '"' +
            (oldNameLocal !== newName ? ' → "' + newName + '"' : ''));
        step.name = newName;
        step.agent_role = newRole;
        step.action = newAction;
        step.depends_on = newDeps;
        step.skill = newSkill || null;
        step.output_path = newOutput || null;
        step.feedback_to = newFeedbackTo;
        step.params_template = newParams;
        // Update drawflow data so subsequent lookups (and the next
        // _render if the user clicks Save) use the new name.
        node.data.name = newName;
        node.data.role = newRole;
        node.data.action = newAction;

        // If the step's name changed, any OTHER step's depends_on
        // might reference the OLD name. Update those references.
        if (oldNameLocal !== newName) {
            for (const s of _stepTemplate) {
                if (Array.isArray(s.depends_on)) {
                    const i = s.depends_on.indexOf(oldNameLocal);
                    if (i >= 0) s.depends_on[i] = newName;
                }
                if (Array.isArray(s.feedback_to)) {
                    const j = s.feedback_to.indexOf(oldNameLocal);
                    if (j >= 0) s.feedback_to[j] = newName;
                }
            }
        }

        // Re-render so the card label updates immediately.
        _render();
        // Re-open the side panel for the (possibly renumbered) node
        // so the user sees the freshly-applied values.
        let newNodeId = null;
        for (const [id, el] of Object.entries(_getAllNodes())) {
            // Walk to find the inner element with data-step-name
            const stepEl = el && el.html
                ? document.querySelector(`#${id} [data-step-name]`)
                : null;
            if (stepEl && stepEl.dataset.stepName === newName) {
                newNodeId = id;
                break;
            }
        }
        if (newNodeId) {
            openSidePanel(newNodeId);
        } else {
            closeSidePanel();
        }
        _showBanner(
            oldNameLocal !== newName
                ? `Step renamed: ${oldNameLocal} -> ${newName} (click Save to persist)`
                : 'Step updated (click Save to persist)',
            'success',
        );
        // Phase 1.6: auto-add any new {{var}} placeholders from
        // this step's params_template. Showing the banner right
        // after Apply gives immediate feedback (the var will also
        // be re-checked at save time, defensively).
        const addedVars = _autoAddVariablesFromParams();
        if (addedVars.length > 0) {
            _showBanner(
                `Added variable${addedVars.length > 1 ? 's' : ''}: ${addedVars.join(', ')}. Click Save to persist.`,
                'info'
            );
        }
    }

    // ESC key closes the side panel (standard UX).
    // Bound once on init().
    function _bindGlobalShortcuts() {
        if (window._vfShortcutsBound) return;
        window._vfShortcutsBound = true;
        document.addEventListener('keydown', (ev) => {
            if (ev.key === 'Escape') {
                // v3.8.0: JSON editor is now a modal. Close the
                // topmost open sheet (modal > side panel) so Esc
                // works the way users expect.
                if (_jsonFormIsOpen()) {
                    closeJsonModal();
                    return;
                }
                const sp = _sidePanel();
                if (sp.classList.contains('open')) {
                    closeSidePanel();
                }
            }
        });
    }

    function toggleJsonForm() {
        // v3.8.0: was a toggle on a bottom-of-page div (.open class).
        // Now the JSON editor is a modal overlay. toggleJsonForm()
        // opens the modal (re-renders the textareas from the in-memory
        // mirror if needed) — closeJsonModal() closes it.
        if (_jsonFormIsOpen()) {
            closeJsonModal();
        } else {
            openJsonModal();
        }
    }

    function openJsonModal() {
        // Sync the textareas from the in-memory mirror so the user
        // sees the current state when they open the modal. The
        // server-side rendered text version is the initial value of
        // the textareas, but edits made via the canvas after the page
        // loaded wouldn't be reflected without this re-sync.
        if (typeof _stepTemplate !== 'undefined') {
            const stEl = document.getElementById('vf-step-template-json');
            if (stEl) stEl.value = JSON.stringify(_stepTemplate, null, 2);
        }
        if (typeof _variables !== 'undefined') {
            const vEl = document.getElementById('vf-variables-json');
            if (vEl) vEl.value = JSON.stringify(_variables, null, 2);
        }
        const errEl = document.getElementById('vf-json-error');
        if (errEl) { errEl.classList.add('hidden'); errEl.textContent = ''; }
        _jsonForm().classList.remove('hidden');
    }

    function closeJsonModal() {
        _jsonForm().classList.add('hidden');
    }

    function _showJsonError(msg) {
        const errEl = document.getElementById('vf-json-error');
        if (!errEl) { _showBanner(msg, 'error'); return; }
        errEl.textContent = msg;
        errEl.classList.remove('hidden');
    }

    async function save() {
        // v3.8.0: was "Q4 c" — the JSON form was a hidden bottom-of-page
        // div. Now it's a modal; we use _jsonFormIsOpen() to detect.
        // If the JSON modal is open, the user may have edited it.
        // Prefer JSON form content over the canvas when it's open.
        // v2.1: checkpoint once at the top of save() so undo can
        // revert the entire save operation (including the JSON-form
        // text edits and any topo-sort or auto-add-variable side
        // effects) back to the pre-save state.
        _checkpoint('Save (before persist)');
        let stepTemplate, variables;
        if (_jsonFormIsOpen()) {
            try {
                stepTemplate = JSON.parse(
                    document.getElementById('vf-step-template-json').value || '[]'
                );
            } catch (e) {
                // v3.8.0: also show in the modal's inline error
                // element so the user sees the error next to the
                // textarea they were editing, not just at the top
                // of the page (the banner is easy to miss).
                _showJsonError('Bad JSON in step_template: ' + e.message);
                _showBanner('Bad JSON in step_template: ' + e.message, 'error');
                return;
            }
            try {
                variables = JSON.parse(
                    document.getElementById('vf-variables-json').value || '[]'
                );
            } catch (e) {
                _showJsonError('Bad JSON in variables: ' + e.message);
                _showBanner('Bad JSON in variables: ' + e.message, 'error');
                return;
            }
            // The canvas may be stale; resync the canonical state
            _stepTemplate = stepTemplate;
            _variables = variables;
        } else {
            stepTemplate = _stepTemplate;
            variables = _variables;
        }
        // Phase 1.5 (2026-07-25): if the user has forward-ref
        // depends_on (because they dragged cards around without
        // thinking about execution order), auto-reorder the
        // step_template topologically so the server-side validator
        // accepts it. The visual layout (Y positions) is reset to
        // match the new order. We show a banner so the user knows
        // the layout changed.
        const reordered = _topoSortSteps();
        if (reordered) {
            _showBanner(
                'Reordered steps to satisfy depends_on (top → bottom = execution order).',
                'success'
            );
            stepTemplate = _stepTemplate;
        }
        // Phase 1.6 (2026-07-25): auto-add any new {{var}}
        // placeholders to _variables. The server-side validator
        // requires every {{var}} in step_template to have a
        // matching variables entry. We scan all params_templates
        // and add missing ones (type=string default) so the user
        // doesn't have to manage variables separately.
        const addedVars = _autoAddVariablesFromParams();
        if (addedVars.length > 0) {
            _showBanner(
                `Added variable${addedVars.length > 1 ? 's' : ''}: ${addedVars.join(', ')}. Edit type/default in Edit as JSON.`,
                'info'
            );
            variables = _variables;
        }
        // Phase 2.5 (2026-07-26): if the JSON form was used, the user
        // bypassed the canvas — _visualLayout may still be valid (we
        // didn't touch it) but it's safest to skip the position field
        // so the server doesn't overwrite the saved positions with
        // whatever was last in the in-memory mirror. The next drag
        // (or a future Save through the canvas) will repopulate it.
        // If the user used the canvas path, capture the live positions
        // one more time so the most recent drag is included.
        let visualLayoutToSend;
        if (_jsonFormIsOpen()) {
            visualLayoutToSend = _visualLayout;
        } else {
            _captureAllNodePositions();
            visualLayoutToSend = _visualLayout;
        }
        const url = `/api/workflows/${encodeURIComponent(_workflowId)}`;
        const body = {
            step_template: stepTemplate,
            variables: variables,
            visual_layout: visualLayoutToSend,
        };
        try {
            const r = await _fetchWithTimeout(url, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            }, 10000);
            if (!r.ok) {
                const detail = await r.text();
                _showBanner(`Save failed (${r.status}): ${detail.slice(0, 200)}`, 'error');
                return;
            }
            _showBanner('Saved', 'success');
            // Re-render so the canvas reflects the new server state
            // (especially if the JSON form was the source of truth)
            _render();
        } catch (e) {
            _showBanner('Save error: ' + e.message, 'error');
        }
    }

    // Topological sort of _stepTemplate by depends_on. Returns
    // true if the order changed (and _stepTemplate + visual Y
    // positions were updated). Steps with no dependencies come
    // first, in their existing relative order. Ties are broken
    // by the existing Y position (top-of-canvas first), so the
    // sort is stable for users who arranged cards in dependency
    // order already.
    //
    // This is the "do the right thing" workaround for the
    // forward-ref validator rule: instead of telling the user
    // "drag search-1 to the top", we just rearrange for them
    // when they Save. The user can re-drag cards to customize
    // if they want a different layout, but the step_template
    // order is always consistent with the dependency graph.
    function _topoSortSteps() {
        if (!_stepTemplate || _stepTemplate.length <= 1) return false;
        const byName = new Map(_stepTemplate.map((s) => [s.name, s]));
        // Build adjacency: dep -> [dependents], plus in-degree
        // count (number of dependencies each step has).
        const depsOf = new Map();
        for (const s of _stepTemplate) depsOf.set(s.name, []);
        for (const s of _stepTemplate) {
            const ds = Array.isArray(s.depends_on) ? s.depends_on : [];
            for (const d of ds) {
                if (!depsOf.has(d)) continue; // skip dangling refs
                depsOf.get(d).push(s.name);
            }
        }
        // Kahn's algorithm with stable order: process nodes whose
        // dependencies are all already-placed, in their existing
        // step_template order.
        const placed = new Set();
        const order = [];
        let changed = false;
        // We need to iterate in a stable way: at each step, pick
        // the first unplaced step whose deps are all in 'placed'.
        while (order.length < _stepTemplate.length) {
            let picked = null;
            for (const s of _stepTemplate) {
                if (placed.has(s.name)) continue;
                const ds = Array.isArray(s.depends_on) ? s.depends_on : [];
                if (ds.every((d) => placed.has(d) || !depsOf.has(d))) {
                    picked = s.name;
                    break;
                }
            }
            if (picked === null) {
                // Cycle or unsatisfiable — bail, leave order alone
                return false;
            }
            if (order[order.length] !== picked &&
                _stepTemplate[order.length] !== byName.get(picked)) {
                changed = true;
            }
            order.push(picked);
            placed.add(picked);
        }
        // Detect change: did the order differ from the input?
        const inputOrder = _stepTemplate.map((s) => s.name);
        if (order.every((n, i) => n === inputOrder[i])) {
            return false; // already in order, no re-layout needed
        }
        // Apply the new order to _stepTemplate
        const reordered = order.map((n) => byName.get(n));
        _stepTemplate = reordered;
        // Phase 2.5 (2026-07-26): clear the saved visual_layout
        // before re-render. The old positions are now meaningless
        // (they pointed to the pre-reorder layout) and the
        // default-render path is exactly the "stacked
        // top-to-bottom in execution order" arrangement the user
        // expects from a "Topo sort" click. The user can still
        // re-drag after this and the next drag will repopulate
        // _visualLayout via the nodeMoved listener.
        _visualLayout = {};
        // Re-render so visual Y positions match the new order.
        // _render uses a simple Y = 50 + i*120 layout, so cards
        // end up stacked top-to-bottom in execution order.
        _render();
        return true;
    }

    window.visualBuilder = {
        init,
        save,
        openSidePanel,
        closeSidePanel,
        applyEdit,
        // v3.8.0: JSON form is now a modal. toggleJsonForm opens/closes
        // the modal; openJsonModal/closeJsonModal are explicit.
        toggleJsonForm,
        openJsonModal,
        closeJsonModal,
        // v2.1 (2026-07-30): undo / redo exposed for the toolbar
        // buttons + future keyboard shortcuts. State is the snapshot
        // model: a single "checkpoint(label)" call before any
        // mutation pushes the pre-state onto the undo stack; undo
        // pops + restores + re-renders; redo is the inverse.
        undo: _undo,
        redo: _redo,
        // v2.1: copy / paste step. Ctrl+C / Ctrl+V also work via
        // _bindCopyPasteShortcuts. The clipboard is module-level
        // (single-step; paste twice → step-copy, step-copy-2).
        copyStep: _copySelectedStep,
        pasteStep: _pasteClipboard,
        // v2.1: workflow template import / export. The export
        // writes a self-contained .workflow.json file (step_template
        // + variables + format_version) the user can share or
        // archive. The import replaces the in-memory state and
        // re-renders. The user still has to click Save to persist.
        exportJson: _exportWorkflowAsJson,
        importJson: _importWorkflowFromJson,
        // v2.1: parameter hints. Calls the backend
        // /suggest-vars endpoint which extracts {{var}}
        // placeholders + infers types from literal values in
        // the same step's params. No LLM call (the v2.1
        // implementation is local + deterministic; the LLM-
        // backed version is a v2.2 followup).
        suggestVars: _suggestAndMergeVars,
        topoSort: () => {
            const changed = _topoSortSteps();
            _showBanner(
                changed
                    ? 'Reordered steps to satisfy depends_on (top → bottom = execution order).'
                    : 'Already in execution order — no change.',
                changed ? 'success' : 'info'
            );
        },
        resetLayout: () => {
            // Phase 2.5 (2026-07-26): clear all saved card positions
            // and re-render the canvas using the default vertical
            // stack (50, 50 + i*120). Does NOT change step_template
            // order — that's what distinguishes this from Topo sort.
            // The change is in-memory only; the user must click Save
            // to clear visual_layout on the server. Otherwise the
            // next page load will restore the old positions.
            if (!confirm(
                'Reset all card positions to the default vertical layout?\n\n'
                + 'This only affects the visual arrangement — step_template '
                + 'is untouched. Click Save to persist the new layout.'
            )) return;
            _visualLayout = {};
            _render();
            _showBanner(
                'Layout reset to default stack. Click Save to persist.',
                'info'
            );
        },
    };
    // Debug hook: expose internals for Playwright headless tests.
    // NOT used by the UI. Safe to ship to production.
    window._vfDebug = {
        getEditor: () => _editor,
        getStepTemplate: () => _stepTemplate,
        getSelectedNodeId: () => _selectedNodeId,
        getVisualLayout: () => _visualLayout,
    };

    // Auto-init when the DOM is ready (this script is loaded with
    // `defer`, so by the time it runs the canvas element exists).
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
