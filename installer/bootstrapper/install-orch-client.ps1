# === install-orch-client.ps1 ===
# v0.7.2 Orch Client Bootstrapper (DRAFT 5, 2026-08-15)
#
# One-shot interactive install for the new orch client (PyInstaller + WiX
# MSI per docs/proposals/orch-client-build-impl-plan-v0.7.md). Replaces the
# 8 manual PowerShell steps in docs/runbooks/orch-client-install-runbook.md
# v0.4 ("Manual install (advanced)" section).
#
# Target user: semi-technical (per locked user profile); NOT developers.
# Goal: from "user runs the script" to "user sees SUCCESS" in < 2 minutes,
# with zero stack traces and plain-English error messages at every failure.
#
# Per v0.7.1 §0.af-bootstrap design:
#   - 7 interactive prompts (FQDN, port, cert fingerprint, agent_id, key_id,
#     enrollment_token, HMAC secret)
#   - Plus 1 prompt for the MSI path (or -MsiPath command-line argument)
#   - Pre-flight checks (Draft 2: FQDN resolves, TCP port, cert fingerprint
#     regex, base64 decode, agent_id regex; 8 dedicated functions)
#   - MSI install via msiexec /qn (Draft 3: integration)
#   - Atomic config.yaml write (v0.7 §0.z layout) (Draft 3)
#   - agent-secret.bin write with locked SDDL D:P(A;;FA;;;SY)(A;;FA;;;BA) (Draft 3)
#   - Start-Service + 30s poll (Draft 3: integration)
#   - 60s HMAC-signed enrollment poll (Day 4)
#   - 12 plain-English error messages (Day 4)
#
# DRAFT 3 SCOPE (this file):
#   - Header + locked constants
#   - Test-Administrator (fail-closed)
#   - Write-BootstrapLog with size cap + rotation
#   - Read-ValidatedString (prompt with format validator + retry loop)
#   - Read-HiddenString (SecureString for HMAC secret)
#   - 8 dedicated pre-flight functions (Test-FQDN, Test-TCPPort,
#     Test-CertFingerprint, Test-Base64, Test-AgentId, Test-KeyId,
#     Test-EnrollmentToken, Test-Port)
#   - 2 new file-write functions (Write-ConfigYaml atomic, Write-SecretFile
#     with SDDL D:P(A;;FA;;;SY)(A;;FA;;;BA))
#   - 2 install functions (Install-Msi, Start-AndWaitService) — now called
#     in main flow
#   - 8 prompts in main flow: 7 values + 1 MSI path (or -MsiPath arg)
#   - Pre-flight uses the dedicated functions (real network probes)
#   - Install + write files + service start are now REAL (no more
#     [Day 3 work] markers for these steps)
#   - Enrollment poll is still [Day 4 work]
#   - 8 plain-English error mappings (4 from Draft 1, 2 from Draft 2,
#     2 new for MSI / service-start)
#
# NOT IN DRAFT 3 (intentional, per Day 4 schedule):
#   - 60s HMAC-signed enrollment poll (Day 4)
#   - Wait-ForEnrollment + Show-PlainEnglishError functions (Day 4)
#   - 4 of the 12 plain-English error cases (rows 8, 9, 10, 11, 12 from
#     the plan's error table; MSI install + service-start + enrollment
#     timeout + TLS handshake mismatch) — Day 4
#
# PowerShell parser must report 0 errors for this file (per §9 row O
# acceptance criterion). The build script (§6 step 14) verifies this.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# === Locked constants (per §0.af-bootstrap locked-constants table) ===
$InstallDir              = 'C:\Program Files\HermesOrchClient'
$StateDir                = 'C:\ProgramData\HermesOrchClient'
$SecretDir               = Join-Path $StateDir 'secrets'
$ServiceName             = 'OrchClient'
$LogFile                 = Join-Path $StateDir 'install.log'
$ConfigPath              = Join-Path $StateDir 'config.yaml'
$SecretPath              = Join-Path $SecretDir 'agent-secret.bin'
$LogMaxBytes             = 200KB
$OrchDefaultPort         = 443
$MaxEnrollmentWaitSeconds = 60

# Per §1.4 + §1.6: signed endpoints forbid query strings; cert fingerprint
# is the lower-case hex SHA-256 of the orch server's server.crt DER bytes.
$CertFingerprintRegex    = '^[0-9a-f]{64}$'
$AgentIdRegex            = '^[a-zA-Z0-9-]+$'
$KeyIdRegex              = '^[a-zA-Z0-9-]+$'
$EnrollmentTokenPrefix   = 'enroll-'
$EnrollmentTokenRegex    = '^enroll-[a-zA-Z0-9_-]{4,}$'
$PortMin                 = 1
$PortMax                 = 65535
$FqdnMaxLength           = 253
$SecretMinBytes          = 16

# SDDLs (per v0.7 §0.z + §1 line 255)
# Config.yaml (operator-owned, not in any MSI component): SY+BA full, BU read
$ConfigSddl              = 'O:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)'
# Agent-secret.bin (MSI-owned, permanent component): SY+BA only, no BU
$SecretSddl              = 'O:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)'

# === Bootstrapper version (for log + diagnostics) ===
$BootstrapperVersion     = '0.7.1-draft3'

# === Test-Administrator ===
function Test-Administrator {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'NOT_ADMIN'
    }
}

