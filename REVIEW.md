# Hermes Orchestrator 設計 Review

> 對應原始設計文件：`../local-network-orchestrator-dashboard.md` (位於 Desktop)
> 狀態：初稿 review，列出我看到的關鍵缺口跟需要決定的點
> 用途：迭代討論的工作文件，決策落地後會把結論搬回主設計文件

---

## Design principles（2026-07-15 加）

呢個系統嘅 user 目標係 **productize** 呢個 orchestrator（最終打包賣出去），所以全局 design 原則：

1. **簡單為主** — "太多設定就不好了"。能 default 嘅就 default
2. **跨平台一致** — Windows 同 Linux 唔該有兩套 setup 流程
3. **單一 entry point** — User 對住 Orchestrator，唔直接搞 SMB / NFS / shared folder
4. **少 OS-specific config** — 透過 Orchestrator API 統一 access，唔好要 user 自己 set 嘢
5. **Hermes 假設存在** — 預期 user 已經裝咗 Nous Research hermes-agent，唔包安裝（安裝係 Hermes 自己嘅事）

呢 5 條影響晒 §1-§7 嘅 design 細節。

---

## 0. TL;DR — 哪些最需要先定下來

依照「不定下來什麼都沒法 build」的標準排序，下面這 6 項最關鍵：

1. **Task 生命週期** — retry / timeout / crash recovery / partial success
2. **Hermes 整合介面** — wrapper 怎麼 call 本機 Hermes (auth / API contract)
3. **Supervisor / Loop Engineering trigger** — 主管 agent 怎麼知道「有 task 完了」
4. **Task DAG 與依賴** — supervisor 怎麼拆任務、跨 agent 依賴、failure propagation
5. **Artifact 處理** — wrapper 產出的檔案怎麼送到 dashboard
6. **Auth scheme** — wrapper 跟 orchestrator 之間的認證 + rotate

其他（observability、多 user、DB migration、deploy 自動化、UI 細節）先不做，列在文末「Phase 2 之後」。

---

## 1. Task 生命週期 (agreed 2026-07-15)

### 1.0 Agreed decisions

| 議題 | 決定 |
|---|---|
| Dispatch retry | 2 retries × 1 min timeout → 3 min 總上限 |
| Recovery | 手動；dashboard 顯示 "client not responded"，operator 自己 restart |
| Task execution timeout | soft 30 min；orchestrator 定期 poll，liveness OK 就 reset |
| Failed dispatch | 停喺嗰個 agent，**不自動 failover** |
| Agent busy | 入 queue（FIFO，wrapper 端管理） |
| Interrupt（打尖） | Dashboard "interrupt now" 按鈕；**停舊 + 跑新**，舊 task 唔 requeue |
| Per-agent role | 保留 capability-based dispatch |

**Out of scope（呢 section 唔包）**：
- Task-level application retry（只有 dispatch retry）
- Partial success（binary success / fail 就夠）

### 1.1 Status state machine

```
[queued] → [assigned] → [running] → [completed]            (success)
                              → [failed]                 (application error)
                              → [failed_timeout]         (soft 30 min, no liveness)
                              → [failed_dispatch]        (3 dispatch attempts all timed out)
                              → [cancelled]              (operator manual cancel)
                              → [interrupted]            (operator pressed "interrupt now")

[assigned] → [failed_dispatch]  (initial dispatch never reached wrapper)
```

### 1.2 Dispatch protocol（Orchestrator → Wrapper POST /tasks）

| Attempt | Timeout | Timeout 後 |
|---|---|---|
| 1st | 1 min | → Retry 1 |
| Retry 1 | 1 min | → Retry 2 |
| Retry 2 | 1 min | → task `failed_dispatch` + client `not_responded` |

Total worst-case: **3 min**。

### 1.3 Failed dispatch 行為

- **Task status** → `failed_dispatch`
- **Agent state** → `client_not_responded`
- **Dashboard** → "stopped at agent {X} @ {event_time}"
- **唔自動 failover** 去其他 agent
- Operator 決定下一步（restart client 再 re-dispatch / 改派其他 agent / fail project）

### 1.4 Recovery（手動，agent 端 restart）

- Dashboard 顯示 "client not responded" warning
- Operator 去 **agent OS**（Windows B / Linux A 機器本身）人手 restart 嗰個「orchestrator client」process（由我哋寫嘅 agent-side daemon）
- Restart 完成後 agent 自動 re-register / available
- `failed_dispatch` 嘅 task **唔自動 retry** — operator 要自己 re-dispatch

> **架構備註**：呢個「orchestrator client」係行喺 **agent 端** 嘅 long-running process，負責同 orchestrator 通訊。原本 design doc 寫 wrapper 係 HTTP server（orchestrator 主動 push），呢度帶出嘅 model 比較似「agent daemon」— 具體係 push server、pull client、long-poll、還是 WebSocket 留返 §2 / §6 一齊定。Recovery 行為一樣：agent OS 開 terminal/restart 個 process。

### 1.5 Task execution timeout（soft 30 min）

- Default 30 min（自上次成功 liveness check 計起）
- Liveness 機制：orchestrator 每 30s poll `GET /tasks/{id}`
  - 回 `running` → reset 計時，dashboard 顯示 "still running"
  - 回 `completed` / `failed` → task 結束
  - poll 失敗（client hang）→ 走 §1.2 dispatch protocol
  - 連續 30 min 冇成功 poll → mark `failed_timeout`

