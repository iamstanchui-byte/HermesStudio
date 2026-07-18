$regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\hermes-orch-agent"
$name = "Environment"
$values = @(
    "HERMES_BIN=C:\Users\stanley\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe",
    "HERMES_HOME=C:\Users\stanley\AppData\Local\hermes"
)

# Use .NET to write a true REG_MULTI_SZ (the @(...) form may be coerced to REG_SZ)
$key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($regPath.Replace("HKLM:\", ""), $true)
if ($null -eq $key) { Write-Error "Could not open key"; exit 1 }
$key.SetValue($name, $values, [Microsoft.Win32.RegistryValueKind]::MultiString)
$key.Close()

# Read back
$key2 = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($regPath.Replace("HKLM:\", ""))
$read = $key2.GetValue($name)
$key2.Close()
"Type: $($read.GetType().FullName)"
"Count: $($read.Count)"
$read | ForEach-Object { "  $_" }