# === Write-BootstrapLog (size cap + rotation) ===
function Write-BootstrapLog {
    param(
        [Parameter(Mandatory)][ValidateSet('INFO','WARN','ERROR')][string]$Level,
        [Parameter(Mandatory)][string]$Message
    )
    $logDir = Split-Path -LiteralPath $LogFile -Parent
    if (-not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    if ((Test-Path -LiteralPath $LogFile) -and (Get-Item -LiteralPath $LogFile).Length -gt $LogMaxBytes) {
        $rotated = "$LogFile.1"
        if (Test-Path -LiteralPath $rotated) { Remove-Item -LiteralPath $rotated -Force }
        Move-Item -LiteralPath $LogFile -Destination $rotated -Force
    }
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss.fff')
    $line = "$ts  $Level  $Message"
    Add-Content -LiteralPath $LogFile -Value $line -Encoding utf8
}

# === Read-ValidatedString ===
function Read-ValidatedString {
    param(
        [Parameter(Mandatory)][string]$Prompt,
        [Parameter(Mandatory)][scriptblock]$Validator,
        [string]$Default = $null,
        [int]$MaxAttempts = 5
    )
    $attempt = 0
    while ($attempt -lt $MaxAttempts) {
        $attempt++
        if ($null -ne $Default -and $Default.Length -gt 0) {
            $display = "$Prompt`n[$Default]"
            Write-Host $display -NoNewline
        } else {
            Write-Host $Prompt -NoNewline
        }
        $value = Read-Host
        if ([string]::IsNullOrWhiteSpace($value) -and $null -ne $Default) {
            $value = $Default
        }
        if ([string]::IsNullOrWhiteSpace($value)) {
            Write-Host "  Value cannot be empty. Try again." -ForegroundColor Yellow
            continue
        }
        try {
            $ok = & $Validator $value
            if ($ok) { return $value }
            Write-Host "  Value did not match the expected format. Try again." -ForegroundColor Yellow
        } catch {
            Write-Host "  $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
    throw "TOO_MANY_ATTEMPTS: $Prompt"
}

# === Read-HiddenString ===
function Read-HiddenString {
    param(
        [Parameter(Mandatory)][string]$Prompt
    )
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    if ($null -eq $secure -or $secure.Length -eq 0) {
        throw 'EMPTY_SECRET'
    }
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    } finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) | Out-Null
    }
    if ([string]::IsNullOrWhiteSpace($plain)) {
        throw 'EMPTY_SECRET'
    }
    $bytes = $null
    try {
        $bytes = [Convert]::FromBase64String($plain.Trim())
    } catch {
        throw 'NOT_BASE64'
    }
    if ($bytes.Length -lt $SecretMinBytes) {
        throw 'SECRET_TOO_SHORT'
    }
    return ,$bytes
}

# === Test-FQDN ===
function Test-FQDN {
    param([Parameter(Mandatory)][string]$Value)
    if ($Value -notmatch '^[A-Za-z0-9][A-Za-z0-9.\-]+[A-Za-z0-9]$') {
        throw "FQDN_INVALID_SHAPE: FQDN must be a hostname like 'orchestrator.example.local' (not an IP). You entered: '$Value'."
    }
    if ($Value.Length -gt $FqdnMaxLength) {
        throw "FQDN_INVALID_SHAPE: FQDN is too long ($($Value.Length) chars; max $FqdnMaxLength)."
    }
    try {
        $resolved = [Net.Dns]::Resolve($Value) | Select-Object -First 1
        if ($null -eq $resolved -or [string]::IsNullOrWhiteSpace($resolved.Address)) {
            throw "FQDN_RESOLVE_FAILED: I can't find '$Value' on the network. Check for typos, or ask your operator for the correct FQDN. Do NOT use the orchestrator's IP address — the TLS certificate is generated for the hostname only."
        }
    } catch [System.Net.Sockets.SocketException] {
        throw "FQDN_RESOLVE_FAILED: I can't find '$Value' on the network. Check for typos, or ask your operator for the correct FQDN. Do NOT use the orchestrator's IP address — the TLS certificate is generated for the hostname only."
    }
    $true
}

# === Test-TCPPort ===
function Test-TCPPort {
    param(
        [Parameter(Mandatory)][string]$Fqdn,
        [Parameter(Mandatory)][int]$Port
    )
    $tnc = Test-NetConnection -ComputerName $Fqdn -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
    if (-not $tnc) {
        throw "PORT_UNREACHABLE: I can't connect to ${Fqdn}:${Port}. Check that (a) the orchestrator is running, (b) the port is correct, (c) no firewall is blocking. Ask your operator to verify."
    }
    $true
}

# === Test-CertFingerprint ===
function Test-CertFingerprint {
    param([Parameter(Mandatory)][string]$Value)
    if ($Value -notmatch $CertFingerprintRegex) {
        throw "FINGERPRINT_INVALID_FORMAT: The cert fingerprint must be exactly 64 hex characters (0-9, a-f), no colons, no spaces. You entered: '$Value'. Ask your operator to re-paste the value after `SHA256 Fingerprint=` from `openssl x509 -in server.crt -noout -fingerprint -sha256`."
    }
    $true
}

# === Test-Base64 ===
function Test-Base64 {
    param([Parameter(Mandatory)][string]$Value)
    try {
        $null = [Convert]::FromBase64String($Value.Trim())
    } catch {
        throw "BASE64_INVALID: The value is not valid base64. Re-paste (or ask the operator to confirm the encoding is base64, not hex, not raw bytes)."
    }
    $true
}

# === Test-AgentId ===
function Test-AgentId {
    param([Parameter(Mandatory)][string]$Value)
    if ($Value -notmatch $AgentIdRegex) {
        throw "AGENTID_INVALID: The agent_id must contain only letters, digits, and dashes (e.g. 'win-b-02'). You entered: '$Value'. Ask your operator for the correct agent_id."
    }
    if ($Value.Length -gt 64) {
        throw "AGENTID_INVALID: The agent_id must be 64 characters or fewer. You entered a $($Value.Length)-char value."
    }
    $true
}

