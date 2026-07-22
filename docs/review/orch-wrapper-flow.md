# Orchestrator ↔ Wrapper 互動 Review

**Date**: 2026-07-22
**Trigger**: `proj-e4c9e5dd` Google Drive 任務連續 timeout 1800s，但 agent 實際有運作（manual session 確認可以 access）。Review 揭出 4 個 deadlock / loop / size / timeout 問題。

---

## 1. 完整互動流程（happy path）

```
[User] POST /api/projects
   │   {name, goal, ...}
   ▼
[Server] project.state = planning
[Server] supervisor.tick → _handle_planning → LLM Planner
   │   ~9K tokens (max_tokens=8000) + thinking
   ▼
[Server] tasks inserted (status=pending), state → ready → running
[Server] supervisor.tick → _assign_task per pending task
   │   SELECT profile WHERE name=<role> AND verified
   │   UPDATE task SET status='assigned', assigned_agent/profile_id
   │   UPDATE agent_profile SET status='busy', current_task_id
   ▼
[Wrapper] 每 5s 嘅 main loop:
   │   tasks, cleanup_ids = _heartbeat()   # POST /api/agents/{id}/heartbeat
   │   for t in tasks where status='assigned':
   │       _claim(t.id)                     # POST /api/tasks/{id}/start
   │       _run_task(t)                     # hermes subprocess
   │       _submit_result(t.id, result)     # POST /api/tasks/{id}/result
   ▼
[Wrapper] _run_task(t):
   │   prompt = f"{action}({params_str})\n\n--- PROJECT CONTEXT ---\n{ctx}\n--- END CONTEXT ---"
   │   hermes_args = [hermes_bin, "-p", role, "chat", "-q", prompt, "--yolo", "--accept-hooks"]
   │   if project_id: +["--resume", session_id]   # GET /api/projects/{id}/session?role=
   │   proc = subprocess.Popen(hermes_args, stdout=PIPE, stderr=PIPE, env=...)
   │   poll_thread starts: 30s loop POST /api/tasks/{tid}/poll (liveness)
   │   raw_stdout, raw_stderr = proc.communicate(timeout=1800)   # ← 30 min hard timeout
   │   if rc==0:
   │       summary = _clean_hermes_output(stdout)[:8000]    # capped 8KB
   │       result = {status:completed, summary, session_id, artifacts, token_usage}
   │       for f in cache_root.rglob("*"):
   │           PUT /api/projects/{id}/files/{rel}  (each file, timeout=60s)
   │   POST /api/tasks/{id}/result  (timeout=10s)
   ▼
[Server] /result handler:
   │   UPDATE task SET status=completed/failed, result=json(...)
   │   UPDATE agent_profile SET status='idle' WHERE current_task_id=...
   │   artifact.registered (per artifact)
   │   task.completed / task.failed (audit)
   │   MemoryWriter.append_fact_L2 (L2 hook)
   ▼
[Server] supervisor.tick (5s) → _maybe_complete → all done? state=completed
```

---

## 2. 互動時序表（每個 step 嘅 size / timeout / 容量）

| Step | HTTP | Wrapper 端 timeout | Server 端 timeout | Payload size | 出現問題嘅 size |
|---|---|---|---|---|---|
| Heartbeat | `POST /api/agents/{id}/heartbeat` | 10s | (async) | < 5KB (status + 4 profile meta) | OK |
| Task claim | `POST /api/tasks/{id}/start` | 10s | - | 0 | OK |
| Session resume lookup | `GET /api/projects/{id}/session?role=` | 10s | - | 0 | OK |
| Liveness poll | `POST /api/tasks/{id}/poll` | 5s | - | 0 | OK |
| **Hermes subprocess** | (Popen, PIPE) | **1800s** | - | **stdout/stderr 無上限** | **💀 DEADLOCK** |
| File upload | `PUT /api/projects/{id}/files/{rel}` | **60s per file** | - | **無上限 (await request.body())** | **⚠️ SIZE** |
| Result submit | `POST /api/tasks/{id}/result` | 10s | - | `summary` capped 8000 chars at wrapper side, **server 端無 cap** | **⚠️ SIZE** |
| Session save | `POST /api/projects/{id}/session` | 10s | - | < 1KB | OK |

---

## 3. 死鎖 / Loop / Size / Timeout 問題（4 個 critical）

### 🔴 C1. Hermes subprocess PIPE 死鎖（**已確診，今日嘅主因**）

**位置**: `agent_cli.py:1478-1484`

