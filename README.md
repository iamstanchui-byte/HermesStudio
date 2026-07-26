# HermesStudio

> **Hybrid Agentic Workflow Runtime** — 以 Workflow 為骨架、以 Object 為能力單元、以 LLM 為決策插件嘅 AI 編排平台。
>
> 唔係 chat-first，亦唔係 n8n-style integration-first，係 **AI-native orchestration substrate**。

---

## 定位（一句話）

LLM 純做 **design-time assistant**（草擬 plan / 建議 route / 評審 audit 等等），runtime 完全 deterministic（depends_on graph + status machine + retry），LLM 永遠唔喺 hot path 落決定。

關鍵唔係「似唔似 n8n」，係 **deterministic 同 agentic 能力要清楚分層**。

---

## 5 層架構

| Layer | 內容 | 狀態 |
|---|---|---|
| **Object Layer** | Skill / Tool / Resource / Policy / AgentProfile | ✅ Commit 1 shipped |
| **Workflow Layer** | DAG via depends_on、promote-to-workflow、apply-workflow | ✅ 之前已有 |
| **Execution Layer** | Python / Bash / API / Tool / App / Queue | ✅ 之前已有 |
| **Agent Layer** | 5 個 planning-time contract（plan / route / judge / repair / audit） | ✅ Commit 2 shipped（plan 即用，4 個 stub）|
| **Audit & Observability** | audit_log、token_usage、execution trace、replay | ⚠️ 部分（audit_log + token_usage 已有，replay/diff 未做）|

---

## 5 個 Object 類型

| Type | 來源 | Schema |
|---|---|---|
| **Skill** | `profile_configs` table，file-based content + 可選 `SKILL.schema.yaml` sidecar | `{input_schema, output_schema, deterministic, llm_required, requires_capabilities}` |
| **Tool** | 新 `tool_definitions` + `profile_tools` junction | `{id, name, version, kind, capabilities, mcp_server_name}` |
| **Resource** | Promote 自 `agent_profiles.storage_refs`（無 schema 改動）| `{kind, uri, auth_ref}` 5 種 kind (smb/local/gdrive/s3/url) |
| **Policy** | 暫存喺 Skill sidecar 嘅 `deterministic` / `llm_required` 欄位 | deferred（將來如有需要先抽 table）|
| **AgentProfile** | `agent_profiles` table（已有）| 不變 |

---

## 5 個 Agent Contract（全部 planning-time）

| Contract | Status | 用途 |
|---|---|---|
| `plan` | ✅ Implemented | 分析 project + skills → 草擬 workflow package |
| `route` | ⚠️ Stub | Task → 建議 skill + agent_role |
| `judge` | ⚠️ Stub | Task + result → pass/fail + score |
| `repair` | ⚠️ Stub | 失敗 task → retry / switch skill / escalate 策略 |
| `audit` | ⚠️ Stub | 6-dim audit（correctness / completeness / format / risk / confidence / reproducibility）|

API：`GET /api/contracts`、`GET /api/contracts/{name}`、`POST /api/contracts/{name}/draft`。

---

## Object Layer API

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/api/objects/skills` | 列出全部 skills（可選 `?profile_id=X` / `?deterministic_only=true` / `?requires_capability=Y`）|
| `GET` | `/api/objects/skills/{profile_id}/{name:path}` | 單個 skill（含 sidecar schema）|
| `GET` | `/api/objects/tools` | 全部 tool definitions + per-profile availability |
| `GET` | `/api/objects/tools/{id}` | 單個 tool |
| `GET` | `/api/objects/tools/{id}/availability` | 邊啲 profile 註冊咗呢個 tool |
| `POST` | `/api/objects/tools/{id}/check-mcp` | 記錄 MCP status（orch 唔主動 probe）|
| `GET` | `/api/objects/resources` | 全部 storage_refs（cross-profile aggregate）|
| `GET` | `/api/objects/registry` | 一個 call 攞晒三類 |

---

## Single Task（虛擬項目）

`tasks.is_single_task=1` 嘅 task 屬於 `__single_tasks__` virtual project。Zero project context，可以用嚟做：

- Code-gen flow（chatbox 叫 agent 寫 script → 新 Skill 入 registry）
- One-off summarize / extract / 一次性查詢
- 之後做 Single tasks section UI（Commit 3 scope）

唔需要改 `tasks.project_id` 嘅 NOT NULL constraint（SQLite table rebuild 太大），用 virtual project + indexed flag 過。

---

## 最近 Update 進度（chronological）

| Commit | Title |
|---|---|
| `063d585` | **Agent contracts**：5 個 planning-time LLM hook（plan 即用 + 4 個 stub）|
| `8b5154a` | **Object Layer foundation**：tool_definitions + profile_tools + virtual __single_tasks__ + Skill sidecar parser + 5 Object Layer endpoints |
| `08823ce` | **Visual view bug fix**：stop showing archived tasks + drawflow re-render-safe init |
| `9b65617` | Visual workflow：persist card positions + Reset layout button |
| `ace5317` | docs：clean up stale "Stage 2b will add" copy |
| `72bf999` | Visual view：background poller 取代 30s reload（no auto-reload）|
| `0d3d965` | docs：apply workflow docstring → additive semantics |
| `e5307f0` | Apply workflow：additive import（保留現有 tasks）|
| `1d42a96` | Apply workflow：pause dispatch + confirm dialog |
| `a5b4735` | Apply workflow feature：push workflow package → project task list |

詳細 commit message 喺每個 commit 入面。

---

## Roadmap

### ✅ Done

- Agent registration + multi-role profile (Model A)
- Task DAG + depends_on + cascade
- Visual builder (Phase 1-2.5)
- Workflow packages (promote / apply / run)
- Single-task-as-virtual-project 嘅 schema foundation
- Object Layer read API + sidecar parsing
- Agent contracts foundation（plan 即用）

### 🚧 In progress / Next

- **Commit 3**: Single task section UI + code-gen flow（chatbox → 寫 script → 註冊做新 Skill）
- Promote-to-workflow refactor → 用 `plan` contract 取代 ad-hoc LLM call
- Visual project page：auto-layout + diff-based status update（Stage 4 follow-up）

### ⏸️ Deferred

- YAML DSL（而家用 JSON step_template）
- Multi-tenant / auth / per-user credential
- Replay / diff / compare run
- Subflows / minimap / sticky notes
- 4 個 stub contract 嘅 LLM tuning + UI 接入

---

## 開發約定

- **Push cadence**：每個 commit 即 `git push origin main`，唔 batch（Perplexity 連住 GitHub repo 睇緊）
- **Test stack**：pytest 為主，Playwright 驗 visual page，all 48 tests pass
- **Schema migration**：純 SQLite，加新 table 用 `CREATE TABLE IF NOT EXISTS`，加新 column 用 idempotent `ALTER TABLE`（catch "duplicate column" 食掉）
- **Pydantic v2**：用 `model_validate` / `model_dump`，唔用 v1 `parse_obj`
- **Deterministic 優先**：task 用 script 而唔係 LLM，cost + reproducibility 都好

---

## Quick start

```bash
# 1. Install
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"

# 2. Configure (set LLM api_key for non-mock mode)
cp config.example.yaml ~/.hermes-orchestrator/config.yaml
# edit config.yaml

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
