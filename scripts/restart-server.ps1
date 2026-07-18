<#
restart-server.ps1 - Kill old server + start fresh with --reload.

Handles the multi-process uvicorn --reload structure:
  hermes-orch.exe  (launcher / parent)
    python.exe  (uvicorn reloader watcher)
      python.exe  (actual server)
        python.exe  (multiprocessing spawn helper, transient)

Just killing the parent hermes-orch.exe with /T /F cleans up the whole tree.
Do NOT match on powershell.exe or this script's own commandline (it contains
'hermes-orch' as text in the script source).
#>
$ErrorActionPreference = "Stop"

$ProjDir = "C:\Project\minimax code\hermes-orchestrator"
$LogFile = Join-Path $ProjDir "server.log"
$LogErr  = Join-Path $ProjDir "server.log.err"
$Exe     = Join-Path $ProjDir ".venv\Scripts\hermes-orch.exe"
$Port    = 8765

function Stop-Server {
    Write-Host "[stop] finding hermes-orch processes..." -ForegroundColor Yellow
    $procs = Get-CimInstance Win32_Process | Where-Object {
        # Match the venv python running the orchestrator (cmdline has
        # "hermes-orch" + "serve") OR the hermes-orch.exe launcher itself
        # OR the uvicorn spawn helper (cmdline has "spawn_main" + "hermes-orch")
        $isOurProcess = $_.CommandLine -match "hermes-orch" -and $_.CommandLine -match "serve"
        $isLauncher = $_.Name -eq "hermes-orch.exe"
        $isSpawnHelper = $_.CommandLine -match "spawn_main" -and $_.CommandLine -match "hermes-orch"
        $isOurType = $_.Name -eq "python.exe" -or $_.Name -eq "hermes-orch.exe"
        ($isOurProcess -or $isLauncher -or $isSpawnHelper) -and $isOurType
    }
    if (-not $procs -or $procs.Count -eq 0) {
        Write-Host "[stop] nothing to kill" -ForegroundColor DarkYellow
        return
    }
    foreach ($p in $procs) {
        Write-Host "[stop] taskkill /T /F /PID $($p.ProcessId) ($($p.Name))"
        # Don't let "process not found" (because parent /T already killed it) abort us
        try { $null = taskkill /T /F /PID $p.ProcessId 2>&1 }
        catch { Write-Host "        (already dead, ok)" -ForegroundColor DarkYellow }
    }
    # Wait for everything to actually die
    $deadline = (Get-Date).AddSeconds(8)
    while ((Get-Date) -lt $deadline) {
        $left = Get-CimInstance Win32_Process | Where-Object {
            $isOurProcess = $_.CommandLine -match "hermes-orch" -and $_.CommandLine -match "serve"
            $isLauncher = $_.Name -eq "hermes-orch.exe"
            $isSpawnHelper = $_.CommandLine -match "spawn_main" -and $_.CommandLine -match "hermes-orch"
            $isOurType = $_.Name -eq "python.exe" -or $_.Name -eq "hermes-orch.exe"
            ($isOurProcess -or $isLauncher -or $isSpawnHelper) -and $isOurType
        }
        if (-not $left -or $left.Count -eq 0) { break }
        Start-Sleep -Milliseconds 500
    }
    $stillAlive = Get-CimInstance Win32_Process | Where-Object {
        $isOurProcess = $_.CommandLine -match "hermes-orch" -and $_.CommandLine -match "serve"
        $isLauncher = $_.Name -eq "hermes-orch.exe"
        $isSpawnHelper = $_.CommandLine -match "spawn_main" -and $_.CommandLine -match "hermes-orch"
        $isOurType = $_.Name -eq "python.exe" -or $_.Name -eq "hermes-orch.exe"
        ($isOurProcess -or $isLauncher -or $isSpawnHelper) -and $isOurType
    }
    if ($stillAlive -and $stillAlive.Count -gt 0) {
        Write-Host "[stop] WARNING: $($stillAlive.Count) process(es) still alive after timeout" -ForegroundColor Red
    } else {
        Write-Host "[stop] all stopped" -ForegroundColor Green
    }
}

function Wait-Port-Free {
    param([int]$Port, [int]$TimeoutSec = 5)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if (-not $conn) { return $true }
        Start-Sleep -Milliseconds 300
    }
    return $false
}

function Wait-Health {
    param([int]$TimeoutSec = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $elapsed = 0
    while ((Get-Date) -lt $deadline) {
        $elapsed += 1
        try {
            $r = Invoke-RestMethod -Uri "http://localhost:$Port/api/health" -Method GET -TimeoutSec 3
            if ($r.status -eq "ok") {
                Write-Host "  health check ok after ${elapsed}s" -ForegroundColor DarkGreen
                return $true
            }
        } catch {
            # server still starting up
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

# Main
Write-Host "[1/4] stopping old server..." -ForegroundColor Cyan
Stop-Server

if (-not (Wait-Port-Free -Port $Port)) {
    Write-Host "[fail] port $Port still busy after 5s" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[2/4] starting fresh server (--reload)..." -ForegroundColor Cyan
$proc = Start-Process -FilePath $Exe -ArgumentList @("serve", "--reload") `
    -WorkingDirectory $ProjDir `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError $LogErr `
    -PassThru
Write-Host "[2/4] started PID=$($proc.Id)" -ForegroundColor Green

Write-Host ""
Write-Host "[3/4] waiting for health check..." -ForegroundColor Cyan
if (Wait-Health -TimeoutSec 30) {
    Write-Host "[3/4] healthy" -ForegroundColor Green
} else {
    Write-Host "[3/4] FAILED to become healthy within 30s" -ForegroundColor Red
    Write-Host "        check $LogErr for details"
    exit 1
}

Write-Host ""
Write-Host "[4/4] process tree:" -ForegroundColor Cyan
$tree = Get-CimInstance Win32_Process | Where-Object {
    ($_.CommandLine -match "hermes-orch" -or $_.CommandLine -match "spawn_main") -and
    ($_.Name -eq "python.exe" -or $_.Name -eq "hermes-orch.exe")
} | Select-Object ProcessId, Name, ParentProcessId
$tree | Format-Table -AutoSize

Write-Host ""
Write-Host "All set. --reload is active. Edit any .py file and it auto-reloads." -ForegroundColor Green
Write-Host "Run this script again to restart." -ForegroundColor Green
