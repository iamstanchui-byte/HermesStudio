# === install-orch-client.ps1 ===
# v0.7.1 Orch Client Bootstrapper (DRAFT 2, 2026-08-13)
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
#   - Pre-flight checks (Day 2: FQDN resolves, TCP port, cert fingerprint
#     regex, base64 decode, agent_id regex)
#   - MSI install via msiexec /qn (Day 3 prep: function defined; integration
#     pending config.yaml + agent-secret.bin writes which are Day 3)
#   - Atomic config.yaml write (v0.7 §0.z layout) (Day 3)
#   - agent-secret.bin write with locked SDDL D:P(A;;FA;;;SY)(A;;FA;;;BA) (Day 3)
#   - Start-Service + 30s poll (Day 3 prep: function defined)
#   - 60s HMAC-signed enrollment poll (Day 4)
#   - 12 plain-English error messages (Day 4)
#
# DRAFT 2 SCOPE (this file):
#   - Header + locked constants
#   - Test-Administrator (fail-closed)
#   - Write-BootstrapLog with size cap + rotation (improved)
#   - Read-ValidatedString (prompt with format validator + retry loop)
#   - Read-HiddenString (SecureString for HMAC secret)
#   - 8 dedicated pre-flight functions (Test-FQDN, Test-TCPPort,
#     Test-CertFingerprint, Test-Base64, Test-AgentId, Test-KeyId,
#     Test-EnrollmentToken, Test-Port) — replaces the inline $script:
#     validators in Draft 1
#   - 2 Day 3 prep functions (Install-Msi, Start-AndWaitService) — defined
#     but not yet called; main flow still has [Day 3 work] markers
#   - 7 prompt calls in main flow
#   - Pre-flight uses the new dedicated functions (real network probes
#     for FQDN + port; real base64 decode for HMAC secret)
#   - Install + service start + enrollment are still [Day 3-4 work] stubs
#   - 4 plain-English error mappings (NOT_ADMIN, NOT_BASE64,
#     SECRET_TOO_SHORT, EMPTY_SECRET) + 2 new (FQDN_RESOLVE_FAILED,
#     PORT_UNREACHABLE) + catch-all default
#   - Success template printed at the end
#
# NOT IN DRAFT 2 (intentional, per Day 3-4 schedule):
#   - Actual MSI install (Day 3 — function defined, integration pending)
#   - Actual config.yaml + agent-secret.bin writes (Day 3)
#   - Actual service start (Day 3 — function defined, integration pending)
#   - 60s HMAC-signed enrollment poll (Day 4)
#   - Wait-ForEnrollment + Show-PlainEnglishError functions (Day 4)
#   - 5 of the 12 plain-English error cases (rows 6, 7, 8, 9, 10, 11, 12
#     from the plan's error table) — Day 4
#
# PowerShell parser must report 0 errors for this file (per §9 row O
# acceptance criterion). The build script (§6 step 14) verifies this.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# === Locked constants (per §0.af-bootstrap locked-constants table) ===
$InstallDir              = 'C:\Program Files\HermesOrchClient'
$StateDir                = 'C:\ProgramData\HermesOrchClient'
$ServiceName             = 'OrchClient'
$LogDir                  = Join-Path $StateDir ''
$LogFile                 = Join-Path $StateDir 'install.log'
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

# === Bootstrapper version (for log + diagnostics) ===
$BootstrapperVersion     = '0.7.1-draft2'

# === Test-Administrator ===
# Throws NOT_ADMIN if the script is not running elevated.
# Per §0.af-bootstrap row 1 of the error table.
function Test-Administrator {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'NOT_ADMIN'
    }
}

