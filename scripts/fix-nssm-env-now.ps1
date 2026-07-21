<#
fix-nssm-env-now.ps1 — Hot-fix for AppEnvironmentExtra on the
HermesOrchAgent NSSM service.

ROOT CAUSE
The v2 register script set AppEnvironmentExtra via three separate
`nssm set` calls. NSSM's `AppEnvironmentExtra` is a REG_MULTI_SZ
field; each `nssm set` REPLACES the whole value (last one wins), so
only the last value (`HOME=C:\Users\stanley`) survived. The hermes
wrapper daemon running under LocalSystem had no `LOCALAPPDATA`, so
its fallback `Path(local)/hermes/.../hermes.exe` resolved to
`C:\Windows\system32\config\systemprofile\AppData\Local\hermes\...`
(non-existent), and every task failed with
"hermes CLI not found".

USAGE
  1. Open PowerShell as Administrator (Win+X -> I, or right-click
     Start menu -> "Terminal (Admin)" / "PowerShell (Admin)").
  2. cd to the project:
       cd "C:\Project\minimax code\hermes-orchestrator"
  3. Run:
       powershell -ExecutionPolicy Bypass -File scripts\fix-nssm-env-now.ps1
  4. The script tries three write strategies in order, then
     verifies and restarts the service:
       1. `nssm set` (single call, three positional args)
       2. `Set-ItemProperty` (PowerShell-native REG_MULTI_SZ write)
       3. Direct .NET Registry.SetValue (escape-hatch)

The script stops at the first one that succeeds and reports
which one was used.

After this hot-fix is applied, the next win-agent01 task should
find hermes.exe at C:\Users\stanley\AppData\Local\hermes\
hermes-agent\venv\Scripts\hermes.exe and the 'hermes CLI not
found' error should be gone.
#>

$ErrorActionPreference = "Stop"
$ServiceName = "HermesOrchAgent"
$RegKeyPath  = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
$RegValueName = "AppEnvironmentExtra"

# Detect user paths
$UserHome = $env:USERPROFILE
if (-not $UserHome) { $UserHome = "C:\Users\stanley" }
$UserProfile      = $UserHome
$UserLocalAppData = Join-Path $UserHome "AppData\Local"

$envs = @(
    "LOCALAPPDATA=$UserLocalAppData",
    "USERPROFILE=$UserProfile",
    "HOME=$UserHome"
)

Write-Host "=== HermesOrchAgent NSSM service env fix ==="
Write-Host "Service:      $ServiceName"
Write-Host "USERPROFILE:  $UserProfile"
Write-Host "LOCALAPPDATA: $UserLocalAppData"
Write-Host "HOME:         $UserHome"
Write-Host "Reg path:     $RegKeyPath"
Write-Host "Reg value:    $RegValueName"
Write-Host ""

# Locate nssm.exe (best-effort; the .NET fallback works without it)
$nssmCmd = Get-Command nssm -ErrorAction SilentlyContinue
$nssm = if ($nssmCmd) { $nssmCmd.Source } else { "" }
if (-not $nssm -or -not (Test-Path $nssm)) {
    $nssm = "C:\Users\stanley\AppData\Local\Microsoft\WinGet\Links\nssm.exe"
}
if (-not (Test-Path $nssm)) {
    Write-Host "[skip] nssm.exe not found at $nssm (will use direct reg write)"
    $nssm = $null
}
if ($nssm) { Write-Host "nssm: $nssm" }
Write-Host ""

# Show current value
Write-Host "--- current (before) ---"
try {
    $cur = (Get-ItemProperty -Path $RegKeyPath -Name $RegValueName -ErrorAction Stop).$RegValueName
    $cur | ForEach-Object { Write-Host "  $_" }
} catch {
    Write-Host "  (value not present)"
}
Write-Host ""

# Try three write strategies
$writeOk = $false
$writeMethod = ""

# Strategy 1: nssm set
if ($nssm) {
    Write-Host "[1/3] nssm set (single call, three positional args)..."
    try {
        & $nssm set $ServiceName AppEnvironmentExtra $envs[0] $envs[1] $envs[2] 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $writeOk = $true
            $writeMethod = "nssm set (multi-arg)"
        } else {
            Write-Host "  nssm set returned exit code $LASTEXITCODE, trying next strategy"
        }
    } catch {
        Write-Host "  nssm set threw: $($_.Exception.Message), trying next strategy"
    }
}

# Strategy 2: Set-ItemProperty (PowerShell native REG_MULTI_SZ)
if (-not $writeOk) {
    Write-Host "[2/3] Set-ItemProperty (PowerShell REG_MULTI_SZ)..."
    try {
        Set-ItemProperty -Path $RegKeyPath -Name $RegValueName -Value $envs -Type MultiString
        $writeOk = $true
        $writeMethod = "Set-ItemProperty"
    } catch {
        Write-Host "  Set-ItemProperty threw: $($_.Exception.Message), trying next strategy"
    }
}

# Strategy 3: direct .NET
if (-not $writeOk) {
    Write-Host "[3/3] .NET Microsoft.Win32.Registry direct write..."
    try {
        $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey(
            "SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters",
            $true
        )
        if (-not $key) { throw "OpenSubKey returned null (path not found)" }
        $key.SetValue($RegValueName, $envs, [Microsoft.Win32.RegistryValueKind]::MultiString)
        $key.Close()
        $writeOk = $true
        $writeMethod = ".NET Registry"
    } catch {
        Write-Host "  .NET write threw: $($_.Exception.Message)"
    }
}

if (-not $writeOk) {
    throw "All three write strategies failed. Check that you ran this in an Admin PowerShell."
}

Write-Host ""
Write-Host "--- after (verify) ---"
$verified = (Get-ItemProperty -Path $RegKeyPath -Name $RegValueName -ErrorAction Stop).$RegValueName
$verified | ForEach-Object { Write-Host "  $_" }
$expectedCount = $envs.Count
$actualCount = @($verified).Count
if ($actualCount -ne $expectedCount) {
    throw "Verify failed: expected $expectedCount rows, got $actualCount. Manual regedit cleanup required."
}
Write-Host ""
Write-Host "[ok] wrote $actualCount rows via: $writeMethod"

# Restart the service so the daemon picks up the new env on next launch
Write-Host ""
$svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Write-Host "Restarting $ServiceName ..."
    Restart-Service $ServiceName
    Start-Sleep -Seconds 2
    Get-Service $ServiceName | Format-Table Name, Status -AutoSize
} else {
    Write-Host "Service is not running. Start it with: Start-Service $ServiceName"
}

Write-Host ""
Write-Host "=== Done. Re-test a task on win-agent01 to verify hermes is found. ==="
