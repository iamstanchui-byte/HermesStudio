# register-startup-task.ps1 - Register hermes-orch-agent daemon to start at user logon.
#
# Uses Windows Task Scheduler (no admin needed, no password, runs as current user).
# Equivalent to a systemd user service on Linux.
#
# What it does:
#   1. Create a task named "hermes-orch-agent" that:
#      - Triggers at user logon
#      - Runs hermes-orch-agent.exe start --config <path>
#      - Hidden window, no UI
#      - Restarts on failure (every 1 min, up to 3 attempts)
#   2. Starts the task immediately (so you don't have to log out/in)
#
# To remove:
#   Unregister-ScheduledTask -TaskName hermes-orch-agent
#
# This replaces the NSSM service approach, which had two problems for
# hermes-on-Windows:
#   - NSSM runs as LocalSystem, which has a different HOME/LOCALAPPDATA,
#     so hermes can't find its profile registry.
#   - hermes reads HERMES_HOME/LOCALAPPDATA for profile locations and
#     needs the user's actual files.

$ErrorActionPreference = "Stop"

$TaskName = "hermes-orch-agent"

# ----- Find hermes-orch-agent.exe in the project venv -----
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$VenvScripts = Join-Path $ProjectRoot ".venv\Scripts\hermes-orch-agent.exe"
$AgentExe = (Resolve-Path $VenvScripts -ErrorAction SilentlyContinue).Path
if (-not $AgentExe -or -not (Test-Path $AgentExe)) {
    Write-Error "hermes-orch-agent.exe not found at $VenvScripts. Run 'pip install -e .' in the project venv first."
    exit 1
}
Write-Host "[+] Agent binary: $AgentExe"

# ----- Find wrapper-config.json -----
$ConfigPath = Join-Path $env:USERPROFILE ".hermes-orchestrator\wrapper-config.json"
if (-not (Test-Path $ConfigPath)) {
    Write-Error "wrapper-config.json not found at $ConfigPath. Run 'hermes-orch-agent register' first."
    exit 1
}
Write-Host "[+] Config: $ConfigPath"

# ----- Remove existing NSSM service if present (cleanup) -----
$nssm = Get-Command "nssm" -ErrorAction SilentlyContinue
if ($nssm) {
    $existing = Get-Service -Name $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "[+] Removing existing NSSM service '$TaskName'..."
        & nssm stop $TaskName 2>&1 | Out-Null
        Start-Sleep -Seconds 2
        & nssm remove $TaskName confirm 2>&1 | Out-Null
    }
}

# ----- Remove existing scheduled task if present -----
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "[+] Removing existing scheduled task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# ----- Create the scheduled task -----
Write-Host "[+] Creating scheduled task '$TaskName'..."

$action = New-ScheduledTaskAction `
    -Execute $AgentExe `
    -Argument "start --config `"$ConfigPath`" --interval 5" `
    -WorkingDirectory (Split-Path $ConfigPath -Parent)

# At logon trigger; also start now
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT0S"  # no delay

# Run whether user is logged in or not (we want auto-start, but as current user)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

# Restart on failure: every 1 min, up to 3 attempts
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)  # no limit

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Hermes Orchestrator Agent Wrapper - heartbeats to orchestrator + runs tasks via hermes CLI" `
    -Force | Out-Null

Write-Host "[+] Task created."

# ----- Start now -----
Write-Host "[+] Starting task..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 4

$running = Get-ScheduledTask -TaskName $TaskName
if ($running.State -eq "Running") {
    Write-Host "[+] Task is RUNNING" -ForegroundColor Green
} else {
    Write-Host "[!] Task state: $($running.State)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Task management ===" -ForegroundColor Cyan
Write-Host "  Status:    Get-ScheduledTask -TaskName hermes-orch-agent"
Write-Host "  Start:     Start-ScheduledTask -TaskName hermes-orch-agent"
Write-Host "  Stop:      Stop-ScheduledTask -TaskName hermes-orch-agent"
Write-Host "  Uninstall: Unregister-ScheduledTask -TaskName hermes-orch-agent -Confirm:`$false"
Write-Host "  Logs:      Get-Content `$env:USERPROFILE\.hermes-orchestrator\daemon.out.log -Wait"