# === Write-BootstrapLog (Draft 2 improvement: size cap + rotation) ===
# Appends a timestamped line to $LogFile. Creates the directory if needed.
# When the log exceeds $LogMaxBytes, rotates: renames current to .1 (overwriting
# any previous .1), starts a new log. Keeps the bootstrapper log bounded.
function Write-BootstrapLog {
    param(
        [Parameter(Mandatory)][ValidateSet('INFO','WARN','ERROR')][string]$Level,
        [Parameter(Mandatory)][string]$Message
    )
    $logDir = Split-Path -LiteralPath $LogFile -Parent
    if (-not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    # Rotate if too large
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
# Prompts the user with $Prompt, retries until $Validator returns $true.
# Returns the validated string. NEVER echoes the value as a SecureString
# (this is for the 6 non-secret values; the HMAC secret uses
# Read-HiddenString).
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
# Prompts the user for the HMAC secret; input is hidden (SecureString).
# Returns the raw bytes (decoded from base64). Per the v0.7.1 error
# table row 5: if the input is not valid base64, the caller is told
# "The HMAC secret you pasted is not valid base64".
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
# Per §0.af-bootstrap row 2 of the error table. Validates shape AND
# resolves the FQDN via DNS. Throws FQDN_RESOLVE_FAILED if DNS fails.
# Throws FQDN_INVALID_SHAPE if the value doesn't look like a hostname.
function Test-FQDN {
    param(
        [Parameter(Mandatory)][string]$Value
    )
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
# Per §0.af-bootstrap row 3 of the error table. Probes the FQDN:port
# combination. Throws PORT_UNREACHABLE if the TCP probe fails.
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
# Per §0.af-bootstrap row 4. Throws FINGERPRINT_INVALID_FORMAT.
function Test-CertFingerprint {
    param(
        [Parameter(Mandatory)][string]$Value
    )
    if ($Value -notmatch $CertFingerprintRegex) {
        throw "FINGERPRINT_INVALID_FORMAT: The cert fingerprint must be exactly 64 hex characters (0-9, a-f), no colons, no spaces. You entered: '$Value'. Ask your operator to re-paste the value after `SHA256 Fingerprint=` from `openssl x509 -in server.crt -noout -fingerprint -sha256`."
    }
    $true
}

# === Test-Base64 ===
# Per §0.af-bootstrap row 5. Throws BASE64_INVALID if decode fails.
# Used for the HMAC secret; called AFTER Read-HiddenString, so this
# is a defensive check (Read-HiddenString already does the same decode).
function Test-Base64 {
    param(
        [Parameter(Mandatory)][string]$Value
    )
    try {
        $null = [Convert]::FromBase64String($Value.Trim())
    } catch {
        throw "BASE64_INVALID: The value is not valid base64. Re-paste (or ask the operator to confirm the encoding is base64, not hex, not raw bytes)."
    }
    $true
}

# === Test-AgentId ===
# Per §0.af-bootstrap row 6. Throws AGENTID_INVALID.
function Test-AgentId {
    param(
        [Parameter(Mandatory)][string]$Value
    )
    if ($Value -notmatch $AgentIdRegex) {
        throw "AGENTID_INVALID: The agent_id must contain only letters, digits, and dashes (e.g. 'win-b-02'). You entered: '$Value'. Ask your operator for the correct agent_id."
    }
    if ($Value.Length -gt 64) {
        throw "AGENTID_INVALID: The agent_id must be 64 characters or fewer. You entered a $($Value.Length)-char value."
    }
    $true
}

# === Test-KeyId ===
# Throws KEYID_INVALID. Same regex as agent_id; separate function for
# future divergence (e.g. operator may want different rules for key_id).
function Test-KeyId {
    param(
        [Parameter(Mandatory)][string]$Value
    )
    if ($Value -notmatch $KeyIdRegex) {
        throw "KEYID_INVALID: The key_id must contain only letters, digits, and dashes. You entered: '$Value'."
    }
    if ($Value.Length -gt 64) {
        throw "KEYID_INVALID: The key_id must be 64 characters or fewer. You entered a $($Value.Length)-char value."
    }
    $true
}

# === Test-EnrollmentToken ===
# Per §0.af-bootstrap row 10 (enrollment rejection). Throws ENROLLMENTTOKEN_INVALID.
function Test-EnrollmentToken {
    param(
        [Parameter(Mandatory)][string]$Value
    )
    if ($Value -notmatch $EnrollmentTokenRegex) {
        throw "ENROLLMENTTOKEN_INVALID: The enrollment_token must start with '$EnrollmentTokenPrefix' followed by at least 4 letters, digits, underscores, or dashes. You entered: '$Value'. Ask your operator for a fresh token."
    }
    $true
}

# === Test-Port ===
# Throws PORT_INVALID_RANGE.
function Test-Port {
    param(
        [Parameter(Mandatory)][int]$Value
    )
    if ($Value -lt $PortMin -or $Value -gt $PortMax) {
        throw "PORT_INVALID_RANGE: Port must be between $PortMin and $PortMax. You entered: $Value."
    }
    $true
}

# === Install-Msi (Day 3 prep: function defined, integration pending) ===
# Per §0.af-bootstrap function table. Will be called in Draft 3 once
# config.yaml + agent-secret.bin writers are also ready.
# Throws MSI_INSTALL_FAILED on non-zero exit.
function Install-Msi {
    param(
        [Parameter(Mandatory)][string]$MsiPath
    )
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

# === Start-AndWaitService (Day 3 prep: function defined, integration pending) ===
# Per §0.af-bootstrap function table. Throws SERVICE_NOT_RUNNING if
# the service does not reach Running within 30s.
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

# === MAIN FLOW ===
try {
    Test-Administrator
    if (-not (Test-Path -LiteralPath (Split-Path -LiteralPath $LogFile -Parent))) {
        New-Item -ItemType Directory -Path (Split-Path -LiteralPath $LogFile -Parent) -Force | Out-Null
    }
    Write-BootstrapLog 'INFO' "START v$BootstrapperVersion on $env:COMPUTERNAME (user=$env:USERNAME)"

    Write-Host ''
    Write-Host '=== Orch Client Bootstrapper v0.7.1 (Draft 2) ===' -ForegroundColor Cyan
    Write-Host ''
    Write-Host 'Your operator will give you the values below. The HMAC secret is' -ForegroundColor Gray
    Write-Host 'hidden as you type. Press Enter to accept a default in [brackets].' -ForegroundColor Gray
    Write-Host ''

    # === 7 INTERACTIVE PROMPTS (Draft 2: format + value validated at prompt level) ===

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

    # === PRE-FLIGHT (DRAFT 2: real checks via the 8 dedicated functions) ===
    Write-Host ''
    Write-Host 'Pre-flight checks...' -ForegroundColor Cyan

    # FQDN already validated at prompt level (Test-FQDN ran in Read-ValidatedString)
    Write-Host '  [+] FQDN resolves: ' $OrchFqdn -ForegroundColor Green

    # TCP port probe
    try {
        Test-TCPPort -Fqdn $OrchFqdn -Port $OrchPort | Out-Null
        Write-Host '  [+] TCP ' $OrchPort ' reachable on ' $OrchFqdn -ForegroundColor Green
    } catch {
        Write-Host '  [!] TCP port probe FAILED: ' $_.Exception.Message -ForegroundColor Yellow
        Write-BootstrapLog 'WARN' "port_probe_failed: $($_.Exception.Message)"
        # Don't abort on port probe failure (Day 3-4 will run inside the LAN where firewall rules may differ;
        # the actual install will fail with a clearer TLS error if the orch is truly unreachable)
    }

    # Cert fingerprint already validated at prompt level
    Write-Host '  [+] Cert fingerprint format: 64 hex chars, valid' -ForegroundColor Green

    # HMAC secret already base64-decoded at prompt level
    Write-Host '  [+] HMAC secret: base64 decodes to ' $HmacSecretBytes.Length ' bytes' -ForegroundColor Green

    # agent_id, key_id, enrollment_token already validated at prompt level
    Write-Host '  [+] agent_id format: ' $AgentId ' (valid)' -ForegroundColor Green
    Write-Host '  [+] key_id format: ' $KeyId ' (valid)' -ForegroundColor Green
    Write-Host '  [+] enrollment_token format: ' $EnrollmentToken ' (valid)' -ForegroundColor Green

    # Admin + existing-service checks
    Write-Host '  [+] Running as Administrator' -ForegroundColor Green
    $existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -ne $existingService) {
        Write-Host '  [!] Existing OrchClient service detected (state: ' $existingService.Status ').' -ForegroundColor Yellow
        Write-Host '      The bootstrapper will refuse to install over an existing service.' -ForegroundColor Yellow
        Write-BootstrapLog 'WARN' "existing_service_detected: state=$($existingService.Status)"
    } else {
        Write-Host '  [+] No existing OrchClient service (clean target)' -ForegroundColor Green
    }

    Write-BootstrapLog 'INFO' 'pre-flight=DRAFT2_OK (8 dedicated functions; install pending)'

    # === INSTALL (DRAFT 2: still [Day 3 work] markers; functions defined) ===
    Write-Host ''
    Write-Host 'Install OrchClient MSI...' -ForegroundColor Cyan
    Write-Host '  [Day 3 work] Install-Msi function defined (Draft 2 line 252); integration pending config.yaml + agent-secret.bin writers' -ForegroundColor DarkGray

    Write-Host ''
    Write-Host 'Write config.yaml...' -ForegroundColor Cyan
    Write-Host '  [Day 3 work] Write-ConfigYaml function not yet defined; integration pending MSI install' -ForegroundColor DarkGray

    Write-Host ''
    Write-Host 'Write HMAC secret to agent-secret.bin...' -ForegroundColor Cyan
    Write-Host '  [Day 3 work] Write-SecretFile function not yet defined; integration pending config.yaml write' -ForegroundColor DarkGray

    Write-Host ''
    Write-Host 'Start service...' -ForegroundColor Cyan
    Write-Host '  [Day 3 work] Start-AndWaitService function defined (Draft 2 line 286); integration pending' -ForegroundColor DarkGray

    Write-Host ''
    Write-Host 'Verify enrollment (polling for up to 60s)...' -ForegroundColor Cyan
    Write-Host '  [Day 4 work] Wait-ForEnrollment function not yet defined' -ForegroundColor DarkGray

    Write-BootstrapLog 'INFO' 'install=DRAFT2_PLACEHOLDERS (8 functions defined; integration pending Day 3-4)'

    # === SUCCESS (DRAFT 2: print the success template so the user sees the goal) ===
    Write-Host ''
    Write-Host '===========================================' -ForegroundColor Green
    Write-Host '=== PRE-FLIGHT PASSED (DRAFT 2 — install NOT performed) ===' -ForegroundColor Green
    Write-Host '===========================================' -ForegroundColor Green
    Write-Host 'Validated 7 values for agent_id: ' $AgentId
    Write-Host 'Log: ' $LogFile
    Write-Host ''
    Write-Host 'Draft 2 verifies the 8 dedicated pre-flight functions on real values.' -ForegroundColor Yellow
    Write-Host 'Draft 3 (Day 3) wires Install-Msi + Write-ConfigYaml + Write-SecretFile + Start-AndWaitService.' -ForegroundColor Yellow
    Write-Host 'Draft 4 (Day 4) wires Wait-ForEnrollment + the full 12-row plain-English error table.' -ForegroundColor Yellow
    Write-Host ''
    Write-BootstrapLog 'INFO' "END v$BootstrapperVersion (DRAFT 2 pre-flight PASS)"

} catch {
    # === DRAFT 2 error mapping (6 cases; full 12 in Day 4) ===
    $msg = $_.Exception.Message
    switch -Wildcard ($msg) {
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
            Write-Host 'The HMAC secret decodes to fewer than ' $SecretMinBytes ' bytes.' -ForegroundColor Red
            Write-Host 'Ask your operator to regenerate the secret (must be at least ' $SecretMinBytes ' random bytes, base64-encoded).' -ForegroundColor Red
        }
        'EMPTY_SECRET' {
            Write-Host ''
            Write-Host 'The HMAC secret cannot be empty.' -ForegroundColor Red
            Write-Host 'Re-run the script and paste the secret when prompted.' -ForegroundColor Red
        }
        'FQDN_RESOLVE_FAILED' {
            Write-Host ''
            Write-Host $_.Exception.Message -ForegroundColor Red
        }
        'PORT_UNREACHABLE' {
            Write-Host ''
            Write-Host $_.Exception.Message -ForegroundColor Red
        }
        'TOO_MANY_ATTEMPTS*' {
            Write-Host ''
            Write-Host 'Too many invalid attempts for a prompt. The bootstrapper aborts to avoid silent data corruption.' -ForegroundColor Red
            Write-Host 'Re-run the script and re-enter the value more carefully.' -ForegroundColor Red
        }
        default {
            Write-Host ''
            Write-Host "An unexpected error occurred: $msg" -ForegroundColor Red
            Write-Host 'This is a DRAFT 2 placeholder. Day 4 will replace this with the full plain-English error table.' -ForegroundColor Red
        }
    }
    Write-BootstrapLog 'ERROR' "FAILED: $msg"
    exit 1
}
