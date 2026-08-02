# HermesStudio

> **Hybrid Agentic Workflow Runtime** — 以 Workflow 為骨架、以 Object 為能力單元、以 LLM 為決策插件嘅 AI 編排平台。
>
> 唔係 chat-first，亦唔係 n8n-style integration-first，係 **AI-native orchestration substrate**。

---

## 定位（一句話）

LLM 純做 **design-time assistant**（草擬 plan / 建議 route / 評審 audit 等等），runtime 完全 deterministic（depends_on graph + status machine + retry / feedback loop / TASK_FAILED），LLM 永遠唔喺 hot path 落決定。

關鍵唔係「似唔似 n8n」，係 **deterministic 同 agentic 能力要清楚分層**。

---

## 重點功能（v3.11.1, 2026-08-03）

### 1. Project + DAG Plan Engine

- Project lifecycle: `planned → ready → running → completed | failed`
- Step DAG via `depends_on[]` + cascade archive on plan change
- **Loop-back wire (`feedback_to[]`)** — failing step triggers upstream re-dispatch
- **Loop-back cap (`max_iterations`)** — 防止無限 loop（v3.10.10）
- **TASK_FAILED marker convention**（v3.11.0）— agent 印 `TASK_FAILED: <reason>` 觸發 `status=failed` 走 feedback_to

### 2. Visual Editor（兩種）

- **Visual Plan Editor** (`/projects/{id}/visual`) — 直接編輯 JSON `plan_json`（steps, deps, feedback_to, params_template），drawflow 渲染
- **Visual Workflow Builder** (`/workflows/{id}/visual`) — 由 step_template 編 reusable workflow package，drag-and-drop
- 兩者都支援 dark mode（`body.dark` switch），red dashed wire = feedback_to（loop-back data signal）

### 3. Workflow Package System

- 從 project `promote-to-workflow` → reusable `workflow_packages` row
- `apply-workflow` 將 workflow 套落新 project（additive，唔覆寫現有 tasks）
- `run-workflow` 帶 variable substitution（`{{var}}` placeholder）
- **Visual Workflow editor** 整 work 嘅 workflow 都有 version + description + visual_layout

### 4. Agent Fleet + Multi-Role Profile

- Multi-host agent registration（`/api/agents` + HMAC heartbeat）
- 每個 agent 多個 `profile_configs`（不 同 role / skill / capability）
- Routing by `required_capability`（v3.10.0+）
- Storage refs（smb / local / gdrive / s3 / url）per-profile

### 5. Object Layer（Skill / Tool / Resource）

- `tool_definitions` + `profile_tools` junction + MCP integration
- `Skill` 由 `profile_configs` table 派生（file-based content + optional `SKILL.schema.yaml` sidecar）
- `Resource` 從 `agent_profiles.storage_refs` aggregate
- Single API call `/api/objects/registry` 攞晒三類

### 6. Agent Contracts（5 個 planning-time LLM hook）

| Contract | Status | 用途 |
|---|---|---|
| `plan` | ✅ Implemented | 分析 project + skills → 草擬 workflow package |
| `route` | ⚠️ Stub | Task → 建議 skill + agent_role |
| `judge` | ⚠️ Stub | Task + result → pass/fail + score |
| `repair` | ⚠️ Stub | 失敗 task → retry / switch skill / escalate 策略 |
| `audit` | ⚠️ Stub | 6-dim audit（correctness / completeness / format / risk / confidence / reproducibility）|

### 7. Chatbox + LLM Planner

- `/api/projects/{id}/chat` 收 user 文字 → LLM 草擬 plan 或聊天
- LLM **prose-only** 模式（v3.10.5）— 純講解 plan，Apply button server-side POST `/plan/from-llm`
- `feedback_to` 唔會 auto-inject（v3.10.10 起要 user 喺 visual editor 加）
- LLM SOUL auto-seed（v3.10.5）— Generate Tasks 個陣每個 role 自動派 SOUL preset
- Heuristic gate 防止亂 trigger「Create Plan」（v3.10.8）

