# Chatbox as Plan Editor — Design Report

**Author**: Mavis (mvs_a6da89508d134935b0f13e8f26d24b47)
**Date**: 2026-07-28
**Status**: DRAFT for user review (not yet implemented)
**Audience**: Stanley (operator) — review before any code is written

---

## TL;DR

You want the project chatbox to be a **conversational interface for editing
the project's plan workflow object** — the same `plan_json` document that
the visual drawflow editor (Phase C, shipped 2026-07-27) edits. The chat
LLM reads the plan, suggests edits, and writes back via the existing
`PUT /api/projects/{id}/plan`. The chatbox does **NOT** create tasks
directly — the existing `POST /api/projects/{id}/plan/run` button
("Run" on the dashboard) stays as the human-in-loop gate that
materializes the plan into real tasks.

This is **3 days of work, not 3 weeks**. The hard parts (plan schema,
Pydantic validation, run/reset endpoints, visual editor) are already
shipped. The chatbox is a thin LLM-driven wrapper over the existing
plan API.

The change is small because the plan layer is already isolated
(plan = design-time artifact, tasks = execution-time instances). The
chatbox just needs to be a third way to write to that layer.

---

## 1. Background — what already exists (don't rebuild)

The plan-first architecture is already in production. Read these before
touching anything:

| File | What it has |
|---|---|
| `src/hermes_orch/api/plans.py` | All plan endpoints + `ProjectPlan`/`PlanStep` Pydantic models |
| `src/hermes_orch/api/projects.py:2233-2783` | Chat endpoints (list / send / clear / reformat / apply) |
| `src/hermes_orch/api/projects.py:2786-2902` | `apply_chat_suggestion` (legacy direct-task-create; **0 callers**, dead code) |
| `src/hermes_orch/static/visual_plan.js` | Phase C drawflow visual editor |
| `docs/visual-workflow-builder.md` | The design doc for the visual editor (parallel interface) |
| `docs/project-layout.md` | Project state machine (plan vs tasks layers) |

### The 3 plan interfaces (today)

| Interface | Status | Mechanic |
|---|---|---|
| **Visual** (Phase C) | ✅ shipped | Drawflow canvas, side panel, "Plan/Text mode" toggle, Validate/Save/Generate buttons |
| **Text** | partial | Raw `GET / PUT /api/projects/{id}/plan` JSON, no dedicated UI |
| **Chat** | 🟡 partial | `POST /api/projects/{id}/chat` calls LLM, but LLM only knows how to create raw tasks (legacy) |

**All 3 write to the same `projects.plan_json` column** (TEXT, JSON-serialized
`ProjectPlan` Pydantic model). The visual editor is the gold-standard
reference for the contract; chatbox must produce a valid `ProjectPlan`
that the visual editor can also load and re-edit.

### The "Gen Task" gate (already exists)

User clicks **Run** on the dashboard → `POST /api/projects/{id}/plan/run`
materializes `plan.steps` into rows in the `tasks` table. This is
the human-in-loop checkpoint between plan editing and execution. The
chatbox is **upstream** of this gate.

---

## 2. My understanding of your idea

You want a semi-technical user to be able to say:

> "I want to monitor HK weather every hour and post to Slack if it rains"

…and have the chatbox LLM produce a `ProjectPlan` draft (5 steps:
fetch weather, parse, check rain, post slack, log). The user then
refines conversationally:

- "Add a fallback SMS channel if Slack is down"
- "Task 3 and 4 can run in parallel"
- "Split task 2 into two — separate fetching from parsing"

…each turn the LLM re-reads the plan, applies the edit in its working
draft, and renders the current shape as a plain-text DAG so the user
can see what changed. The user clicks **Apply** to write the draft
back via `PUT /api/projects/{id}/plan`. When satisfied, user clicks
**Run** on the dashboard to materialize.

**Key constraints** (your words):

> "現在所有 plan 都只落 project plan 的workflow object , 不會生成 project task,
> 只有 user 按 gen task 才會生成 task"

> "所以 chatbox LLM 是生成 project plan workflow object , 不是生成 project task"

The chatbox is **upstream of "Gen Task"**, never downstream. The
LLM is a **UI abstraction**, not a dispatcher. Every critical op
(run, archive, delete) goes through explicit user clicks, not chat
suggestions.

---

## 3. What success looks like (user-facing outcomes)

### Outcome 1: Chat-only plan creation

