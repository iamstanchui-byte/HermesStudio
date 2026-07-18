<#
test-profile-configs.ps1 - End-to-end test for wrapper-mediated profile config (soul.md)

Flow:
  1. Submit a new desired config (soul.md) via POST /api/agents/.../configs (status=pending)
  2. Verify GET /configs returns it as pending
  3. Simulate wrapper claim: GET /configs/pending (status=applying)
  4. Verify second claim returns None (atomic claim worked)
  5. Wrapper ack applied: POST /configs/{id}/ack
  6. Verify config now status=applied
  7. Submit another, then run real wrapper (hermes-orch-agent apply-configs) to apply
  8. Verify file actually written to disk

Usage:
  & "C:\Project\minimax code\hermes-orchestrator\scripts\test-profile-configs.ps1"
#>

$ErrorActionPreference = "Stop"
$Base = "http://localhost:8765"
$AgentId = "linux-a-01"
$Profile = "data-analyst"
$WrapperRoot = Join-Path $env:USERPROFILE ".hermes-orchestrator\test-profiles\$Profile"
$TestFile = Join-Path $WrapperRoot "soul.md"

function Pass($msg) { Write-Host "  [PASS] $msg" -ForegroundColor Green }
function Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red; throw $msg }
function Step($n, $msg) { Write-Host ""; Write-Host "--- $n. $msg ---" -ForegroundColor Cyan }

# Reset previous test state
if (Test-Path $TestFile) { Remove-Item $TestFile -Force }
if (Test-Path (Join-Path $WrapperRoot "subdir")) { Remove-Item (Join-Path $WrapperRoot "subdir") -Recurse -Force }

# Clean up any leftover pending/applying configs from previous failed test runs
$py = "C:\Project\minimax code\hermes-orchestrator\.venv\Scripts\python.exe"
& $py "C:\Project\minimax code\hermes-orchestrator\scripts\_test-cleanup.py"


Step "1" "Submit new pending config"
$body = @{ file_path = "soul.md"; content = "# Test soul`nLine 2" } | ConvertTo-Json
$cfg = Invoke-RestMethod -Uri "$Base/api/agents/$AgentId/profiles/$Profile/configs" -Method POST -ContentType "application/json" -Body $body
if ($cfg.status -ne "pending") { Fail "expected pending, got $($cfg.status)" }
Pass "submitted id=$($cfg.id) status=pending"
$cfgId = $cfg.id

Step "2" "List configs (should include our pending one)"
$list = Invoke-RestMethod -Uri "$Base/api/agents/$AgentId/profiles/$Profile/configs" -Method GET
$found = $list | Where-Object { $_.id -eq $cfgId }
if (-not $found) { Fail "new config not in list" }
Pass "found in list (total $($list.Count) configs)"

Step "3" "Wrapper claim (GET /configs/pending)"
$claimed = Invoke-RestMethod -Uri "$Base/api/agents/$AgentId/profiles/$Profile/configs/pending" -Method GET
# PowerShell quirk: JSON null becomes string "null", not $null. Handle both.
if (($null -eq $claimed) -or ("$claimed" -eq "null")) { Fail "claim returned null but config was pending" }
if ($claimed.id -ne $cfgId) { Fail "claim returned wrong config id" }
if ($claimed.status -ne "applying") { Fail "expected applying, got $($claimed.status)" }
Pass "claimed; status=applying"

Step "4" "Second claim returns null (atomic)"
$claimed2 = Invoke-RestMethod -Uri "$Base/api/agents/$AgentId/profiles/$Profile/configs/pending" -Method GET
if (($null -ne $claimed2) -and ("$claimed2" -ne "null") -and $claimed2.id) {
    Fail "second claim returned a config (not atomic): $claimed2"
}
Pass "no double-claim (got null)"

Step "5" "Ack as applied"
$ackBody = @{ status = "applied"; actual_sha256 = $cfg.desired_sha256 } | ConvertTo-Json
$result = Invoke-RestMethod -Uri "$Base/api/agents/$AgentId/profiles/$Profile/configs/$cfgId/ack" -Method POST -ContentType "application/json" -Body $ackBody
if ($result.status -ne "applied") { Fail "expected applied, got $($result.status)" }
Pass "ack applied"

