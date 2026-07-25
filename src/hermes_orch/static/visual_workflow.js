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

    // ---- DOM lookup ----
    function _canvas() { return document.getElementById('vf-canvas'); }
    function _sidePanel() { return document.getElementById('vf-side-panel'); }
    function _jsonForm() { return document.getElementById('vf-json-form'); }
    function _saveBanner() { return document.getElementById('vf-save-banner'); }

    // ---- helpers ----
    function _showBanner(text, kind) {
        const b = _saveBanner();
        b.textContent = text;
        b.className = `vf-save-banner show ${kind || 'success'}`;
        setTimeout(() => { b.className = 'vf-save-banner'; }, 2400);
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
            _editor.on('nodeMoved', () => {
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

        // Lay out steps top-to-bottom in their declared order.
        // Drawflow positions are in pixels; we start at (50, 50) and
        // stack vertically with 120px between cards.
        const x = 50, y = 50, dy = 120;
        try {
            _stepTemplate.forEach((step, i) => {
                const html = _stepToCardHtml(step);
                const data = {
                    name: step.name,
                    role: step.agent_role,
                    action: step.action,
                };
                // drawflow 0.0.59 addNode signature:
                //   addNode(name, n_inputs, n_outputs, posx, posy, classoverride, data, html)
                // The inputs/outputs args are NUMBERS (count of
                // connection points), not class names. drawflow
                // auto-generates class names "input_1", "input_2",
                // ... and "output_1", "output_2", ... on the
                // rendered elements. We pass 1 each so each card
                // has one input handle (left) and one output handle
                // (right). CSS in visual_workflow.html targets the
                // .input_1 / .output_1 classes.
                _editor.addNode(
                    step.name,         // 1: name
                    1,                 // 2: number of inputs
                    1,                 // 3: number of outputs
                    x,                 // 4: posx
                    y + i * dy,        // 5: posy
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
                (step.depends_on || []).forEach((depName) => {
                    const sourceNumeric = nameToNumericId[depName];
                    if (!sourceNumeric) return;
                    try {
                        _editor.addConnection(
                            sourceNumeric, targetNumeric,
                            'output_1', 'input_1',
                        );
                    } catch (e) {
                        // Suppress: edges are a nice-to-have, not critical
                        console.warn(`addConnection(${sourceNumeric}->${targetNumeric}) failed:`, e.message);
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

    // Phase 1.2: when the user wires two cards (drag from card A's
    // output handle to card B's input handle), add A's name to B's
    // depends_on. We DON'T add B to A.depends_on — that's the wrong
    // direction. depends_on is "I depend on these earlier steps".
    // Note: We do NOT add a self-reference. Drawing an edge from
    // A back to A is a common user mistake; we silently ignore it.
    function _onConnectionCreated(connection) {
        const { sourceId, targetId } = _normalizeConnection(connection);
        const sourceName = _getConnectionEndpointName(sourceId);
        const targetName = _getConnectionEndpointName(targetId);
        if (!sourceName || !targetName) {
            console.warn('connectionCreated: missing source/target name', connection);
            return;
        }
        if (sourceName === targetName) {
            // Self-reference: drop the connection visually (the
            // user probably didn't mean it) and don't update state.
            // We can't undo the addConnection from here easily,
            // but the next save will reject it via validator.
            console.warn('connectionCreated: ignored self-reference', sourceName);
            return;
        }
        const target = _stepTemplate.find((s) => s.name === targetName);
        if (!target) {
            console.warn('connectionCreated: target step not in template', targetName);
            return;
        }
        if (!Array.isArray(target.depends_on)) target.depends_on = [];
        if (!target.depends_on.includes(sourceName)) {
            target.depends_on.push(sourceName);
            _showBanner(
                `Wired ${sourceName} -> ${targetName}. Click Save to persist.`,
                'success',
            );
        }
    }

    // Phase 1.2: when the user removes a wire, remove the source's
    // name from the target's depends_on.
    // NOTE: drawflow 0.0.59 does NOT fire a connectionRemoved event
    // when the user removes a wire (only connectionCreated fires on
    // add). We wrap _editor.removeConnection in the init block to
    // call this handler manually.
    function _onConnectionRemoved(connection) {
        const { sourceId, targetId } = _normalizeConnection(connection);
        const sourceName = _getConnectionEndpointName(sourceId);
        const targetName = _getConnectionEndpointName(targetId);
        if (!sourceName || !targetName) return;
        const target = _stepTemplate.find((s) => s.name === targetName);
        if (!target || !Array.isArray(target.depends_on)) return;
        const i = target.depends_on.indexOf(sourceName);
        if (i >= 0) {
            target.depends_on.splice(i, 1);
            _showBanner(
                `Unwired ${sourceName} -/-> ${targetName}. Click Save to persist.`,
                'success',
            );
        }
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
        _render();
        _bindGlobalShortcuts();
        // Bind palette chip clicks (Phase 1.4: stub — appends a blank step)
        document.querySelectorAll('.vf-palette-chip').forEach((chip) => {
            chip.addEventListener('click', () => {
                const tmpl = chip.dataset.template;
                const newStep = _newStepFromTemplate(tmpl);
                _stepTemplate.push(newStep);
                _render();
                _showBanner(`Added step: ${newStep.name}`, 'success');
            });
        });
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
    }

    // ESC key closes the side panel (standard UX).
    // Bound once on init().
    function _bindGlobalShortcuts() {
        if (window._vfShortcutsBound) return;
        window._vfShortcutsBound = true;
        document.addEventListener('keydown', (ev) => {
            if (ev.key === 'Escape') {
                const sp = _sidePanel();
                if (sp.classList.contains('open')) {
                    closeSidePanel();
                }
            }
        });
    }

    function toggleJsonForm() {
        _jsonForm().classList.toggle('open');
    }

    async function save() {
        // Q4 c: if the JSON form is open, the user may have edited it.
        // Prefer JSON form content over the canvas when it's open.
        let stepTemplate, variables;
        if (_jsonForm().classList.contains('open')) {
            try {
                stepTemplate = JSON.parse(
                    document.getElementById('vf-step-template-json').value || '[]'
                );
            } catch (e) {
                _showBanner('Bad JSON in step_template: ' + e.message, 'error');
                return;
            }
            try {
                variables = JSON.parse(
                    document.getElementById('vf-variables-json').value || '[]'
                );
            } catch (e) {
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
        const url = `/api/workflows/${encodeURIComponent(_workflowId)}`;
        const body = {
            step_template: stepTemplate,
            variables: variables,
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

    window.visualBuilder = {
        init,
        save,
        openSidePanel,
        closeSidePanel,
        applyEdit,
        toggleJsonForm,
    };
    // Debug hook: expose internals for Playwright headless tests.
    // NOT used by the UI. Safe to ship to production.
    window._vfDebug = {
        getEditor: () => _editor,
        getStepTemplate: () => _stepTemplate,
        getSelectedNodeId: () => _selectedNodeId,
    };

    // Auto-init when the DOM is ready (this script is loaded with
    // `defer`, so by the time it runs the canvas element exists).
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
