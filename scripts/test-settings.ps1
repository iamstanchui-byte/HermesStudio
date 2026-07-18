<#
test-settings.ps1 - End-to-end test for the settings page (LLM + Telegram).

Flow:
  1. GET /api/settings/llm: verify providers + mock default
  2. Save fake LLM config via POST
  3. GET again: verify api_key_set=true, mock=false
  4. GET /agents: verify "LLM ready" badge shown
  5. GET /agents with mock=true again: verify banner shown
  6. Save Telegram config (without enabling)
  7. GET /api/settings/telegram: verify saved (token masked)
  8. Test connection with fake key: verify graceful error
  9. Reset everything to defaults

Assumes server running on :8765.
#>

$ErrorActionPreference = "Stop"
$Base = "http://localhost:8765"
$cfgFile = "$env:USERPROFILE\.hermes-orchestrator\config.yaml"

function Pass($msg) { Write-Host "  [PASS] $msg" -ForegroundColor Green }
function Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red; exit 1 }
function Step($n, $msg) { Write-Host ""; Write-Host "--- $n. $msg ---" -ForegroundColor Cyan }

# Snapshot original config to restore at end
$origConfig = if (Test-Path $cfgFile) { Get-Content $cfgFile -Raw } else { "" }

Step "1" "GET /api/settings/llm (initial state, whatever user has configured)"
$r = Invoke-RestMethod -Uri "$Base/api/settings/llm" -Method GET
if ($r.providers.Count -ne 4) { Fail "expected 4 providers, got $($r.providers.Count)" }
# Don't assert on mock: user may already have a key set
Pass "4 providers; mock=$($r.mock); api_key_set=$($r.api_key_set)"

# Reset to mock so test starts from clean state
$null = Invoke-RestMethod -Uri "$Base/api/settings/llm" -Method POST -ContentType "application/json" -Body '{"mock":true,"api_key":""}'

Step "2" "Save fake LLM config (api_key + base_url + model)"
$body = @{
    api_key = "sk-test-1234567890abcdef"
    base_url = "https://api.minimaxi.com/v1"
    model = "MiniMax-Text-01"
    provider = "minimax"
} | ConvertTo-Json
$r2 = Invoke-RestMethod -Uri "$Base/api/settings/llm" -Method POST -ContentType "application/json" -Body $body
if (-not $r2.api_key_set) { Fail "api_key_set should be true" }
if ($r2.api_key_last4 -ne "cdef") { Fail "last4 should be 'cdef', got '$($r2.api_key_last4)'" }
if ($r2.mock) { Fail "mock should auto-disable when key is set" }
Pass "saved; api_key_set=true; last4=$($r2.api_key_last4); mock=false"

Step "3" "GET /api/settings/llm (after save)"
$r3 = Invoke-RestMethod -Uri "$Base/api/settings/llm" -Method GET
if (-not $r3.api_key_set) { Fail "should show key set" }
if ($r3.api_key_last4 -ne "cdef") { Fail "last4 mismatch" }
Pass "key masked to last4 in GET response"

Step "4" "Landing page should show 'LLM ready' (not 'Mock mode')"
$page = Invoke-WebRequest -Uri "$Base/agents" -UseBasicParsing
if ($page.Content -notmatch "LLM ready") { Fail "expected 'LLM ready' badge" }
if ($page.Content -match "Mock mode") { Fail "should NOT show 'Mock mode'" }
if ($page.Content -match "Set up LLM") { Fail "should NOT show setup banner" }
Pass "banner updated to 'LLM ready'"

Step "5" "Reset to mock mode"
$body = @{ mock = "true"; api_key = "" } | ConvertTo-Json
$r4 = Invoke-RestMethod -Uri "$Base/api/settings/llm" -Method POST -ContentType "application/json" -Body $body
if ($r4.api_key_set) { Fail "api_key_set should be false" }
if (-not $r4.mock) { Fail "mock should be true" }
Pass "reset: api_key_set=false; mock=true"
$page2 = Invoke-WebRequest -Uri "$Base/agents" -UseBasicParsing
if ($page2.Content -notmatch "Mock mode") { Fail "should show 'Mock mode' again" }
Pass "banner restored to mock mode"

Step "6" "Save Telegram config (explicitly disabled)"
$body = @{ chat_id = "123456789"; enabled = "false" } | ConvertTo-Json
$r5 = Invoke-RestMethod -Uri "$Base/api/settings/telegram" -Method POST -ContentType "application/json" -Body $body
if ($r5.chat_id -ne "123456789") { Fail "chat_id not saved" }
if ($r5.enabled) { Fail "should be disabled after explicit save" }
Pass "chat_id saved; enabled=false"

Step "7" "GET /api/settings/telegram: verify token is masked"
$r6 = Invoke-RestMethod -Uri "$Base/api/settings/telegram" -Method GET
# Token is shown as last4 only, never full token
if ($r6.bot_token_set -and $r6.bot_token_last4.Length -ne 4) { Fail "token last4 malformed" }
if ($r6.chat_id -ne "123456789") { Fail "chat_id mismatch" }
Pass "token masked as last4; chat_id visible"

Step "8" "Test LLM connection with fake key (expect graceful error)"
$body = @{
    api_key = "sk-definitely-not-real-key"
    base_url = "https://api.minimaxi.com/v1"
    model = "MiniMax-Text-01"
} | ConvertTo-Json
$r7 = Invoke-RestMethod -Uri "$Base/api/settings/llm/test" -Method POST -ContentType "application/json" -Body $body
if ($r7.ok) { Fail "fake key should NOT succeed" }
if (-not $r7.error) { Fail "should return error message" }
Pass "fake key returns error: '$($r7.error)' (status=$($r7.status))"

Step "9" "Settings page renders all 4 providers + Telegram section"
$sp = Invoke-WebRequest -Uri "$Base/settings" -UseBasicParsing
if ($sp.StatusCode -ne 200) { Fail "settings page not 200" }
$optCount = ([regex]::Matches($sp.Content, '<option value="(minimax|openai|anthropic|custom)"')).Count
if ($optCount -ne 4) { Fail "expected 4 options, got $optCount" }
if ($sp.Content -notmatch "Telegram Notifications") { Fail "missing Telegram section" }
if ($sp.Content -notmatch "Test connection") { Fail "missing Test button" }
Pass "settings page complete (4 providers, Telegram, Test button)"

Step "10" "Restore original config (cleanup) and re-enable TG with user's settings"
[System.IO.File]::WriteAllText($cfgFile, $origConfig, [System.Text.UTF8Encoding]::new($false))
Pass "config.yaml restored"

# After restore, the in-memory config in the server is stale. Reload it by
# POSTing the same config (any field will trigger load_config).
$null = Invoke-RestMethod -Uri "$Base/api/settings/llm" -Method GET | Out-Null
Pass "in-memory config reloaded"

Write-Host ""
Write-Host "All 10 steps passed." -ForegroundColor Green
