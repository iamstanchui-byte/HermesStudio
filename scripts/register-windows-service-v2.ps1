# register-windows-service-v2.ps1 - Register hermes-orch-agent as a Windows Service via NSSM.
#
# Replaces register-windows-service.ps1 (which ran as LocalSystem and
# failed because LocalSystem's HOME is C:\Windows\System32\config\systemprofile,
# not the user's HOME, so hermes couldn't find ~/.hermes/profiles).
#
# This v2 sets AppEnvironmentExtra on the NSSM service to inject
# LOCALAPPDATA, USERPROFILE, and HOME pointing to the user's actual
# paths, so hermes can find the profile registry.
#
# Why this is better than the .bat-in-shell:startup approach:
#   - No console window to accidentally close
#   - Auto-start at boot (no need for user logon)
#   - Auto-restart on crash (NSSM's Restart=always)
#   - Manageable via services.msc or nssm CLI
#   - Doesn't depend on user's logon session
#
# Run (as Administrator):
#   .\register-windows-service-v2.ps1
#
# To uninstall:
#   .\register-windows-service-v2.ps1 -Uninstall
#   OR: nssm stop HermesOrchAgent && nssm remove HermesOrchAgent confirm

# IMPORTANT: `param` MUST be the first executable statement. Self-elevate
# pattern is placed AFTER param so PowerShell doesn't choke on the
# out-of-order syntax. (The elevation block checks IsInRole and exits
# cleanly if not admin; the relaunched process will have admin and
# re-run from the top, hitting the install path.)

param(
    [switch]$Uninstall
)