### 1.6 Concurrency (revised 2026-07-15)

原本 design 講 wrapper-side blocking FIFO queue。**經 Q5 clarification 後取消 hard queue**：

- **Parallel OK** — 每個 `hermes -p profile` invocation 係獨立 subagent（見 §2），多個可以同時跑，唔撞 state
- **冇 hard queue** — wrapper 唔擋住新 task dispatch
- **Dashboard advisory** — 顯示「agent running N tasks」畀 operator 知 workload
- **Soft limit**（optional，後加） — `max_concurrent_per_agent` config 避免爆 RAM
- 從 design doc 原本嘅 `GET /queue` endpoint **移除**

### 1.7 Interrupt（打尖）

- **Trigger**：dashboard 嘅 "interrupt now" 按鈕（喺 running task 上面）
- **Action**：orchestrator 向 wrapper 發 `POST /tasks/{id}/interrupt`
- Wrapper force-kill 當前 task
- **舊 task status** → `interrupted`（新 status，區分 `cancelled`）
- **舊 task 唔 requeue**；operator 要再做就自己再派
- Interrupt 完成後 agent 變 idle
  - **Manual** — operator 自己 dispatch 下一個 task，唔會 auto-pop queue

### 1.8 Edge cases

| 情境 | 行為 |
|---|---|
| `POST /tasks` 200 OK 但 body `{"status":"rejected"}` | **Task-level failure**（唔當 dispatch 失敗），正常 retry / cancel flow，唔觸發 hang |
| 200 OK + task_id，wrapper 之後 crash | polls 失敗 → soft timeout → `failed_timeout` |
| Wrapper 收到 task 但 config 錯即刻 fail | 200 OK + 即刻 reply `failed` → task-level 失敗，唔當 hang |

### 1.9 Resolved（原 Open questions 已解決）

- ~~Q1 Recovery restart 邊度？~~ → Agent 端 operator restart「orchestrator client」process
- ~~Q2 Interrupt 之後 auto-dispatch？~~ → Manual，agent 變 idle，operator 自己 dispatch

### 1.10 Outstanding for later sections

- 「Orchestrator client」嘅實際 protocol（push / pull / long-poll / WebSocket）— 等 §2 Hermes 整合 + §6 Auth 一齊定
- Agent-side daemon 嘅 process management（systemd / Windows Service / NSSM 之類）— deployment section 再傾

---

## 2. Hermes 整合介面 (agreed 2026-07-15)

### 2.0 重大 finding — 設計文件假設錯咗

設計文件假設「Hermes API server 127.0.0.1:8642」係 local HTTP server — **呢個假設錯咗**。

