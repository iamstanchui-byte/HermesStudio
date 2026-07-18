# register-windows-service.ps1 - DEPRECATED. Use register-startup-shortcut.ps1 instead.
#
# Why deprecated: NSSM runs the daemon as LocalSystem, but hermes looks for
# its profile registry in the user's HOME (~/.hermes/profiles). LocalSystem
# has a different HOME (C:\Windows\System32\config\systemprofile), so
# hermes fails with "Profile 'win-agent01' does not exist" even when the
# profile is properly registered for the user.
#
# The user-mode startup approach (register-startup-shortcut.ps1) places a
# .bat in shell:startup that runs the daemon as the current user at logon.
# No admin needed, no env var hacks, hermes finds its profile registry.
#
# This script is kept for reference but will redirect you to the new one.
Write-Host ""
Write-Host "[!] DEPRECATED: register-windows-service.ps1 (NSSM + LocalSystem) is no longer recommended." -ForegroundColor Yellow
Write-Host "    hermes needs the user's HOME to find its profile registry, but LocalSystem has a different HOME." -ForegroundColor Yellow
Write-Host ""
Write-Host "    Use the new user-mode approach instead:" -ForegroundColor Cyan
Write-Host "      .\register-startup-shortcut.ps1" -ForegroundColor White
Write-Host ""
Write-Host "    Auto-starts at logon (no admin, no password, runs as current user)." -ForegroundColor Gray
Write-Host ""
Write-Host "    To remove an existing NSSM service, you can still use this script" -ForegroundColor Gray
Write-Host "    but comment out the install block at the bottom." -ForegroundColor Gray
exit 0

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$ServiceName = "hermes-orch-agent"
$ServiceDisplayName = "Hermes Orchestrator Agent Wrapper"
$ServiceDescription = "Hermes agent daemon - heartbeats to orchestrator, claims + runs tasks via hermes CLI"

# ----- Find hermes-orch-agent.exe in the project venv -----
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# Project root is one level up from scripts/
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$VenvScripts = Join-Path $ProjectRoot ".venv\Scripts\hermes-orch-agent.exe"
$AgentExe = (Resolve-Path $VenvScripts -ErrorAction SilentlyContinue).Path
if (-not $AgentExe -or -not (Test-Path $AgentExe)) {
    Write-Error "hermes-orch-agent.exe not found at $VenvScripts. Run 'pip install -e .' in the project venv first."
    exit 1
}
Write-Host "[+] Found agent binary: $AgentExe"

# ----- Find / install NSSM -----
$NssmPath = (Get-Command "nssm" -ErrorAction SilentlyContinue).Source
if (-not $NssmPath) {
    Write-Host "[+] NSSM not found in PATH. Installing via winget..."
    winget install --id NSSM.NSSM --accept-source-agreements --accept-package-agreements 2>&1 | Out-Host
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $NssmPath = (Get-Command "nssm" -ErrorAction SilentlyContinue).Source
    if (-not $NssmPath) {
        Write-Error "NSSM install succeeded but 'nssm' not in PATH. Try: \$env:Path += ';C:\Program Files\nssm-2.24\win64'"
        exit 1
    }
}
Write-Host "[+] NSSM: $NssmPath"

# ----- Find wrapper-config.json -----
$ConfigPath = Join-Path $env:USERPROFILE ".hermes-orchestrator\wrapper-config.json"
if (-not (Test-Path $ConfigPath)) {
    Write-Error "wrapper-config.json not found at $ConfigPath. Run 'hermes-orch-agent register' first."
    exit 1
}
Write-Host "[+] Config: $ConfigPath"

# ----- Stop existing service if running -----
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[+] Stopping existing service..."
    & nssm stop $ServiceName 2>&1 | Out-Null
    Start-Sleep -Seconds 2
}

# ----- Install service -----
Write-Host "[+] Installing service '$ServiceName'..."
& nssm install $ServiceName $AgentExe "start" "--config" $ConfigPath "--interval" "5" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "nssm install failed"; exit 1 }

# Service metadata
& nssm set $ServiceName DisplayName $ServiceDisplayName 2>&1 | Out-Null
& nssm set $ServiceName Description $ServiceDescription 2>&1 | Out-Null
& nssm set $ServiceName AppDirectory $env:USERPROFILE\.hermes-orchestrator 2>&1 | Out-Null
& nssm set $ServiceName AppStdout "$env:USERPROFILE\.hermes-orchestrator\daemon.out.log" 2>&1 | Out-Null
& nssm set $ServiceName AppStderr "$env:USERPROFILE\.hermes-orchestrator\daemon.err.log" 2>&1 | Out-Null
& nssm set $ServiceName AppRotateFiles 1 2>&1 | Out-Null
& nssm set $ServiceName AppRotateBytes 10485760 2>&1 | Out-Null  # 10MB

# Environment: tell the daemon where the user's hermes CLI lives.
# LocalSystem account has a different HOME/PATH so detection fails.
# HERMES_BIN is the highest-priority lookup in _resolve_hermes_bin().
$HermesExe = Join-Path $env:LOCALAPPDATA "hermes\hermes-agent\venv\Scripts\hermes.exe"
if (Test-Path $HermesExe) {
    & nssm set $ServiceName AppEnvironmentExtra "HERMES_BIN=$HermesExe" 2>&1 | Out-Null
    Write-Host "[+] HERMES_BIN=$HermesExe"
} else {
    Write-Host "[!] WARN: hermes.exe not found at $HermesExe - service will fail to run tasks" -ForegroundColor Yellow
    Write-Host "    Set HERMES_BIN manually: nssm set hermes-orch-agent AppEnvironmentExtra HERMES_BIN=C:\path\to\hermes.exe"
}

# Restart policy: on failure, restart up to 10 times within 5 min
& nssm set $ServiceName ExitActions Rotate 2>&1 | Out-Null
& nssm set $ServiceName AppExitDelay 5000 2>&1 | Out-Null  # 5s grace before forced kill
& nssm set $ServiceName AppRestartDelay 10000 2>&1 | Out-Null  # 10s between restarts
& nssm set $ServiceName AppThrottle 60000 2>&1 | Out-Null  # restart at most once per 60s

# Start automatically on boot
& nssm set $ServiceName Start SERVICE_AUTO_START 2>&1 | Out-Null

Write-Host "[+] Service configured."

# ----- Start service -----
Write-Host "[+] Starting service..."
& nssm start $ServiceName 2>&1 | Out-Null
Start-Sleep -Seconds 4

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Write-Host "[+] Service is RUNNING (PID will appear in NSSM's console or Task Manager)" -ForegroundColor Green
} else {
    Write-Host "[!] Service status: $($svc.Status). Check logs:" -ForegroundColor Yellow
    Write-Host "    Get-Content $env:USERPROFILE\.hermes-orchestrator\daemon.out.log -Tail 30"
}

Write-Host ""
Write-Host "=== Service management ===" -ForegroundColor Cyan
Write-Host "  Status:    Get-Service hermes-orch-agent"
Write-Host "  Start:     nssm start hermes-orch-agent"
Write-Host "  Stop:      nssm stop hermes-orch-agent"
Write-Host "  Edit:      nssm edit hermes-orch-agent"
Write-Host "  Uninstall: nssm stop hermes-orch-agent ; nssm remove hermes-orch-agent confirm"
Write-Host "  Logs:      Get-Content \$env:USERPROFILE\.hermes-orchestrator\daemon.out.log -Wait"
