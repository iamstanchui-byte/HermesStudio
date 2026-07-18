# register-startup-shortcut.ps1 - Auto-start hermes-orch-agent at user logon.
#
# Approach: create a .bat in shell:startup so the daemon launches every time
# the user logs in. No admin needed, no password needed, runs as the user
# (so it sees stanley's HOME/hermes profiles, no env var hacks).
#
# This replaces the NSSM service approach (which ran as LocalSystem and
# couldn't see user's hermes profile registry).
#
# Run:
#   .\register-startup-shortcut.ps1
#
# To uninstall:
#   Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\hermes-orch-agent.bat"
#   Stop-Process -Name hermes-orch-agent

$ErrorActionPreference = "Continue"  # tolerate NSSM taskkill failures (LocalSystem processes)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$VenvScripts = Join-Path $ProjectRoot ".venv\Scripts\hermes-orch-agent.exe"
$AgentExe = (Resolve-Path $VenvScripts -ErrorAction SilentlyContinue).Path
if (-not $AgentExe -or -not (Test-Path $AgentExe)) {
    Write-Error "hermes-orch-agent.exe not found at $VenvScripts. Run 'pip install -e .' first."
    exit 1
}

$ConfigPath = Join-Path $env:USERPROFILE ".hermes-orchestrator\wrapper-config.json"
if (-not (Test-Path $ConfigPath)) {
    Write-Error "wrapper-config.json not found at $ConfigPath. Run 'hermes-orch-agent register' first."
    exit 1
}

$LogOut = Join-Path $env:USERPROFILE ".hermes-orchestrator\daemon.out.log"
$LogErr = Join-Path $env:USERPROFILE ".hermes-orchestrator\daemon.err.log"

# ----- Build a .bat that launches the daemon with output redirected to files -----
# Self-restart loop: if the daemon exits for any reason (crash, signal, etc.)
# the .bat waits 5s and re-launches. This is the Windows equivalent of
# systemd's Restart=always. Without this, a single crash would leave the
# agent offline until the next logon.
$batContent = @"
@echo off
REM hermes-orch-agent daemon launcher + auto-restart watchdog.
REM Runs at logon (via shell:startup shortcut). Restarts the daemon if it
REM exits for any reason (crash, signal, etc.) -- equivalent to systemd's
REM Restart=always. Loop with short delay to avoid hot-restart loops.

cd /d "$($env:USERPROFILE)\.hermes-orchestrator"
set EXE="$AgentExe"
set CFG="$ConfigPath"
set LOG="$LogOut"
set ERR="$LogErr"

echo [%date% %time%] hermes-orch-agent watchdog started >> "%LOG%"

:loop
echo [%date% %time%] starting daemon... >> "%LOG%"
%EXE% start --config %CFG% --interval 5 1>> "%LOG%" 2>> "%ERR%"
echo [%date% %time%] daemon exited (rc=%ERRORLEVEL%), restarting in 5s... >> "%LOG%"
timeout /t 5 /nobreak >nul
goto loop
"@
$batPath = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\hermes-orch-agent.bat"
[System.IO.File]::WriteAllText($batPath, $batContent, [System.Text.UTF8Encoding]::new($false))
Write-Host "[+] Wrote startup script: $batPath"

# ----- Remove old NSSM service if present -----
$nssm = Get-Command "nssm" -ErrorAction SilentlyContinue
if ($nssm) {
    $existing = Get-Service -Name "hermes-orch-agent" -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "[+] Removing NSSM service..."
        & nssm stop hermes-orch-agent 2>&1 | Out-Null
        Start-Sleep -Seconds 2
        & nssm remove hermes-orch-agent confirm 2>&1 | Out-Null
    }
}

# ----- Kill any running daemon so the new one takes over -----
Get-Process -Name hermes-orch-agent -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "[+] Killing old daemon PID $($_.Id)..."
    taskkill /F /T /PID $_.Id 2>&1 | Out-Null
}
Start-Sleep -Seconds 3

# ----- Start the daemon NOW (so you don't have to log out/in to test) -----
Write-Host "[+] Starting daemon..."
Start-Process -FilePath $AgentExe -ArgumentList "start","--config",$ConfigPath,"--interval","5" `
    -WorkingDirectory (Split-Path $ConfigPath -Parent) `
    -RedirectStandardOutput $LogOut `
    -RedirectStandardError $LogErr `
    -WindowStyle Hidden

Start-Sleep -Seconds 5
$proc = Get-Process -Name hermes-orch-agent -ErrorAction SilentlyContinue
if ($proc) {
    Write-Host "[+] Daemon started (PID $($proc.Id))" -ForegroundColor Green
} else {
    Write-Host "[!] Daemon not running - check $LogErr" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Management ===" -ForegroundColor Cyan
Write-Host "  Status:   Get-Process -Name hermes-orch-agent"
Write-Host "  Logs:     Get-Content `$env:USERPROFILE\.hermes-orchestrator\daemon.out.log -Wait"
Write-Host "  Stop:     taskkill /F /IM hermes-orch-agent.exe"
Write-Host "  Remove:   Remove-Item '$batPath'"
