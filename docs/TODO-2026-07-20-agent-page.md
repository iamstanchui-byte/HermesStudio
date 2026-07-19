# TODO: Agent page 改動 (2026-07-20)

Plan: 詳見 `~/.mavis/scratchpads/mvs_3f53c32f9c694c468fdbfd58f8469159/scratchpad.md`
Design ref image: `C:\Users\stanley\Downloads\agent_dashboard.jpg` (generic dashboard layout, 唔係 agent page mockup)
Real profile config schema: `C:\Users\stanley\AppData\Local\hermes\profiles\win-agent01\config.yaml`

## Implementation order (per user "先做 #9 睇效果")

1. **#8** Roles input 完全移除
2. **#9** Overview dashboard (4 stat cards + donut + subagent count) — 用戶想先睇 layout
3. **#10** LLM model per-profile (3 columns)
4. **#12** MCP per-profile (1 column)
5. **#11** Token usage (大件最後, 用戶要 detail)

## 通用守則

- **Parallel-path drift check** (memory pattern #9 / #11): 每個新 column / field, 確保
  - `api/agents.py:_row_to_profile` 解析 (JSON API path)
  - `api/dashboard.py:_load_agents` 解析 (HTML page path)
  - **兩條都唔可以漏**, 否則 JSON API 200 但 HTML page 500
- **Migration safety**: 全部 ALTER TABLE 加落 `db.py:MIGRATIONS` list (try/except 包住, SQLite 唔支援 IF NOT EXISTS for columns)
- **Verify each step**:
  - `curl -s http://127.0.0.1:8765/agents -o /dev/null -w "%{http_code}\n"` → expect 200
  - 開 browser reload `/agents` 頁
  - 每 task 完 commit, message 跟 pattern `<area>: <change>` (e.g. `Agents: remove roles input from register form`)

---

## #8 移除 register form 嘅 roles input

**Files**:
- `src/hermes_orch/api/agents.py` — `AgentRegister` Pydantic model
- `src/hermes_orch/templates/agents.html` — register form HTML + JS

**Changes**:
- `AgentRegister`: 移除 `roles: list[str]`, payload 變 `{agent_id, ip, os_type}`
- `register_agent()`: 移除 `for role in body.roles: ...` loop
- `agents.html` form: 移除 `Roles (comma-separated)` input field
- 加一段 tip text: "Profiles will be added separately on the Agents page after register"
- **Backward compat**: `body.roles` 仍然 accept (用 `body.roles if hasattr(body, 'roles') else []`) 但 silently ignore + audit log `roles_ignored`

**Verify**:
- `curl -X POST http://127.0.0.1:8765/api/agents/ -H "Content-Type: application/json" -d '{"agent_id":"test-x","ip":"1.2.3.4","os_type":"linux"}'` → 201
- `curl -X POST .../api/agents/ -d '{"agent_id":"test-y","roles":["foo"]}'` → 201, profiles empty
- HTML page load → form 冇 roles input, 冇 error
- DB check: `SELECT * FROM agent_profiles WHERE agent_id='test-x'` → 0 rows

**Commit**: `Agents: remove roles input from register form (server auto-detects profile path)`

---

## #9 Overview dashboard (4 stat cards + donut)

**Files**:
- `src/hermes_orch/api/dashboard.py` — 新 `_load_agents_overview()` helper + 改 `agents_page()`
- `src/hermes_orch/templates/agents.html` — 新 overview section (在 register form 之前)

**Changes**:

`dashboard.py`:
- 新 helper:
  ```python
  async def _load_agents_overview(db) -> dict:
      now = now_aware()
      online_cutoff = (now - timedelta(seconds=90)).isoformat()
      row = await db.fetchone("""
          SELECT
              COUNT(DISTINCT a.id) AS total_agents,
              SUM(CASE WHEN a.status='verified' AND a.last_heartbeat_at >= ? THEN 1 ELSE 0 END) AS online,
              SUM(CASE WHEN a.status='verified' AND a.last_heartbeat_at >= ? AND p.status='idle' THEN 1 ELSE 0 END) AS idle,
              SUM(CASE WHEN a.status='verified' AND a.last_heartbeat_at >= ? AND p.status='busy' THEN 1 ELSE 0 END) AS busy,
              SUM(CASE WHEN a.os_type='windows' AND a.last_heartbeat_at >= ? THEN 1 ELSE 0 END) AS windows_online,
              SUM(CASE WHEN a.os_type='linux' AND a.last_heartbeat_at >= ? THEN 1 ELSE 0 END) AS linux_online
          FROM agents a LEFT JOIN agent_profiles p ON p.agent_id = a.id
      """, (online_cutoff,) * 5)
      profiles_total = await db.fetchone("SELECT COUNT(*) as n FROM agent_profiles")
      subagents_online = await db.fetchone(
          "SELECT COUNT(*) as n FROM agent_profiles p "
          "JOIN agents a ON a.id = p.agent_id "
          "WHERE a.last_heartbeat_at >= ?", (online_cutoff,))
      return {
          "total_agents": row["total_agents"] or 0,
          "online": row["online"] or 0,
          "idle": row["idle"] or 0,
          "busy": row["busy"] or 0,
          "windows_online": row["windows_online"] or 0,
          "linux_online": row["linux_online"] or 0,
          "profiles_total": profiles_total["n"] if profiles_total else 0,
          "subagents_online": subagents_online["n"] if subagents_online else 0,
      }
  ```
- `agents_page()`: 加 `overview = await _load_agents_overview(db)`, 傳入 template context
- 順便計 donut 比例 (idle / busy / offline) — Python-side, 唔喺 SQL

`agents.html` (喺 register form 之前新 section):
- 4 stat cards: Total / Online / Idle / Busy
- 1 donut SVG (inline): 顯示 idle / busy / offline 比例, color: green / red / grey
- 1 stat block (右手邊): Profiles (N) · Windows (N) · Linux (N) · Subagents online (N)
- 全部 inline, 唔加 Chart.js
- Inline SVG donut helper function 喺 agents.html 入面 (path d="M ... A ...", polar to cartesian)

**SVG donut** (inline, ~30 lines JS):
```javascript
function renderDonut(svgId, slices) {
    // slices = [{label, count, color}, ...]
    // polar-to-cartesian helper
    // sum slices → 100%, draw each as <path d="M cx,cy L x1,y1 A r,r 0 large,1 x2,y2 Z">
}
```

**Verify**:
- `curl http://127.0.0.1:8765/agents -o /dev/null -w "%{http_code}\n"` → 200
- 開 `/agents` 喺 browser: 見到 4 cards + donut + stat block
- 全部 agent offline 時 donut 100% grey
- 1 busy, 2 idle, 5 online: donut 顯示 1/7 red + 2/7 green + 4/7 grey
- Stat cards 數字 對 `SELECT COUNT(*) FROM agents` 等

**Commit**: `Dashboard: agent overview section (4 stat cards + donut + subagent count)`

---

## #10 LLM model per-profile (3 columns)

**Files**:
- `src/hermes_orch/db.py` — `MIGRATIONS` list
- `src/hermes_orch/api/agents.py` — `HeartbeatBody` Pydantic + `heartbeat()` handler
- `src/hermes_orch/api/agents.py` — `_row_to_profile` (parse)
- `src/hermes_orch/api/dashboard.py` — `_load_agents` (parse, 同 pattern #9 一致)
- `src/hermes_orch/templates/agents.html` — profile card display

**Schema** (per-profile):
- `agent_profiles.llm_model_default TEXT`
- `agent_profiles.llm_model_base_url TEXT`
- `agent_profiles.llm_model_provider TEXT`
- Migration (3 lines 加落 MIGRATIONS list):
  ```sql
  ALTER TABLE agent_profiles ADD COLUMN llm_model_default TEXT;
  ALTER TABLE agent_profiles ADD COLUMN llm_model_base_url TEXT;
  ALTER TABLE agent_profiles ADD COLUMN llm_model_provider TEXT;
  ```

**Pydantic**:
```python
class HeartbeatBody(BaseModel):
    status: str | None = None
    profile: str | None = None              # NEW: which profile (optional)
    model_default: str | None = None        # NEW
    model_base_url: str | None = None       # NEW
    model_provider: str | None = None       # NEW
    mcp_servers: list[dict] | None = None   # NEW (for #12)
```

**`heartbeat()` handler**:
- 如果 `body.model_*` 有 → UPDATE `agent_profiles` 嗰啲 columns
- Profile filter: 如果 `body.profile` 有就只 set 該 profile, 否則 fan-out 全部 該 agent 嘅 profiles
- 只 update 有值嘅 fields (`if body.model_default is not None: ...`)

**Parse** (memory pattern #9 — 兩條 path):
- `_row_to_profile` (api/agents.py): 唔加 capabilities 解析, 因為 plain text columns
- `_load_agents` (api/dashboard.py): 加埋 3 個 fields 去 `p` dict (plain string, 唔需要 parse)

**Display** (agents.html profile card):
- 喺 `p.status` badge 旁邊加新 badge: `model: MiniMax-M3 (minimax-oauth)`
- Sub-text (細啲): `endpoint: https://api.minimax.io/anthropic`
- 全部 3 個 field NULL 時: 灰色 `(LLM not reported by wrapper)` + tooltip "wrapper needs to read <profile>/config.yaml and report in heartbeat"

**Verify**:
- Migration: 跑 server, check `PRAGMA table_info(agent_profiles)` 見 3 個新 columns
- Heartbeat 報 model: `curl -X POST .../heartbeat -d '{"status":"idle","profile":"win-agent01","model_default":"MiniMax-M3","model_base_url":"https://api.minimax.io/anthropic","model_provider":"minimax-oauth"}'` → DB updated
- agents page 顯示新 badge
- 冇報 model → 顯示 fallback

**Commit**: `Agents: per-profile LLM model (3 cols, wrapper-reports)`

---

## #12 MCP per-profile (1 column)

**Files**: 同 #10, 共享 heartbeat endpoint 改動

**Schema**:
- `agent_profiles.mcp_servers TEXT NOT NULL DEFAULT '[]'`
- Migration: `ALTER TABLE agent_profiles ADD COLUMN mcp_servers TEXT NOT NULL DEFAULT '[]'`

**Parse** (memory pattern #9):
- `_load_agents` (dashboard.py):
  ```python
  mcp_raw = p.get("mcp_servers")
  mcps = []
  if mcp_raw:
      try:
          parsed = json.loads(mcp_raw) if isinstance(mcp_raw, str) else mcp_raw
          if isinstance(parsed, list):
              mcps = [{"name": str(m.get("name", "?")), "enabled": bool(m.get("enabled", True))} for m in parsed if isinstance(m, dict)]
      except (json.JSONDecodeError, TypeError):
          pass
  p["mcp_servers"] = mcps
  ```
- `_row_to_profile` (api/agents.py) — 加 `mcp_servers` field to Pydantic model:
  ```python
  class AgentProfile(BaseModel):
      ...
      mcp_servers: list[dict] = Field(default_factory=list)  # NEW
  ```
  Parse 一樣: 讀 row["mcp_servers"], JSON 解析成 list

**`heartbeat()` handler**:
- 讀 `body.mcp_servers` (list of `{name, enabled}`)
- Validate 每個 entry 必須有 `name` (str)
- Store: `UPDATE agent_profiles SET mcp_servers = ? WHERE ...` (JSON encoded)

**Display** (agents.html profile card, 喺 skills section 旁邊):
```html
<div class="border-t pt-2 mt-2">
  <div class="text-xs font-medium text-gray-500 uppercase tracking-wide">
    MCP servers ({{ enabled_count }}/{{ total_count }})
  </div>
  {% if p.mcp_servers %}
  <div class="mt-1 space-y-0.5">
    {% for m in p.mcp_servers %}
    <div class="text-xs flex items-center gap-1">
      <span class="status-dot {{ 'dot-running' if m.enabled else 'dot-idle' }}"></span>
      <span class="font-mono">{{ m.name }}</span>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="mt-1 text-xs text-gray-400">No MCP servers configured</div>
  {% endif %}
</div>
```

**Verify**:
- Migration: column 加咗
- Heartbeat 報 mcp: `curl -X POST .../heartbeat -d '{"profile":"win-agent01","mcp_servers":[{"name":"tradingview","enabled":true}]}'` → DB updated
- agents page 顯示 "MCP servers (1/1)" + "tradingview" with green dot
- 0 MCP → 顯示 "No MCP servers configured"
- 1 enabled + 1 disabled → "(1/2)" with mixed colors

**Commit**: `Agents: per-profile MCP server list (1 col, simplified)`

---

## #11 Token usage (大件, 一次過做齊)

**Files**:
- `src/hermes_orch/db.py` — SCHEMA + MIGRATIONS
- `src/hermes_orch/api/agents.py` — 新 `POST /api/agents/{id}/token-usage` endpoint + heartbeat batch
- `src/hermes_orch/core/planner.py` — 加 `record_token_usage()` call after each LLM call
- `src/hermes_orch/core/synthesis.py` — 同上
- `src/hermes_orch/api/dashboard.py` — `_load_token_usage_overview()` helper
- `src/hermes_orch/templates/agents.html` — overview section 加 3 cards + breakdown tables + sparkline

**Schema** (新 table):
```sql
CREATE TABLE IF NOT EXISTS token_usage (
    id TEXT PRIMARY KEY,
    agent_id TEXT,
    profile_id TEXT,
    project_id TEXT,
    task_id TEXT,
    role TEXT,
    model TEXT NOT NULL,
    base_url TEXT,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    call_kind TEXT NOT NULL,    -- 'planner' | 'synthesis' | 'agent_task' | 'wrapper_other'
    call_label TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_token_usage_agent ON token_usage(agent_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_profile ON token_usage(profile_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_project ON token_usage(project_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_task ON token_usage(task_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_created ON token_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_token_usage_model ON token_usage(model);
```
(加落 SCHEMA block, IF NOT EXISTS handle 舊 DB)

**Write helper** (新 file `src/hermes_orch/core/token_usage.py`):
```python
async def record_token_usage(
    db, *,
    agent_id=None, profile_id=None, project_id=None, task_id=None,
    role=None, model, base_url=None,
    prompt_tokens, completion_tokens, total_tokens,
    call_kind, call_label=None,
):
    await db.insert("token_usage", {
        "id": str(uuid.uuid4()),
        "agent_id": agent_id, "profile_id": profile_id, ...
        "model": model, "base_url": base_url,
        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "call_kind": call_kind, "call_label": call_label,
    })
```

**Write sources**:
- `api/agents.py:heartbeat()` — 加 batch field `token_usage: list[dict] | None`, loop 寫入
- 新 `POST /api/agents/{id}/token-usage` (獨立 endpoint, single record)
- `core/planner.py:_call_llm()` 後 (睇 actual code structure, 可能喺 `_call_planner_llm` 之類) → `await record_token_usage(... call_kind="planner", call_label=<action>)`
- `core/synthesis.py` → `await record_token_usage(... call_kind="synthesis", call_label="synthesize_final_report")`

**Display** (agents.html, 喺 #9 嘅 overview section 下面):
- **3 stat cards**: 4h / 24h / 7d, 各 display total tokens (formatted 1.2K / 12.5M) + sub-line by call_kind
- **By model breakdown** (small table, sortable 7d DESC):
  - model | 4h | 24h | 7d | calls
- **By agent breakdown** (small table):
  - agent_id | profile | 4h | 24h | 7d
- **By project breakdown** (collapsible `<details>`):
  - project_id | name | 4h | 24h | 7d | top model
- **Top-5 tasks** (table, 7d DESC):
  - task_id | project | role | model | total tokens | calls
- **7d sparkline** (inline SVG, daily buckets): 7 個 `<rect>` 高度 = daily total

**Query helper** (api/dashboard.py):
```python
async def _load_token_usage_overview(db) -> dict:
    now = now_aware()
    cutoffs = {
        "4h": (now - timedelta(hours=4)).isoformat(),
        "24h": (now - timedelta(hours=24)).isoformat(),
        "7d": (now - timedelta(days=7)).isoformat(),
    }
    out = {"totals": {}, "by_model": [], "by_agent": [], "by_project": [], "top_tasks": [], "sparkline": []}
    for window, cutoff in cutoffs.items():
        row = await db.fetchone(
            "SELECT COALESCE(SUM(total_tokens),0) as total, "
            "COALESCE(SUM(prompt_tokens),0) as prompt, "
            "COALESCE(SUM(completion_tokens),0) as completion, "
            "COUNT(*) as calls FROM token_usage WHERE created_at >= ?",
            (cutoff,))
        out["totals"][window] = row
    # by_model (7d)
    rows = await db.fetchall(
        "SELECT model, SUM(total_tokens) as total, COUNT(*) as calls "
        "FROM token_usage WHERE created_at >= ? GROUP BY model ORDER BY total DESC LIMIT 10",
        (cutoffs["7d"],))
    out["by_model"] = rows
    # by_agent, by_project, top_tasks: 類似
    # sparkline: 7 daily buckets
    for i in range(7):
        day_start = (now - timedelta(days=6-i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        r = await db.fetchone(
            "SELECT COALESCE(SUM(total_tokens),0) as total FROM token_usage "
            "WHERE created_at >= ? AND created_at < ?", (day_start.isoformat(), day_end.isoformat()))
        out["sparkline"].append({"date": day_start.strftime("%m-%d"), "total": r["total"] if r else 0})
    return out
```

**Verify**:
- Schema: `SELECT name FROM sqlite_master WHERE type='table' AND name='token_usage'` → 1 row
- Insert test row: `python -c "..."` → 1 row 喺 table
- planner call 完之後 (用 mock mode): 1 row 寫入
- agents page: 3 cards 顯示數字, by_model table 有 row
- 0 data: cards 顯示 "0 / 0 calls", tables empty
- sparkline: 7 個 bars, 今日有 1 row, 6 日前 0 → height 比例啱

**Commit**: `Dashboard: token usage overview (4h/24h/7d, by-model/agent/project, top-N, sparkline)`

---

## Memory pitfalls to watch

1. **Pattern #9 / #11 / #12** (parallel-path drift): 每個新 column / field 改完, grep 確認兩條 path 都 hit:
   ```bash
   grep -n 'llm_model\|mcp_servers\|token_usage' src/hermes_orch/api/*.py
   ```
2. **Pattern #6 / #8** (HTML page still has old logic): curl `/agents` 確認 200, 唔可以只測 JSON API
3. **Migration order**: 先改 schema, 再 restart server, 再改 code. 唔可以 code reference 未存在 column
4. **auto-reload limitation**: `server --reload` re-imports Python, 但 template 改要 next request 才生效. stale tab 可能 hold 住舊 version → hard refresh 一次

## Out of scope (之後做)

- **Wrapper 改 (hermes-agent repo)**: 讀 `<profile>/config.yaml` 嘅 `model.default` + `mcp_servers` dict, push 喺 heartbeat. PR 去 upstream 或 local fork
- **Token cost estimation**: 將來加 `_cost_per_1k` table, 顯示 estimated USD

## Quick start command

```bash
cd "C:\Project\minimax code\hermes-orchestrator"
# 確認 server 跑緊
curl -s http://127.0.0.1:8765/agents -o /dev/null -w "Server: %{http_code}\n"
# 開始 #8
```