### 8. SOUL Routing + Dispatch

- `project_soul_presets` 跟 step name → agent_role 配
- LLM-driven SOUL generation（v3.10.5）at Generate-Task time
- 9 個事件類型 emit 到 audit_log：`project.started`, `task.dispatched_via_soul`, `task.completed`, `loopback.fired`, `loopback.cap_reached`, ...

### 9. Audit / Observability

- `audit_log` 表（180+ event types，retention 90d）
- `token_usage` 逐 LLM call 計
- SSE event stream `/api/projects/{id}/events`（v1.8，throttled）
- `__task_progress_panel__` template 行 live progress bar

### 10. File + Artifact Management

- Per-project share folder（mount via `storage_refs`）
- File write through wrapper（HMAC signed）→ 註冊 `artifact`
- `POST /api/projects/{id}/files/{path:path}` PUT / DELETE
- `/api/artifacts/{id}/download` + SHA-256 integrity check

### 11. Dashboard + Auth

- FastAPI + Jinja2 + Tailwind + drawflow
- 18 個 HTML page：agents / projects / workflows / single-tasks / settings / admin / token-usage / history / ...
- Bootstrap user auth（v3.4）— `password_hash` NULL → 強制 setup
- HMAC agent auth 獨立（per-host key rotation）

### 12. HTTPS / TLS（v3.12.0, optional）

- **Default off**（HTTP for dev / LAN）
- Settings page 加 HTTPS section：toggle + cert/key path + 「Generate self-signed」+「Upload」button
- `hermes-orch gen-cert` CLI subcommand 整 365-day self-signed cert（SANs: hostname + localhost + 127.0.0.1），寫到 `~/.hermes-orchestrator/certs/server.{crt,key}`
- uvicorn SSL 自動 pick up（cert + key 兩個 file readable 嗰陣），否則 fallback HTTP + warning
- Session cookie 自動 set `Secure` flag 喺 HTTPS request 上面（HTTP 保持 backward-compat）
- HMAC agent auth over TLS 照 work（HMAC 喺 application layer）
- Wrapper 暫未 auto-pick HTTPS，需要 set `ORCHESTRATOR_URL=https://...`（self-signed 加 `INSECURE_SKIP_TLS_VERIFY=1`，follow-up）

---

## API Surface（164 endpoints，18 modules）

### `/api/agents` — Agent Fleet

| Method | Path | 用途 |
|---|---|---|
| `POST` | `/` | Register agent（HMAC key 配發）|
| `GET` | `/` | List agents（含 profile / liveness）|
| `GET` | `/{agent_id}` | 單個 agent |
| `PUT` | `/{agent_id}` | Update agent |
| `DELETE` | `/{agent_id}` | Delete agent |
| `POST` | `/{agent_id}/heartbeat` | Wrapper heartbeat（HMAC signed）|
| `POST` | `/{agent_id}/profiles` | Add profile（role / skill / capability）|
| `DELETE` | `/{agent_id}/profiles/{name}` | Remove profile |
| `PATCH` | `/{agent_id}/profiles/{name}` | Update profile |
| `GET` | `/{agent_id}/profiles/{name}/configs` | Per-profile configs |
| `POST` | `/{agent_id}/rotate-key` | 換 HMAC key |
| `POST` | `/{agent_id}/secret` | 存 per-agent secret |
| `POST` | `/{agent_id}/sessions/{id}/cleanup-ack` | 確認 session 清理 |

### `/api/artifacts` — Output Files

| Method | Path | 用途 |
|---|---|---|
| `POST` | `/` | Upload artifact |
| `GET` | `/` | List artifacts |
| `POST` | `/external` | 註冊外部 path 為 artifact（唔 copy）|
| `GET` | `/{id}` | Get metadata |
| `DELETE` | `/{id}` | Delete |
| `GET` | `/{id}/download` | Download binary |