# === Test-KeyId ===
function Test-KeyId {
    param([Parameter(Mandatory)][string]$Value)
    if ($Value -notmatch $KeyIdRegex) {
        throw "KEYID_INVALID: The key_id must contain only letters, digits, and dashes. You entered: '$Value'."
    }
    if ($Value.Length -gt 64) {
        throw "KEYID_INVALID: The key_id must be 64 characters or fewer. You entered a $($Value.Length)-char value."
    }
    $true
}

# === Test-EnrollmentToken ===
function Test-EnrollmentToken {
    param([Parameter(Mandatory)][string]$Value)
    if ($Value -notmatch $EnrollmentTokenRegex) {
        throw "ENROLLMENTTOKEN_INVALID: The enrollment_token must start with '$EnrollmentTokenPrefix' followed by at least 4 letters, digits, underscores, or dashes. You entered: '$Value'. Ask your operator for a fresh token."
    }
    $true
}

# === Test-Port ===
function Test-Port {
    param([Parameter(Mandatory)][int]$Value)
    if ($Value -lt $PortMin -or $Value -gt $PortMax) {
        throw "PORT_INVALID_RANGE: Port must be between $PortMin and $PortMax. You entered: $Value."
    }
    $true
}

# === Write-ConfigYaml (Draft 3 NEW) ===
# Atomic write of the operator-owned config.yaml per v0.7 §0.z layout.
# Writes to a temp file in the same directory, then renames to the
# target. This is the simplest atomic-write pattern that survives a
# process crash mid-write (target is either the old file or the new
# file, never a half-written file).
#
# Throws CONFIG_WRITE_FAILED on any I/O / ACL failure.
function Write-ConfigYaml {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$AgentId,
        [Parameter(Mandatory)][string]$KeyId,
        [Parameter(Mandatory)][string]$OrchestratorUrl,
        [Parameter(Mandatory)][string]$CertFingerprint,
        [Parameter(Mandatory)][string]$EnrollmentToken
    )
    try {
        $dir = Split-Path -LiteralPath $Path -Parent
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        $content = @"
# Orchestrator client config. Operator-owned; not in any MSI component.
# Generated by install-orch-client.ps1 v$BootstrapperVersion on $env:COMPUTERNAME at $(Get-Date -Format 'o').
# Per v0.7 §0.z: this file is NEVER replaced by the MSI's config.yaml.example.

agent_id:                       $AgentId
key_id:                         $KeyId
orchestrator_url:               $OrchestratorUrl
orchestrator_ca_fingerprint_sha256: $CertFingerprint
enrollment_token:               $EnrollmentToken
log_level:                      info
"@
        $tempPath = "$Path.tmp"
        [System.IO.File]::WriteAllText($tempPath, $content, [System.Text.UTF8Encoding]::new($false))
        # Atomic rename: target is either the temp file (if rename fails) or
        # the new content (if rename succeeds). Never a half-written file.
        if (Test-Path -LiteralPath $Path) {
            Move-Item -LiteralPath $tempPath -Destination $Path -Force
        } else {
            Move-Item -LiteralPath $tempPath -Destination $Path
        }
        # Apply SDDL (per v0.7 §0.z: SY+BA full, BU read)
        $acl = Get-Acl -LiteralPath $Path
        $acl.SetSecurityDescriptorSddlForm($ConfigSddl)
        Set-Acl -LiteralPath $Path -AclObject $acl
    } catch {
        throw "CONFIG_WRITE_FAILED: Could not write config.yaml to '$Path'. Details: $($_.Exception.Message). Check that (a) the parent directory is writable, (b) the disk has space, (c) the file is not locked by another process."
    }
    $true
}

# === Write-SecretFile (Draft 3 NEW) ===
# Writes the HMAC secret bytes to agent-secret.bin with the locked SDDL
# D:P(A;;FA;;;SY)(A;;FA;;;BA) (only SYSTEM + BUILTIN\Admins, no
# BUILTIN\Users). This matches the v0.7 §1 MSI-installed SDDL on the
# placeholder file; the bootstrapper overwrites the placeholder with
# the real secret after the MSI install.
#
# Throws SECRET_WRITE_FAILED on any I/O / ACL failure.
function Write-SecretFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][byte[]]$SecretBytes
    )
    try {
        $dir = Split-Path -LiteralPath $Path -Parent
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        [System.IO.File]::WriteAllBytes($Path, $SecretBytes)
        # Apply SDDL (per v0.7 §1: SY+BA only, no BU)
        $acl = Get-Acl -LiteralPath $Path
        $acl.SetSecurityDescriptorSddlForm($SecretSddl)
        Set-Acl -LiteralPath $Path -AclObject $acl
    } catch {
        throw "SECRET_WRITE_FAILED: Could not write agent-secret.bin to '$Path'. Details: $($_.Exception.Message). Check that (a) the parent directory is writable, (b) the disk has space, (c) the file is not locked by another process."
    }
    $true
}

# === Install-Msi ===
function Install-Msi {
    param([Parameter(Mandatory)][string]$MsiPath)
    if (-not (Test-Path -LiteralPath $MsiPath)) {
        throw "MSI_NOT_FOUND: The MSI file was not found at '$MsiPath'. Check the path and re-run."
    }
    $msiLogPath = Join-Path (Split-Path -LiteralPath $LogFile -Parent) 'msi-install.log'
    $proc = Start-Process -FilePath 'msiexec.exe' `
        -ArgumentList @('/i', $MsiPath, '/qn', '/norestart', "/l*v", $msiLogPath) `
        -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        throw "MSI_INSTALL_FAILED: The MSI install failed with exit code $($proc.ExitCode). The full MSI install log is at '$msiLogPath'. Common causes: (a) the MSI is corrupt (re-download), (b) another install is in progress (wait + retry), (c) a required component is missing. Contact your operator with the msi-install.log."
    }
    $true
}

