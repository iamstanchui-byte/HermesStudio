<#
test-supervisor.ps1 - End-to-end test for the supervisor (brain).

Flow:
  1. Create a project (state=planning, goal=backtest EURUSD)
  2. Wait 7s for supervisor to call mock planner
  3. Verify: 3 tasks created, project state=ready->running
  4. Verify: first task is running, others pending
  5. Mark first task as FAILED via SQL
  6. Wait 7s for failure propagation
  7. Verify: downstream tasks skipped
  8. Mark all remaining as completed
  9. Wait 7s
  10. Verify: project state=completed
  11. Verify: full audit log

Assumes:
  - server running with --reload
  - mock planner (no LLM key)
  - data-analyst + backtest-runner profiles registered
#>

$ErrorActionPreference = "Stop"
$Base = "http://localhost:8765"
$py = "C:\Project\minimax code\hermes-orchestrator\.venv\Scripts\python.exe"
$sqlHelper = "C:\Project\minimax code\hermes-orchestrator\scripts\_test-sql.py"

function Pass($msg) { Write-Host "  [PASS] $msg" -ForegroundColor Green }
function Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red; exit 1 }
function Step($n, $msg) { Write-Host ""; Write-Host "--- $n. $msg ---" -ForegroundColor Cyan }

function SqlQuery { param([string]$sql); & $py $sqlHelper query $sql }
function SqlExec  { param([string]$sql); & $py $sqlHelper exec $sql }

function WaitForState {
    param([string]$projId, [string]$expected, [int]$maxWaitSec = 25)
    $waited = 0
    while ($waited -lt $maxWaitSec) {
        $res = SqlQuery "SELECT state FROM projects WHERE id = '$projId'"
        if ($res -match "state': '$expected'") { return $true }
        Start-Sleep -Seconds 2
        $waited += 2
        Write-Host "    ... waiting for state=$expected (${waited}s)"
    }
    return $false
}

# Sanity: server up
$h = Invoke-RestMethod -Uri "$Base/api/health" -Method GET
if ($h.status -ne "ok") { Fail "server not healthy" }

Step "1" "Create a planning project with a backtest goal"
$body = @{
    name = "Supervisor Test " + (Get-Date -Format "HHmmss")
    goal = "Backtest EURUSD with M5"
    state = "planning"
} | ConvertTo-Json
$proj = Invoke-RestMethod -Uri "$Base/api/projects/" -Method POST -ContentType "application/json" -Body $body
if ($proj.state -ne "planning") { Fail "expected planning, got $($proj.state)" }
Pass "created $($proj.id)"
$projId = $proj.id

Step "2-3" "Wait for supervisor to drive project to running"
if (-not (WaitForState -projId $projId -expected "running")) { Fail "project never reached 'running'" }
Pass "state=running"

Step "4" "Verify 3 tasks, first one running"
$res = SqlQuery "SELECT id, status, name FROM tasks WHERE project_id = '$projId' ORDER BY created_at"
$taskLines = $res -split "`r?`n" | Where-Object { $_ -match "id" }
if ($taskLines.Count -lt 3) { Fail "expected 3+ tasks, got: $($taskLines.Count)`n$res" }
Pass "$($taskLines.Count) tasks created"

# Pull the first task id (use SQL to get the running one)
$firstTaskId = (& $py $sqlHelper query "SELECT id FROM tasks WHERE project_id = '$projId' AND status = 'running' ORDER BY created_at LIMIT 1")
if ($firstTaskId -notmatch "t-[0-9a-f]{8}") { Fail "no running task found: $firstTaskId" }
$firstTaskId = ($firstTaskId | Select-String -Pattern "t-[0-9a-f]{8}" | Select-Object -First 1).Matches.Value
Pass "first running task: $firstTaskId"

Step "5" "Mark first task as FAILED"
SqlExec "UPDATE tasks SET status = 'failed', error = 'simulated for test' WHERE id = '$firstTaskId'"
Pass "marked $firstTaskId as failed"

Step "6-7" "Wait for failure propagation (max 15s)"
$skipped = 0
for ($i = 0; $i -lt 8; $i++) {
    Start-Sleep -Seconds 2
    $res = SqlQuery "SELECT name, status FROM tasks WHERE project_id = '$projId' ORDER BY created_at"
    $skipped = ($res | Select-String -Pattern "'status': 'skipped'").Count
    if ($skipped -ge 1) { break }
}
if ($skipped -lt 1) { Fail "expected at least 1 skipped, got: $res" }
Pass "$skipped task(s) skipped (failure propagation works)"

Step "8" "Mark all non-failed tasks as completed"
SqlExec "UPDATE tasks SET status = 'completed' WHERE project_id = '$projId' AND status NOT IN ('failed','completed','cancelled')"
Pass "marked remaining as completed"

Step "9-10" "Wait for project to reach completed"
if (-not (WaitForState -projId $projId -expected "completed")) { Fail "project never reached 'completed'" }
Pass "state=completed"

Step "11" "Verify audit log has full lifecycle"
$res = SqlQuery "SELECT event_type FROM audit_log WHERE project_id = '$projId' ORDER BY id"
$resStr = $res -join "`n"
foreach ($e in @("project.created", "project.plan_generated", "project.started", "project.completed")) {
    if (-not $resStr.Contains($e)) { Fail "missing audit event '$e' in: $resStr" }
}
Pass "all key events present"

Write-Host ""
Write-Host "All 11 steps passed." -ForegroundColor Green