### `/api/auth` — Dashboard Login

`POST /login`, `POST /logout`, `GET /me`, `POST /password`, `POST /setup` (bootstrap)

### `/api/contracts` — LLM Hook Surface

`GET /`, `GET /{name}`, `POST /{name}/draft`（plan 即用，4 個 stub）

### `/api/objects` — Object Layer Read API

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/registry` | 一次攞 skills + tools + resources |
| `GET` | `/skills[?profile_id=X&requires_capability=Y]` | List skills |
| `GET` | `/skills/{profile_id}/{name:path}` | 單個 skill（含 sidecar schema）|
| `GET` | `/tools` | List tools + per-profile availability |
| `GET` | `/tools/{id}` | Get tool |
| `GET` | `/tools/{id}/availability` | 邊啲 profile 註冊咗 |
| `POST` | `/tools/{id}/check-mcp` | 記錄 MCP status |
| `GET` | `/resources` | List storage_refs（cross-profile）|

### `/api/plans` — Plan Management

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/projects/{id}/plan` | Get plan_json + step_template |
| `PUT` | `/projects/{id}/plan` | Update plan（auto-archive 舊 task）|
| `DELETE` | `/projects/{id}/plan` | Delete plan |
| `POST` | `/projects/{id}/plan/from-llm` | 將 LLM output 落 plan_json |
| `POST` | `/projects/{id}/plan/run` | Run plan（state: planned → ready）|
| `GET` | `/projects/{id}/plan/visual` | Visual editor page |

### `/api/projects` — Project Core

| Method | Path | 用途 |
|---|---|---|
| `POST` | `/` | Create project |
| `GET` | `/` | List projects |
| `GET` | `/{id}` | Get project (含 plan_json, state, iter) |
| `DELETE` | `/{id}` | Soft delete |
| `POST` | `/{id}/archive` / `unarchive` | Archive toggle |
| `POST` | `/{id}/delete` / `undelete` | Trash toggle |
| `POST` | `/{id}/bulk-archive` / `bulk-delete` | Batch ops |
| `POST` | `/{id}/run` | Run project (planned → ready) |
| `POST` | `/{id}/replan` | Re-plan with new context |
| `POST` | `/{id}/session` / `GET` | Manage hermes session |
| `POST` | `/{id}/procedure/auto-generate` | Auto-write procedure.md |
| `POST` | `/{id}/apply-workflow` | 將 workflow package 套入 project |
| `GET` | `/{id}/open` | Open project share folder |
| `GET` / `PUT` / `DELETE` | `/{id}/files/{path:path}` | Per-project file CRUD |
| `GET` | `/{id}/file-preview/{path:path}` | Inline preview (markdown) |
| `GET` | `/{id}/events` | SSE event stream |
| `GET` / `PATCH` | `/{id}/memory/facts` | Project-scoped facts |
| `GET` / `POST` | `/{id}/memory/state` / `regenerate` | LLM-compressed state |
| `GET` | `/{id}/memory/trace` | L1/L2/L3 trace summary |
| `GET` | `/memory/recent` / `regenerate` | Cross-project recent summary |
| `GET` | `/{id}/chat` | List chat messages |
| `POST` | `/{id}/chat` | Send chat (LLM call) |
| `GET` | `/{id}/chat.jsonl` | Chat export |
| `POST` | `/{id}/chat/apply` | Apply suggestion (create plan / etc) |
| `POST` | `/{id}/chat/clear` | Reset chat history |
| `POST` | `/{id}/chat/reformat` | Re-run LLM on last message |
| `GET` / `POST` / `DELETE` | `/{id}/tasks/{task_id}/...` | Task control（status / output / tool-call / cancel）|

### `/api/schedules` — Cron + Templates