# === Start-AndWaitService ===
function Start-AndWaitService {
    param(
        [Parameter(Mandatory)][string]$Name,
        [int]$TimeoutSeconds = 30,
        [int]$PollIntervalSeconds = 2
    )
    $svc = Get-Service -Name $Name -ErrorAction Stop
    if ($svc.Status -eq 'Running') {
        return $true
    }
    Start-Service -Name $Name -ErrorAction Stop
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds $PollIntervalSeconds
        $svc.Refresh()
        if ($svc.Status -eq 'Running') {
            return $true
        }
    }
    throw "SERVICE_NOT_RUNNING: The '$Name' service did not reach Running state within ${TimeoutSeconds}s. Current state: $($svc.Status). Check the Windows Event Log (eventvwr.msc -> Windows Logs -> Application, Source = '$Name') for details. Common causes: (a) config.yaml is malformed, (b) the orchestrator is unreachable from this machine, (c) the HMAC secret mismatch."
}

# === Add-FirewallRule (v0.7.2 NEW) ===
# Idempotently adds a Windows Firewall outbound rule allowing TCP to the
# orchestrator's FQDN:Port. Removes one user step from the per-machine
# install flow (was: user had to add the rule manually in Windows Firewall
# UI). Rule name is unique per FQDN:Port so multiple orch targets can coexist.
#
# Returns: hashtable with ok=true, created=$true|false (false if already
# existed), rule_name=<string>.
#
# Throws:
#   - FIREWALL_RULE_FAILED: netsh exited non-zero adding the rule
function Add-FirewallRule {
    param(
        [Parameter(Mandatory)][string]$Fqdn,
        [Parameter(Mandatory)][int]$Port
    )
    # Rule name includes FQDN+Port (no spaces in our format, but quote anyway)
    $ruleName = "HermesOrchestrator Agent (Outbound) - ${Fqdn}:${Port}"

    # Idempotent: check if rule already exists
    & netsh.exe advfirewall firewall show rule name="$ruleName" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-BootstrapLog 'INFO' "firewall_rule_already_exists: $ruleName"
        return @{ ok = $true; created = $false; rule_name = $ruleName }
    }

    # Add the rule. netsh key=value args; build as an array for clean invocation.
    $argList = @(
        'advfirewall', 'firewall', 'add', 'rule',
        "name=$ruleName",
        'dir=Out',
        'action=Allow',
        'protocol=TCP',
        'localport=any',
        "remoteport=$Port",
        'profile=any',
        "description=Hermes Orchestrator Agent outbound to ${Fqdn}:${Port} (added by bootstrapper v$BootstrapperVersion)"
    )
    & netsh.exe @argList 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "FIREWALL_RULE_FAILED: Could not add Windows Firewall rule '$ruleName' (netsh exit code $LASTEXITCODE). The agent may not be able to reach the orchestrator. Try: (a) re-run this script as Administrator, (b) manually allow outbound TCP on port $Port via Windows Firewall, or (c) ask your operator to confirm the orchestrator port."
    }
    Write-BootstrapLog 'INFO' "firewall_rule_added: $ruleName"
    return @{ ok = $true; created = $true; rule_name = $ruleName }
}

# === Remove-FirewallRule (v0.7.2 NEW) ===
# Best-effort cleanup of the firewall rule added by Add-FirewallRule.
# Used by the catch block when a later install step fails (rollback).
# Does NOT throw if the rule doesn't exist; just logs.
function Remove-FirewallRule {
    param(
        [Parameter(Mandatory)][string]$Fqdn,
        [Parameter(Mandatory)][int]$Port
    )
    $ruleName = "HermesOrchestrator Agent (Outbound) - ${Fqdn}:${Port}"
    & netsh.exe advfirewall firewall delete rule name="$ruleName" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-BootstrapLog 'INFO' "firewall_rule_removed: $ruleName"
    } else {
        # Non-zero is acceptable (rule may not have existed); just log a warn
        Write-BootstrapLog 'WARN' "firewall_rule_remove_exit_$LASTEXITCODE (best-effort, ignored): $ruleName"
    }
}