# Self-elevate to admin if not already. Standard UAC pattern.
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ""
    Write-Host "===========================================================" -ForegroundColor Yellow
    Write-Host "  Need Administrator privileges to install Windows Service" -ForegroundColor Yellow
    Write-Host "===========================================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Three options to run this as admin:" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  1. EASIEST:  Press Win+X, then press I (opens Admin PowerShell)" -ForegroundColor White
    Write-Host "             Then paste:  & '$($MyInvocation.MyCommand.Path)'"
    Write-Host ""
    Write-Host "  2. A UAC dialog may pop up below. If it does, click YES." -ForegroundColor White
    Write-Host "             If the dialog disappears without effect, use option 1." -ForegroundColor DarkYellow
    Write-Host ""
    Write-Host "  3. Run from an already-elevated PowerShell session." -ForegroundColor White
    Write-Host ""
    Write-Host "===========================================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "[elevate] Attempting self-elevation (UAC may appear)..."

    $script = $MyInvocation.MyCommand.Path
    $psArgs = @("-ExecutionPolicy", "Bypass", "-File", "`"$script`"")
    if ($Uninstall) { $psArgs += "-Uninstall" }
    try {
        Start-Process -FilePath "powershell.exe" -ArgumentList $psArgs -Verb RunAs -Wait -ErrorAction Stop
    } catch {
        Write-Host ""
        Write-Host "[!] Self-elevation failed: $_" -ForegroundColor Red
        Write-Host "[!] Please use option 1 above (Win+X, then I)" -ForegroundColor Red
        exit 1
    }
    exit 0
}

# Don't use ErrorActionPreference=Stop here — individual nssm failures
# (e.g. nssm set on a non-existent param) should not abort the whole
# install. The install is idempotent: re-running it fixes partial state.
$ErrorActionPreference = "Continue"

$ServiceName = "HermesOrchAgent"
$ServiceDisplayName = "Hermes Orchestrator Agent Wrapper"
$ServiceDescription = "Hermes agent daemon - heartbeats to orchestrator, claims + runs tasks via hermes CLI"

# ----- Find hermes-orch-agent.exe in the project venv -----
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$VenvScripts = Join-Path $ProjectRoot ".venv\Scripts\hermes-orch-agent.exe"
$AgentExe = (Resolve-Path $VenvScripts -ErrorAction SilentlyContinue).Path
if (-not $AgentExe -or -not (Test-Path $AgentExe)) {
    Write-Host "[!] hermes-orch-agent.exe not found at $VenvScripts" -ForegroundColor Red
    Write-Host "    Run 'pip install -e .' in the project venv first." -ForegroundColor Yellow
    exit 1
}
Write-Host "[+] Found agent binary: $AgentExe"

# ----- Find NSSM -----
$NssmPath = (Get-Command "nssm" -ErrorAction SilentlyContinue).Source
if (-not $NssmPath) {
    Write-Host "[+] NSSM not found in PATH. Installing via winget..."
    winget install --id=nssm --accept-package-agreements --accept-source-agreements | Out-Null
    $NssmPath = (Get-Command "nssm" -ErrorAction SilentlyContinue).Source
    if (-not $NssmPath) {
        Write-Host "[!] NSSM install failed. Install manually: winget install nssm" -ForegroundColor Red
        exit 1
    }
}
Write-Host "[+] Found NSSM: $NssmPath"

# ----- Resolve user paths (used for env var injection) -----
$UserLocalAppData = $env:LOCALAPPDATA
$UserProfile = $env:USERPROFILE
$UserHome = $UserProfile  # On Windows, ~ expands to $HOME which defaults to $USERPROFILE
$ConfigPath = Join-Path $UserProfile ".hermes-orchestrator\wrapper-config.json"
$LogOut = Join-Path $UserProfile ".hermes-orchestrator\daemon.out.log"
$LogErr = Join-Path $UserProfile ".hermes-orchestrator\daemon.err.log"

if (-not (Test-Path $ConfigPath)) {
    Write-Host "[!] wrapper-config.json not found at $ConfigPath" -ForegroundColor Red
    Write-Host "    Run 'hermes-orch-agent register' first." -ForegroundColor Yellow
    exit 1
}

# ----- Uninstall path -----
if ($Uninstall) {
    Write-Host "[uninstall] Removing NSSM service..."
    & nssm stop $ServiceName 2>&1 | Out-Null
    Start-Sleep -Seconds 2
    & nssm remove $ServiceName confirm 2>&1 | Out-Null
    Write-Host "[uninstall] Done. Removed service: $ServiceName"
    Write-Host ""
    Write-Host "  Note: the .bat in shell:startup is NOT removed by this script."
    Write-Host "  Run:  Remove-Item \"$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\hermes-orch-agent.bat\""
    exit 0
}

# ----- Install / reconfigure service -----
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[+] Service $ServiceName already exists. Reconfiguring..."
    # Only try to stop if it's running. nssm stop on a Stopped service
    # returns non-zero ("The service has not been started") which under
    # ErrorActionPreference=Stop would abort the script. We tolerate that
    # case here because reconfig can happen with the service stopped.
    if ($existing.Status -eq "Running") {
        Write-Host "    Stopping..."
        & nssm stop $ServiceName 2>&1 | Out-Null
        Start-Sleep -Seconds 2
    } else {
        Write-Host "    (currently $($existing.Status), skipping stop)"
    }
} else {
    Write-Host "[+] Installing NSSM service: $ServiceName"
    & nssm install $ServiceName $AgentExe "start --config `"$ConfigPath`" --interval 5"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[!] nssm install failed (rc=$LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
}

& nssm set $ServiceName DisplayName $ServiceDisplayName
& nssm set $ServiceName Description $ServiceDescription
& nssm set $ServiceName Start SERVICE_AUTO_START
& nssm set $ServiceName AppDirectory $ProjectRoot

