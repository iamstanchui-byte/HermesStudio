$nssm = 'C:\Users\stanley\AppData\Local\Microsoft\WinGet\Links\nssm.exe'
$exe = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\hermes.exe'
$envLine = "HERMES_BIN=$exe"
$logFile = 'C:\Users\stanley\.hermes-orchestrator\debug-nssm.log'

"=== Debug run at $(Get-Date) ===" | Out-File $logFile
"HERMES_BIN target: $exe" | Out-File $logFile -Append

"`n--- nssm set ---" | Out-File $logFile -Append
& $nssm set hermes-orch-agent AppEnvironmentExtra $envLine 2>&1 | Out-File $logFile -Append

"`n--- nssm get ---" | Out-File $logFile -Append
& $nssm get hermes-orch-agent AppEnvironmentExtra 2>&1 | Out-File $logFile -Append

"`n--- registry Environment ---" | Out-File $logFile -Append
$reg = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\hermes-orch-agent'
"  Environment = [$($reg.Environment)]" | Out-File $logFile -Append

"`n--- sc qc (filtered) ---" | Out-File $logFile -Append
sc.exe qc hermes-orch-agent | Select-String -Pattern 'BIN|ENV' | ForEach-Object { $_.Line } | Out-File $logFile -Append

"`n--- env at this process level (sanity check) ---" | Out-File $logFile -Append
"  LOCALAPPDATA = $env:LOCALAPPDATA" | Out-File $logFile -Append
"  USERPROFILE  = $env:USERPROFILE" | Out-File $logFile -Append
"  WHOAMI       = $(whoami)" | Out-File $logFile -Append

exit