Step "6" "List again - status applied"
$list2 = Invoke-RestMethod -Uri "$Base/api/agents/$AgentId/profiles/$Profile/configs" -Method GET
$found2 = $list2 | Where-Object { $_.id -eq $cfgId }
if ($found2.status -ne "applied") { Fail "expected applied in list" }
Pass "list shows applied"

Step "7" "Submit another, then run real wrapper apply-configs"
$body2 = @{ file_path = "soul.md"; content = "# Real wrapper e2e`nTimestamp: $(Get-Date -Format o)" } | ConvertTo-Json
$cfg2 = Invoke-RestMethod -Uri "$Base/api/agents/$AgentId/profiles/$Profile/configs" -Method POST -ContentType "application/json" -Body $body2
Pass "submitted id=$($cfg2.id)"

# Write a test-local wrapper-config so we don't depend on user's wrapper-config.json
$testCfgFile = Join-Path $env:USERPROFILE ".hermes-orchestrator\test-wrapper-config.json"
$secretFile = Join-Path $env:USERPROFILE ".hermes-orchestrator\.secret-linux-a-01"
$testCfg = @{
    agent_id = $AgentId
    orchestrator_url = $Base
    secret_file = $secretFile
    profiles = @{ $Profile = @{ root = $WrapperRoot } }
} | ConvertTo-Json
[System.IO.File]::WriteAllText($testCfgFile, $testCfg, [System.Text.UTF8Encoding]::new($false))

Write-Host "  running: hermes-orch-agent apply-configs"
$py = "C:\Project\minimax code\hermes-orchestrator\.venv\Scripts\python.exe"
$proc = Start-Process -FilePath $py -ArgumentList @("-m", "hermes_orch.agent_cli", "apply-configs", "--config", $testCfgFile) -WorkingDirectory "C:\Project\minimax code\hermes-orchestrator" -Wait -PassThru -NoNewWindow -RedirectStandardOutput "stdout.txt" -RedirectStandardError "stderr.txt"
if ($proc.ExitCode -ne 0) {
    Write-Host "STDOUT:"; Get-Content "stdout.txt"
    Write-Host "STDERR:"; Get-Content "stderr.txt"
    Fail "wrapper exited $($proc.ExitCode)"
}
Get-Content "stdout.txt" | Where-Object { $_ -match "wrote|applied|done" } | ForEach-Object { Write-Host "    $_" }
Remove-Item "stdout.txt","stderr.txt" -ErrorAction SilentlyContinue
Remove-Item $testCfgFile -ErrorAction SilentlyContinue

Step "8" "Verify file was actually written"
if (-not (Test-Path $TestFile)) { Fail "file not at $TestFile" }
$content = Get-Content $TestFile -Raw
if ($content -notmatch "Real wrapper e2e") { Fail "file content does not match submitted" }
Pass "file written; content matches"
Write-Host "  $TestFile contents:"
Get-Content $TestFile | ForEach-Object { Write-Host "    | $_" }

Step "9" "Verify DB config status now applied"
$list3 = Invoke-RestMethod -Uri "$Base/api/agents/$AgentId/profiles/$Profile/configs" -Method GET
$found3 = $list3 | Where-Object { $_.id -eq $cfg2.id }
if ($found3.status -ne "applied") { Fail "expected applied, got $($found3.status)" }
Pass "wrapper ack worked; config status=applied"

Step "10" "Dashboard page loads with soul editor"
$page = Invoke-WebRequest -Uri "$Base/agents" -UseBasicParsing
if ($page.StatusCode -ne 200) { Fail "dashboard returned $($page.StatusCode)" }
if ($page.Content -notmatch "soul-editor-") { Fail "soul editor HTML not in page" }
Pass "dashboard renders soul editor"

Step "11" "Audit log has profile.* events"
$auditCount = & $py "C:\Project\minimax code\hermes-orchestrator\scripts\_test-audit-count.py"
if ([int]$auditCount -lt 5) { Fail "expected >=5 profile.* audit events, got $auditCount" }
Pass "audit log has $auditCount profile.* events"

Write-Host ""
Write-Host "All 11 steps passed." -ForegroundColor Green