# Key: inject env vars so the LocalSystem daemon sees the user's HOME/LOCALAPPDATA.
# Without these, hermes looks for profiles at LocalSystem's HOME and can't
# find them. With them, hermes finds the user's profile registry.
#
# IMPORTANT: AppEnvironmentExtra is a REG_MULTI_SZ value. Calling `nssm set
# X AppEnvironmentExtra "k=v"` three times REPLACES the whole value each
# time (last call wins). To get all three rows, pass all three as
# positional args to a SINGLE nssm set call. The v2 script previously
# used three separate calls and the user hit "hermes CLI not found"
# on every task because only HOME survived.
& nssm set $ServiceName AppEnvironmentExtra `
    "LOCALAPPDATA=$UserLocalAppData" `
    "USERPROFILE=$UserProfile" `
    "HOME=$UserHome"

# Log rotation (10 MB per file, keep history)
& nssm set $ServiceName AppStdout $LogOut
& nssm set $ServiceName AppStderr $LogErr
& nssm set $ServiceName AppRotateFiles 1
& nssm set $ServiceName AppRotateBytes 10485760

# Restart on failure: 5s delay.
# NSSM AppExit syntax:
#   nssm set <service> AppExit Default <action>     <- action for any non-zero exit
# Actions: Restart | Exit | Ignore
# The restart delay is a SEPARATE parameter (AppRestartDelay), NOT a 3rd arg.
# Ref: https://nssm.cc/commands
& nssm set $ServiceName AppExit Default Restart
& nssm set $ServiceName AppRestartDelay 5000

# ----- Start the service -----
Write-Host "[+] Starting service..."
& nssm start $ServiceName
Start-Sleep -Seconds 4

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$proc = Get-CimInstance Win32_Process -Filter "Name='hermes-orch-agent.exe'"
if ($svc -and $svc.Status -eq "Running" -and $proc) {
    Write-Host ""
    Write-Host "[+] Service started successfully!" -ForegroundColor Green
    Write-Host "    Name:    $ServiceName" -ForegroundColor Green
    Write-Host "    PID:     $($proc.ProcessId)" -ForegroundColor Green
    Write-Host "    Status:  $($svc.Status)" -ForegroundColor Green
    Write-Host "    Logs:    $LogOut" -ForegroundColor Green
} else {
    Write-Host "[!] Service did not start cleanly. Check $LogErr" -ForegroundColor Yellow
    exit 1
}

# ----- Clean up old approaches -----
Write-Host ""
Write-Host "[+] Cleaning up old approaches..."

# Kill any running daemon NOT under NSSM (Start-Process ones, etc.)
$runningProcs = Get-CimInstance Win32_Process -Filter "Name='hermes-orch-agent.exe'"
foreach ($p in $runningProcs) {
    if ($p.ProcessId -ne $proc.ProcessId) {
        Write-Host "  - killing old daemon PID $($p.ProcessId) (not NSSM-managed)"
        taskkill /F /T /PID $p.ProcessId 2>&1 | Out-Null
    }
}

# Remove old .bat from shell:startup (replace with NSSM)
$batPath = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\hermes-orch-agent.bat"
if (Test-Path $batPath) {
    Write-Host "  - removing legacy startup .bat: $batPath"
    Remove-Item $batPath -Force
    Write-Host "  (NSSM service handles auto-start now)" -ForegroundColor Gray
}

# Optionally remove deprecated NSSM v1 service (had wrong name 'hermes-orch-agent')
$oldSvc = Get-Service -Name "hermes-orch-agent" -ErrorAction SilentlyContinue
if ($oldSvc) {
    Write-Host "  - removing old NSSM service (deprecated name 'hermes-orch-agent')"
    & nssm stop "hermes-orch-agent" 2>&1 | Out-Null
    Start-Sleep -Seconds 2
    & nssm remove "hermes-orch-agent" confirm 2>&1 | Out-Null
}

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Cyan
Write-Host "  Manage via: services.msc (GUI) or 'nssm start/stop/restart $ServiceName' (CLI)"
Write-Host "  Logs:       Get-Content '$LogOut' -Wait"
Write-Host "  Uninstall:  .\register-windows-service-v2.ps1 -Uninstall"
