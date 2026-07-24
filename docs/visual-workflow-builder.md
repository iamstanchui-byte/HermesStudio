# Visual Workflow Builder — Design Report

**Author**: Mavis (mvs_f4e610aa044443e5bb42ffb1b6674610)
**Date**: 2026-07-24
**Status**: DRAFT for user review (not yet implemented)
**Audience**: Stanley (operator) — review before any code is written

---

## TL;DR

You want a web-based **pick-and-drop workflow builder** on the orch dashboard so a
semi-technical user (who only knows how to type a goal in natural language) can
see, edit, and reorder the steps of a workflow visually — without ever writing
JSON. The data model for this **already exists** in `workflow_packages.step_template`
(an ordered list of `{name, agent_role, action, depends_on, params_template,
output_path, skill}` plus a separate `variables` list). The visual builder is
**purely a new UI layer** on top of that model.

**The change is big because it's central, not because it's hard.** Most of the
work is in two areas: (a) building the drag-and-drop UI, and (b) closing the
**stale-data bug** you caught — when an audit step fails and re-dispatches an
earlier step, all **transitive dependents** (via `depends_on`) must also be
reset to `pending`, otherwise downstream analysis agents keep using the old
output.

This doc proposes **4 phases** (Phase 0–3). Total estimated effort: **3–4 weeks
of focused work** spread over however long is comfortable. The plan prioritises
the cascading-invalidation primitive (Phase 0) because every later phase builds
on it.

---

## 1. Background — why this matters

### The product principle (your words, 2026-07-22)

> orch = coordinator, NOT worker. Orch owns: task lifecycle / dispatch / audit
> / memory / synthesis / schedule. Agent owns: hermes / tools / files / API /
> result. **15 MB file cap. Share folder via OS mount / storage_refs / skill.**

### The audience problem (your words, 2026-07-22)

> Target audience: semi-technical (majority). **NOT developers** — "if
> developer, they can write similar tools themselves". **Templates are the
> moat** for semi-technical audience (backtest / research / monitor 1-click
> starters). **Error messages must be human-readable**, not stack traces.

A semi-technical user can already:

- Pick a workflow from `/workflows`
- Fill in `{{var}}` values on a form
- Click Run

But they **cannot** look at the workflow and say "step 3 should be inserted
between step 1 and step 2" or "step 4's `depends_on` should also include step
3, not just step 1". To do that today they have to edit JSON. That's a
deal-breaker for the audience.

A visual builder turns "edit JSON" into "drag a card" — the same way Notion /
n8n / Zapier did for their respective domains.

### What's already there (don't rebuild)

| Layer | Where | Status |
|---|---|---|
| Data model (step_template + variables) | `workflow_packages` table | ✅ done |
| LLM synth from goal → JSON package | `_SKILL_SYNTHESIS_PROMPT` style prompt in `workflows.py` | ✅ done (Stage 1) |
| Validation of the package | `_validate_workflow_package()` in `workflows.py` | ✅ done |
| Variable substitution on Run | `_substitute_variables()` | ✅ done |
| Project-from-package execution | `POST /api/workflows/{id}/run` | ✅ done (Stage 2b) |
| Skill reference + multi-skill awareness | step `skill` field | ✅ done (Stage 1.5) |
| Text-only edit page | `templates/workflow_detail.html` | ✅ done |

What's **NOT** there is: visual rendering, drag-reorder, edge editing, add/remove
step with form, loop-back. That's what this design covers.

---

## 2. My understanding of your idea

A workflow is conceptually **a directed graph of task cards + the variables they
share**. The cards have inputs (which previous cards they depend on) and
metadata (agent role, action, params template, skill). Variables are declared
once and referenced from `{{name}}` placeholders inside any card.

You want the user to be able to:

1. **Open a workflow** and see it as a graph of cards on a canvas
2. **Reorder** cards by dragging
3. **Wire** cards together by dragging from one card's output handle to
   another card's input handle (this sets `depends_on`)
4. **Edit a card's metadata** in a side panel (name, role, action, params
   template, output path, skill)