| Method | Path | 用途 |
|---|---|---|
| `GET` / `POST` | `/` | List / create schedule |
| `GET` / `PATCH` / `DELETE` | `/{id}` | Manage schedule |
| `GET` | `/{id}/next-fires` | Preview next 5 fire times |
| `POST` | `/{id}/run-now` | Manual fire |
| `POST` | `/project/{id}/mark-template` | 標 project 做 template |
| `POST` | `/project/{id}/promote-to-skill` | 升做 Skill 入 Object Layer |
| `GET` | `/templates/list` | List templates |

### `/api/settings` — Global Config

`/llm` (get/post/test), `/telegram` (get/post/test), `/cleanup` (preview/run), `/project` (storage)

### `/api/single-tasks` — 一次性 Task

`POST /`, `GET /`, `GET /{id}` — zero project context, virtual `__single_tasks__` project

### `/api/soul-templates` — Built-in SOUL Library

`GET /api/soul-templates`, `GET /api/soul-templates/{name}` — shipped 內建 SOUL preset 庫

### `/api/tasks` — Task Operations

`POST /`, `GET /`, `GET /{id}`, `PATCH /{id}`, `DELETE /{id}` — CRUD
`POST /{id}/start`, `/assign`, `/cancel`, `/interrupt`, `/retry` — lifecycle
`POST /{id}/result`, `/poll`, `/clone-and-cascade`, `/promote-to-workflow`

### `/api/users` — Admin

`GET /`, `POST /`, `DELETE /{username}`, `POST /{username}/password|disable|enable`

### `/api/workflows` — Reusable Workflow Package

`GET /api/workflows/`, `POST /api/workflows/from-project/{id}`, `GET /{id}`, `PATCH /{id}`, `DELETE /{id}`, `POST /{id}/run`, `POST /{id}/suggest-vars`

### `/api/optimize` — LLM-driven Task Optimization

`POST /` — batch refine task params

### Page Routes（HTML）

`/agents`, `/projects`, `/projects/{id}`, `/projects/{id}/visual`, `/workflows`, `/workflows/{id}`, `/workflows/{id}/visual`, `/single-tasks`, `/single-tasks/{id}`, `/tasks`, `/schedules`, `/settings`, `/history`, `/token-usage`, `/admin/soul-templates`, `/admin/users`, `/login`, `/setup-password`

---

## 5 層架構

| Layer | 內容 | 狀態 |
|---|---|---|
| **Object Layer** | Skill / Tool / Resource / Policy / AgentProfile | ✅ Shipped |
| **Workflow Layer** | DAG via depends_on、promote-to-workflow、apply-workflow | ✅ Shipped |
| **Execution Layer** | Python / Bash / API / Tool / App / Queue | ✅ Shipped |
| **Agent Layer** | 5 個 planning-time contract（plan / route / judge / repair / audit）| ✅ plan 即用，4 stub |
| **Audit & Observability** | audit_log、token_usage、SSE events、replay | ✅ audit + SSE，replay 未做 |

---

## 5 個 Object 類型

| Type | 來源 | Schema |
|---|---|---|
| **Skill** | `profile_configs` table，file-based content + optional `SKILL.schema.yaml` sidecar | `{input_schema, output_schema, deterministic, llm_required, requires_capabilities}` |
| **Tool** | `tool_definitions` + `profile_tools` junction | `{id, name, version, kind, capabilities, mcp_server_name}` |
| **Resource** | Promote 自 `agent_profiles.storage_refs` | `{kind, uri, auth_ref}` 5 種 kind (smb/local/gdrive/s3/url) |
| **Policy** | 暫存喺 Skill sidecar 嘅 `deterministic` / `llm_required` 欄位 | deferred（將來如有需要先抽 table）|
| **AgentProfile** | `agent_profiles` table | 不變 |

---

## Single Task（虛擬項目）

`tasks.is_single_task=1` 嘅 task 屬於 `__single_tasks__` virtual project。Zero project context，可以用嚟做：

- Code-gen flow（chatbox 叫 agent 寫 script → 新 Skill 入 registry）
- One-off summarize / extract / 一次性查詢
- Single tasks section UI（`/single-tasks` list + `/single-tasks/{id}` detail）

