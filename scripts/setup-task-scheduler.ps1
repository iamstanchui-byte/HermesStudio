# setup-task-scheduler.ps1
# v3.12.5: Idempotently apply wrapper-lifecycle fixes to the
# HermesOrchWrapper-WinLocal1 Task Scheduler entry:
#   1. Add a TimeTrigger every 5 minutes (backstop -- if the wrapper
#      dies between reboots AND the RecoveryConfig respawn doesn't fire,
#      this catches it within 5 minutes)
#   2. Set Settings.RestartCount = 999, RestartInterval = PT1M,
#      StartWhenAvailable = $true (RecoveryConfig equivalent --
#      when the wrapper exits non-zero, Task Scheduler will respawn it
#      up to 999 times with 1 minute between attempts)
#
# Safe to re-run: only adds the TimeTrigger if not already present;
# the Settings.* fields are always set to the target values.
#
# Usage (PowerShell 5.1+, run as the wrapper's user):
#   .\scripts\setup-task-scheduler.ps1
#
# Does NOT touch the running wrapper process -- only the task definition
# in Task Scheduler. Any change takes effect on the next wrapper exit.

[CmdletBinding()]
param(
    [string]$TaskName = "HermesOrchWrapper-WinLocal1",
    [int]$RepetitionMinutes = 5,
    [int]$RestartCount = 999,
    [string]$RestartInterval = "PT1M"
)

$ErrorActionPreference = 'Stop'

Write-Host "=== HermesOrch wrapper Task Scheduler setup (v3.12.5) ===" -ForegroundColor Cyan
Write-Host "Task: $TaskName"
Write-Host ""

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop

# Check existing triggers
$hasBootTrigger = $false
$hasTimeTrigger = $false
foreach ($tr in $task.Triggers) {
    $cls = $tr.CimClass.CimClassName
    if ($cls -like '*BootTrigger*') { $hasBootTrigger = $true }
    if ($cls -like '*TimeTrigger*') { $hasTimeTrigger = $true }
}
Write-Host "Existing triggers: Boot=$hasBootTrigger, Time=$hasTimeTrigger"

# Build new trigger list
# Note: we rebuild the BootTrigger from scratch (assuming no Delay set,
# which matches the deployment XML). If a future deployment needs a
# Delay, set it explicitly here.
$bootTrigger = New-ScheduledTaskTrigger -AtStartup
$triggers = @($bootTrigger)

if (-not $hasTimeTrigger) {
    # Start 1 minute from now so the trigger fires shortly after we exit.
    # RepetitionDuration = 10 years from now (effectively forever).
    $startAt = (Get-Date).AddMinutes(1)
    $timeTrigger = New-ScheduledTaskTrigger -Once -At $startAt `
        -RepetitionInterval (New-TimeSpan -Minutes $RepetitionMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $triggers += $timeTrigger
    Write-Host "[OK] Added TimeTrigger: every $RepetitionMinutes min starting $startAt" -ForegroundColor Green
} else {
    Write-Host "[skip] TimeTrigger already present" -ForegroundColor Yellow
}

# Update Settings (RecoveryConfig equivalent + StartWhenAvailable)
$settings = $task.Settings
$settings.RestartCount = $RestartCount
$settings.RestartInterval = $RestartInterval
$settings.StartWhenAvailable = $true
Write-Host "[OK] Settings updated: RestartCount=$RestartCount, RestartInterval=$RestartInterval, StartWhenAvailable=True" -ForegroundColor Green

# Re-register the task via Register-ScheduledTask -Force.
# We pass the existing Action, Principal, Description so all original
# attributes (LogonType=Interactive per-user, the python.exe path,
# the --config and --interval args, the description) are preserved.
Register-ScheduledTask -TaskName $TaskName `
    -Action $task.Actions `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $task.Principal `
    -Description $task.Description `
    -Force | Out-Null

# Verify (read-only)
Write-Host ""
Write-Host "=== AFTER ===" -ForegroundColor Cyan
$verify = Get-ScheduledTask -TaskName $TaskName
Write-Host "Triggers ($($verify.Triggers.Count)):"
foreach ($tr in $verify.Triggers) {
    Write-Host "  - $($tr.CimClass.CimClassName)"
}
Write-Host "Settings:"
Write-Host "  RestartCount           = $($verify.Settings.RestartCount)"
Write-Host "  RestartInterval        = $($verify.Settings.RestartInterval)"
Write-Host "  StartWhenAvailable     = $($verify.Settings.StartWhenAvailable)"
Write-Host "  MultipleInstancesPolicy= $($verify.Settings.MultipleInstancesPolicy)"
Write-Host "Principal LogonType     = $($verify.Principal.LogonType)"

Write-Host ""
Write-Host "Done. Changes take effect on the next wrapper exit." -ForegroundColor Cyan
