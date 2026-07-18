# test-api.ps1 - Test the Hermes Orchestrator file API endpoints.
# Run from any directory; uses 127.0.0.1:8765.

function J {
    param([Parameter(ValueFromPipeline=$true)] $o)
    process { $o | ConvertTo-Json -Depth 5 }
}

Write-Host "=== Hermes Orchestrator API test ===" -ForegroundColor Cyan

# 1. Create project
Write-Host "`n[1] Create project..." -ForegroundColor Yellow
$body = '{"goal":"Backtest EURUSD Q3 + write report","name":"EURUSD Q3"}'
$r = Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/projects/" `
    -ContentType "application/json" -Body $body
J $r

$projId = $r.id
Write-Host "Project ID: $projId" -ForegroundColor Green

# 2. List projects
Write-Host "`n[2] List projects..." -ForegroundColor Yellow
Invoke-RestMethod -Uri "http://localhost:8765/api/projects/" | J

# 3. Get one project
Write-Host "`n[3] Get project $projId..." -ForegroundColor Yellow
Invoke-RestMethod -Uri "http://localhost:8765/api/projects/$projId" | J

# 4. Read default plan.md (auto-created on project init)
Write-Host "`n[4] Read default plan.md..." -ForegroundColor Yellow
Invoke-RestMethod -Uri "http://localhost:8765/api/projects/$projId/plan" | J

# 5. Write a file
Write-Host "`n[5] Write a file (agent notes)..." -ForegroundColor Yellow
$body = "Linux agent notes: EURUSD has high volatility on London open."
Invoke-RestMethod -Method Put `
    -Uri "http://localhost:8765/api/projects/$projId/files/agents/linux-a-01/notes.md" `
    -ContentType "text/plain" -Body $body | J

# 6. Read it back
Write-Host "`n[6] Read the file back..." -ForegroundColor Yellow
Invoke-RestMethod -Uri "http://localhost:8765/api/projects/$projId/files/agents/linux-a-01/notes.md"

# 7. Update plan with 3 tasks
Write-Host "`n[7] Update plan with 3 tasks..." -ForegroundColor Yellow
$planJson = @"
{
  "frontmatter": {
    "project_id": "$projId",
    "state": "running",
    "created_at": "2026-07-15T21:00:00+00:00",
    "tasks": [
      {"id": "t-001", "name": "Research EURUSD", "agent_role": "data-analyst", "status": "pending", "depends_on": []},
      {"id": "t-002", "name": "Run backtest", "agent_role": "backtest-runner", "status": "pending", "depends_on": ["t-001"]},
      {"id": "t-003", "name": "Write report", "agent_role": "report-writer", "status": "pending", "depends_on": ["t-002"]}
    ]
  },
  "body": "\n# Project\n\nGoal here.\n"
}
"@
Invoke-RestMethod -Method Put -Uri "http://localhost:8765/api/projects/$projId/plan" `
    -ContentType "application/json" -Body $planJson | J

# 8. Get plan back
Write-Host "`n[8] Get plan back (should show 3 tasks)..." -ForegroundColor Yellow
Invoke-RestMethod -Uri "http://localhost:8765/api/projects/$projId/plan" | J

# 9. Path traversal -- should return 400
Write-Host "`n[9] Path traversal test..." -ForegroundColor Yellow
try {
    $traversal = Invoke-RestMethod -Uri "http://localhost:8765/api/projects/$projId/files/..%2F..%2Fetc%2Fpasswd" -ErrorAction Stop
    Write-Host "UNEXPECTED: no error" -ForegroundColor Red
} catch {
    $code = $_.Exception.Response.StatusCode
    Write-Host "Got status: $code  (expect 400 BadRequest)" -ForegroundColor Yellow
}

# 10. Non-existent project -- should return 404
Write-Host "`n[10] Non-existent project -- should return 404..." -ForegroundColor Yellow
try {
    Invoke-RestMethod -Uri "http://localhost:8765/api/projects/proj-nonexistent" -ErrorAction Stop
    Write-Host "UNEXPECTED: no error" -ForegroundColor Red
} catch {
    $code = $_.Exception.Response.StatusCode
    Write-Host "Got status: $code  (expect 404 NotFound)" -ForegroundColor Yellow
}

Write-Host "`n=== All tests done ===" -ForegroundColor Cyan
