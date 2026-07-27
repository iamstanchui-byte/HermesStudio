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

    // ===== Step rendering =====
    function nodeHtml(step) {
        // Compact card. No skill/feedback_to in the plan (those
        // are workflow concepts; plans are simpler).
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
            <button class="vp-node-delete" data-node-name="${escapeHtml(step.name)}" title="Delete step">×</button>
            <div class="vp-node-name">${escapeHtml(step.name)}</div>
            ${rolePill}
            ${action}
            ${depsHtml}
        `;
    }

    function addNodeToCanvas(step, x, y) {
        // drawflow 0.0.59: editor.addNode(name, inputs, outputs, posx, posy, class, data, html)
        // We give each node 1 input + 1 output, so depends_on wires
        // use the standard 1-to-1 connection.
        // eslint-disable-next-line no-undef
        _editor.addNode(
            'vp-' + step.name,     // node name (also used as id prefix)
            1,                      // inputs
            1,                      // outputs
            x, y,
            'vp-node-wrapper',      // wrapper class (drawflow adds this)
            { stepName: step.name }, // node data (custom field)
            nodeHtml(step),         // inner HTML
        );
    }

    function wireDepsForStep(step) {
        // For each dep, find the target node by step name and
        // connect output_1 -> input_1. drawflow stores connections
        // as {nodeId, outputId, inputId, class}.
        if (!step.depends_on) return;
        for (const depName of step.depends_on) {
            const sourceId = 'vp-' + depName;
            const targetId = 'vp-' + step.name;
            try {
                // eslint-disable-next-line no-undef
                _editor.addConnection(sourceId, 'output_1', targetId, 'input_1');
            } catch (e) {
                // source not found yet — happens when drawing
                // forward refs. We'll retry after all nodes are
                // rendered. See renderAllSteps.
            }
        }
    }

    function renderAllSteps() {
        if (!_editor) return;
        // Clear existing
        _editor.clear();
        // Add each step at a default position (later, Phase C+
        // will use the visual_layout field for persisted positions)
        const baseX = 100, baseY = 100;
        const dx = 280, dy = 130;
        for (let i = 0; i < _plan.steps.length; i++) {
            const step = _plan.steps[i];
            const col = i % 3;
            const row = Math.floor(i / 3);
            addNodeToCanvas(step, baseX + col * dx, baseY + row * dy);
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
        // Remove from plan model
        _plan.steps = _plan.steps.filter(s => s.name !== name);
        // Remove from any other step's depends_on
        for (const s of _plan.steps) {
            if (s.depends_on) s.depends_on = s.depends_on.filter(d => d !== name);
        }
        // Remove from canvas (drawflow: editor.removeNode(nodeId))
        const nodeId = 'vp-' + name;
        try { _editor.removeNode(nodeId); } catch (e) { /* may already be removed */ }
        // Clear side panel if it was showing this step
        if (_selectedNodeName === name) closeSidePanel();
        updateMinimap();
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
        // Highlight the selected node visually
        document.querySelectorAll('.vp-node').forEach(n => n.classList.remove('selected'));
        const nodeEl = document.querySelector('.vp-node-wrapper#node-vp-' + stepName);
        if (nodeEl) {
            const inner = nodeEl.querySelector('.vp-node');
            if (inner) inner.classList.add('selected');
        }
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
        // Re-sync depends_on from drawflow's connection data.
        // drawflow 0.0.59 stores connections in editor.drawflow
        // (a map: nodeId -> {data, inputs, outputs, class, html, ...}).
        // We pull depends_on from the outputs of each node.
        syncDepsFromCanvas();
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

    function syncDepsFromCanvas() {
        // drawflow connection model: editor.drawflow[nodeId].outputs
        // = {output_1: {connections: [{node: targetNodeId, output: 'input_1'}]}}
        // We translate that back to step-name depends_on by stripping
        // the 'vp-' prefix from each side.
        if (!_editor || !_editor.drawflow) return;
        const df = _editor.drawflow;
        for (const step of _plan.steps) {
            const nodeId = 'vp-' + step.name;
            const node = df[nodeId];
            if (!node || !node.outputs) { step.depends_on = []; continue; }
            const deps = [];
            for (const outKey of Object.keys(node.outputs)) {
                const out = node.outputs[outKey];
                if (!out.connections) continue;
                for (const conn of out.connections) {
                    if (conn.node && conn.node.startsWith('vp-')) {
                        deps.push(conn.node.substring(3));
                    }
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
                showBanner('Generate failed: ' + _errDetailToString(d.detail, r.status), 'error');
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
            // Node click: any element inside .vp-node (or .vp-node itself).
            // The drawflow canvas puts the node HTML inside a wrapper
            // div with id "node-vp-{name}".
            const nodeInner = ev.target.closest('.vp-node');
            if (nodeInner) {
                const wrapper = nodeInner.closest('[id^="node-vp-"]');
                if (wrapper) {
                    const name = wrapper.id.replace('node-vp-', '');
                    openSidePanel(name);
                }
            }
        });
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
    };

    // ===== Bootstrap =====
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() { init(); wireCanvasEvents(); });
    } else {
        init(); wireCanvasEvents();
    }
})();