5. **Add a new card** by picking from a palette of common patterns
   (search / analyze / audit / write) — not from a blank form
6. **Save** the visual graph back as a `step_template` JSON in the package

You also want a **loop-back pattern**: search → analyze → audit. If audit
fails, **re-run the search** (not just the audit) so the analyze step can pick
up the corrected data.

### What you caught (the stale-data bug)

> 如果是 A, 會不會只是那個 feedback_to task 重做, 但之後有關聯的 task 不會
> 再做?即是如果是搜 data 失敗,再搜後成功, 我怕就當完成, 之後的分析 agent
> 不會再去分析新 data

You are exactly right. My first draft of the loop-back pattern (a `feedback_to`
field that re-dispatches only that one step) had this bug. The correct design is
**cascading invalidation**: when step X is re-dispatched, all tasks that
transitively depend on X (via `depends_on`) must also be reset to `pending`.
Otherwise the analyze agent happily reports "task complete" using yesterday's
data.

This is **not a feature add** — it's a primitive the supervisor needs to do
correctly. It is Phase 0 of this plan.

---

## 3. What success looks like (user-facing outcomes)

### Outcome 1: Visual edit on an existing workflow

> User opens `wf-dailyhkweather-v2`, sees 3 cards: `fetch-hko-forecast` →
> `analyze-precipitation` → `write-report-to-gdrive`. Drags a new card
> `cross-check-with-jma` between step 1 and 2. Wires its input handle to
> step 1's output. Edits the new card's params_template in the side panel.
> Clicks Save. Now `wf-dailyhkweather-v3` exists with the new step. Next
> Run uses v3.

### Outcome 2: Visual edit + loop-back

> User opens a `wf-research-and-audit` workflow. Sees 4 cards: `search` →
> `analyze` → `audit` → `deliver`. The audit card has a "loop-back" arrow
> pointing to `search`. User runs the workflow. Search fetches data, analyze
> reports on it, audit fails (e.g. insufficient sources). **Audit
> automatically re-dispatches search**. Supervisor cascades: `search` →
> re-runs; `analyze` (depends on search) → reset to `pending`; `audit`
> (depends on analyze) → reset to `pending`; `deliver` (depends on audit)
> → reset to `pending`. Search runs again with the fix (e.g. wider date
> range), then analyze, then audit, then deliver. Max 3 iterations before
> giving up.

### Outcome 3: Semi-tech user can ship a new workflow

> User types "I want a workflow that every morning checks my email for
> invoices, downloads the PDFs, and saves them to my Google Drive folder".
> The LLM synth produces a 4-step `step_template` (the existing flow). User
> clicks "Edit visually". Drags/reorders/wires as needed. Clicks Save. The
> workflow is now a reusable asset. User runs it once to verify. Tomorrow
> it auto-runs on schedule.

### What's explicitly OUT of scope for this design

- Building a new LLM synthesiser from scratch (we already have it)
- Replacing the text edit form (we keep it as fallback for power users)
- Real-time multi-user collaboration (single-user is fine for v1)
- A marketplace of community workflows (private install is enough)
- Visual diffing between workflow versions (we have `version` field, not diff UI)

---

## 4. Current state — schema is already good

### `workflow_packages` table

```sql
id, name, version, description,
step_template (JSON),       -- ordered list of step dicts
variables (JSON),           -- list of variable definitions
source_project_id, created_at, updated_at
```

### `step_template[i]` schema (current, 7 fields)

| field | required | example | notes |
|---|---|---|---|
| `name` | yes | `"fetch-hko-forecast"` | kebab-case, ≤ 40 chars, unique in template |
| `agent_role` | yes | `"win-agent01"` | must exist in user's agents |
| `action` | yes | `"fetch_url"` | snake_case verb_thing |
| `depends_on` | yes | `["search-hko"]` | list of earlier step names; `[]` if root |
| `params_template` | yes | `{"url": "{{hko_url}}"}` | dict; may contain `{{var}}` |
| `output_path` | no | `"./out/forecast.md"` | where the agent writes output |
| `skill` | no | `"hk-weather-forecast"` | optional kebab-case skill reference (Stage 1.5) |