A user with no plan yet opens a project, clicks the chat icon, types
their goal in plain Chinese/English. The LLM produces a 5-step plan
draft. The user accepts, the plan is saved. They click **Run** on
the dashboard to dispatch.

### Outcome 2: Chat edits a visual-built plan

A user built a plan with the drawflow editor. Realizes one step is
missing. Opens chat, says "add a step that retries 3 times before
failing". LLM adds the step, updates `depends_on` of downstream
steps, shows the diff. User clicks Apply.

### Outcome 3: Chat explains a plan

User inherited a plan from someone else. Opens chat, asks
"what does this plan do?" LLM summarizes in plain language
(reads `steps[*].action`, renders DAG, explains dependency flow).

### What's explicitly OUT of scope

- ❌ Chat-creates-tasks (legacy `create_task` suggestion type — deprecate)
- ❌ Chat-runs/dispatches (user clicks Run on dashboard, always)
- ❌ Chat-skill-extraction (separate feature, see "Skill synthesizer" in
  cross-project LLM discussion 2026-07-28)
- ❌ Chat-cross-project (project-internal only per your decision)
- ❌ Chat-script-generation (you explicitly said no, do plan object first)

---

## 4. Current state — schema is already good

### `ProjectPlan` (the document)

```python
# src/hermes_orch/api/plans.py:101
class ProjectPlan(BaseModel):
    version: str = "1.0"               # Plan schema version
    name: str = ""                      # kebab-case or empty
    description: str = ""
    trigger: str = "manual"             # manual | schedule:<id>
    variables: list[PlanVariable]       # {{var}} placeholders
    steps: list[PlanStep]               # ← this is what chatbox edits
```

### `PlanStep` (one task in the plan)

```python
# src/hermes_orch/api/plans.py:55
class PlanStep(BaseModel):
    name: str                           # kebab-case, unique within plan
    agent_role: str = ""                # canonical role name
    action: str = ""                    # what the agent does
    skill: str = ""                     # canonical skill name (not id)
    tool: str = ""                      # canonical tool name (not id)
    required_capability: str = ""
    depends_on: list[str]               # other step NAMES (not ids)
    params_template: dict = {}          # {{var}} substitution targets
    output_path: str = ""
```

**Two design decisions already baked in (must respect)**:

1. **Names, not IDs** — `name` is kebab-case and unique; `depends_on`
   uses names. Chatbox must preserve this. LLM cannot invent new IDs.
2. **References by canonical name** — `agent_role`, `skill`, `tool` are
   strings that must match registered `agent_profiles` / `skills` /
   `tools` tables. LLM must validate these (or get a clear error from
   the existing PUT endpoint — the visual editor already does this).

### Existing plan API (re-use, don't re-build)

| Method | Path | Body | Effect |
|---|---|---|---|
| `GET` | `/api/projects/{id}/plan` | — | Returns `ProjectPlanResponse` |
| `PUT` | `/api/projects/{id}/plan` | `{plan: ProjectPlan}` | **Last-write-wins** (no lock today) |
| `DELETE` | `/api/projects/{id}/plan` | — | Sets `plan_json = NULL` (back to legacy) |
| `POST` | `/api/projects/{id}/plan/run` | `{archive_existing: bool}` | Materialize → tasks (Run button) |
| `POST` | `/api/projects/{id}/plan/reset` | — | Reset terminal state for re-plan |
| `GET` | `/api/projects/{id}/plan/visual` | — | Render drawflow page (Phase C) |

**PUT today is last-write-wins** (plans.py:243-246 comment). We need to
add an optimistic-lock check (your Q1=b decision). This is a small
backward-compatible addition: send `If-Match: <updated_at>` header,
server returns 409 on mismatch.

### Chat endpoints today (re-use, narrow suggestion types)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/projects/{id}/chat` | List messages |
| `POST` | `/api/projects/{id}/chat` | Send user message → LLM responds with `suggestions: []` |
| `POST` | `/api/projects/{id}/chat/clear` | Clear history |
| `POST` | `/api/projects/{id}/chat/reformat` | Reformat a message as action chips |
| `POST` | `/api/projects/{id}/chat/apply` | Apply a suggestion (legacy: `create_task` / `run` / `replan`) |

The chat LLM **already returns structured `suggestions`**. The work is
to teach it to return **`update_plan` suggestions** (a new type) instead
of (or in addition to) `create_task`. The apply endpoint needs to
learn to handle `update_plan`.

---

## 5. The core technical challenges

### Challenge 1: Optimistic lock on PUT (you caught this, Q1=b)

