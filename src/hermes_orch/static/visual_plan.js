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
    let _selectedNodeName = null;   // which step's details are shown in the side panel
    let _jsonMode = false;          // toggle between visual and JSON textarea
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

    // ===== Init =====
    function init() {
        const wrap = $('vp-wrap');
        if (!wrap) return;
        _projectId = wrap.getAttribute('data-project-id');
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
    function _onConnectionCreated(connection) {
        const sourceInternal = connection.output_id;
        const targetInternal = connection.input_id;
        const sourceName = _stepNameFromInternalId(sourceInternal);
        const targetName = _stepNameFromInternalId(targetInternal);
        if (!sourceName || !targetName) return;
        const target = _plan.steps.find(s => s.name === targetName);
        if (!target) return;
        if (!Array.isArray(target.depends_on)) target.depends_on = [];
        if (!target.depends_on.includes(sourceName)) {
            target.depends_on.push(sourceName);
        }
    }
    // Mirror of _onConnectionCreated for the patched removeConnection.
    // drawflow doesn't fire a "connectionRemoved" event, so we wrap
    // removeConnection (above in init) to call this manually.
    function _onConnectionRemoved(connection) {
        const sourceInternal = connection.output_id;
        const targetInternal = connection.input_id;
        const sourceName = _stepNameFromInternalId(sourceInternal);
        const targetName = _stepNameFromInternalId(targetInternal);
        if (!sourceName || !targetName) return;
        const target = _plan.steps.find(s => s.name === targetName);
        if (!target || !Array.isArray(target.depends_on)) return;
        target.depends_on = target.depends_on.filter(n => n !== sourceName);
    }
    // drawflow fires nodeRemoved when a card is removed (Delete key,
    // our X button via removeNodeId, or any removeNode call). Payload
    // is the numeric id as a string. We scrub the step from _plan
    // so the in-memory model matches what's on canvas.
    function _onNodeRemoved(numericId) {
        const name = _stepNameFromInternalId(String(numericId));
        if (!name) return;
        _plan.steps = _plan.steps.filter(s => s.name !== name);
        for (const s of _plan.steps) {
            if (Array.isArray(s.depends_on)) {
                s.depends_on = s.depends_on.filter(n => n !== name);
            }
        }
        if (_selectedNodeName === name) closeSidePanel();
    }

    // ===== Step rendering =====
    function nodeHtml(step) {
        // Compact card. No skill/feedback_to in the plan (those
        // are workflow concepts; plans are simpler).
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
        const action = step.action
            ? `<div class="vp-node-action">${escapeHtml(step.action)}</div>`
            : '';
        const depsHtml = (step.depends_on && step.depends_on.length)
            ? `<div class="vp-node-deps" style="color:#6b7280;font-size:10px;margin-top:3px">← ${step.depends_on.length} dep${step.depends_on.length === 1 ? '' : 's'}</div>`
            : '';
        return `
            <div class="vp-node" data-step-name="${escapeHtml(step.name)}">
                <button class="vp-node-delete" data-node-name="${escapeHtml(step.name)}" title="Delete step">×</button>
                <div class="vp-node-header">
                    <span class="vp-node-name">${escapeHtml(step.name)}</span>
                    ${rolePill}
                </div>
                ${action}
                ${depsHtml}
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
        // We give each node 1 input + 1 output, so depends_on wires
        // use the standard 1-to-1 connection.
        // eslint-disable-next-line no-undef
        _editor.addNode(
            'vp-' + step.name,     // node name (stored as `name` on
                                   // the node data, but NOT used as
                                   // the key in drawflow's data map
                                   // — see below)
            1,                      // inputs
            1,                      // outputs
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
            _addWireManually(sourceId, targetId);
        }
    }

    // Manually add a connection: push to the data map + create the
    // SVG path. Mirrors what drawflow 0.0.59's addConnection does,
    // but the in-library version silently no-ops (see wireDepsForStep
    // comment). Tested by: doing the same push by hand in the dev
    // console — array length goes 0 -> 1 as expected.
    function _addWireManually(sourceId, targetId) {
        if (!_editor) return;
        try {
            // Push to data: source's outputs.output_1.connections
            const sourceData = _editor.drawflow.drawflow.Home.data[sourceId];
            const targetData = _editor.drawflow.drawflow.Home.data[targetId];
            if (!sourceData || !targetData) return;
            const sourceOut = sourceData.outputs.output_1;
            const targetIn = targetData.inputs.input_1;
            if (!sourceOut || !targetIn) return;
            // Skip if already connected
            for (const c of sourceOut.connections) {
                if (c.node == targetId && c.output == 'input_1') return;
            }
            sourceOut.connections.push({node: targetId.toString(), output: 'input_1'});
            targetIn.connections.push({node: sourceId.toString(), input: 'output_1'});
            // Create the SVG path (drawflow uses SVG for wires)
            if (_editor.precanvas && _editor.module === 'Home') {
                const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
                const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
                path.classList.add("main-path");
                path.setAttributeNS(null, "d", "");
                svg.classList.add("connection");
                svg.classList.add("node_in_node-" + targetId);
                svg.classList.add("node_out_node-" + sourceId);
                svg.classList.add("output_1");
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
                    output_class: "output_1", input_class: "input_1",
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
        setTimeout(() => {
            for (const step of _plan.steps) wireDepsForStep(step);
        }, 50);
    }

    // ===== Step CRUD =====
    function addStep() {
        // Generate a default name. We scan existing names and
        // pick "step-N" with the smallest N that's not taken.
        const used = new Set(_plan.steps.map(s => s.name));
        let n = 1;
        while (used.has('step-' + n)) n++;
        const name = 'step-' + n;
        const step = {
            name: name,
            agent_role: '',
            action: '',
            skill: '',
            tool: '',
            required_capability: '',
            depends_on: [],
            params_template: {},
            output_path: '',
        };
        _plan.steps.push(step);
        // Position: place below the lowest existing node
        const lastIdx = _plan.steps.length - 1;
        addNodeToCanvas(step, 100 + (lastIdx % 3) * 280, 100 + Math.floor(lastIdx / 3) * 130);
        updateMinimap();
        showBanner('Step added', 'success');
    }

    function deleteStepByName(name) {
        // Remove from plan model (in-memory). The nodeRemoved event
        // listener we registered in init() will fire when drawflow
        // actually removes the card, and that listener will run
        // ANOTHER filter pass — that's a no-op the second time
        // because the step is already gone. We do the filter here
        // first so the in-memory model is correct even if removeNodeId
        // throws (e.g. on a stale step name).
        _plan.steps = _plan.steps.filter(s => s.name !== name);
        // Scrub from any other step's depends_on
        for (const s of _plan.steps) {
            if (s.depends_on) s.depends_on = s.depends_on.filter(d => d !== name);
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
        $('vp-side-title').textContent = 'Step: ' + stepName;
        $('vp-f-name').value = step.name;
        $('vp-f-action').value = step.action || '';
        $('vp-f-role').value = step.agent_role || '';
        $('vp-f-capability').value = step.required_capability || '';
        $('vp-f-skill').value = step.skill || '';
        $('vp-f-output').value = step.output_path || '';
        $('vp-f-params').value = JSON.stringify(step.params_template || {}, null, 2);
        $('vp-f-deps').value = (step.depends_on || []).join(', ');
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
        $('vp-side-panel').classList.add('hidden');
        document.querySelectorAll('.vp-node').forEach(n => n.classList.remove('selected'));
    }

    function saveStepEdits() {
        if (!_selectedNodeName) return;
        const step = _plan.steps.find(s => s.name === _selectedNodeName);
        if (!step) return;
        const newName = ($('vp-f-name').value || '').trim();
        if (!newName) { showBanner('Name required', 'error'); return; }
        if (!KEBAB_RE.test(newName)) {
            showBanner('Name must be kebab-case (lowercase letters, digits, hyphens)', 'error');
            return;
        }
        // If name changed, check uniqueness + update refs
        if (newName !== step.name) {
            if (_plan.steps.some(s => s.name === newName)) {
                showBanner('A step with that name already exists', 'error');
                return;
            }
            const oldName = step.name;
            step.name = newName;
            // Update depends_on in other steps
            for (const s of _plan.steps) {
                if (s.depends_on) {
                    s.depends_on = s.depends_on.map(d => d === oldName ? newName : d);
                }
            }
            // Update the canvas node id
            const oldId = 'vp-' + oldName;
            const newId = 'vp-' + newName;
            // drawflow 0.0.59 doesn't have a public rename API;
            // the easiest reliable way is to remove + re-add the
            // node with the new id, preserving position.
            const pos = _editor.nodeId ? null : null;  // we don't track per-node position here
            try { _editor.removeNode(oldId); } catch (e) {}
            // Re-render the whole canvas (preserves deps via the
            // model). Simple, robust. For 10+ step plans this
            // becomes expensive; Phase C+ will add a smarter
            // rename path.
            renderAllSteps();
        }
        step.action = $('vp-f-action').value.trim();
        step.agent_role = $('vp-f-role').value.trim();
        step.required_capability = $('vp-f-capability').value.trim();
        step.skill = $('vp-f-skill').value.trim();
        step.output_path = $('vp-f-output').value.trim();
        // params: parse JSON, fall back to empty dict
        const paramsRaw = $('vp-f-params').value.trim();
        if (paramsRaw) {
            try { step.params_template = JSON.parse(paramsRaw); }
            catch (e) { showBanner('Params must be valid JSON: ' + e.message, 'error'); return; }
        } else {
            step.params_template = {};
        }
        // depends_on: comma-separated, trimmed
        const depsRaw = $('vp-f-deps').value.trim();
        step.depends_on = depsRaw
            ? depsRaw.split(',').map(s => s.trim()).filter(s => s)
            : [];
        // Re-render the canvas (to update the node card content
        // + the wires from the new depends_on). For Phase C this
        // is acceptable; Phase C+ will patch in place.
        renderAllSteps();
        // Re-open the side panel on the same step (render cleared
        // the selection).
        openSidePanel(step.name);
        showBanner('Step saved', 'success');
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
        // Save first
        await savePlan();
        if (!confirm(
            'Generate tasks from plan?\n\n' +
            'This will:\n' +
            '  • Archive the project\'s existing non-running tasks\n' +
            '  • Create ' + _plan.steps.length + ' new pending task(s) from the plan\n' +
            '  • Set project state → ready (supervisor will dispatch on next tick)\n\n' +
            'Continue?'
        )) return;
        try {
            const r = await fetch('/api/projects/' + _projectId + '/plan/run', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({archive_existing: true, name_suffix: ''}),
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
                            // Retry the run. We bypass the
                            // confirm() this time (the user
                            // already confirmed) and skip
                            // re-saving (the plan is unchanged).
                            const r2 = await fetch('/api/projects/' + _projectId + '/plan/run', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({archive_existing: true, name_suffix: ''}),
                            });
                            if (!r2.ok) {
                                const d2 = await r2.json().catch(() => ({}));
                                showBanner('Generate failed: ' + _errDetailToString(d2.detail, r2.status), 'error');
                                return;
                            }
                            const d2 = await r2.json();
                            showBanner('Generated ' + d2.tasks_created + ' task(s) — going to project...', 'success');
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
            showBanner('Generated ' + d.tasks_created + ' task(s) — going to project...', 'success');
            setTimeout(() => { location.href = '/projects/' + _projectId; }, 1500);
        } catch (e) {
            showBanner('Network error: ' + e.message, 'error');
        }
    }

    function validatePlan() {
        // Client-side validation only. Doesn't catch everything
        // (the server is the authority), but the common cases
        // — duplicate names, missing deps, empty names — surface
        // here as fast feedback.
        const issues = [];
        const names = new Set();
        for (const s of _plan.steps) {
            if (!s.name) issues.push('Step with empty name');
            else if (!KEBAB_RE.test(s.name)) issues.push('Step name "' + s.name + '" not kebab-case');
            else if (names.has(s.name)) issues.push('Duplicate step name: ' + s.name);
            else names.add(s.name);
        }
        // Check depends_on
        for (const s of _plan.steps) {
            if (!s.depends_on) continue;
            for (const d of s.depends_on) {
                if (!names.has(d)) {
                    issues.push('Step "' + s.name + '" depends on unknown step "' + d + '"');
                }
            }
        }
        if (issues.length === 0) {
            showBanner('Plan OK: ' + _plan.steps.length + ' step(s), no issues', 'success');
        } else {
            alert('Validation issues:\n\n' + issues.map((i, idx) => (idx + 1) + '. ' + i).join('\n'));
        }
    }

    // ===== JSON mode toggle =====
    function toggleJsonMode() {
        _jsonMode = !_jsonMode;
        const form = $('vp-json-form');
        if (_jsonMode) {
            // Sync current plan to the textarea
            $('vp-json-textarea').value = JSON.stringify(_plan, null, 2);
            form.classList.add('open');
        } else {
            form.classList.remove('open');
        }
    }

    function applyJsonToCanvas() {
        try {
            const newPlan = JSON.parse($('vp-json-textarea').value);
            // Minimal validation (the server is the real authority)
            if (!newPlan.steps) { alert('Plan must have a steps array'); return; }
            _plan = newPlan;
            $('vp-plan-name').value = _plan.name || '';
            $('vp-plan-description').value = _plan.description || '';
            renderAllSteps();
            updateMinimap();
            showBanner('JSON applied to canvas', 'success');
        } catch (e) {
            showBanner('Invalid JSON: ' + e.message, 'error');
        }
    }

    function copyCanvasToJson() {
        $('vp-json-textarea').value = JSON.stringify(_plan, null, 2);
        showBanner('Canvas → JSON copied', 'success');
    }

    // ===== Event wiring =====
    // drawflow 0.0.59 fires 'nodeSelected' on click and 'nodeRemoved'
    // on delete. We listen on the parent wrap.
    function wireCanvasEvents() {
        const wrap = $('vp-canvas');
        if (!wrap) return;
        // Use capture-phase to intercept clicks on our × delete buttons
        // and on nodes (open side panel).
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
            // Node click: any element inside .vp-node (or .vp-node
            // itself). The .vp-node inner div has data-step-name set
            // by nodeHtml() in renderAllSteps. This was the bug
            // before 2026-07-27 — the handler used to look for
            // `[id^="node-vp-"]` but drawflow's DOM id is just
            // "node-1", "node-2", etc., so the wrapper was never
            // found and openSidePanel was never called. That's why
            // double-clicking a card did nothing.
            const nodeInner = ev.target.closest('.vp-node[data-step-name]');
            if (nodeInner) {
                const name = nodeInner.dataset.stepName;
                if (name) openSidePanel(name);
            }
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
                180_000,  // 3 min for LLM
            );
            const j = await r.json();
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
        addStep: addStep,
        savePlan: savePlan,
        generateTasks: generateTasks,
        validatePlan: validatePlan,
        toggleJsonMode: toggleJsonMode,
        applyJsonToCanvas: applyJsonToCanvas,
        copyCanvasToJson: copyCanvasToJson,
        saveStepEdits: saveStepEdits,
        deleteSelectedStep: deleteSelectedStep,
        openGeneratePlanModal: openGeneratePlanModal,
        closeGeneratePlanModal: closeGeneratePlanModal,
        generatePlanFromLlm: generatePlanFromLlm,
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

    // ===== Bootstrap =====
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() { init(); wireCanvasEvents(); });
    } else {
        init(); wireCanvasEvents();
    }
})();