**Allowed field set is enforced by `_STEP_FIELDS` in `workflows.py:38-49`** —
any extra key in the package is rejected at validation time. So new fields
added in later phases (e.g. `feedback_to`) must be added to `_STEP_FIELDS` AND
to the validator.

### `tasks` table (relevant columns)

```sql
id, project_id, name, agent_role, action, status,
depends_on (JSON list of task IDs),
on_parent_failure (default 'skip'),
retry_count, max_retries, timeout_seconds,
error, result, output_path, started_at, ended_at, ...
```

Status state machine (current):
`pending → assigned → running → completed | failed | cancelled | skipped`

The supervisor already has `_find_ready_tasks()` (line 1166) that selects
pending tasks where all `depends_on` are `completed` or `skipped`. This is
**exactly** the dispatcher we need for cascading invalidation — we just
need to make sure the re-set task is back to `pending` so it shows up in
this query.

---

## 5. The core technical challenges

### Challenge 1: Cascading invalidation (you caught this)

When step X is re-dispatched (because an audit step looped back), we must:

1. Set X to `status = 'pending'`, clear `result`, clear `error`, clear
   `started_at` / `ended_at`
2. Find all tasks Y where Y.depends_on contains X.task_id
3. For each such Y, repeat: if Y is already in a terminal state, reset to
   `pending` and recurse
4. Stop when no more dependents (BFS termination)
5. Free any profile that was `busy` on a now-`pending` task (so it gets
   reassigned)

This is **the single most important correctness primitive** in the whole
visual-builder feature. Get this wrong and the loop-back pattern is
silently broken (output looks complete but is based on stale data).

### Challenge 2: Visual graph representation

The user needs to see a graph. JS libraries to evaluate:

- **drawflow** (MIT, simple, JSON-driven, good for n8n-style flows) —
  recommended for MVP