```python
proc = subprocess.Popen(
    hermes_args,
    cwd=hermes_cwd,
    stdout=subprocess.PIPE,   # 容量 64KB
    stderr=subprocess.PIPE,   # 容量 64KB
    env=...,
)
...
raw_stdout, raw_stderr = proc.communicate(timeout=timeout)  # 等 exit 先讀
```

**問題**:
- Hermes stream LLM output（9K+ output tokens + 19+ tool calls）→ stdout pipe 寫到 64KB 滿
- OS 將 hermes 嘅 stdout write() 阻塞住（pipe 滿咗，要 reader 食先可以再寫）
- Wrapper 喺 `proc.communicate()` 阻塞等 hermes 退出先讀
- 雙向 deadlock → 30 min 後 timeout → kill hermes
- **Agent 收到指令但永遠 output 唔到 → 你乜都睇唔到**

**證據** (proj-e4c9e5dd):
- t-1b3f8c42: 30 min 1800s timeout
- t-0264f947: 30 min 1800s timeout
- t-614a90de: 30 min 1800s timeout
- 全部 `stdout_len=???` 都係 wrapper 殺咗 hermes 之後冇得讀
- 同一個 session `20260720_160820_635017` 用 manual CLI 跑：54 messages, 19 tool calls, 95K input + 9K output tokens 成功 access Google Drive（< 5 秒）

**修法（推薦 A）**:
```python
# 改 stdout/stderr 直接去 file，唔再用 PIPE
stdout_log = cache_dir / f"hermes.stdout.{tid}.log"
stderr_log = cache_dir / f"hermes.stderr.{tid}.log"
proc = subprocess.Popen(
    hermes_args,
    cwd=hermes_cwd,
    stdout=open(stdout_log, "wb"),
    stderr=open(stderr_log, "wb"),
    env=...,
)
...
proc.wait(timeout=timeout)  # 唔再讀 pipe
# 之後用 _clean_hermes_output() 讀 stdout_log file
```

**副作用（好處）**:
- 解決死鎖
- 每個 task 嘅完整 transcript 落地，可以 review 究竟 agent 講咗乜
- 唔再依賴 in-process buffer
- 大 output 唔再 OOM

---

### 🔴 C2. Server 端 body size 無上限（**潛在 DoS**）

**位置**: `api/projects.py:326` (write_file), `api/tasks.py:361` (submit_result)

```python
# write_file
body = await request.body()  # ← 冇 size check
full.write_bytes(body)       # ← 直接寫 100GB 都得

# submit_result  
class TaskResult(BaseModel):
    summary: str | None = None  # ← 冇 max_length
    artifacts: list[dict] = Field(default_factory=list)  # ← 冇 max_items / size
```

**問題**:
- 一個 PUT `/api/projects/{id}/files/{path}` 可以 100GB body → 磁碟爆
- `result` JSON 10MB+ → audit_log / result column 寫爆
- FastAPI 默認有 client_max_size=1MB（如果 middleware 設咗），但要確認

**修法**:
1. Pydantic 加 `Field(max_length=...)`：`summary: str | None = Field(None, max_length=50000)`，`artifacts: list = Field(default_factory=list, max_length=100)`
2. 寫 file 嘅 endpoint 開頭加 `if len(body) > MAX_FILE_BYTES: raise HTTPException(413)`
3. middleware 設 `client_max_size = 50 * 1024 * 1024` (50MB 已經夠晒 LLM output 同 file upload)

---

### 🟠 C3. Stuck-task detector 3 分鐘 threshold 配合 30 分鐘 timeout 太寬鬆

**位置**: `supervisor.py:248` (stuck cutoff 180s), `agent_cli.py` (default 1800s)

```python
# supervisor.py:248
stuck_cutoff = (now_aware() - timedelta(seconds=180)).isoformat()  # 3 min

# supervisor.py:256-262
stuck_tasks = await self.db.fetchall(
    "SELECT t.id, t.project_id, ... FROM tasks t JOIN agents a ON a.id = t.assigned_agent_id "
    "WHERE t.status = 'running' AND a.last_heartbeat_at < ?",
    (stuck_cutoff,),
)
```

**問題**:
- 180s 邏輯：task running 緊，但 agent 嘅 heartbeat > 3 min stale → mark failed
- 但我哋已經有 bg heartbeat thread (commit 983a364)，所以 agent heartbeat 一定 < 5s
- 所以 stuck-task detector **永遠唔會 trigger**（只要 wrapper 仲行緊）
- 真實 hang 嘅 task 會跑到 1800s 30 min 然後 wrapper 自己 timeout

