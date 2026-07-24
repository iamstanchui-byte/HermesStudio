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
        return `
            <div class="vf-node-header">
                <span class="vf-node-name">${esc(step.name || '(unnamed)')}</span>
                <span class="vf-node-role">${esc(step.agent_role || '?')}</span>
            </div>
            <div class="vf-node-action">${esc(step.action || '?')}</div>
            ${skill}
        `;
    }

    function _clearConnections(editor) {
        // Remove all existing connections so a re-render doesn't
        // duplicate them. Drawflow keeps connections per-node, so we
        // walk every node and clear its outputs.
        Object.keys(editor.drawflow.drawflow).forEach((nodeId) => {
            const node = editor.drawflow.drawflow[nodeId];
            if (node && node.outputs) {
                Object.keys(node.outputs).forEach((outKey) => {
                    const out = node.outputs[outKey];
                    if (out && out.connections) {
                        out.connections = [];
                    }
                });
            }
        });
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
        }
        // Always clear and re-render from canonical state
        _editor.clear();
        _clearConnections(_editor);

        // Lay out steps top-to-bottom in their declared order.
        // Drawflow positions are in pixels; we start at (50, 50) and
        // stack vertically with 120px between cards.
        const x = 50, y = 50, dy = 120;
        const nameToNodeId = {};
        try {
            _stepTemplate.forEach((step, i) => {
                const html = _stepToCardHtml(step);
                const data = {
                    name: step.name,
                    role: step.agent_role,
                    action: step.action,
                };
                _editor.addNode(
                    html,                                  // html
                    ['vf-input'],                          // inputs (1 input point on the left)
                    ['vf-output'],                         // outputs (1 output point on the right)
                    x, y + i * dy,                         // pos
                    'vf-node',                             // class
                    data,                                  // data
                    step.name,                             // name as drawn on node
                );
                // Drawflow names new nodes node-<n> where n is an internal counter.
                // We rely on the editor's last node id.
                const lastId = Object.keys(_editor.drawflow.drawflow)
                    .map((k) => parseInt(k.replace('node-', ''), 10))
                    .reduce((a, b) => Math.max(a, b), 0);
                nameToNodeId[step.name] = `node-${lastId}`;
            });
        } catch (e) {
            console.error('visual_workflow: addNode failed:', e);
            _showInitError(e);
            return;
        }

        // Wire depends_on: for each step B with deps ["A","C"], find
        // node A and C, and connect A's output -> B's input.
        try {
            _stepTemplate.forEach((step) => {
                const targetNodeId = nameToNodeId[step.name];
                if (!targetNodeId) return;
                (step.depends_on || []).forEach((depName) => {
                    const sourceNodeId = nameToNodeId[depName];
                    if (!sourceNodeId) return;
                    // Drawflow addConnection: sourceNode, sourceOutput, targetNode, targetInput
                    // Each node has 1 output (index 0) and 1 input (index 0).
                    _editor.addConnection(sourceNodeId, targetNodeId, 'output_1', 'input_1');
                });
            });
        } catch (e) {
            console.error('visual_workflow: addConnection failed:', e);
            // Non-fatal: cards still show, just no edges
        }

        // Click handler: open the side panel with details
        // We bind to the wrap, not the inner target, so clicks
        // anywhere in the canvas area can reach us.
        if (!wrap._vfClickBound) {
            wrap.addEventListener('click', (ev) => {
                const nodeEl = ev.target.closest('.drawflow-node');
                if (!nodeEl) return;
                // The element id is e.g. "node-3"
                const id = nodeEl.id;
                window.visualBuilder.openSidePanel(id);
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
        // nodeId is "node-<n>". We mapped name -> nodeId at render time
        // in the local nameToNodeId, but that's gone after _render
        // returns. Re-derive by walking _stepTemplate in the same
        // order as render (stable).
        // The Drawflow editor assigns node ids in the order addNode
        // is called, so node-1 = first step, node-2 = second, etc.
        const idx = parseInt(nodeId.replace('node-', ''), 10) - 1;
        if (Number.isNaN(idx) || idx < 0) return null;
        return _stepTemplate[idx] || null;
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
        const step = _findStepByNodeId(nodeId);
        if (!step) {
            console.warn('visual_workflow: no step for node', nodeId);
            return;
        }
        document.getElementById('vf-edit-name').value = step.name || '';
        document.getElementById('vf-edit-role').value = step.agent_role || '';
        document.getElementById('vf-edit-action').value = step.action || '';
        document.getElementById('vf-edit-deps').value =
            Array.isArray(step.depends_on) ? step.depends_on.join(', ') : '';
        document.getElementById('vf-edit-skill').value = step.skill || '';
        document.getElementById('vf-edit-output-path').value = step.output_path || '';
        document.getElementById('vf-edit-params').value = JSON.stringify(
            step.params_template || {}, null, 2
        );
        _sidePanel().classList.add('open');
    }

    function closeSidePanel() {
        _selectedNodeId = null;
        _sidePanel().classList.remove('open');
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
        toggleJsonForm,
    };

    // Auto-init when the DOM is ready (this script is loaded with
    // `defer`, so by the time it runs the canvas element exists).
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