Today `PUT /api/projects/{id}/plan` blindly overwrites. Add:

- Client sends `If-Match: <updated_at>` header (or `?expected_updated_at=` query)
- Server compares: if `row["updated_at"] != If-Match`, return **409 Conflict**
  with body `{current_plan: ProjectPlanResponse, your_draft: <echo>}` so the
  client (chatbox) can show a 3-way merge prompt
- First PUT (no plan yet) skips the check (set expected_updated_at to NULL)

This is a 30-line addition to `put_project_plan`.

### Challenge 2: LLM working draft vs plan on disk

The LLM needs to hold the in-progress edit in conversation context
but never write to disk until the user says Apply. Drift detection:

- Every LLM turn starts with a hidden `GET /api/projects/{id}/plan` call
- If `updated_at` changed since last turn → warn user
- If user clicks Apply with stale draft → 409 from optimistic lock,
  chatbox re-fetches, offers "Reload + apply your diff on top" or
  "Discard your diff"

### Challenge 3: Deprecation of legacy `create_task` / `run` suggestions

`apply_chat_suggestion` (projects.py:2786) has 0 callers (graphify
verified 2026-07-28). All 3 types (`create_task`, `run`, `replan`)
are vestigial. Options:

- **(a) Hard delete** — remove the endpoint, remove from LLM system prompt
- **(b) Soft deprecate** — keep endpoint, add `DEPRECATED` warning header
- **(c) Repurpose** — change `create_task` to "add plan step", `run` to
  "save plan" (but conceptually confusing)

I recommend **(a) hard delete** in the same PR as adding `update_plan`.
YAGNI — the visual editor and Run button already cover the use case.

### Challenge 4: Chatbox UI surface (your Q2=b decision)

User clicks chat icon → opens side panel. NOT auto-open, NOT near Run
button. The chat widget should be on the project detail page,
positioned consistently (e.g. right-side slide-out, or modal). The
visual editor at `/api/projects/{id}/plan/visual` is a full-page
route — the chatbox is a widget-level interaction.

### Challenge 5: LLM system prompt discipline

The LLM must:

- NEVER suggest running/dispatching (Run is human-only)
- NEVER call `POST /api/projects/{id}/plan/run` or any task-CREATE endpoint
- ONLY suggest `update_plan` (replace whole plan) or read-only `GET`
- Always re-read plan at start of each turn (for drift detection)
- Always end response with DAG render + diff-since-last-apply
- Use kebab-case step names, validate against registered
  `agent_profiles` / `skills` / `tools` before suggesting

---

## 6. Modified plan — 3 phases

### Phase 0 — Optimistic lock + LLM-friendly GET (FOUNDATION)

**Goal**: Make the existing `PUT /api/projects/{id}/plan` safe for
concurrent edits and easy for the LLM to call.

| Task | Effort |
|---|---|
| Add `If-Match: <updated_at>` to PUT `/api/projects/{id}/plan`, return 409 on mismatch | 2 hr |
| Add a `GET /api/projects/{id}/plan/agents` helper that returns valid `agent_role` / `skill` / `tool` names (LLM needs this for validation) | 2 hr |
| Deprecate `apply_chat_suggestion` (delete endpoint + 3 suggestion types) | 1 hr |
| Add `update_plan` suggestion type to `apply_chat_suggestion` (re-enabled) | 2 hr |
| Tests: optimistic lock race, suggest→apply round-trip | 3 hr |

**Total: ~1.5 days**

### Phase 1 — Chatbox widget + LLM system prompt (MVP)

**Goal**: A user can open chat on a project page, describe a goal,
refine the plan conversationally, click Apply, see it persisted.

| Task | Effort |
|---|---|
| Build chat side-panel widget (existing chat endpoints, new UI) | 2 days |
| Write LLM system prompt (Phase 1 scope: only `update_plan` suggestions, no other types) | 1 day |
| Add DAG renderer to chat responses (plain text box-drawing, per your Q3=A) | 4 hr |
| End-to-end test: describe → edit → apply → re-open → see persisted plan | 1 day |

**Total: ~4 days**

### Phase 2 — Polish + drift UX (OPTIONAL)

| Task | Effort |
|---|---|
| 3-way merge UI when optimistic lock fires (show disk vs your draft, side-by-side) | 1 day |
| Chatbox explains a plan ("what does this do?") | 4 hr |
| Persist chat history per-project (`chat.jsonl` in project folder) so user can come back | 4 hr |
| Conflict history in audit log | 2 hr |