唔需要改 `tasks.project_id` 嘅 NOT NULL constraint（SQLite table rebuild 太大），用 virtual project + indexed flag 過。

---

## Tests

- **69 pytest tests** in `tests/test_*.py` — all passing
- Categories: chat / plan / task / supervisor / visual / auth / object layer / contracts / promote / wrapper config
- Playwright e2e (`tests/test_visual_*_e2e.py`) for visual editor + chatbox
- HMAC signed-call sites：`tests/test_signed_call_sites.py` ensures no leaky unsigned endpoints

---

## 最近 Update 進度（chronological, 2026-07-31 ~ 2026-08-03）

| Commit | Title |
|---|---|
| `cf24d2d` | **v3.11.1** docs: add approval-based task runner design + super profile soul template |
| `dd935c3` | docs: refresh README — full API catalog + feature list + recent v3.10.x/v3.11.x progress |
| (pending) | **v3.12.0** HTTPS / TLS: settings toggle + self-signed cert gen + cookie Secure flag |
| `d932c21` | **v3.11.1** test: cover v3.10.10 'no auto-dispatch on Generate tasks' + Run button flow |
| `87f16fe` | **v3.11.1** fix feedback_to wire color in dark mode (visual_workflow editor) |
| `2c6837f` | **v3.11.0** TASK_FAILED marker convention for agent-driven failure |
| `6c7f5cd` | **v3.10.10.1** fix 500 on `/plan/visual` — SELECT missing `max_iterations` |
| `66870e8` | **v3.10.10** Generate Tasks modal exposes loop-back cap (`max_iterations`) |
| `a7bd8ac` | **v3.10.9** Ctrl+C/V on canvas only copies card when selected |
| `c29b1a1` | **v3.10.8** tighten "Create plan" heuristic + skip undo in text fields |
| `1c4fd81` | **v3.10.7** supervisor skips archived tasks (pending/assigned) |
| `08c3872` | **v3.10.6** fix seed helper logic + run_project_plan overwrites generic |
| `1933351` | **v3.10.6** PUT `/plan` archives stale tasks on plan change |
| `0b6cf50` | **v3.10.5** chat prose-only refactor |
| `4fa7ebe` | **v3.10.5** LLM-driven SOUL generation at Generate-Task time |
| `896f743` | Planner `default_soul` rule + generic template fallback |
| `0e742b8` | `max_tokens 1500→4000` + auto-retry |
| `05a0277` | Chatbox 502 on LLM think-only response |
| `04bb776` | No auto-dispatch on Generate tasks + SOUL auto-seed |
| `650bd9e` | Wrapper daemon loop: apply configs BEFORE task pool |
| `4d51ea1` | Wrapper skip dir list: glob patterns |
| `67cddff` | Wrapper skip dir list (exact names) |
| `906ea2a` | Plan-task promote |
| `2d824ef` | Middleware: `/secret` + skills/mcp/llm regex |
| `8a2c4fd` | Dispatch timeout 10s → 30s |
| `e411d7b` | `Content-Type: application/json` in ack POSTs |
| `033f7fa` | Stuck-config reaper (60s) |
| `1e09f6c` | Routing same-name preference |
| `09b7308` | from-template `role_name` = profile.name |
| `5f1b8fa` | SOUL template card click hit-area |

詳細 commit message 喺每個 commit 入面。

---

## Roadmap

### ✅ Done

- Agent registration + multi-role profile + HMAC auth
- Task DAG + depends_on + cascade archive + loop-back (`feedback_to`)
- Loop-back cap (`max_iterations`) — v3.10.10
- TASK_FAILED marker convention — v3.11.0
- Visual plan + visual workflow editors (drawflow, dark mode)
- Workflow packages (promote / apply / run) with visual builder
- Single-task-as-virtual-project + UI
- Object Layer read API + sidecar parsing
- Agent contracts foundation（plan 即用 + 4 stub）
- Chatbox + LLM planner (prose-only mode, heuristic gate, chat JSONL export)
- SOUL auto-seed (LLM-driven at Generate-Task time)
- Dashboard user auth (v3.4)
- Audit log (180+ event types, retention 90d)
- SSE event stream (v1.8, throttled)
- File + artifact management (per-project share folder)
- 69 pytest tests passing