**應該**:
- 180s 改成睇 `task.last_liveness_at` < 180s (not agent's)
- 即係睇 liveness poller 有冇成功 update task
- 如果 poller 都 hang 咗（HTTP 死，server 死），3 分鐘夠時間 mark failed

**修法**:
```python
# supervisor.py:248
stuck_cutoff = (now_aware() - timedelta(seconds=180)).isoformat()
stuck_tasks = await self.db.fetchall(
    "SELECT t.id, t.project_id, t.name, t.assigned_agent_id, t.last_liveness_at "
    "FROM tasks t "
    "WHERE t.status = 'running' "
    "AND t.last_liveness_at < ?",     # ← 改呢度
    (stuck_cutoff,),
)
```

---

### 🟠 C4. Heartbeat 端點嘅 body parsing 冇 HMAC 驗簽

**位置**: `api/agents.py:398-426`

```python
async def heartbeat(...) -> dict:
    """Agent heartbeat. HMAC-authed (per §6.1).
    For MVP: verifies presence of X-Agent-Id/X-Timestamp/X-Signature headers.
    Real HMAC signature verification TODO (when wrapper sends real requests).
    """
    if not x_agent_id or not x_timestamp or not x_signature:
        raise HTTPException(401, "Missing auth headers ...")
    ...
    if agent["id"] != x_agent_id:  # ← 只 check agent_id，唔 check signature
        raise HTTPException(401, ...)
```

**問題**:
- `x_signature` header **有 check presence 但從來冇 verify**
- 任何人都可以自稱係任何 agent（只要知道 agent_id）
- 雖然 internal LAN 唔算 critical，但係 security bug

**修法**:
```python
expected_sig = hashlib.sha256(
    f"{agent_id}:{x_timestamp}:{agent_secret}".encode()
).hexdigest()
if not hmac.compare_digest(x_signature, expected_sig):
    raise HTTPException(401, "Invalid signature")
```

**附加問題**: `X-Timestamp` 都冇 check 新鮮度（replay attack window 無限大）

---

## 4. 其他次要問題

### 🟡 M1. Submit result 後冇等 server 確認就繼續下一個 task

```python
# agent_cli.py:2070-2076
for t in assigned:
    if not _claim(t["id"]): continue
    result = _run_task(t)
    _submit_result(t["id"], result)  # 失敗就 return False，但 wrapper 唔理
```

`_submit_result` return False（網絡 timeout / 400 error）只係 print 一句 warn，下一個 iteration 就會 claim 新 task。但 **舊 task 喺 server DB 仲係 running**（因為 result 冇 submit），server stuck-task detector 唔 trigger（agent heartbeat 仲 update 緊），新 task claim 又會 fail（task 唔再 assigned）。

**修法**: `if not _submit_result: log error AND emit task.lost 事件 AND skip the profile for 5 min`

---

### 🟡 M2. Hermes session ID 提取靠 regex

```python
# agent_cli.py:1572-1575
m = re.search(r"Session:\s+(\S+)", stdout)
```

stdout 因為死鎖可能永遠讀唔到。改咗 C1 用 file 之後，可以直接用 `re.search` 喺 `stdout_log` file，或者改用 hermes CLI flag `--output-session-id` 直接寫去指定 file。

**更好嘅修法**: 用 `hermes --json` flag（如果 hermes 0.17+ 有）parse JSON 而唔係 regex。

---

### 🟡 M3. Wrapper 對 `proc.communicate(timeout=1800)` 用 try/except TimeoutExpired 但 cleanup 唔 complete

```python
# agent_cli.py:1543-1553
try:
    raw_stdout, raw_stderr = proc.communicate(timeout=timeout)
except subprocess.TimeoutExpired:
    proc.kill()
    try:
        proc.communicate(timeout=5)  # ← cleanup
    except Exception:
        pass
    stop_poll.set()
    return {"status": "failed", "error": f"hermes timeout after {timeout}s"}
```

呢段 OK。但有 race：poll thread 可能仲喺 `proc.kill()` 之後 attempt `proc.kill()` 多次。**idempotent** 所以冇事，但可能 log WARN 多咗。

**修法**: poll thread 收到 `stop_poll.is_set()` 應該 exit。但 `stop_poll.set()` 喺 timeout 處理之後先 call，poll thread 30s sleep 中。

---

### 🟡 M4. Auto-upload 唔檢查 file size

```python
# agent_cli.py:1644-1700
for f in cache_root.rglob("*"):
    if not f.is_file(): continue
    file_bytes = f.read_bytes()  # ← 可能讀 100GB file
    ...
    r2 = httpx.put(..., content=file_bytes, timeout=60)  # 60s 可能 upload 唔晒
```

**問題**:
- LLM 可能 write 好大 log / cache file，wrapper 嘗試 upload → 60s timeout → fail
- 同時 memory 爆（read 100GB into memory）

**修法**:
```python
MAX_AUTO_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB
if f.stat().st_size > MAX_AUTO_UPLOAD_BYTES:
    click.echo(f"  SKIP auto-upload {rel}: too large ({f.stat().st_size} bytes)")
    continue
file_bytes = f.read_bytes()
```

---

### 🟡 M5. /result 冇 idempotency

```python
# api/tasks.py:361-373
@router.post("/{task_id}/result", response_model=Task)
async def submit_result(task_id: str, body: TaskResult, ...):
    ...
    if task["status"] != "running":
        raise HTTPException(400, f"Task not in running state: {task['status']}")
```

Wrapper 提交 result 失敗（網絡 timeout）會 retry，但 server 已經收到，retry 會 400。**冇 idempotency key**。

**修法**: 加 `Idempotency-Key` header（UUID per submit attempt），server dedupe。

---

## 5. 修法優先級

| 優先 | 項目 | 工作量 | 影響 |
|---|---|---|---|
| **P0 立即** | C1 (PIPE → file) | 30 min | 解決所有 timeout，可見 agent output |
| **P0 立即** | C3 (stuck-task 用 last_liveness_at) | 15 min | 唔再等 30 min |
| **P1 今週** | C2 (size limits) | 1-2 hr | 防 OOM / DoS |
| **P1 今週** | C4 (HMAC verify) | 30 min | Security |
| **P2 之後** | M1 (lost-task handling) | 30 min | 改進錯誤可見性 |
| **P2 之後** | M2 (session id 提取) | 30 min | 配合 C1 順手做 |
| **P2 之後** | M3 (poll thread cleanup) | 15 min | 純 tidy |
| **P2 之後** | M4 (auto-upload size cap) | 15 min | 防爆 ram |
| **P3 nice-to-have** | M5 (idempotency) | 1 hr | 解決 retry 問題 |

---

## 6. 附錄：額外 observations

### a. `audit_log` 表無 index（潛在慢查詢）

```sql
-- supervisor.py 多處 query 依賴：
SELECT * FROM audit_log WHERE project_id=? ORDER BY id DESC LIMIT N
-- 冇 index on (project_id, id)
```

對於 100+ audit row 嘅 project（每 task 4-6 event），呢個 query 每次 supervisor tick 都跑。10K audit row 之後會慢。

**修法**: `CREATE INDEX idx_audit_project_id_id ON audit_log(project_id, id DESC)`

### b. `_heartbeat()` 對每個 profile 都讀一次 config.yaml

```python
# agent_cli.py:1032-1049
for role, pcfg in (profiles_cfg or {}).items():
    root = resolve_profile_root(...)
    meta = _read_profile_config(root)  # ← 每 5s 讀 2 次 file
```

對 4 profile × 2 agent = 8 file read / 5s。影響微但唔乾淨。

**修法**: 5s 內 cache 同一個 root 嘅 meta

### c. Supervisor tick 同時做 stuck-task + dispatch + cleanup

```python
# supervisor.py:223-300 (tick method)
- stale agent check
- stuck task check
- per-project: planning/ready/running
- session sweep
- project sweep
```

一個 tick 處理 N 個 project × (M 個 task + planning)，慢嘅 query 會 block 整個 tick loop。

**修法**: 將 cleanup sweep 拆去獨立 asyncio task，每 60s 跑一次

---

## 7. Test plan 建議

| 測試 | 方法 | 預期 |
|---|---|---|
| **C1 fix verify** | submit 個 task 寫 100KB output，等 30s 之後睇 stdout_log file | 唔再 30 min timeout，< 5s 完成 |
| **C2 size cap** | PUT 100MB file → 應該 413 | 拒絕 |
| **C3 stuck detect** | 殺 hermes 子進程（simulate hang），3 分鐘內 task 應該 mark failed | < 3 min failed |
| **C4 HMAC** | 用錯 signature POST heartbeat → 401 | reject |
| **M4 size cap** | 寫 100MB file 落 cache，等 wrapper auto-upload | skip + log |

---

## 8. 結論

最大嘅單一問題係 **C1 PIPE 死鎖**，解咗 80% 嘅 `proj-e4c9e5dd` timeout 問題。其次係 **C3 stuck detector 邏輯錯咗 target column**。

其他 6 個問題屬於 hardening，未有實際觸發過但係 known sharp edges。

**P0 兩項 (C1+C3) 估 45 min 寫 + 30 min test**。我哋可以一齊整定 C1+C3 先，然後再睇 P1。