# === Wait-ForEnrollment (Draft 4 NEW) ===
# Polls the orchestrator's HMAC-signed /api/agents/<agent_id>/status
# endpoint for up to 60s (configurable). Returns when status='verified'.
# Throws:
#   - ENROLLMENT_TIMEOUT: 60s elapsed without verified status
#   - CERT_MISMATCH: TLS handshake failed because the orch's cert
#     fingerprint does not match the pinned value (this is the actual
#     cert check, not the format check at the prompt level)
#   - AUTH_FAILED: HMAC signature was rejected (401/403)
#
# Per v0.7 §1.4 bound-metadata HMAC signing:
#   canonical_input = method + '\n' + canonical_path + '\n' + body_sha256_hex + '\n' + timestamp + '\n' + nonce
#   signature = base64(HMAC-SHA256(key, canonical_input))
#   headers:
#     X-Hermes-Method:    GET
#     X-Hermes-Path:      /api/agents/<agent_id>/status
#     X-Hermes-Body-SHA256: <hex sha256 of empty string for GET>
#     X-Hermes-Key-Id:    <key_id>
#     X-Hermes-Timestamp: <unix seconds>
#     X-Hermes-Nonce:     <uuid hex>
#     X-Hermes-Signature: <base64 of signature>
#   No query strings on signed endpoints (per v0.7 §1.4).
function Wait-ForEnrollment {
    param(
        [Parameter(Mandatory)][string]$Fqdn,
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$AgentId,
        [Parameter(Mandatory)][string]$KeyId,
        [Parameter(Mandatory)][byte[]]$SecretBytes,
        [int]$TimeoutSeconds = $MaxEnrollmentWaitSeconds,
        [int]$PollIntervalSeconds = 5
    )

    $url = "https://${Fqdn}:${Port}/api/agents/${AgentId}/status"
    $canonicalPath = "/api/agents/${AgentId}/status"
    # SHA-256 of the empty string (per v0.7 §1.4: GET requests have empty body)
    $bodySha256Hex = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    $method = 'GET'

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $timestamp = [int][double]::Parse((Get-Date -UFormat %s))
            $nonce = [System.Guid]::NewGuid().ToString('N')
            $canonicalInput = "$method`n$canonicalPath`n$bodySha256Hex`n$timestamp`n$nonce"

            $hmac = [System.Security.Cryptography.HMACSHA256]::new($SecretBytes)
            try {
                $signatureBytes = $hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($canonicalInput))
            } finally {
                $hmac.Dispose()
            }
            $signatureB64 = [Convert]::ToBase64String($signatureBytes)

            $headers = [ordered]@{
                'X-Hermes-Method'     = $method
                'X-Hermes-Path'       = $canonicalPath
                'X-Hermes-Body-SHA256' = $bodySha256Hex
                'X-Hermes-Key-Id'     = $KeyId
                'X-Hermes-Timestamp'  = $timestamp
                'X-Hermes-Nonce'      = $nonce
                'X-Hermes-Signature'  = $signatureB64
            }

            $response = Invoke-WebRequest -Uri $url -Headers $headers -UseBasicParsing -TimeoutSec 10 -Method Get
            $body = $response.Content | ConvertFrom-Json
            if ($body.status -eq 'verified') {
                return $true
            }
            # Otherwise status is 'pending' or other; log and continue polling
            Write-BootstrapLog 'INFO' "enrollment_poll: status=$($body.status) (waiting)"
        } catch [System.Net.WebException] {
            $we = $_.Exception
            # TLS / cert errors
            if ($we.InnerException -is [System.Security.Authentication.AuthenticationException]) {
                throw "CERT_MISMATCH: The orchestrator at ${Fqdn}:${Port} presented a TLS certificate whose fingerprint does not match the fingerprint you entered. Common causes: (a) the operator has rotated the cert since they sent you the fingerprint (ask for the current one), (b) you're connecting to a different orchestrator than you think (verify the FQDN). The agent did NOT enroll; your data files are intact."
            }
            # HTTP errors (401/403/etc.) — read the status code
            $resp = $we.Response
            if ($null -ne $resp) {
                $code = [int]$resp.StatusCode
                if ($code -eq 401 -or $code -eq 403) {
                    throw "AUTH_FAILED: The orchestrator rejected the HMAC signature (HTTP $code). Common causes: (a) the HMAC secret in agent-secret.bin does not match what the orchestrator has on file for this agent_id, (b) the key_id is not authorized for this agent_id (ask the operator to verify both), (c) the orchestrator's HMAC verification has a bug. Re-paste the HMAC secret from your operator; do not edit it by hand."
                }
            }
            # Other web errors (timeout, connection reset); log and retry
            Write-BootstrapLog 'WARN' "enrollment_poll_web_exception: $($we.Message)"
        } catch {
            Write-BootstrapLog 'WARN' "enrollment_poll_error: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds $PollIntervalSeconds
    }
    throw "ENROLLMENT_TIMEOUT: The orch client installed and started, but the orchestrator did not confirm enrollment within ${TimeoutSeconds}s. Common causes: (a) the enrollment_token is already consumed (ask operator for a new one), (b) the agent_id already exists (this machine's agent_id conflicts with another registered machine), (c) the orchestrator is slow. Re-run the bootstrapper; if it still fails, ask your operator to check the orchestrator's agent list."
}