- **React Flow** (MIT, more powerful, but React-based; we'd need a build step)
- **D3 + custom** (max flexibility, max work)
- **dagre** for layout (used under the hood by both)

**Recommendation**: drawflow for MVP. It loads from CDN, takes JSON in, spits
JSON out, and has 80% of what we need. If we hit a wall (e.g. nested groups,
zooming, undo/redo), we can swap to React Flow in Phase 3.

### Challenge 3: `feedback_to` schema

The current `step_template` schema has no concept of "re-dispatch me on
failure of another step". We need a new optional field:

```json
{
  "name": "search-hko",
  "agent_role": "win-agent01",
  "action": "fetch_url",
  "depends_on": [],
  "params_template": {...},
  "output_path": "...",
  "skill": "hk-weather-forecast",
  "feedback_to": null,
  "max_iterations": 3
}
```

`feedback_to` is a **list of step names** whose failure should re-dispatch
this step. Default `null` (no loop-back). Add to `_STEP_FIELDS`.

The LLM synth prompt needs a new rule:

> "If a later step audits the output of an earlier step, the earlier step
> may need to be re-dispatched if the audit fails. Set `feedback_to` on the
> earlier step to the audit step's name. **Both steps' `max_iterations` will
> be respected** — the system will refuse to loop more than `max_iterations`
> times total."

### Challenge 4: Iteration counter (project-level)

We already have `projects.max_iterations` and `projects.current_iteration`.
For the loop-back pattern, we increment `current_iteration` each time we
re-enter `search`. If `current_iteration > max_iterations`, supervisor
marks the project `failed` with a clear human-readable error
("Audit kept failing after 3 retries — please review the search params").

### Challenge 5: Human-readable error messages

When a loop-back fails (max iterations hit), the error must say something
like:

> "Workflow looped 3 times but audit step kept failing. The last search
> returned: ... The last audit said: ... Please check the params and try
> again."

NOT:

> "task_audit_xyz failed with exit code 1 after 3 retries"

The existing supervisor already writes `error` to the task row — we just
need to make sure the orchestrator frontend renders the project-level
`error` column nicely when the project fails due to loop-exhaustion.

---

## 6. Modified plan — 4 phases

The original conversation proposed 3 phases (visual MVP → side panel →
marketplace). The cascading invalidation primitive is **new** and critical
enough to be its own phase. Revised plan:

### Phase 0 — Cascading invalidation primitive (CRITICAL FOUNDATION)

**Why first**: every later phase assumes the supervisor can correctly
reset dependent tasks. Without this, loop-back silently produces stale
data.

**Scope**:
- Add `feedback_to` and `max_iterations` to `_STEP_FIELDS` and validator
- New supervisor method `_cascade_reset(task_id)`: BFS through dependents
  and reset all to `pending`
- New supervisor method `_maybe_loop_back(project_id)`: after a task
  fails, check if any of its dependents' `feedback_to` references it; if
  so, increment `current_iteration`, run cascade, log a `loopback.fired`
  audit event
- Update LLM synth prompt (the `_SKILL_SYNTHESIS_PROMPT` style block in
  `workflows.py`) to include the feedback_to rule
- Add unit tests in `tests/test_cascade.py` for: 3-step chain reset,
  diamond reset (DAG with 2 branches), terminal-state short-circuit,
  max_iterations enforcement
- Manual E2E: run a workflow with deliberate `feedback_to` and verify
  downstream tasks actually re-run

**Effort**: 3-4 days of focused work (mostly supervisor + tests; small
schema touch).

**Acceptance**:
- Re-dispatching task X in a 3-step chain resets X + Y + Z to pending
- A re-dispatch that would exceed `max_iterations` is refused and
  surfaces a human-readable error
- The `loopback.fired` audit event is written each time a loop fires
  (useful for debugging)

### Phase 1 — Visual builder MVP (the "pick and drop" page)

**Why second**: now the user can re-arrange the graph visually. But the
audit/loop-back wiring comes in Phase 2, so the MVP first delivers the
"see it as a graph + drag-reorder + edit metadata" experience.

**Scope**:
- New route: `GET /workflows/{id}/visual` → `templates/visual_workflow.html`
- New API: `PUT /api/workflows/{id}` (already exists, but extend to accept
  the full package from the visual editor)
- JS dependencies (loaded from CDN, not bundled): `drawflow` + `dagre`
- Render `step_template` as cards: name on top, agent_role badge, action
  chip, depends_on edge list
- Drag-to-reorder (just changes order in the list; `depends_on` references
  survive)
- Drag-to-wire (output handle of card A → input handle of card B sets
  `B.depends_on += [A.name]`)
- Side panel on card click: edit name / role / action / params_template /
  output_path / skill
- "Add Step" button: palette of 4 templates (search / analyze / audit /
  write) that pre-fill a new step card
- "Save" button: PUT the new `step_template` + `variables` back to the
  API; existing validator enforces shape; bump version
- Keep the text edit form as fallback (link "Edit as JSON" in the corner)

**Effort**: 1-1.5 weeks.

**Acceptance**:
- User can open a workflow, drag a card, drop it between two others, click
  Save, and the new order persists across reload
- User can wire two cards by dragging handle-to-handle
- User can edit a card's params_template in the side panel and see the
  `{{var}}` highlighted
- User can add a card from the palette and the LLM validation passes

### Phase 2 — Loop-back visual wiring

**Why third**: the data model is now in (Phase 0) and the user can edit
the graph (Phase 1). Now we add the UI for the loop-back arrow.

**Scope**:
- Each card has a small "loop-back handle" in addition to the input/output
  handles
- Drag from card Y's loop-back handle to card X's input = set
  `X.feedback_to = [Y.name]`
- Visual: dashed red arrow from Y back to X, labeled "on Y's fail, re-run X"
- Each card shows its current `feedback_to` set and `max_iterations` cap
  in the side panel
- "Test loop-back" button on the side panel: simulates a failure of the
  audit step and shows which tasks would be reset
- LLM synth prompt: still the same as Phase 0, but the visual editor now
  exposes the field so the user can override

**Effort**: 1 week.

**Acceptance**:
- User can wire `search` ← `audit` with the loop-back handle
- Saving persists the feedback_to and the visual shows the dashed arrow
- Running a workflow with a loop-back configured actually re-dispatches
  the search step when audit fails (covered by Phase 0 tests + manual E2E)

### Phase 3 — Polish + power features (OPTIONAL)

**Why last**: nice-to-haves. None are required for the semi-tech user
to be productive.

**Scope** (pick from these based on remaining time / user demand):
- Live preview: side-by-side of the visual graph + a synthetic execution
  trace ("at this point search would call fetch_url with these params")
- Undo/redo (drawflow has basic support, wire to keyboard shortcuts)
- Variable picker: when editing a card's params_template, show a
  dropdown of declared variables instead of free-typing `{{var}}`
- "Duplicate step" and "Disable step" (set `enabled: false`, supervisor
  treats disabled steps as no-ops)
- Visual diff between workflow versions
- Per-step timeout override (the underlying `tasks.timeout_seconds` field
  already exists; expose it in the side panel)
- Export workflow to a portable JSON file / import from file (the
  current save is a PUT; add `GET /api/workflows/{id}/export` +
  `POST /api/workflows/import`)

**Effort**: 1-2 weeks for everything, but each item is 1-3 days.

**Acceptance**: depends on which items are picked.

---

## 7. Cascading invalidation — the implementation sketch

This is the bit you caught. Here's what the supervisor needs to do when
"audit failed and feedback_to points to search":

```python
async def _cascade_reset(self, project_id: str, root_task_id: str) -> list[str]:
    """BFS through the depends_on graph, resetting terminal-state
    descendants of root_task_id to pending. Returns the list of reset
    task IDs (for audit logging)."""
    reset_ids = []
    queue = [root_task_id]
    seen = {root_task_id}

    while queue:
        current = queue.pop(0)
        # 1. Reset this task to pending (if it was terminal)
        await self.db.execute(
            "UPDATE tasks SET status = 'pending', result = NULL, "
            "error = NULL, started_at = NULL, ended_at = NULL, "
            "retry_count = 0, updated_at = ? "
            "WHERE id = ? AND status IN ('completed', 'failed', 'skipped', 'cancelled')",
            (_now_iso(), current),
        )
        reset_ids.append(current)

        # 2. Find all tasks that depend on this one
        rows = await self.db.fetchall(
            "SELECT id, depends_on FROM tasks WHERE project_id = ?",
            (project_id,),
        )
        for row in rows:
            deps = json.loads(row["depends_on"] or "[]")
            if current in deps and row["id"] not in seen:
                seen.add(row["id"])
                queue.append(row["id"])

    # 3. Free any profile stuck on a now-pending task
    for tid in reset_ids:
        await self.db.execute(
            "UPDATE agent_profiles SET status = 'idle', current_task_id = NULL "
            "WHERE current_task_id = ?",
            (tid,),
        )

    return reset_ids
```

And the loop-back decision:

```python
async def _maybe_loop_back(self, project_id: str, failed_task_id: str) -> bool:
    """If failed_task_id is in any other task's feedback_to list, AND
    the project's current_iteration < max_iterations, run cascade
    reset on that other task and return True. Otherwise return False."""
    proj = await self.db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not proj:
        return False
    if proj["max_iterations"] and proj["current_iteration"] >= proj["max_iterations"]:
        return False  # would exceed cap

    # Find the failed task's name (so we can search feedback_to)
    failed = await self.db.fetchone("SELECT name FROM tasks WHERE id = ?", (failed_task_id,))
    if not failed:
        return False
    failed_name = failed["name"]

    # Find tasks in this project whose feedback_to includes failed_name
    rows = await self.db.fetchall(
        "SELECT id, name, feedback_to FROM tasks WHERE project_id = ?",
        (project_id,),
    )
    targets = []
    for r in rows:
        fb = json.loads(r.get("feedback_to") or "null")
        if isinstance(fb, list) and failed_name in fb:
            targets.append(r["id"])

    if not targets:
        return False

    # Increment iteration
    await self.db.execute(
        "UPDATE projects SET current_iteration = current_iteration + 1, "
        "updated_at = ? WHERE id = ?",
        (_now_iso(), project_id),
    )

    # Cascade reset each target
    for tid in targets:
        reset = await self._cascade_reset(project_id, tid)
        await audit_log(self.db, "loopback.fired", actor="supervisor",
                        project_id=project_id, task_id=tid,
                        payload={"trigger": failed_name, "reset_count": len(reset)})
    return True
```

The hook point is in `_handle_execution` (line 754 of `supervisor.py`) where
failed tasks are propagated — right after that, call `_maybe_loop_back`
and if it returns True, the next tick will pick up the now-pending tasks
via `_find_ready_tasks` (line 1166) and dispatch them normally.

**Time estimate**: 3-4 hours of supervisor code + 1-2 hours of tests. The
hard part is getting the BFS right (especially the diamond DAG case where
the same task depends on two different tasks, both of which got reset).

---

## 8. Key design questions for your review

These are the things I'm **assuming** but want to confirm before writing code.

**Status**: all 9 questions reviewed on 2026-07-24. Answers below.

### Q1: Phase 0 first? → **(a) Phase 0 first**

Phase 0 ships the cascade primitive + `feedback_to` schema + LLM synth
rule + 13 unit tests. Logic foundation verified before any visual
layer is built on top. (Done — commit `bc26a85`.)

### Q2: Which JS library for the visual canvas? → **(a) drawflow**

MIT, simple, n8n-style, JSON in/out, single CDN include (no build
step). We can swap to React Flow in Phase 3 if we outgrow it.

### Q3: Loop-back UI — handle or palette? → **(a) Third "loop-back handle"**

Each card has 3 handles: input (left), output (right), loop-back
(bottom). User drags from card Y's loop-back handle → card X's input
handle to set `X.feedback_to = [Y.name]`. Visual: red dashed arrow
from Y back to X, labeled "on Y's fail, re-run X". Consistency with
the other handles wins over the simpler "side panel dropdown".

### Q4: Keep the text edit form? → **(c) Hidden but kept**

The text form on `workflow_detail.html` is hidden by default (UI
collapse) but accessible via a small toggle in the corner. Power users
get bulk-edit + LLM synth output lands here. Semi-tech users won't
see it unless they ask.

### Q5: max_iterations default? → **(a) 0 (opt-in)**

`max_iterations=0` is the default for all projects. Loop-back is fully
disabled until a project explicitly sets `max_iterations > 0`. Safe
default; old workflows unchanged.

### Q6: Show loop-back in the project execution log? → **(a) New "Iterations" tab**

`project.html` gets a new "Iterations" tab that joins on `audit_log`
events with type `loopback.fired` and `loopback.cap_reached`. Renders:

```
Iteration 1 (2026-07-24 12:34:56):
  - search: completed
  - analyze: completed
  - audit: FAILED  ← trigger
  → re-dispatched: search
  → cascade reset: 3 task(s) [analyze, audit, deliver]

Iteration 2 (2026-07-24 12:36:12):
  - search: completed (with new params)
  ...
```

Phase 2 ships this together with the loop-back handle.

### Q7: Skills + loop-back interaction? → **(a) Same skill, same params, same agent**

When a task re-runs via loop-back, the skill reference, agent_role,
and params_template (with substituted values) are identical to the
original run. The only thing that changes is the **data inputs**
(variables re-read from the world, e.g. a fresh URL). Supervisor code
needs no special handling — the task row is already populated with
everything except `result`/`started_at`/`ended_at`, which is exactly
what the wrapper expects to start fresh.

### Q8: Manual loop-back trigger? → **(b) No, automatic only**

We do NOT add a manual "Re-run" button. Use cases the user explored:
- "Config wrong, auto didn't fire" → fix the config; not a UX issue
- "Result unsatisfactory" → change params + re-run the whole workflow
- "Test new params" → same
- "A/B test" → duplicate the workflow with a different name

All four are better served by re-running the workflow than by a manual
trigger. The cost of building the button (new state, new audit event,
cascading concerns) is not justified.

### Q9: Out-of-scope confirmation? → **(a) All 5 confirmed in-scope**

5 items confirmed **NOT in this design** (Phase 3+ or never):

1. Multi-user real-time collaboration
2. Workflow marketplace / community sharing
3. Visual diff between workflow versions
4. Live execution preview
5. Workflow permissions (who can run what)

Phase 1/2/3 plans are unaffected. Re-evaluate at Phase 3 planning.

---

## 9. Effort summary

| Phase | Effort | Risk | Hardest part |
|---|---|---|---|
| 0 — Cascade primitive | 3-4 days | medium (BFS correctness) | Diamond DAG reset |
| 1 — Visual MVP | 1-1.5 weeks | medium (UX) | Picking the right library + side panel design |
| 2 — Loop-back UI | 1 week | low (builds on 0+1) | Discoverability of the third handle |
| 3 — Polish | 1-2 weeks (pick-and-choose) | low | Live preview is the most complex |

Total: **3-4 weeks** of focused work, but each phase is independently
shippable. We can stop after any phase and still have a useful system.

---

## 10. Risks

1. **Drawflow limits**: if we need nested groups, custom node rendering,
   or zoom-to-1M-node graphs, we have to swap libraries mid-build. **Mitigation**:
   build a thin data adapter layer between the JSON and the library so
   the swap is one file.

2. **Cascading invalidation bugs**: missing a node in the BFS means
   stale data sneaks through. **Mitigation**: comprehensive unit tests
   for chain, diamond, and disconnected-component DAGs.

3. **LLM synth prompt drift**: the synth prompt already has 6 known
   failure modes (catalog 32-37 in memory). Adding a `feedback_to` rule
   introduces 1-2 more. **Mitigation**: add explicit WRONG/RIGHT examples
   for `feedback_to`, following the same pattern as the existing 6.

4. **The `feedback_to` field is invisible to existing workflows**: any
   workflow created before this change has no `feedback_to` set, so
   loop-back is a no-op (safe default). New workflows opt in.

5. **The visual editor breaks the LLM synth output**: if a user
   visually edits a workflow that the LLM originally created, the
   LLM's next pass might re-override the user's edits. **Mitigation**:
   the visual editor's PUT bumps the version (`v0.1.0` → `v0.2.0`), so
   the LLM synth creates a new workflow (with the same name + v-bump)
   rather than mutating the visual version.

---

## 11. What I'll do next (after your review)

Once you've answered Q1-Q9, I'll:

1. Update this doc with your answers (committed to `docs/`)
2. Start Phase 0 (cascade primitive + feedback_to schema) — if you
   chose (a) for Q1
   - OR start Phase 1 (visual MVP) — if you chose (b) for Q1
3. Update memory with the new patterns as they emerge
4. Push each phase as a separate commit series so review is granular

If you want to **defer the whole thing** and address something else
first (the long-pending E2E test on `wf-c72f7f380af8`, the M5
idempotency work, the temp-file cleanup, etc.), that's also a
perfectly reasonable answer to this design review — just say
"hold the visual builder" and we move on.

---

## 12. Review summary (2026-07-24)

| # | Question | Answer |
|---|---|---|
| Q1 | Phase 0 first? | **(a) Phase 0 first** ✅ shipped (`bc26a85`) |
| Q2 | Which JS library? | **(a) drawflow** |
| Q3 | Loop-back UI? | **(a) Third "loop-back handle"** (red dashed arrow) |
| Q4 | Keep text edit form? | **(c) Hidden but kept** (toggle in corner) |
| Q5 | max_iterations default? | **(a) 0 (opt-in)** — already shipped |
| Q6 | Show loop-back in log? | **(a) New "Iterations" tab** (Phase 2) |
| Q7 | Skills + loop-back? | **(a) Same skill/params/agent re-run** |
| Q8 | Manual loop-back trigger? | **(b) No, automatic only** |
| Q9 | Out-of-scope confirm? | **(a) All 5 in-scope** (not in design) |

All 9 questions answered. Phase 0 done. Ready to start Phase 1 (visual
MVP) on your signal.

---

## 13. Phase 4+ vision: visual project page (2026-07-24, future)

**User-stated (2026-07-24)**: workflow 圖像化 only ships the workflow
piece. The final vision is to ALSO visually operate the project page:

> "現在只是 workflow, 做完 workflow 圖像化後, 最後在 project 也是要加
> 這個圖像化 create task, import skill, storage_refs, workflow 等
> resource"

This means the project page should let the operator:

1. **Create task visually** — drag-add a step card onto the project
   timeline (similar to a workflow but live, not a reusable asset).
2. **Import skill visually** — pick from the agent's installed skill
   library and attach to a step.
3. **Manage storage_refs visually** — add/edit/remove storage paths
   the agent can write to.
4. **Pick workflow as a resource** — choose a workflow package from
   the library and launch it as the project's "starter template".

### Why this is a separate phase (not Phase 1-3)

- **Different lifecycle**: workflow = long-lived reusable asset;
  project = short-lived execution container. Their visual models
  differ. The workflow editor cares about reusable composition; the
  project editor cares about live execution + iteration.
- **Different audience impact**: workflow editor helps power-users
  + semi-tech users DESIGN workflows. Project visual helps operators
  (and end-users) LAUNCH them and inspect mid-run state.
- **Reuse, don't rewrite**: the visual patterns from workflow
  (drawflow cards, edge wiring, side panel, palette, save) are
  reusable as React-style components. The project visual is a
  different page that composes them, not a separate framework.

### Reusable patterns from workflow (Phase 1)

After Phase 1 ships, the following are extracted and reusable:

- `card.{name, agent_role, action, params_template, output_path, skill, feedback_to}`
- `palette.addStepFromTemplate(template)` (4 default templates)
- `edge.depends_on` (drawflow connection)
- `edge.feedback_to` (Phase 2, red dashed loop-back)
- `sidePanel.{open, close, editField, save}`
- `save.{patch, onSuccess, onError, statusBanner}`

The project visual page can import these and compose them with
**project-specific affordances**: live task status, run history,
iteration log, agent profile.

### Phase 4+ roadmap (sketch — review needed)

| Phase | Effort | Scope |
|---|---|---|
| 4.1 | 1-2 days | Reusable JS components: `vf-card`, `vf-edge`, `vf-side-panel`, `vf-palette` extracted into `src/hermes_orch/static/vf-components.js`. Each component has a documented public API. |
| 4.2 | 1 week | Project page visual: `/projects/{id}/visual` route. Live task cards (color-coded by status: pending / running / completed / failed). `import skill` picker (opens a side panel listing the agent's installed skills). `storage_refs` manager (add/remove paths, validation against current OS mount). |
| 4.3 | 1 week | `pick workflow as resource` flow: a "Use a workflow" button on a new project page that opens a workflow library picker, then the chosen workflow's `step_template` is rendered as the project's initial task graph. |
| 4.4 | 1 week | Live execution view: tasks animate as the supervisor transitions them; iteration counter increments visibly; `loopback.fired` events get a visual ripple on the affected cards. |
| 4.5 | 1-2 weeks | Optional: shared editing of a project by 2+ operators, undo/redo, project export to workflow (the reverse of "promote from project"). |

### Re-evaluate at Phase 3 planning

When Phase 3 (visual builder polish) ships, re-decide whether to
start Phase 4. The cost-benefit depends on:
- How many operators actually use the visual builder daily
- How much time they save vs editing JSON
- Whether the user-stated "final vision" has shifted

---

*End of design report. Phase 0 shipped; Phase 1 skeleton shipped
(commits bc26a85 through b59b56d). Phase 2+ awaiting decisions.
Phase 4+ vision captured for future reference.*