### 🚧 In progress / Next

- **v3.11.1 supervisor code fix** — add `AND archived = 0` to 4 sites
  (`_maybe_iterate` nonterm, `_maybe_iterate` review, `_maybe_complete_after_iter_cap` review, `_maybe_advance_project_state` nonterm)
  + `tests/test_supervisor_state_ignores_archived.py` (4 cases)
- **HTTPS wrapper support (v3.12.0 follow-up)** — `INSECURE_SKIP_TLS_VERIFY=1` + cert pin mode
- **Sprint B** Approval-based task runner — design 喺 `docs/approval-design.md`（2026-07-31）
- Single task section UI + code-gen flow（chatbox → 寫 script → 註冊做新 Skill）
- Promote-to-workflow refactor → 用 `plan` contract 取代 ad-hoc LLM call
- Visual project page：auto-layout + diff-based status update（Stage 4 follow-up）

### ⏸️ Deferred

- YAML DSL（而家用 JSON step_template）
- Multi-tenant / auth / per-user credential
- Replay / diff / compare run
- Subflows / minimap / sticky notes
- 4 個 stub contract 嘅 LLM tuning + UI 接入
- Action-template library（解決 LLM planner 寫唔到 instruction-heavy plans 嘅根本問題）

---

## 開發約定

- **Push cadence**：每個 commit 即 `git push origin main`，唔 batch（Perplexity 連住 GitHub repo 睇緊）
- **Test stack**：pytest 為主，Playwright 驗 visual page，all 69 tests pass
- **Schema migration**：純 SQLite，加新 table 用 `CREATE TABLE IF NOT EXISTS`，加新 column 用 idempotent `ALTER TABLE`（catch "duplicate column" 食掉）
- **Pydantic v2**：用 `model_validate` / `model_dump`，唔用 v1 `parse_obj`
- **Deterministic 優先**：task 用 script 而唔係 LLM，cost + reproducibility 都好
- **Dark mode** rule：catch-all `!important` rule 之後必須加 sub-class override（loop-back / status color / data signal element）

---

## Quick start

```bash
# 1. Install
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"

# 2. Configure (set LLM api_key for non-mock mode)
cp config.example.yaml ~/.hermes-orchestrator/config.yaml
# edit config.yaml — set llm.api_key to your LLM provider key

# 3. Run
.venv\Scripts\hermes-orch.exe serve --reload
# → http://127.0.0.1:8765

# 4. Register an agent
curl -X POST http://127.0.0.1:8765/api/agents \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "my-host-01", "ip": "192.168.1.10", "os_type": "linux"}'

# 5. Open http://127.0.0.1:8765/projects and create a project
```

詳細 dev workflow 睇 `REVIEW.md`（design review 文件，2026-07-15 開始）。

## See also

- `docs/visual-workflow-builder.md` — Visual workflow editor design + drawflow integration
- `docs/chatbox-plan-editor.md` — Chatbox + LLM planner + heuristic gate
- `docs/soul-routing-design.md` — SOUL auto-seed + dispatch flow
- `docs/task-progress-monitor.md` — Live progress bar + SSE throttling
- `docs/sse-events-v1.8.md` — Event bus + event types reference
- `docs/loop-detection-v1.7.md` — Loop-back mechanics
- `docs/hmac-agent-auth.md` — Per-host HMAC + key rotation
- `docs/install-spec.md` — Install + watchdog setup
- `docs/approval-design.md` — Sprint B approval step (design)
- `docs/design/3-tier-memory.md` — L1 / L2 / L3 trace model
- `docs/soul-templates/super.md` — Sample SOUL preset