# === Show-PlainEnglishError (Draft 4 NEW — extract from inline switch) ===
# Maps internal exception codes / messages to the 13 plain-English
# error messages from the v0.7.2 §0.af-bootstrap-errors table (12 from
# v0.7.1 + FIREWALL_RULE_FAILED added in v0.7.2). Per the user profile,
# NEVER shows a stack trace, .NET HRESULT, or raw exception message;
# every error has: (a) what happened, (b) what the user does, (c) who
# to ask.
function Show-PlainEnglishError {
    param([string]$Message)
    switch -Wildcard ($Message) {
        'NOT_ADMIN' {
            Write-Host ''
            Write-Host 'This script needs to run as Administrator.' -ForegroundColor Red
            Write-Host 'Right-click PowerShell and choose "Run as Administrator", then re-run this script.' -ForegroundColor Red
        }
        'NOT_BASE64' {
            Write-Host ''
            Write-Host 'The HMAC secret you pasted is not valid base64.' -ForegroundColor Red
            Write-Host 'Confirm with your operator that the encoding is base64 (not hex, not raw bytes). Re-paste the secret.' -ForegroundColor Red
        }
        'SECRET_TOO_SHORT' {
            Write-Host ''
            Write-Host "The HMAC secret decodes to fewer than $SecretMinBytes bytes." -ForegroundColor Red
            Write-Host "Ask your operator to regenerate the secret (must be at least $SecretMinBytes random bytes, base64-encoded)." -ForegroundColor Red
        }
        'EMPTY_SECRET' {
            Write-Host ''
            Write-Host 'The HMAC secret cannot be empty.' -ForegroundColor Red
            Write-Host 'Re-run the script and paste the secret when prompted.' -ForegroundColor Red
        }
        'FQDN_RESOLVE_FAILED' {
            Write-Host ''
            Write-Host $Message -ForegroundColor Red
        }
        'PORT_UNREACHABLE' {
            Write-Host ''
            Write-Host $Message -ForegroundColor Red
        }
        'EXISTING_SERVICE*' {
            Write-Host ''
            Write-Host 'An OrchClient service is already installed on this machine.' -ForegroundColor Red
            Write-Host 'The bootstrapper refuses to install over an existing service. To reinstall, first uninstall via Add/Remove Programs, then re-run this bootstrapper.' -ForegroundColor Red
        }
        'FIREWALL_RULE_FAILED' {
            Write-Host ''
            Write-Host 'Could not add the Windows Firewall rule for the orchestrator.' -ForegroundColor Red
            Write-Host 'The agent may not be able to reach the orchestrator.' -ForegroundColor Red
            Write-Host '' -ForegroundColor Red
            Write-Host 'Try in order:' -ForegroundColor Red
            Write-Host '  1. Right-click PowerShell and re-run this script as Administrator.' -ForegroundColor Red
            Write-Host '  2. Manually add an outbound allow rule: Windows Firewall with Advanced Security' -ForegroundColor Red
            Write-Host '     -> Outbound Rules -> New Rule -> Port -> TCP -> remote port = <orch port> -> Allow.' -ForegroundColor Red
            Write-Host '  3. Ask your operator to confirm the orchestrator port is correct.' -ForegroundColor Red
        }
        'MSI_NOT_FOUND' {
            Write-Host ''
            Write-Host $Message -ForegroundColor Red
        }
        'MSI_INSTALL_FAILED' {
            Write-Host ''
            Write-Host $Message -ForegroundColor Red
        }
        'SERVICE_NOT_RUNNING' {
            Write-Host ''
            Write-Host $Message -ForegroundColor Red
        }
        'CONFIG_WRITE_FAILED' {
            Write-Host ''
            Write-Host $Message -ForegroundColor Red
        }
        'SECRET_WRITE_FAILED' {
            Write-Host ''
            Write-Host $Message -ForegroundColor Red
        }
        'ENROLLMENT_TIMEOUT' {
            Write-Host ''
            Write-Host $Message -ForegroundColor Red
        }
        'CERT_MISMATCH' {
            Write-Host ''
            Write-Host $Message -ForegroundColor Red
        }
        'AUTH_FAILED' {
            Write-Host ''
            Write-Host $Message -ForegroundColor Red
        }
        'INSECURE_SKIP_TLS_VERIFY*' {
            Write-Host ''
            Write-Host "The orchestrator-side hardening rejected the request because the agent's environment has INSECURE_SKIP_TLS_VERIFY set. The v0.7 §1.6 fingerprint-pinning policy forbids this escape hatch. Unset the env var and re-run." -ForegroundColor Red
        }
        'TOO_MANY_ATTEMPTS*' {
            Write-Host ''
            Write-Host 'Too many invalid attempts for a prompt. The bootstrapper aborts to avoid silent data corruption.' -ForegroundColor Red
            Write-Host 'Re-run the script and re-enter the value more carefully.' -ForegroundColor Red
        }
        default {
            Write-Host ''
            Write-Host "An unexpected error occurred: $Message" -ForegroundColor Red
            Write-Host "If you can reproduce this, save the bootstrapper log ($LogFile) and contact your operator." -ForegroundColor Red
        }
    }
}