**實際情況**：
- 用緊嘅 Hermes 係 [Nous Research 嘅 hermes-agent](https://github.com/NousResearch/hermes-agent) **v0.17.0** (open-source, MIT)
- 架構：CLI + messaging gateway (Telegram/Discord/Slack, ~20 個) + TUI + Electron desktop，**全部 share 同一個 agent core**
- 擴展主要靠 **plugins + skills**，唔好加 core tools
- **冇 built-in OpenAI-compatible API server 跑緊** — `.plans/openai-api-server.md` 寫得好詳細但**係 plan doc，未實作**
- 已 listening port：
  - `27770` 係 messaging gateway 內部 IPC，**唔係 HTTP**（curl 連唔到）
  - `1239` 估係 internal service
  - `8642`（OpenAI API server 預設 port）**冇 listening**
- Hermes 嘅 **per-conversation prompt caching is sacred**（AGENTS.md 明文寫），對我哋 session 管理有約束

### 2.1 MVP integration：CLI subprocess

Wrapper 用 `subprocess.run(...)` 叫 Hermes 官方 CLI（**唔使等 OpenAI API server 開發**）：

```python
import subprocess

def run_hermes_task(task: dict) -> dict:
    """對齊 user 現有 build_prompt + handoff_file pattern。"""
    prompt = build_prompt(task)
    profile = task["profile"]
    session_id = task.get("session_id")
    
    cmd = ["hermes", "-p", profile]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += ["chat", "-q", prompt]
    
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=task.get("timeout_seconds", 1800),  # 30 min, 對齊 §1.5
    )
    
    if result.returncode != 0:
        return {"status": "failed", "error": result.stderr, "code": result.returncode}
    
    return parse_hermes_output(result.stdout, result.stderr)
```

### 2.2 Session 管理

- 每個 task 一個 Hermes session
- **第一次 call 冇 `--resume`** — Hermes 開新 session
- **Hermes stdout 會包 session_id** — wrapper 解析後**存落 DB**（task_id → session_id）
- 同一 task 嘅後續 call 帶 `--resume <session_id>`（例：supervisor 嘅 loop engineering 多輪對話）
- ⚠️ **Hermes 嘅 prompt cache 要求**：一個 session 內**唔好 swap tools / 改 system prompt**，否則 cache 失效、用 token 急升

### 2.3 Auth

- **MVP 唔加** — localhost trust，subprocess 行同一個 user 嘅 context
- 唔需要 bearer token / HMAC（同 §6 講嘅 wrapper↔orchestrator auth 唔同層次）

### 2.4 Phase 2 enhancement（等 Hermes upstream 實作）

- 等 [Nous Research 實作 `.plans/openai-api-server.md`](file:///C:/Users/stanley/AppData/Local/hermes/hermes-agent/.plans/openai-api-server.md) 嘅 Phase 1 MVP（port 8642，Bearer auth，non-streaming）
- Wrapper 升級用 HTTP 叫 `/v1/chat/completions`，獲得：
  - **SSE streaming**（Hermes 出 token 即時 push）
  - **X-Session-ID header** 標準化 session 管理
  - **interrupt 支援更乾淨**（同 session ID 嘅 in-flight request 觸發 cancel）
- 升級可以**保留 subprocess 模式做 fallback**

### 2.5 Open questions（要再釐清）

- **Q3**：`hermes` CLI 嘅 absolute path 同啟動佢嘅 env 點 set（會唔會喺 wrapper 環境 load 唔到 MiniMax credentials？）
- **Q4**：`parse_hermes_output` 點 parse stdout？JSON / plain text / Markdown with frontmatter / 其他？可以貼個 sample
- **Q5**：多個 `hermes` subprocess 同時跑會唔會撞 session state？要唔要 serialize（每個 agent 同時一個 hermes process）？
- **Q6**：Failure detection — returncode 已經夠？定要睇 stdout 有冇特定 marker？
- **Q7**：Deterministic task（pywinauto / PowerShell / MT5）你現有 dispatch 方式？同 `hermes` wrapper 共存框架點樣？

### 2.6 Resolved (2026-07-15)

| Q | Resolution |
|---|---|
| Q3 | **PATH-based default**；setup / register agent step 跑 OS-specific check（Windows 用 `where.exe hermes` + `hermes --version`，Linux 用 `which hermes` + `hermes --version`）。Agent registration form 分 Windows / Linux version，視乎 agent 個 OS 揀對應 setup script |
| Q4 | **Plain text + footer pattern**。Sample output 解構後，parser 用 regex 抽 `Session:\s+(\S+)` 攞 session_id，回應 body 從「Resume this session with:」之前嘅內容 extract。詳見 §2.7 |
| Q5 | **Parallel OK** — 每個 `hermes -p profile` 係獨立 subagent，唔撞 state。§1.6 queue 降級做 advisory |
| Q6 | **4 個 failure type** 全部歸類入 `failed` status，error info 落 `task.error` 欄位。唔分細 |
| Q7 | **Agentic-only** — wrapper 唔支援 deterministic mode（唔識 pywinauto / PowerShell / MT5 直接執行）。所有 task 都行 hermes subprocess。要 deterministic task？自己另外開 script 跑，唔入 orchestrator 系統 |

### 2.7 Concrete parser design（基於實際 sample）

Hermes actual stdout format：
```
Query: <query>
Initializing agent...
══════...banner...══════
║ 🜲 Hermes ═══...
║    <response>
╚═══...banner...═══

Resume this session with:
  hermes --resume <session_id> -p <profile>

Session:        <session_id>
Duration:       <seconds>s
Messages:       <count> (<user> user, <tool> tool calls)
```

Parser pseudocode：
```python
import re

def parse_hermes_output(stdout: str, stderr: str, returncode: int) -> dict:
    if returncode != 0:
        return {"status": "failed", "error": stderr.strip() or f"exit {returncode}"}
    
    # Extract session_id from "Session:        <id>" line
    m = re.search(r"Session:\s+(\S+)", stdout)
    session_id = m.group(1) if m else None
    
    # Extract response: text between "Resume this session with:" 
    # and "Session:" — strip banner characters
    body = stdout.split("Resume this session with:", 1)[0]
    body = re.sub(r"^.*?══+", "", body, flags=re.DOTALL)  # strip header
    body = re.sub(r"╚═+\s*$", "", body).strip()           # strip banner end
    
    return {
        "status": "completed",
        "session_id": session_id,
        "response": body,
    }
```

### 2.8 §2 Resolved summary

✅ **agreed**：CLI subprocess path、session 管理、agentic-only、parallel OK、4-type failure detection
🟡 **OS-specific pending**：register agent step 嘅 Windows vs Linux setup script（§2.5 Q3，唔阻 wrapper code）
⏸️ **Phase 2**：等 Hermes upstream 實作 OpenAI API server 後再升級

---

## 3. Supervisor / Loop Engineering (agreed 2026-07-15)

### 3.0 Major reframe (after user clarification)

原本 design doc 冇分清楚 Orchestrator 同 Supervisor — **呢個係 critical reframe**：

| 角色 | 職責 | 性質 |
|---|---|---|
| **Orchestrator** | Dispatch、status dashboard、command UI、session 管理 | **純 infra，冇 intelligence** |
| **Supervisor (per project)** | 收 user 指令、拆 tasks、管 subagents、loop engineering、決定下一步 | **Per-project 嘅「腦」** |
| **Subagent** | 真正做嘢（hermes subprocess） | Stateless per task |

User 嗰句「subagent 都是回覆supervisor agent 的, 除非是我直接跟個別subagent 溝通, 他們才直接回覆我」清楚劃出兩個 communication mode：
- **Supervised mode (default)**: User → Orchestrator → Supervisor → Orchestrator → Subagent
- **Direct mode**: User → Orchestrator → Subagent（user 揀咗要 direct chat）

### 3.1 Agreed decisions

| 議題 | 決定 |
|---|---|
| Loop iteration 頻率 | **每個 task 完成都 review**（supervisor 每次諗下一步） |
| Supervisor state 持久化 | **Yes**，per-project |
| Supervisor 點知 task 完了 | **Pull** — supervisor 每 5-10s 撈 status |
| Multi-project | **一次只行一個 project**，停晒先可以轉 |
| Supervisor state 雙重保險 | DB（task history）+ project folder（plan/decisions/status） |
| Session recovery | Session 太長 / 不穩定 → 開新 session，新 session 讀 project folder 接手 |

### 3.2 Per-project folder（喺 Windows A / orchestrator 機）

每個 project 一個 folder，agents 同 supervisor 共用：

```
./projects/<project_id>/
├── status.md              # 當前 project 狀態（supervisor / agents 讀）
├── plan.md                # Task plan / DAG
├── decisions.md           # Supervisor 嘅 decision log
├── session_id             # 當前 Hermes session_id（純文字）
├── agents/<agent_id>/
│   └── notes.md           # 該 agent 寫低嘅 notes
└── artifacts/             # 輸出
```

### 3.3 Session lifecycle（Orchestrator 負責）

- **Start**：新 project → 開新 Hermes session，session_id 寫落 `session_id` 檔
- **Resume**：supervisor turn 帶 `--resume <session_id>`，讀 `session_id` 檔取 ID
- **Restart（不穩定時）**：
  1. Orchestrator 偵測到 session 太長 / 太多 error / 推理 timeout
  2. Kill 當前 session
  3. 開新 session
  4. 新 session 嘅 first turn prompt 包含「read project folder 了解 status」嘅指示
  5. 將新 session_id 寫返 `session_id` 檔
- 每次 restart 都要喺 `decisions.md` 記錄（audit trail）

### 3.4 Communication modes

- **Supervised（default）**：
  ```
  User command → Orchestrator → Supervisor (hermes session)
  Supervisor 決定 dispatch → Orchestrator (POST /tasks) → Subagent
  Subagent result → Orchestrator → Supervisor (下一 turn 嘅 tool result)
  Supervisor 決定完成 → Orchestrator 通知 User
  ```
- **Direct**：
  ```
  User command → Orchestrator (揀咗邊個 subagent) → Subagent (hermes subprocess)
  Subagent result → Orchestrator → User
  ```
  Direct mode 唔經 Supervisor，純 ad-hoc 操作 / debug

### 3.5 Resolved (2026-07-15)

| Q | Resolution |
|---|---|
| Q11 | **(b) Orchestrator file API** — 一致、簡單、product-friendly。Agents 透過 `GET/PUT /projects/{id}/files/<path>` access folder 內容，唔靠 SMB / NFS / shared folder。對齊 design principle §0 嘅「少 OS-specific config」 |
| Q12 | **(c) Markdown + frontmatter** — YAML header 存 metadata（state / task list / last_updated），body 係 freeform notes。Machine parseable + human readable |
| Q13 | **(c) Supervisor 顯式 signal** — supervisor 寫 `decision: "start_fresh"` 去 status.md 嘅 frontmatter，Orchestrator 撈到就 trigger restart。唔靠 turn count，supervisor 自己最清楚幾時該 fresh start |
| Q14 | **(a) Dashboard 點 subagent card → 開 chat panel** |

### 3.6 Refined: 全部 access 經 Orchestrator API

由於 Q11 = file API，**agents 同 supervisor 唔直接 access 磁碟**，全部透過 HTTP：

- Supervisor 讀 status: `GET /projects/{id}/files/status.md`
- Supervisor 寫 status: `PUT /projects/{id}/files/status.md`（body 係成個 file 內容）
- Subagent 寫 notes: `PUT /projects/{id}/files/agents/<self_id>/notes.md`
- Orchestrator 內部維護 DB：
  - `session_id` table 取代 `session_id` file（DB 一致，唔好兩處存）
  - `projects` table
  - `tasks` / `agents` / `artifacts` etc.

### 3.7 §3 Resolved summary

✅ **§3 鎖死**：Orchestrator/Supervisor 分工、loop freq、persistence、pull model、single project、dual storage、file API、Markdown+frontmatter、supervisor-driven session restart、dashboard direct mode
📦 **productize ready**：少 OS-specific config、單一 entry point、setup 簡單

---

## 4. Task DAG 與依賴 (agreed 2026-07-15)

### 4.0 Agreed decisions

| 議題 | 決定 |
|---|---|
| 拆任務 | **(a) Supervisor 自動拆** — user 講 goal，supervisor 諗 plan |
| DAG 邊度存 | **`plan.md` 嘅 YAML frontmatter**（canonical） + **DB denormalized view**（query 用） |
| 依賴表達 | `task.depends_on: ["t-001", "t-002"]` — list of task IDs |
| Failure propagation | `on_parent_failure: "skip"`（default） — 可 per-task override |
| Dynamic 改 plan | Orchestrator 每 5s 撈 plan.md 嘅 frontmatter diff，新 task 自動 register |

### 4.1 Plan structure — `plan.md` 範例

```markdown
---
project_id: proj-001
state: running
created_at: 2026-07-15T10:00:00+08:00
tasks:
  - id: t-001
    name: "研究 EURUSD 過去 30 日 pattern"
    agent_role: data-analyst
    status: running
    depends_on: []
  - id: t-002
    name: "做 backtest"
    agent_role: backtest-runner
    status: queued
    depends_on: [t-001]
  - id: t-003
    name: "出 report"
    agent_role: report-writer
    status: pending
    depends_on: [t-002]
---

# Project Plan

研究 EURUSD 過去 30 日 pattern，先 backtest，再出 report。
```

- **Canonical**：plan.md（via §3.6 file API）
- **Denormalized view**：Orchestrator DB 有 `tasks` table，**每次 poll plan.md 都 parse frontmatter 同 DB 對齊**（新增 / 更新 status / 新依賴）
- Dashboard 直接 query DB，唔 parse 檔案

### 4.2 Task envelope

任務 dispatch 個 envelope：
```json
{
  "task_id": "t-001",
  "project_id": "proj-001",
  "name": "研究 EURUSD 過去 30 日 pattern",
  "agent_role": "data-analyst",
  "assigned_to": "linux-a-01",
  "depends_on": [],
  "on_parent_failure": "skip",
  "action": "...",
  "params": {...},
  "priority": "normal"
}
```

### 4.3 Failure propagation semantics

| `on_parent_failure` | 行為 |
|---|---|
| `skip`（default）| Parent 失敗 → 此 task 直接 mark `skipped`，唔 dispatch |
| `wait` | Parent 失敗 → 此 task 留喺 `queued`，等 operator 決定 |
| `fail` | Parent 失敗 → 此 task 直接 mark `failed`，連鎖 mark 啲 dependent 都 `skipped` |

- Multiple parents 失敗時，**任何一個 fail 就 trigger**（OR 邏輯）
- 失敗後 supervisor 喺下次 turn review，決定：retry parent / reassign / 改 plan 跳過呢個 branch

### 4.4 Dynamic plan update 流程

```
Supervisor 喺某個 turn 發現要加 task
   → 寫落 plan.md frontmatter（append new task entry）
   → Orchestrator 5s poll 一次，發現 frontmatter 多咗 entry
   → Insert 落 DB tasks table（status: pending，depends_on 解析）
   → Supervisor 之後 dispatch 嗰個 task
```

新 task 嘅 `depends_on` 自動解析，照 §4.3 規則處理。

### 4.5 Resolved summary

✅ **§4 鎖死**：supervisor 自動拆 / plan.md + DB 雙存 / depends_on 表達 / on_parent_failure 三選一 / 5s poll dynamic update
📦 **productize ready**：DAG 全部由 supervisor 管，user 唔識都得

---

## 5. Artifact 處理 (agreed 2026-07-15)

### 5.0 Agreed decisions

| 議題 | 決定 |
|---|---|
| Upload 機制 | `POST /artifacts`（multipart）— 同 §3.6 file API 分開 |
| Storage 位置 | Central: `./artifacts/<task_id>/<id>.<ext>`（喺 Windows A） |
| Size limit | **50MB configurable** (`MAX_ARTIFACT_SIZE` env) |
| 大過 50MB | **`storage_kind = 'external'`** — 只記 path + agent_id，file 留喺 agent OS |
| Cross-agent share | Orchestrator proxies（中央入口，唔使 SMB / NFS） |
| User 下載 external | 兩個方法：(a) UI 顯示 scp command 一鍵 copy (b) Dashboard download button 經 Orchestrator proxy |
| 多選下載 | Dashboard 排隊一個一個下載（避免 agent / network 過載） |
| Retention | **永久保留**（no auto-prune MVP） |

### 5.1 Storage policy

| Size | `storage_kind` | 處理 |
|---|---|---|
| ≤ 50MB | `central` | Multipart upload → Orchestrator 本地磁碟 |
| > 50MB | `external` | Wrapper 報：name + path + size + agent_id，**唔 upload file** |

### 5.2 `artifacts` table schema

```sql
CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  content_type TEXT,
  size_bytes INTEGER,
  checksum TEXT,
  storage_kind TEXT NOT NULL,         -- 'central' | 'external'
  storage_path TEXT NOT NULL,         -- if central: ./artifacts/<task_id>/<id>.<ext>
                                      -- if external: 留喺 agent OS, e.g. /data/models/run-001/model.pkl
  agent_id TEXT,                      -- for external: 邊部 agent 有呢個 file
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (task_id) REFERENCES tasks(id),
  FOREIGN KEY (project_id) REFERENCES projects(id)
);
```

### 5.3 Upload flow

```
Wrapper 完成 task，發現 output files
   │
   ├── file size ≤ 50MB
   │     → POST /artifacts (multipart)
   │     → Orchestrator 存 ./artifacts/<task_id>/<id>.<ext>
   │     → DB 記: {storage_kind: 'central', storage_path: ...}
   │
   └── file size > 50MB
         → POST /artifacts (JSON body, no file)
            body: {name, path, size, content_type, agent_id, storage_kind: 'external'}
         → Orchestrator 唔 upload file，只 DB 記 reference
```

### 5.4 Dashboard UX

**Central artifacts**：
- 直接 download link
- Image / PDF 顯示 inline preview（thumbnail / 第一頁）
- 其他 type 顯示 file icon + size + download

**External artifacts**：
- Default：顯示完整 `scp` command，**一鍵 copy** 掣
  ```
  scp stanley@linux-a-01:/data/models/run-001/model.pkl ./
  ```
- 可選：**[Download via Orchestrator]** button
  - `GET /artifacts/{id}/download`
  - Orchestrator 透過 agent wrapper proxy stream 返
  - 用於唔想 command line 嘅 user

### 5.5 Multi-select queue download

User 喺 dashboard 一次選 N 個 external file → 按 "Download selected"：
- Frontend state 排隊，**一次一個 request** 落 backend
- Backend 簡單 FIFO（`GET /artifacts/{id}/download` 一個一個處理）
- Dashboard 顯示進度：queue position / current / done
- 大 file / 慢 network 唔會 block UI

可以**之後優化**（Phase 2）：
- 整包成 zip 一個 request
- 顯示 estimated time
- Resume 暫停咗嘅 download

### 5.6 §5 Resolved summary

✅ **§5 鎖死**：multipart upload / 50MB configurable / central + external 兩種 storage / Orchestrator proxy / scp fallback / multi-select queue
📦 **productize ready**：user 唔使識 SMB / NFS，dashboard 全部搞掂

---

## 6. Auth Scheme (agreed 2026-07-15)

### 6.0 Agreed decisions

| 議題 | 決定 |
|---|---|
| Algorithm | **HMAC-SHA256**（per agent secret） |
| Key scope | **Per-agent**（每個 wrapper / subagent 自己一把 key） |
| Bootstrap | Orchestrator 產生 key，UI 顯示一次性 setup token，user copy 去 agent OS 貼入 `~/.hermes-orchestrator/.secret-<agent_id>` |
| Rotation | **手動 + 7-day grace**：兩把 key 同時 valid，過期 cut 舊 |
| Replay 防護 | **Timestamp ±5min**，冇 nonce（per-agent scope 已經夠 narrow） |
| Secret storage | File `~/.hermes-orchestrator/.secret-<agent_id>`，chmod 0600 |

### 6.1 Signing scheme

**Signing payload**：
```
signing_string = f"{method}\n{path}\n{sha256(body)}\n{timestamp}"
signature = base64(hmac_sha256(secret, signing_string))
```

**Request headers**：
```
X-Agent-Id: linux-a-01
X-Timestamp: 2026-07-15T17:50:00+08:00
X-Signature: <base64>
Content-Type: application/json
```

**Server-side verification**（`POST /tasks` 例子）：
1. 讀 `X-Agent-Id` → 撈 secret
2. 讀 `X-Timestamp` → 檢查 `|now - timestamp| < 5min`
3. 重計 signature，compare 返 `X-Signature`（constant-time compare）
4. 任一 fail → 401

### 6.2 Bootstrap flow

```
User 喺 dashboard 開 "Register Agent" form
   → 填 agent_id (e.g. "linux-a-01")、OS type、IP、**roles (comma-separated)**
   → Submit
   → Orchestrator 產生 random secret (32 bytes base64)
   → DB 記：{agent_id, secret_hash, roles: [...], ip, created_at}
   → UI 顯示一次性 setup：
       secret: <random-base64>
       instruction: "ssh linux-a-01, run: 
         echo '<secret>' > ~/.hermes-orchestrator/.secret-linux-a-01
         chmod 600 ~/.hermes-orchestrator/.secret-linux-a-01
         hermes-orch agent start"
   → User 執行 setup
   → Agent heartbeat 自動 verify（X-Signature OK）→ status: "verified"
   → Secret 唔再喺 UI display（after first view）
```

### 6.3 Rotation flow

```
User 喺 dashboard agent 上面按 "Rotate Key"
   → Orchestrator 產生新 secret
   → DB 同時保留舊 + 新（new_secret_hash + old_secret_hash + old_expires_at = now + 7 days）
   → UI 顯示新 secret + 提示 "7 day grace period for old key"
   → User 去 agent OS 換 secret
   → 7 日後舊 key 自動失效
```

### 6.4 Multi-role model (2026-07-15)

決定：**1 agent = 1 機，agent 內含多個 role/profile**（Model A）。

- Agent 入 DB：`{id, secret_hash, roles: [...], ip, os_type, ...}`
- Profile 入 sub-table：`agent_profiles {id, agent_id, name, description, status, current_task_id}`
- 同一部機唔需要多個 agent record / 多個 secret / 多個 heartbeat
- 4 個 profile 嘅 Linux A 機 = 1 個 agent record + 4 個 profile record

**Add profile**（after agent registered）：
```bash
hermes-orch agent <agent_id> add-profile --role <name> --description <text>
# → INSERT into agent_profiles
# → Wrapper auto-restart（load 新 profile config）
# → Dashboard sub-card 出現
```

**Remove profile**：
```bash
hermes-orch agent <agent_id> remove-profile --role <name>
# → Check 冇 in-flight task
# → DELETE from agent_profiles
# → Wrapper auto-restart
```

**Update profile description**：
```bash
hermes-orch agent <agent_id> update-profile --role <name> --description <new>
```

### 6.5 DB schema（agents + profiles）

```sql
CREATE TABLE agents (
  id TEXT PRIMARY KEY,
  secret_hash TEXT NOT NULL,
  ip TEXT,
  os_type TEXT,                          -- 'windows' | 'linux'
  status TEXT,                           -- 'verifying' | 'verified' | 'unreachable'
  last_heartbeat_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE agent_profiles (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  name TEXT NOT NULL,                    -- 'data-analyst'
  description TEXT,                      -- optional
  status TEXT,                           -- 'idle' | 'busy' (per-profile state)
  current_task_id TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (agent_id) REFERENCES agents(id),
  UNIQUE(agent_id, name)
);
```

### 6.6 §6 Resolved summary

✅ **§6 鎖死**：HMAC-SHA256 / per-agent secret / dashboard bootstrap / manual rotation with grace / timestamp replay protection / file-based secret / **multi-role Model A**
📦 **productize ready**：user register agent 填 form + 多 role 一次過；加減 profile 1 個 command + auto-restart
🔒 **Phase 2**：HTTPS / SSO / OAuth / 硬件 key（V 形 token）— 唔做

---

## 7. Dashboard UI (agreed 2026-07-15)

### 7.0 Agreed decisions

| 議題 | 決定 |
|---|---|
| Tech | **FastAPI + Jinja2 templates + Tailwind CSS**（vanilla，唔用 React） |
| Live updates | **5s polling**（唔用 WebSocket MVP） |
| Pages | 4 個：Agents / Tasks / Project / History |
| Direct mode chat | 喺 subagent card 開 **chat panel**（slide-over） |

### 7.1 Pages wireframe

**Agents page**（Model A：1 agent card，多 profile sub-card）：
```
┌─ Agents ───────────────────────────────────────┐
│ [+ Register Agent]                              │
│                                                 │
│ ┌─ linux-a-01 ─────────[● running 1]──[▾]──────┐│
│ │ last seen: 3s ago                            ││
│ │                                              ││
│ │ Roles (4):                                   ││
│ │   ┌─ data-analyst     [● busy]  3 tasks ──┐││
│ │   ├─ backtest-runner  [○ idle]  0 tasks   │││
│ │   ├─ report-writer    [○ idle]  0 tasks   │││
│ │   └─ mt5-automation   [○ idle]  0 tasks   │││
│ │                                              ││
│ │ [Open Chat] [View All Tasks] [+ Profile]     ││
│ └──────────────────────────────────────────────┘│
│                                                 │
│ ┌─ windows-b-01 ────────[○ idle]──[▸]──────────┐│
│ │ last seen: 1m ago                            ││
│ │ Roles (2):                                   ││
│ │   ┌─ report-writer    [○ idle]  0 tasks ──┐││
│ │   └─ mt5-automation  [○ idle]  0 tasks   │││
│ │ [Open Chat] [View All Tasks] [+ Profile]     ││
│ └──────────────────────────────────────────────┘│
└─────────────────────────────────────────────────┘
```

- **Sub-card `[▾]` expand** 預設開住（user 唔使 click 都睇到 4 個 role status）
- **Direct mode chat** 從 agent card 開（唔係 sub-card）— 因為 1 部機 1 個 chat
- **「+ Profile」button** 即時 trigger §6.4 add-profile flow

**Tasks page**：
```
┌─ Tasks ─────────────────────────────────┐
│ filter: [All|Queued|Running|Done|Failed] │
│                                          │
│ t-001 [✓] backtest done     2m ago      │
│   by: linux-a-01                        │
│   [3 artifacts] [view plan]             │
│                                          │
│ t-002 [●] running report    30s         │
│   by: windows-b-01                      │
│   [Interrupt] [Cancel]                  │
└──────────────────────────────────────────┘
```

**Project page**：
```
┌─ Project ───────────────────────────────┐
│ proj-001: EURUSD Q3 analysis             │
│ state: running    goal: "..."           │
│                                          │
│ plan.md (read-only):                     │
│ ┌────────────────────────────────────┐  │
│ │ t-001 [✓] backtest                 │  │
│ │   └─> t-002 [●] report  (depends)  │  │
│ │   └─> t-003 [○] chart    (depends) │  │
│ └────────────────────────────────────┘  │
│                                          │
│ [Send command to supervisor]             │
│ > "再 run 多一次"                       │
└──────────────────────────────────────────┘
```

**History page**：
```
┌─ History ───────────────────────────────┐
│ filter: [agent|project|time]             │
│                                          │
│ 17:50  t-002 failed: timeout            │
│ 17:48  supervisor restarted session      │
│ 17:30  t-001 completed by linux-a-01    │
│ ...                                      │
└──────────────────────────────────────────┘
```

### 7.2 Direct mode chat panel

Slide-over panel 喺 agent card 上面：
```
┌─ linux-a-01: Direct Chat ──────[×]──┐
│                                      │
│ [user] 幫我睇下 /tmp/run-001/ 入面有咩│
│                                      │
│ [linux-a-01] 跑緊... 5s              │
│                                      │
│ [linux-a-01] 有 4 個 files:          │
│   - backtest.csv (5MB)               │
│   - equity_curve.png (200KB)         │
│   - model.pkl (200MB, external)      │
│   - run.log (50KB)                   │
│                                      │
│ [Type a message...]            [Send] │
└──────────────────────────────────────┘
```

### 7.3 Polling strategy

- 5s polling 用 vanilla `setInterval` + `fetch`
- 每個 page 一個 polling endpoint：
  - `GET /api/agents` → agent list with status
  - `GET /api/tasks?status=...` → task list
  - `GET /api/projects/<id>/plan` → plan + task states
  - `GET /api/history?since=...` → events log
- Direct chat 走 `POST /api/chat/<agent_id>`（一次性，唔 poll）

### 7.4 §7 Resolved summary

✅ **§7 鎖死**：4-page layout / FastAPI + Jinja2 + Tailwind / 5s polling / slide-over direct chat
📦 **productize ready**：vanilla stack 唔需要 build pipeline，deploy 簡單
⏸️ **Phase 2**：WebSocket live update / React SPA / dark mode / 拖拽 reorder — 唔做

---

## 8. Deployment / Distribution (agreed 2026-07-15)

### 8.0 Agreed decisions

| 議題 | 決定 |
|---|---|
| Package 結構 | **單一 package** `hermes-orchestrator`，兩個 CLI entry point：`hermes-orch` (orchestrator) + `hermes-orch-agent` (agent) |
| Build artifact (MVP) | **Pure Python + `pip install`**（3 個 OS 即時 work） |
| Build artifact (Phase 2) | 加 **PyInstaller** standalone exe（Windows）+ 同等 Linux build |
| Agent daemon | **Linux: systemd service**；**Windows: NSSM wrapper** 註冊做 Windows Service |
| Config 來源 | **Env vars** + **`~/.hermes-orchestrator/config.yaml`**（fallback） |
| Distribution channel | **GitHub releases**（PyPI 暫不 publish） |
| License | **MIT**（v1 免費） |
| Audience | **Semi-technical** — setup wizard 要有 step-by-step 教學、screenshots、video 短片 |

### 8.1 Install / setup flow

**Orchestrator 第一次裝（喺 Windows A）**：
```powershell
# 1. Install
pip install hermes-orchestrator

# 2. Init（建立 folder + DB + admin token）
hermes-orch init
# Creates ~/.hermes-orchestrator/:
#   ├── config.yaml
#   ├── projects/
#   ├── artifacts/
#   ├── hermes-orch.db
#   └── admin-token.txt   ← first-time bootstrap token

# 3. 啟動 server
hermes-orch serve --port 8765
# Auto-opens http://localhost:8765
# User 第一次開 dashboard 見到 "paste admin token"
```

**Agent 第一次裝（喺 Windows B / Linux A）**：

Windows：
```powershell
pip install hermes-orchestrator
hermes-orch agent register `
  --orchestrator http://192.168.1.10:8765 `
  --agent-id windows-b-01 `
  --roles "mt5-automation,report-writer"
# 互動：問 user 喺 dashboard 攞 setup token
# 完成：secret 寫入 ~/.hermes-orchestrator/.secret-windows-b-01
# 註冊做 Windows Service（NSSM）：開機自動 start

hermes-orch agent start
```

Linux：
```bash
pip install hermes-orchestrator
hermes-orch agent register \
  --orchestrator http://192.168.1.10:8765 \
  --agent-id linux-a-01 \
  --roles "data-analyst,backtest-runner,report-writer,mt5-automation"
# systemd unit 自動裝：開機自動 start

hermes-orch agent start
```

### 8.2 Config 範例（`~/.hermes-orchestrator/config.yaml`）

```yaml
orchestrator:
  port: 8765
  host: "0.0.0.0"           # 對 LAN 開放 dashboard
  log_level: INFO

artifacts:
  max_size_mb: 50
  storage_root: ./artifacts

projects:
  storage_root: ./projects

auth:
  hmac_timestamp_tolerance_seconds: 300   # 5 min
  key_grace_period_days: 7

supervisor:
  session_turn_warn_threshold: 50

logging:
  audit_log_path: ./audit.log
  audit_log_retention_days: 90
```

Env var override（`ORCHESTRATOR_PORT=9000 hermes-orch serve` 即可 override `port`）。

### 8.3 Auto-update strategy

| Channel | Update 方式 |
|---|---|
| `pip install` (MVP) | `pip install --upgrade hermes-orchestrator` + 重啟 service |
| PyInstaller exe (Phase 2) | 內建 update check，user 確認後 download 新版 |
| Config | 自動 reload `config.yaml`（file watch），無需重啟 |

### 8.4 §8 Resolved summary

✅ **§8 鎖死**：single package / pip install (MVP) / systemd + NSSM daemon / config.yaml + env / GitHub releases / MIT / semi-technical audience
📦 **productize ready**：user 3 步裝好 orchestrator；1 步 register agent；step-by-step wizard
⏸️ **Phase 2**：PyInstaller standalone exe / 自動 update notification / 商業 license — 唔做

---

## Phase 2 之後 (先不做，先記下來免得忘掉)

- 多人 / RBAC（多 user 共用同一個 dashboard）
- 完整 observability（Prometheus / Grafana、structured log、distributed trace）
- WebSocket 即時更新（MVP 用 5s polling 即可）
- SQLite → PostgreSQL migration 策略
- 自動 deploy / config management（Ansible / 腳本）
- Artifact inline preview（image / PDF 在 dashboard 直接看）
- Supervisor 多 profile / 角色分工
- 跨 supervisor 協作（multi-project 共享資源）
- 任務排程 / SLA 規則
- 反向：agent 主動 push event 給 orchestrator（目前都是 pull）

---

## ✅ Design phase complete (2026-07-15)

所有 8 個 section 收得：§0 Design principles / §1-§7 + §8 Deployment。

可以開始落手 build。**Build order 建議**：

1. **Orchestrator skeleton**（FastAPI + SQLite + §6 HMAC auth）— 1 個鐘
2. **Project folder API**（§3.6 file API）— 30 min
3. **Wrapper skeleton**（hermes subprocess + auth client）— 1 個鐘
4. **Task lifecycle endpoints**（§1 dispatch / status / interrupt）— 1 個鐘
5. **Artifact endpoints**（§5 upload / download）— 30 min
6. **Dashboard pages**（§7 Agents / Tasks）— 1 個鐘
7. **Plan.md polling + supervisor**（§3 + §4）— 1-2 個鐘

預 6-8 個鐘寫到 MVP 端到端行得。