**Total: ~3 days (defer until Phase 1 ships)**

---

## 7. The implementation sketch

### 7.1 Optimistic lock (Phase 0)

```python
# src/hermes_orch/api/plans.py — modify put_project_plan
@router.put("/projects/{project_id}/plan", response_model=ProjectPlanResponse)
async def put_project_plan(
    project_id: str,
    body: ProjectPlanUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> ProjectPlanResponse:
    db = request.app.state.db
    proj = await db.fetchone(
        "SELECT id, plan_json, updated_at FROM projects WHERE id = ?",
        (project_id,),
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {project_id}")
    # Optimistic lock: client must echo updated_at they read
    current_updated_at = proj.get("updated_at") or ""
    if if_match is not None and if_match != current_updated_at:
        # Return the current plan so the client can show a 3-way diff
        has_plan, current_plan, _ = _load_plan_from_row(proj)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "plan was modified since you last read it",
                "your_if_match": if_match,
                "current_updated_at": current_updated_at,
                "current_plan": current_plan.model_dump() if current_plan else None,
            },
        )
    # ... rest of existing PUT logic
```

### 7.2 `update_plan` suggestion type

```python
# src/hermes_orch/api/projects.py — modify apply_chat_suggestion
if stype == "update_plan":
    plan = s.get("plan")
    if not isinstance(plan, dict):
        raise HTTPException(400, "update_plan suggestion missing 'plan' object")
    # Use the same PUT semantics (which now has optimistic lock)
    from hermes_orch.api.plans import put_project_plan
    return await put_project_plan(
        project_id=project_id,
        body=ProjectPlanUpdate(plan=ProjectPlan.model_validate(plan)),
        request=request,
        if_match=s.get("if_match"),  # LLM must echo the updated_at it read
    )
```

### 7.3 LLM system prompt (Phase 1)

```text
You are the chatbox assistant for a project's plan editor.

# What you can do
- Read the current plan: GET /api/projects/{id}/plan
- Replace the plan: PUT /api/projects/{id}/plan (with If-Match header)
- Reply with a plain-text DAG render of the current plan
- Suggest edits the user can accept or reject

# What you MUST NEVER do
- Call POST /api/projects/{id}/plan/run (Run is human-only)
- Call POST /api/tasks/ or any task-CREATE endpoint
- Call DELETE /api/projects/{id}/plan without explicit user request
- Invent step names that aren't kebab-case
- Use IDs in depends_on (must use step NAMES)
- Use agent_role / skill / tool names that aren't in the registered list
  (always GET /api/projects/{id}/plan/agents first to see valid names)

# Each turn you must
1. GET /api/projects/{id}/plan (for drift detection — compare updated_at
   to the value at end of last turn)
2. If drift: warn "⚠️ plan was edited externally. Reload or apply on top?"
3. Apply the user's edit to your in-memory draft
4. Validate: step names unique, kebab-case, no cycle, deps resolve,
   agent_role/skill/tool exist
5. End response with the DAG render + "Apply?" chip with the full plan
6. NEVER write to disk until user clicks Apply

# DAG render format (plain text, box-drawing)
Linear chain:
  fetch-weather
    └─ parse-json
         └─ check-rain
              └─ post-slack

Branching:
  load-config
    ├─ fetch-A
    └─ fetch-B
         └─ combine
              └─ report

# Output format
End every response with:
  [DAG render]
  [✓ 5 steps, no cycles, all deps resolved]
  [⚠ optional warnings]
  [Apply chip with {type: "update_plan", plan: {...}, if_match: "..."}]

# Reference data (load once at session start, cache)
- /api/projects/{id}/plan/agents — list of valid agent_role / skill / tool names
```

### 7.4 Chat widget UI (Phase 1)

- Slide-out right panel, opens on chat icon click (per your Q2=b)
- 60% width on desktop, full-screen on mobile
- Message list + input box (use existing `/api/projects/{id}/chat` endpoint)
- Each assistant message has 3 zones: text, DAG render (monospace `<pre>`), suggestion chip
- Click chip → POST `/api/projects/{id}/chat/apply` with the suggestion → on success, show "Applied ✓" and re-render

---

## 8. Design decisions (locked)

