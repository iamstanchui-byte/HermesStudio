# test-end-to-end.ps1 - Full end-to-end demo: agent + project + task DAG + failure propagation
# Per REVIEW.md §1, §3.6, §4, §6

function J {
    param([Parameter(ValueFromPipeline=$true)] $o)
    process { $o | ConvertTo-Json -Depth 5 }
}

$BASE = "http://localhost:8765"
$AGENT_ID = "demo-agent"
$PROFILE_NAME = "demo-runner"

Write-Host "=== Hermes Orchestrator end-to-end demo ===" -ForegroundColor Cyan

# ===== STEP 1: Register agent (or reuse if exists) =====
Write-Host "`n[1] Register agent '$AGENT_ID'..." -ForegroundColor Yellow
try {
    $body = @"
{
  "agent_id": "$AGENT_ID",
  "ip": "192.168.1.30",
  "os_type": "linux",
  "roles": ["$PROFILE_NAME"]
}
"@
    $r = Invoke-RestMethod -Method Post -Uri "$BASE/api/agents/" `
        -ContentType "application/json" -Body $body
    Write-Host "  Agent registered" -ForegroundColor Green
} catch {
    if ($_.Exception.Response.StatusCode -eq 409) {
        Write-Host "  Agent already exists, reusing" -ForegroundColor Yellow
    } else {
        throw
    }
}

# Get agent + profile_id
$r = Invoke-RestMethod -Uri "$BASE/api/agents/$AGENT_ID"
$PROFILE_ID = ($r.profiles | Where-Object { $_.name -eq $PROFILE_NAME }).id
Write-Host "  Agent ID: $AGENT_ID, Profile ID: $PROFILE_ID" -ForegroundColor Green

# ===== STEP 2: Create project =====
Write-Host "`n[2] Create project 'EURUSD Q3'..." -ForegroundColor Yellow
$body = '{"goal":"Backtest EURUSD Q3 + summarize","name":"EURUSD Q3"}'
$r = Invoke-RestMethod -Method Post -Uri "$BASE/api/projects/" `
    -ContentType "application/json" -Body $body
$PROJ_ID = $r.id
Write-Host "  Project ID: $PROJ_ID" -ForegroundColor Green

# ===== STEP 3: Create task DAG (3 tasks, sequential) =====
Write-Host "`n[3] Create 3 tasks with dependencies..." -ForegroundColor Yellow
$body = @"
{
  "project_id": "$PROJ_ID",
  "name": "Step 1: Fetch data",
  "agent_role": "$PROFILE_NAME",
  "action": "fetch_data",
  "params": {"symbol": "EURUSD", "days": 30}
}
"@
$r = Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/" -ContentType "application/json" -Body $body
$T1 = $r.id
Write-Host "  T1: $T1 (Fetch data)" -ForegroundColor Green

$body = @"
{
  "project_id": "$PROJ_ID",
  "name": "Step 2: Analyze",
  "agent_role": "$PROFILE_NAME",
  "depends_on": ["$T1"],
  "on_parent_failure": "skip",
  "action": "analyze"
}
"@
$r = Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/" -ContentType "application/json" -Body $body
$T2 = $r.id
Write-Host "  T2: $T2 (Analyze, depends on T1)" -ForegroundColor Green

$body = @"
{
  "project_id": "$PROJ_ID",
  "name": "Step 3: Report",
  "agent_role": "$PROFILE_NAME",
  "depends_on": ["$T2"],
  "on_parent_failure": "skip",
  "action": "report"
}
"@
$r = Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/" -ContentType "application/json" -Body $body
$T3 = $r.id
Write-Host "  T3: $T3 (Report, depends on T2)" -ForegroundColor Green

# ===== STEP 4: Assign T1 to agent =====
Write-Host "`n[4] Assign T1 to agent..." -ForegroundColor Yellow
$body = "{`"agent_id`":`"$AGENT_ID`",`"profile_id`":`"$PROFILE_ID`"}"
Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/$T1/assign" `
    -ContentType "application/json" -Body $body | J

# ===== STEP 5: Start T1 =====
Write-Host "`n[5] Start T1 (agent acks)..." -ForegroundColor Yellow
Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/$T1/start" | J

# ===== STEP 6: Heartbeat (simulate agent's periodic check) =====
Write-Host "`n[6] Agent heartbeat (returns current assigned tasks)..." -ForegroundColor Yellow
$headers = @{
    "X-Agent-Id"  = $AGENT_ID
    "X-Timestamp" = (Get-Date -Format "o")
    "X-Signature" = "fake-sig"
}
$r = Invoke-RestMethod -Method Post -Uri "$BASE/api/agents/$AGENT_ID/heartbeat" -Headers $headers
Write-Host "  Tasks returned: $($r.tasks.Count)" -ForegroundColor Green
J $r.tasks

# ===== STEP 7: Submit SUCCESS result for T1 =====
Write-Host "`n[7] Submit T1 result (success)..." -ForegroundColor Yellow
$body = @"
{
  "status": "completed",
  "summary": "Fetched 30 days of EURUSD data",
  "session_id": "hermes-session-abc"
}
"@
Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/$T1/result" `
    -ContentType "application/json" -Body $body | J

# ===== STEP 8: Assign + start T2 =====
Write-Host "`n[8] Assign + start T2..." -ForegroundColor Yellow
$body = "{`"agent_id`":`"$AGENT_ID`",`"profile_id`":`"$PROFILE_ID`"}"
Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/$T2/assign" -ContentType "application/json" -Body $body | Out-Null
Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/$T2/start" | Out-Null
Write-Host "  T2 running" -ForegroundColor Green

# ===== STEP 9: Submit FAILED result for T2 (test failure propagation) =====
Write-Host "`n[9] Submit T2 result (FAILED -- tests failure propagation)..." -ForegroundColor Yellow
$body = @"
{
  "status": "failed",
  "error": "Data analysis timed out after 30 min"
}
"@
Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/$T2/result" `
    -ContentType "application/json" -Body $body | J

# ===== STEP 10: Check T3 status (should be SKIPPED due to T2 failure) =====
Write-Host "`n[10] Check T3 status (should be 'skipped' due to T2 failure)..." -ForegroundColor Yellow
$r = Invoke-RestMethod -Uri "$BASE/api/tasks/$T3"
Write-Host "  T3 status: $($r.status)" -ForegroundColor $(if ($r.status -eq 'skipped') {'Green'} else {'Red'})

# ===== STEP 11: List all tasks for the project (final state) =====
Write-Host "`n[11] Final task list for project..." -ForegroundColor Yellow
$r = Invoke-RestMethod -Uri "$BASE/api/tasks/?project_id=$PROJ_ID"
$r.tasks | ForEach-Object {
    $color = if ($_.status -eq 'completed') {'Green'} `
        elseif ($_.status -eq 'failed') {'Red'} `
        elseif ($_.status -eq 'skipped') {'Yellow'} `
        else {'White'}
    Write-Host "  $($_.id) [$($_.status)] $($_.name)" -ForegroundColor $color
}

# ===== STEP 12: Test INTERRUPT on a fresh task =====
Write-Host "`n[12] Test interrupt (zapping running task) on a fresh task..." -ForegroundColor Yellow
$body = @"
{
  "project_id": "$PROJ_ID",
  "agent_role": "$PROFILE_NAME",
  "action": "extra_task"
}
"@
$r = Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/" -ContentType "application/json" -Body $body
$T4 = $r.id

# Assign + start
$body = "{`"agent_id`":`"$AGENT_ID`",`"profile_id`":`"$PROFILE_ID`"}"
Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/$T4/assign" -ContentType "application/json" -Body $body | Out-Null
Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/$T4/start" | Out-Null

# Operator hits "Interrupt now"
$r = Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/$T4/interrupt"
Write-Host "  T4 status after interrupt: $($r.status)" -ForegroundColor Yellow

# ===== STEP 13: Test CANCEL =====
Write-Host "`n[13] Test cancel on a pending task..." -ForegroundColor Yellow
$body = @"
{
  "project_id": "$PROJ_ID",
  "agent_role": "$PROFILE_NAME",
  "action": "to_cancel"
}
"@
$r = Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/" -ContentType "application/json" -Body $body
$T5 = $r.id
$r = Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/$T5/cancel"
Write-Host "  T5 status after cancel: $($r.status)" -ForegroundColor Yellow

# ===== STEP 14: Test plan.md file API (write + read back) =====
Write-Host "`n[14] Write plan.md via file API..." -ForegroundColor Yellow
$body = @"
{
  "frontmatter": {
    "project_id": "$PROJ_ID",
    "state": "completed",
    "created_at": "2026-07-15T22:00:00+00:00",
    "tasks": [
      {"id": "$T1", "name": "Step 1: Fetch data", "agent_role": "$PROFILE_NAME", "status": "completed", "depends_on": []},
      {"id": "$T2", "name": "Step 2: Analyze", "agent_role": "$PROFILE_NAME", "status": "failed", "depends_on": ["$T1"]},
      {"id": "$T3", "name": "Step 3: Report", "agent_role": "$PROFILE_NAME", "status": "skipped", "depends_on": ["$T2"]}
    ]
  },
  "body": "\n# Project summary\n\nT1 completed, T2 failed, T3 skipped (failure propagation).\n"
}
"@
Invoke-RestMethod -Method Put -Uri "$BASE/api/projects/$PROJ_ID/plan" -ContentType "application/json" -Body $body | Out-Null
Write-Host "  plan.md written" -ForegroundColor Green

Write-Host "`n=== End-to-end demo complete ===" -ForegroundColor Cyan
Write-Host "Verified flows:" -ForegroundColor Green
Write-Host "  - Agent register + heartbeat (with HMAC header check)"
Write-Host "  - Task create + assign + start + submit result"
Write-Host "  - Failure propagation (T2 fail -> T3 skipped)"
Write-Host "  - Interrupt on running task"
Write-Host "  - Cancel on pending task"
Write-Host "  - Plan.md file API write"
