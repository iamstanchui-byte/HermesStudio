# test-tasks.ps1 - Test task endpoints (REVIEW.md §1, §4)
# Run after server is up: hermes-orch serve --reload

function J {
    param([Parameter(ValueFromPipeline=$true)] $o)
    process { $o | ConvertTo-Json -Depth 5 }
}

Write-Host "=== Hermes Orchestrator task API test ===" -ForegroundColor Cyan

# 0. Create a project first (needed for tasks)
Write-Host "`n[0] Create test project..." -ForegroundColor Yellow
$body = '{"goal":"Task lifecycle test","name":"TaskTest"}'
$r = Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/projects/" `
    -ContentType "application/json" -Body $body
$projId = $r.id
Write-Host "Project ID: $projId" -ForegroundColor Green

# 1. Create task t-001
Write-Host "`n[1] Create task t-001..." -ForegroundColor Yellow
$body = @"
{
  "project_id": "$projId",
  "name": "Backtest EURUSD",
  "agent_role": "backtest-runner",
  "action": "run_backtest",
  "params": {"symbol": "EURUSD", "timeframe": "M5"}
}
"@
$r = Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/tasks/" `
    -ContentType "application/json" -Body $body
$t1 = $r.id
J $r
Write-Host "Task 1: $t1" -ForegroundColor Green

# 2. Create task t-002 with depends_on t-001
Write-Host "`n[2] Create task t-002 (depends on t-001)..." -ForegroundColor Yellow
$body = @"
{
  "project_id": "$projId",
  "name": "Write report",
  "agent_role": "report-writer",
  "depends_on": ["$t1"],
  "on_parent_failure": "skip",
  "action": "write_report"
}
"@
$r = Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/tasks/" `
    -ContentType "application/json" -Body $body
$t2 = $r.id
J $r
Write-Host "Task 2: $t2" -ForegroundColor Green

# 3. List tasks
Write-Host "`n[3] List tasks for project..." -ForegroundColor Yellow
$r = Invoke-RestMethod -Uri "http://localhost:8765/api/tasks/?project_id=$projId"
J $r

# 4. Get one task
Write-Host "`n[4] Get task $t1..." -ForegroundColor Yellow
Invoke-RestMethod -Uri "http://localhost:8765/api/tasks/$t1" | J

# 5. Try to assign without agent -- should 404
Write-Host "`n[5] Assign to nonexistent agent (should 404)..." -ForegroundColor Yellow
try {
    $body = '{"agent_id":"nonexistent-agent"}'
    Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/tasks/$t1/assign" `
        -ContentType "application/json" -Body $body -ErrorAction Stop
    Write-Host "UNEXPECTED: no error" -ForegroundColor Red
} catch {
    Write-Host "Got: $($_.Exception.Response.StatusCode)  (expect 404)" -ForegroundColor Yellow
}

# 6. Try to start an unassigned task -- should 400
Write-Host "`n[6] Start unassigned task (should 400)..." -ForegroundColor Yellow
try {
    Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/tasks/$t1/start" -ErrorAction Stop
    Write-Host "UNEXPECTED: no error" -ForegroundColor Red
} catch {
    Write-Host "Got: $($_.Exception.Response.StatusCode)  (expect 400)" -ForegroundColor Yellow
}

# 7. Create a 3rd task and cancel it
Write-Host "`n[7] Create t-003 then cancel..." -ForegroundColor Yellow
$body = @"
{
  "project_id": "$projId",
  "agent_role": "data-analyst",
  "action": "do_something"
}
"@
$r = Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/tasks/" `
    -ContentType "application/json" -Body $body
$t3 = $r.id
Write-Host "Task 3: $t3" -ForegroundColor Green
Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/tasks/$t3/cancel" | J

# 8. Try to cancel a cancelled task -- should 400
Write-Host "`n[8] Cancel already-cancelled task (should 400)..." -ForegroundColor Yellow
try {
    Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/tasks/$t3/cancel" -ErrorAction Stop
    Write-Host "UNEXPECTED: no error" -ForegroundColor Red
} catch {
    Write-Host "Got: $($_.Exception.Response.StatusCode)  (expect 400)" -ForegroundColor Yellow
}

# 9. Try to interrupt pending task -- should 400 (only running can be interrupted)
Write-Host "`n[9] Interrupt pending task (should 400)..." -ForegroundColor Yellow
try {
    Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/tasks/$t1/interrupt" -ErrorAction Stop
    Write-Host "UNEXPECTED: no error" -ForegroundColor Red
} catch {
    Write-Host "Got: $($_.Exception.Response.StatusCode)  (expect 400)" -ForegroundColor Yellow
}

# 10. Create task with invalid parent -- should 400
Write-Host "`n[10] Create task with nonexistent parent (should 400)..." -ForegroundColor Yellow
try {
    $body = @"
{
  "project_id": "$projId",
  "agent_role": "data-analyst",
  "action": "x",
  "depends_on": ["t-nonexistent"]
}
"@
    Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/tasks/" `
        -ContentType "application/json" -Body $body -ErrorAction Stop
    Write-Host "UNEXPECTED: no error" -ForegroundColor Red
} catch {
    Write-Host "Got: $($_.Exception.Response.StatusCode)  (expect 400)" -ForegroundColor Yellow
}

Write-Host "`n=== Task API tests done ===" -ForegroundColor Cyan
Write-Host "Note: assign/start/poll/result/Interrupt all need a registered agent." -ForegroundColor Gray
Write-Host "      Those will be tested once agents are built." -ForegroundColor Gray