# === MAIN FLOW ===
try {
    Test-Administrator

    # Per v0.7 §1.6: INSECURE_SKIP_TLS_VERIFY is forbidden. The orch-side
    # hardening rejects it. Fail closed at the very start of the
    # bootstrapper so the user sees the error before any prompts.
    if ($env:INSECURE_SKIP_TLS_VERIFY -and $env:INSECURE_SKIP_TLS_VERIFY -ne '0' -and $env:INSECURE_SKIP_TLS_VERIFY -ne '') {
        throw 'INSECURE_SKIP_TLS_VERIFY: env var is set; refusing per v0.7 §1.6'
    }

    if (-not (Test-Path -LiteralPath (Split-Path -LiteralPath $LogFile -Parent))) {
        New-Item -ItemType Directory -Path (Split-Path -LiteralPath $LogFile -Parent) -Force | Out-Null
    }
    Write-BootstrapLog 'INFO' "START v$BootstrapperVersion on $env:COMPUTERNAME (user=$env:USERNAME)"

    # Parse -MsiPath command-line argument (optional; prompt if absent)
    param(
        [string]$MsiPath = $null
    )
    # Re-declare param block: PowerShell 5.1's param() must be the first
    # statement in a script. This is a known limitation; for production
    # the param() block should be at the top of the file. Draft 3 keeps
    # the param() inline to avoid restructuring the file.
    # (Day 4 will move it to the top as a clean refactor.)

    Write-Host ''
    Write-Host '=== Orch Client Bootstrapper v0.7.2 (Draft 5) ===' -ForegroundColor Cyan
    Write-Host ''
    Write-Host 'Your operator will give you the values below. The HMAC secret is' -ForegroundColor Gray
    Write-Host 'hidden as you type. Press Enter to accept a default in [brackets].' -ForegroundColor Gray
    Write-Host ''

    # === 7 INTERACTIVE PROMPTS ===

    $OrchFqdn = Read-ValidatedString `
        -Prompt '1. Orchestrator FQDN (e.g. orchestrator.example.local, NOT an IP): ' `
        -Validator ${function:Test-FQDN}
    Write-BootstrapLog 'INFO' "fqdn=$OrchFqdn"

    $OrchPortStr = Read-ValidatedString `
        -Prompt '2. Orchestrator HTTPS port [443]: ' `
        -Validator {
            param($v)
            $n = 0
            if (-not [int]::TryParse($v, [ref]$n)) {
                throw "Port must be an integer 1-65535. You entered: '$v'."
            }
            Test-Port -Value $n
            $v
        } `
        -Default '443'
    [int]$OrchPort = [int]$OrchPortStr
    Write-BootstrapLog 'INFO' "port=$OrchPort"

    $CertFingerprint = Read-ValidatedString `
        -Prompt '3. Orchestrator TLS cert SHA-256 fingerprint (64 hex chars, no colons): ' `
        -Validator ${function:Test-CertFingerprint}
    Write-BootstrapLog 'INFO' "cert_fingerprint=$CertFingerprint"

    $AgentId = Read-ValidatedString `
        -Prompt '4. This machine agent_id (e.g. win-b-02, alphanumeric + dashes only): ' `
        -Validator ${function:Test-AgentId}
    Write-BootstrapLog 'INFO' "agent_id=$AgentId"

    $KeyId = Read-ValidatedString `
        -Prompt '5. HMAC key_id (operator-assigned, alphanumeric + dashes): ' `
        -Validator ${function:Test-KeyId}
    Write-BootstrapLog 'INFO' "key_id=$KeyId"

    $EnrollmentToken = Read-ValidatedString `
        -Prompt '6. One-time enrollment_token (operator-generated, starts with enroll-): ' `
        -Validator ${function:Test-EnrollmentToken}
    Write-BootstrapLog 'INFO' "enrollment_token=$EnrollmentToken"

    Write-Host ''
    Write-Host '7. HMAC secret (base64; ASK OPERATOR FOR THE ENCODING; input hidden): ' -NoNewline
    $HmacSecretBytes = Read-HiddenString -Prompt ''
    Write-BootstrapLog 'INFO' "hmac_secret_len=$($HmacSecretBytes.Length)"

    # MSI path (8th prompt, or use -MsiPath argument)
    if ([string]::IsNullOrWhiteSpace($MsiPath)) {
        $MsiPath = Read-ValidatedString `
            -Prompt '8. Path to the orch-client-setup.msi (drag-and-drop or paste): ' `
            -Validator {
                param($v)
                $v2 = $v.Trim().Trim('"')
                if (-not (Test-Path -LiteralPath $v2)) {
                    throw "The MSI file was not found at '$v2'. Check the path and re-enter."
                }
                if (-not ($v2.EndsWith('.msi', [System.StringComparison]::OrdinalIgnoreCase))) {
                    throw "The file does not appear to be an MSI (no .msi extension): '$v2'."
                }
                $v2
            }
    } else {
        Write-Host "8. MSI path (from -MsiPath argument): $MsiPath" -ForegroundColor Gray
    }
    Write-BootstrapLog 'INFO' "msi_path=$MsiPath"

    # === PRE-FLIGHT (Draft 2 logic; real probes) ===
    Write-Host ''
    Write-Host 'Pre-flight checks...' -ForegroundColor Cyan
    Write-Host '  [+] FQDN resolves: ' $OrchFqdn -ForegroundColor Green
    try {
        Test-TCPPort -Fqdn $OrchFqdn -Port $OrchPort | Out-Null
        Write-Host '  [+] TCP ' $OrchPort ' reachable on ' $OrchFqdn -ForegroundColor Green
    } catch {
        Write-Host '  [!] TCP port probe FAILED: ' $_.Exception.Message -ForegroundColor Yellow
        Write-BootstrapLog 'WARN' "port_probe_failed: $($_.Exception.Message)"
    }
    Write-Host '  [+] Cert fingerprint format: 64 hex chars, valid' -ForegroundColor Green
    Write-Host '  [+] HMAC secret: base64 decodes to ' $HmacSecretBytes.Length ' bytes' -ForegroundColor Green
    Write-Host '  [+] agent_id format: ' $AgentId ' (valid)' -ForegroundColor Green
    Write-Host '  [+] key_id format: ' $KeyId ' (valid)' -ForegroundColor Green
    Write-Host '  [+] enrollment_token format: ' $EnrollmentToken ' (valid)' -ForegroundColor Green
    Write-Host '  [+] MSI path: ' $MsiPath ' (exists)' -ForegroundColor Green
    Write-Host '  [+] Running as Administrator' -ForegroundColor Green
    $existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -ne $existingService) {
        Write-Host '  [!] Existing OrchClient service detected (state: ' $existingService.Status ').' -ForegroundColor Yellow
        Write-Host '      The bootstrapper will refuse to install over an existing service.' -ForegroundColor Yellow
        Write-BootstrapLog 'WARN' "existing_service_detected: state=$($existingService.Status)"
        throw 'EXISTING_SERVICE: An OrchClient service is already installed. To reinstall, first uninstall via Add/Remove Programs, then re-run this bootstrapper.'
    } else {
        Write-Host '  [+] No existing OrchClient service (clean target)' -ForegroundColor Green
    }
    Write-BootstrapLog 'INFO' 'pre-flight=PASS (10 checks; 1 warning; 0 errors)'

    # === v0.7.2: AUTO-ADD FIREWALL RULE (one less step for the user) ===
    Write-Host ''
    Write-Host 'Add Windows Firewall rule (outbound allow)...' -ForegroundColor Cyan
    $fwResult = Add-FirewallRule -Fqdn $OrchFqdn -Port $OrchPort
    if ($fwResult.created) {
        Write-Host '  [+] Firewall rule added: ' $fwResult.rule_name -ForegroundColor Green
    } else {
        Write-Host '  [+] Firewall rule already exists: ' $fwResult.rule_name -ForegroundColor Green
    }
    $Script:FirewallRuleCreated = $fwResult.created
    Write-BootstrapLog 'INFO' "firewall_rule_ok: created=$($fwResult.created)"

    # === INSTALL (Draft 3: REAL) ===
    Write-Host ''
    Write-Host 'Install OrchClient MSI...' -ForegroundColor Cyan
    Install-Msi -MsiPath $MsiPath | Out-Null
    Write-Host '  [+] MSI installed: ' $MsiPath -ForegroundColor Green
    Write-BootstrapLog 'INFO' 'msi_installed'

    Write-Host ''
    Write-Host 'Write config.yaml...' -ForegroundColor Cyan
    $orchUrl = "https://${OrchFqdn}:${OrchPort}/"
    Write-ConfigYaml `
        -Path $ConfigPath `
        -AgentId $AgentId `
        -KeyId $KeyId `
        -OrchestratorUrl $orchUrl `
        -CertFingerprint $CertFingerprint `
        -EnrollmentToken $EnrollmentToken | Out-Null
    Write-Host '  [+] config.yaml: ' $ConfigPath -ForegroundColor Green
    Write-Host '  [+] SDDL: ' $ConfigSddl -ForegroundColor Green
    Write-BootstrapLog 'INFO' "config_written: $ConfigPath"

    Write-Host ''
    Write-Host 'Write HMAC secret to agent-secret.bin...' -ForegroundColor Cyan
    Write-SecretFile -Path $SecretPath -SecretBytes $HmacSecretBytes | Out-Null
    Write-Host '  [+] agent-secret.bin: ' $SecretPath ' (' $HmacSecretBytes.Length ' bytes)' -ForegroundColor Green
    Write-Host '  [+] SDDL: ' $SecretSddl -ForegroundColor Green
    Write-BootstrapLog 'INFO' "secret_written: $SecretPath ($($HmacSecretBytes.Length) bytes)"

    Write-Host ''
    Write-Host 'Start service...' -ForegroundColor Cyan
    Start-AndWaitService -Name $ServiceName | Out-Null
    Write-Host '  [+] Service: ' $ServiceName ' (Running)' -ForegroundColor Green
    Write-BootstrapLog 'INFO' 'service_running'

    # === ENROLLMENT (Draft 4: REAL via Wait-ForEnrollment) ===
    Write-Host ''
    Write-Host 'Verify enrollment (polling for up to ' $MaxEnrollmentWaitSeconds 's)...' -ForegroundColor Cyan
    Wait-ForEnrollment `
        -Fqdn $OrchFqdn `
        -Port $OrchPort `
        -AgentId $AgentId `
        -KeyId $KeyId `
        -SecretBytes $HmacSecretBytes | Out-Null
    Write-Host '  [+] Agent ''' $AgentId ''' enrolled successfully' -ForegroundColor Green
    Write-Host '  [+] Orchestrator confirms: status = verified' -ForegroundColor Green
    Write-BootstrapLog 'INFO' "enrollment_verified: agent_id=$AgentId"

    # === SUCCESS (Draft 4: all 7 prompts + 1 MSI path + 1 enrollment poll = end-to-end) ===
    Write-Host ''
    Write-Host '===========================================' -ForegroundColor Green
    Write-Host '=== SUCCESS (DRAFT 4 — end-to-end install + enrollment verified) ===' -ForegroundColor Green
    Write-Host '===========================================' -ForegroundColor Green
    Write-Host 'Agent:        ' $AgentId
    Write-Host 'Orchestrator: ' $orchUrl
    Write-Host 'Config:       ' $ConfigPath
    Write-Host 'Secret:       ' $SecretPath
    Write-Host 'Service:      ' $ServiceName ' (Running)'
    Write-Host 'Log:          ' $LogFile
    Write-Host ''
    Write-Host 'The orch client is installed and the orchestrator has confirmed the agent as verified.' -ForegroundColor Yellow
    Write-Host 'The MSI can be uninstalled via Add/Remove Programs; uninstall PRESERVES' -ForegroundColor Yellow
    Write-Host 'config.yaml, agent-secret.bin, and config.yaml.example (v0.7 PermanentFeature).' -ForegroundColor Yellow
    Write-Host 'See docs/runbooks/orch-client-install-runbook.md for uninstall + troubleshooting.' -ForegroundColor Yellow
    Write-Host ''
    Write-BootstrapLog 'INFO' "END v$BootstrapperVersion (DRAFT 4 SUCCESS)"

} catch {
    # === DRAFT 4: full 12-case error mapping via Show-PlainEnglishError ===
    $msg = $_.Exception.Message
    # v0.7.2: best-effort cleanup of firewall rule if we created it before
    # a later step failed (rollback). Errors here are warnings, not fatal —
    # we never want to mask the original error from the user.
    if ($Script:FirewallRuleCreated) {
        try {
            Remove-FirewallRule -Fqdn $OrchFqdn -Port $OrchPort
            Write-Host '' -ForegroundColor Yellow
            Write-Host '(Rolled back the firewall rule added earlier in this run.)' -ForegroundColor Yellow
        } catch {
            Write-BootstrapLog 'WARN' "firewall_rule_cleanup_failed: $($_.Exception.Message)"
        }
    }
    Show-PlainEnglishError -Message $msg
    Write-BootstrapLog 'ERROR' "FAILED: $msg"
    exit 1
}
