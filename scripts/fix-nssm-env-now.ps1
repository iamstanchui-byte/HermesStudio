<#
fix-nssm-env-now.ps1 — Hot-fix for AppEnvironmentExtra on the
HermesOrchAgent NSSM service.

ROOT CAUSE
The v2 register script set AppEnvironmentExtra via three separate
`nssm set` calls. NSSM's `AppEnvironmentExtra` is a REG_MULTI_SZ
field; each `nssm set` REPLACES the whole value (not append), so
only the last value (`HOME=C:\Users\stanley`) survived. The hermes
wrapper daemon running under LocalSystem had no `LOCALAPPDATA`, so
its fallback `Path(local)/hermes/.../hermes.exe` resolved to
`C:\Windows\system32\config\systemprofile\AppData\Local\hermes\...`
(non-existent), and every task failed with
"hermes CLI not found".

FIX
Set AppEnvironmentExtra in a SINGLE `nssm set` call with all three
values as positional args — NSSM then writes them as a proper
REG_MULTI_SZ with three rows.

USAGE
  1. Open PowerShell as Administrator (Win+X → I, or right-click
     Start menu → "Terminal (Admin)" / "PowerShell (Admin)").
  2. cd to the project:
       cd "C:\Project\minimax code\hermes-orchestrator"
  3. Run:
       powershell -ExecutionPolicy Bypass -File scripts\fix-nssm-env-now.ps1
  4. (Re)start the service:
       Restart-Service HermesOrchAgent
  5. Verify the new project task can find hermes.

After this hot-fix is applied, the fix in scripts/register-windows-service-v2.ps1
ensures future re-registers do not regress.
#>

$ErrorActionPreference = "Stop"
$ServiceName = "HermesOrchAgent"

# Detect user paths
$UserHome        = $env:USERPROFILE
if (-not $UserHome) { $UserHome = (Get-CimInstance Win32_UserProfile | Where-Object { $_.LocalPath -like "*stanley*" } | Select-Object -First 1).LocalPath }
if (-not $UserHome) {
    $candidates = @("C:\Users\stanley", "$env:SystemDrive\Users\stanley")
    foreach ($c in $candidates) { if (Test-Path $c) { $UserHome = $c; break } }
}
if (-not $UserHome) { throw "Could not detect user home. Set $UserHome manually." }
$UserProfile      = $UserHome
$UserLocalAppData = Join-Path $UserHome "AppData\Local"

Write-Host "=== HermesOrchAgent NSSM service env fix ==="
Write-Host "Service:      $ServiceName"
Write-Host "USERPROFILE:  $UserProfile"
Write-Host "LOCALAPPDATA: $UserLocalAppData"
Write-Host "HOME:         $UserHome"
Write-Host ""

# Locate nssm.exe
$nssmCmd = Get-Command nssm -ErrorAction SilentlyContinue
$nssm = if ($nssmCmd) { $nssmCmd.Source } else { "" }
if (-not $nssm -or -not (Test-Path $nssm)) {
    $nssm = "C:\Users\stanley\AppData\Local\Microsoft\WinGet\Links\nssm.exe"
}
if (-not (Test-Path $nssm)) { throw "nssm.exe not found at $nssm" }
Write-Host "nssm: $nssm"
Write-Host ""

# Get current value for diff visibility
Write-Host "--- before ---"
& $nssm get $ServiceName AppEnvironmentExtra
Write-Host ""

# Set the env in ONE call with three positional args. NSSM writes
# them as a proper REG_MULTI_SZ with three rows.
Write-Host "--- setting (one nssm set call, three positional args) ---"
& $nssm set $ServiceName AppEnvironmentExtra `
    "LOCALAPPDATA=$UserLocalAppData" `
    "USERPROFILE=$UserProfile" `
    "HOME=$UserHome"
if ($LASTEXITCODE -ne 0) { throw "nssm set AppEnvironmentExtra failed: $LASTEXITCODE" }

Write-Host ""
Write-Host "--- after ---"
& $nssm get $ServiceName AppEnvironmentExtra

# Restart the service so the daemon picks up the new env on next launch
Write-Host ""
$svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Write-Host "Restarting $ServiceName ..."
    Restart-Service $ServiceName
    Start-Sleep -Seconds 2
    Get-Service $ServiceName | Format-Table Name, Status
} else {
    Write-Host "Service is not running. Start it with: Start-Service $ServiceName"
}

Write-Host ""
Write-Host "=== Done. Re-test a task on win-agent01 to verify hermes is found. ==="
