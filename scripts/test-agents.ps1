# test-agents.ps1 - Test agent endpoints (REVIEW.md §6, §6.4 multi-role Model A)

function J {
    param([Parameter(ValueFromPipeline=$true)] $o)
    process { $o | ConvertTo-Json -Depth 5 }
}

Write-Host "=== Hermes Orchestrator agent API test ===" -ForegroundColor Cyan

# 1. Register agent with 3 roles
Write-Host "`n[1] Register agent 'linux-a-01' with 3 roles..." -ForegroundColor Yellow
$body = @"
{
  "agent_id": "linux-a-01",
  "ip": "192.168.1.30",
  "os_type": "linux",
  "roles": ["data-analyst", "backtest-runner", "report-writer"]
}
"@
$r = Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/agents/" `
    -ContentType "application/json" -Body $body
J $r
$agentId = $r.agent.id
Write-Host "Agent ID: $agentId" -ForegroundColor Green
Write-Host "Setup secret: $($r.setup_secret)" -ForegroundColor Yellow

# 2. Try to register same agent again -- should 409
Write-Host "`n[2] Register same agent again (should 409)..." -ForegroundColor Yellow
try {
    $body2 = '{"agent_id":"linux-a-01","roles":[]}'
    Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/agents/" `
        -ContentType "application/json" -Body $body2 -ErrorAction Stop
    Write-Host "UNEXPECTED: no error" -ForegroundColor Red
} catch {
    Write-Host "Got: $($_.Exception.Response.StatusCode)  (expect 409)" -ForegroundColor Yellow
}

# 3. List agents
Write-Host "`n[3] List agents..." -ForegroundColor Yellow
Invoke-RestMethod -Uri "http://localhost:8765/api/agents/" | J

# 4. Get one agent
Write-Host "`n[4] Get agent $agentId..." -ForegroundColor Yellow
Invoke-RestMethod -Uri "http://localhost:8765/api/agents/$agentId" | J

# 5. Update agent metadata (change IP)
Write-Host "`n[5] Update agent IP..." -ForegroundColor Yellow
$body = '{"ip":"192.168.1.31"}'
Invoke-RestMethod -Method Put -Uri "http://localhost:8765/api/agents/$agentId" `
    -ContentType "application/json" -Body $body | J

# 6. Add new profile
Write-Host "`n[6] Add profile 'mt5-automation'..." -ForegroundColor Yellow
$body = '{"name":"mt5-automation","description":"MT5 trading automation"}'
$r = Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/agents/$agentId/profiles" `
    -ContentType "application/json" -Body $body
J $r

# 7. List agents again -- should show 4 profiles now
Write-Host "`n[7] List agents (should show 4 profiles)..." -ForegroundColor Yellow
$r = Invoke-RestMethod -Uri "http://localhost:8765/api/agents/$agentId"
Write-Host "Profile count: $($r.profiles.Count)" -ForegroundColor Green
J $r.profiles

# 8. Update profile description
Write-Host "`n[8] Update profile 'mt5-automation' description..." -ForegroundColor Yellow
$body = '{"description":"MT5 EURUSD chart screenshot + order management"}'
Invoke-RestMethod -Method Patch -Uri "http://localhost:8765/api/agents/$agentId/profiles/mt5-automation" `
    -ContentType "application/json" -Body $body | J

# 9. Try to add duplicate profile -- should 409
Write-Host "`n[9] Add duplicate profile (should 409)..." -ForegroundColor Yellow
try {
    $body = '{"name":"data-analyst"}'
    Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/agents/$agentId/profiles" `
        -ContentType "application/json" -Body $body -ErrorAction Stop
    Write-Host "UNEXPECTED: no error" -ForegroundColor Red
} catch {
    Write-Host "Got: $($_.Exception.Response.StatusCode)  (expect 409)" -ForegroundColor Yellow
}

# 10. Heartbeat without headers -- should 401
Write-Host "`n[10] Heartbeat without headers (should 401)..." -ForegroundColor Yellow
try {
    Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/agents/$agentId/heartbeat" -ErrorAction Stop
    Write-Host "UNEXPECTED: no error" -ForegroundColor Red
} catch {
    Write-Host "Got: $($_.Exception.Response.StatusCode)  (expect 401)" -ForegroundColor Yellow
}

# 11. Heartbeat with headers -- should 200 + return empty tasks list
Write-Host "`n[11] Heartbeat with proper headers..." -ForegroundColor Yellow
$headers = @{
    "X-Agent-Id"   = $agentId
    "X-Timestamp"  = (Get-Date -Format "o")
    "X-Signature"  = "fake-sig-for-test"
}
$r = Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/agents/$agentId/heartbeat" `
    -Headers $headers
J $r
Write-Host "Tasks returned: $($r.tasks.Count)" -ForegroundColor Green

# 12. Rotate key
Write-Host "`n[12] Rotate agent key..." -ForegroundColor Yellow
$r = Invoke-RestMethod -Method Post -Uri "http://localhost:8765/api/agents/$agentId/rotate-key"
J $r

# 13. Remove a profile
Write-Host "`n[13] Remove profile 'report-writer'..." -ForegroundColor Yellow
try {
    Invoke-RestMethod -Method Delete -Uri "http://localhost:8765/api/agents/$agentId/profiles/report-writer" -ErrorAction Stop
    Write-Host "OK (status 204)" -ForegroundColor Green
} catch {
    Write-Host "Status: $($_.Exception.Response.StatusCode)" -ForegroundColor Yellow
}

# 14. Verify profile removed
Write-Host "`n[14] Verify profile removed (should be 3 profiles)..." -ForegroundColor Yellow
$r = Invoke-RestMethod -Uri "http://localhost:8765/api/agents/$agentId"
Write-Host "Profile count: $($r.profiles.Count) (expect 3)" -ForegroundColor Green

# 15. Try to remove nonexistent profile -- should 404
Write-Host "`n[15] Remove nonexistent profile (should 404)..." -ForegroundColor Yellow
try {
    Invoke-RestMethod -Method Delete -Uri "http://localhost:8765/api/agents/$agentId/profiles/nonexistent" -ErrorAction Stop
    Write-Host "UNEXPECTED: no error" -ForegroundColor Red
} catch {
    Write-Host "Got: $($_.Exception.Response.StatusCode)  (expect 404)" -ForegroundColor Yellow
}

Write-Host "`n=== Agent API tests done ===" -ForegroundColor Cyan