| # | Decision | Source |
|---|---|---|
| 1 | Chatbox LLM = plan editor (NOT task creator) | your 2026-07-28 |
| 2 | "Gen Task" = Run button (existing `POST /plan/run`), chatbox does NOT trigger | your 2026-07-28 |
| 3 | API: `GET / PUT /api/projects/{id}/plan` (existing) — no new plan endpoint | Phase 0 |
| 4 | PUT optimistic lock via `If-Match: <updated_at>` header | your Q1=b |
| 5 | Chatbox loads existing plan as draft on open | your Q2=B |
| 6 | Render: plain-text DAG, box-drawing chars (`└─`, `├─`) | your Q3=A |
| 7 | User clicks icon to open chatbox (NOT auto-open) | your Q2=b |
| 8 | Whole-plan PUT (LLM re-reads each turn for drift) | Phase 0 design |
| 9 | Deprecate legacy `create_task` / `run` / `replan` suggestion types | Phase 0 |
| 10 | Add `update_plan` suggestion type (new) | Phase 0 |
| 11 | Optimistic lock `If-Match: <updated_at>` ISO timestamp | A=a |
| 12 | Audit actor: `operator:chat` (consistent with old convention) | B=a |
| 13 | Chat history: `projects/{id}/chat.jsonl` per-project file | C=a |

---

## 9. Effort summary

| Phase | Effort | When |
|---|---|---|
| **0** Optimistic lock + LLM-friendly endpoints + deprecation | 1.5 days | Week 1 |
| **1** Chatbox widget + LLM prompt + DAG render + E2E | 4 days | Week 1-2 |
| **2** 3-way merge, plan explainer, chat history | 3 days (optional) | Week 3+ |
| **TOTAL MVP** | **~5.5 days** | 1.5 weeks |

---

## 10. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| LLM hallucinates invalid `agent_role` / `skill` / `tool` names | High | Always GET `/plan/agents` first; Pydantic validator on PUT already rejects (visual editor pattern) |
| Drift during long chat sessions (visual editor edits while chat open) | Medium | Optimistic lock + 3-way merge UI (Phase 2) |
| User clicks Apply with stale draft, gets 409, loses work | Medium | Chatbox catches 409, re-fetches, offers "Apply your diff on top" with surgical merge by step name |
| LLM invents step IDs (UUIDs) instead of using names | Medium | System prompt forbids; LLM has reference to existing step names from GET |
| Concurrent chat + visual edits corrupt the DAG | Low (single user MVP) | Optimistic lock prevents last-write-wins; Phase 2 adds merge UI |
| `apply_chat_suggestion` removal breaks existing LLM prompts | High (if any external prompt uses old types) | None known (graphify shows 0 callers), but grep before deletion |
| Visual editor and chatbox both write same `plan_json` | Low (single user MVP) | Same optimistic lock mechanism applies to visual editor's save (extending Phase 0 to both paths) |

---

## 11. What I'll do next (after your review)

1. **Confirm open micro-decisions** (A/B/C above) — 5 min
2. **Open PR for Phase 0** — optimistic lock, helper endpoint, deprecation
3. **Draft LLM system prompt** (Phase 1) as a separate file under
   `prompts/chatbox_plan_editor.system.txt` (need to check if this dir exists)
4. **Wire chat widget to existing chat endpoints** — minimal HTML+JS,
   no framework change
5. **E2E test on a real project** — describe goal, edit in chat, click
   Run on dashboard, verify tasks materialized

---

## 12. Cross-references

- `docs/visual-workflow-builder.md` — the visual editor design (Phase C)
  the chatbox is parallel to
- `docs/project-layout.md` — project state machine (plan vs tasks layers)
- `src/hermes_orch/api/plans.py` — the plan API source of truth
- `src/hermes_orch/api/projects.py:2233-2783` — chat endpoints
- `src/hermes_orch/api/projects.py:2786-2902` — legacy apply_chat_suggestion
  (to be deleted in Phase 0)
- User profile (2026-07-22): "orch = coordinator, NOT worker. Orch owns:
  task lifecycle / dispatch / audit / memory / synthesis / schedule.
  Agent owns: hermes / tools / files / API / result. 15MB file cap."

---

## 13. Review checklist (for Stanley)

- [ ] Confirm open micro-decisions A/B/C
- [ ] Agree with phase plan (Phase 0 first?)
- [ ] Confirm `apply_chat_suggestion` hard-delete vs soft-deprecate
- [ ] Confirm chat widget UI placement (side panel right? modal? full page?)
- [ ] Confirm audit log actor naming
- [ ] Confirm chat history persistence choice
- [ ] Sign off on LLM system prompt scope (no task-create, no script-gen)

---

**End of spec. ~530 lines. Awaiting review before any code change.**
