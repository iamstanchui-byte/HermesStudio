# _install-as-admin.ps1 - Self-elevate wrapper. Two modes:
#   (no flag):  re-launch itself with -Verb RunAs, output to log file
#   -Elevated:  actually run the install (the re-launched invocation)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File "...\_install-as-admin.ps1"
#
# After you click Yes on UAC, the elevated process writes to:
#   C:\Users\stanley\AppData\Local\Temp\svc-install.log

param(
    [switch]$Elevated
)

$logFile = "C:\Users\stanley\AppData\Local\Temp\svc-install.log"

if (-not $Elevated) {
    # We're in the non-elevated parent. Re-launch as admin.
    $script = $MyInvocation.MyCommand.Path
    $args = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$script`"",
        "-Elevated"
    )
    Write-Host "[elevate] Re-launching as Administrator..."
    Write-Host "[elevate] Output will be in: $logFile"
    Write-Host "[elevate] If UAC dialog disappears, you may need to click Yes."
    Start-Process -FilePath "powershell.exe" -ArgumentList $args -Verb RunAs -Wait
    exit 0
}

# We're now elevated. Start transcript so all output is captured.
Start-Transcript -Path $logFile -Append
Write-Host ""
Write-Host "=== ELEVATED INSTALL STARTED $(Get-Date -Format 'o') ==="
Write-Host "  PID:     $PID"
Write-Host "  User:    $([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)"
Write-Host "  Admin:   $(([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"

# Now run the actual install script (inherits elevation)
$installScript = "C:\Project\minimax code\hermes-orchestrator\scripts\register-windows-service-v2.ps1"
if (-not (Test-Path $installScript)) {
    Write-Host "[!] install script not found: $installScript" -ForegroundColor Red
    Stop-Transcript
    exit 1
}

Write-Host ""
Write-Host "=== Running $installScript ==="
try {
    & $installScript
    Write-Host ""
    Write-Host "=== INSTALL EXIT CODE: $LASTEXITCODE ===" -ForegroundColor Cyan
} catch {
    Write-Host "[!] Install threw: $_" -ForegroundColor Red
    Stop-Transcript
    exit 1
}

Stop-Transcript
Write-Host ""
Write-Host "=== Install log written to: $logFile ===" -ForegroundColor Green
Write-Host "    Get-Content $logFile -Tail 50"
exit $LASTEXITCODE
