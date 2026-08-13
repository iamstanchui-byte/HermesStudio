# === install-orch-client.ps1 ===
# v0.7.1 Orch Client Bootstrapper (DRAFT 1, 2026-08-13)
#
# One-shot interactive install for the new orch client (PyInstaller + WiX
# MSI per docs/proposals/orch-client-build-impl-plan-v0.7.md). Replaces the
# 8 manual PowerShell steps in docs/runbooks/orch-client-install-runbook.md
# v0.3.
#
# Target user: semi-technical (per locked user profile); NOT developers.
# Goal: from "user runs the script" to "user sees SUCCESS" in < 2 minutes,
# with zero stack traces and plain-English error messages at every failure.
#
# Per v0.7.1 §0.af-bootstrap design:
#   - 7 interactive prompts (FQDN, port, cert fingerprint, agent_id, key_id,
#     enrollment_token, HMAC secret)
#   - Pre-flight checks (Day 2-4: FQDN resolves, TCP port, cert fingerprint
#     regex, base64 decode, agent_id regex)
#   - MSI install via msiexec /qn (Day 3)
#   - Atomic config.yaml write (v0.7 §0.z layout) (Day 3)
#   - agent-secret.bin write with locked SDDL D:P(A;;FA;;;SY)(A;;FA;;;BA) (Day 3)
#   - Start-Service + 30s poll (Day 3)
#   - 60s HMAC-signed enrollment poll (Day 4)
#   - 12 plain-English error messages (Day 4)
#
# DRAFT 1 SCOPE (this file):
#   - Header + locked constants
#   - Test-Administrator (fail-closed)
#   - Write-BootstrapLog (timestamps + plain text)
#   - Read-ValidatedString (prompt with format validator + retry loop)
#   - Read-HiddenString (SecureString for HMAC secret)
#   - 7 prompt calls in main flow
#   - Pre-flight + install are STUBS that print "[Day 2-4 work]" markers
#   - The 7 format validators are inline in main flow (Day 2 will move them
#     into dedicated helper functions Test-FQDN / Test-TCPPort / etc.)
#
# NOT IN DRAFT 1 (intentional):
#   - Actual MSI install (Day 3)
#   - Actual config.yaml + agent-secret.bin writes (Day 3)
#   - Actual service start (Day 3)
#   - Actual enrollment poll (Day 4)
#   - Plain-English error mapping for install/enroll errors (Day 4)
#   - 8 of the 15 helper functions (per §0.af-bootstrap function table)
#
# PowerShell parser must report 0 errors for this file (per §9 row O
# acceptance criterion). The build script (§6 step 14) verifies this.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# === Locked constants (per §0.af-bootstrap locked-constants table) ===
$InstallDir              = 'C:\Program Files\HermesOrchClient'
$StateDir                = 'C:\ProgramData\HermesOrchClient'
$ServiceName             = 'OrchClient'
$LogFile                 = Join-Path $StateDir 'install.log'
$OrchDefaultPort         = 443
$MaxEnrollmentWaitSeconds = 60

# Per §1.4 + §1.6: signed endpoints forbid query strings; cert fingerprint
# is the lower-case hex SHA-256 of the orch server's server.crt DER bytes.
$CertFingerprintRegex    = '^[0-9a-f]{64}$'
$AgentIdRegex            = '^[a-zA-Z0-9-]+$'
$EnrollmentTokenPrefix   = 'enroll-'

# === Bootstrapper version (for log + diagnostics) ===
$BootstrapperVersion     = '0.7.1-draft1'

# === Test-Administrator ===
# Throws a plain-English error if the script is not running elevated.
# Per §0.af-bootstrap row 1 of the error table.
function Test-Administrator {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'NOT_ADMIN'
    }
}

# === Write-BootstrapLog ===
# Appends a timestamped line to $LogFile. Creates the directory if needed.
# Format: 'YYYY-MM-DD HH:MM:SS.mmm  LEVEL  message'
function Write-BootstrapLog {
    param(
        [Parameter(Mandatory)][ValidateSet('INFO','WARN','ERROR')][string]$Level,
        [Parameter(Mandatory)][string]$Message
    )
    if (-not (Test-Path -LiteralPath (Split-Path -LiteralPath $LogFile -Parent))) {
        New-Item -ItemType Directory -Path (Split-Path -LiteralPath $LogFile -Parent) -Force | Out-Null
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
    if ($bytes.Length -lt 16) {
        throw 'SECRET_TOO_SHORT'
    }
    return ,$bytes
}

# === Format validators (inline in Draft 1; Day 2 will extract) ===

# Cert fingerprint: 64 hex chars, no colons
$script:ValidateCertFingerprint = {
    param($v)
    if ($v -notmatch $CertFingerprintRegex) {
        throw "Cert fingerprint must be exactly 64 hex characters (0-9, a-f), no colons, no spaces. You entered: '$v'."
    }
    $true
}

# agent_id: alphanumeric + dashes
$script:ValidateAgentId = {
    param($v)
    if ($v -notmatch $AgentIdRegex) {
        throw "agent_id must contain only letters, digits, and dashes (e.g. 'win-b-02'). You entered: '$v'."
    }
    if ($v.Length -gt 64) {
        throw "agent_id must be 64 characters or fewer. You entered a $( $v.Length)-char value."
    }
    $true
}

# key_id: alphanumeric + dashes (same regex as agent_id; keep separate for future divergence)
$script:ValidateKeyId = {
    param($v)
    if ($v -notmatch $AgentIdRegex) {
        throw "key_id must contain only letters, digits, and dashes. You entered: '$v'."
    }
    $true
}

# enrollment_token: starts with 'enroll-', rest is [a-zA-Z0-9_-]
$script:ValidateEnrollmentToken = {
    param($v)
    if ($v.Length -lt 8) {
        throw "enrollment_token must be at least 8 characters. You entered a $( $v.Length)-char value."
    }
    if ($v -notmatch "^${EnrollmentTokenPrefix}[a-zA-Z0-9_-]+$") {
        throw "enrollment_token must start with '$EnrollmentTokenPrefix' followed by letters, digits, underscores, or dashes. You entered: '$v'."
    }
    $true
}

# Port: 1-65535
$script:ValidatePort = {
    param($v)
    $n = 0
    if (-not [int]::TryParse($v, [ref]$n)) {
        throw "Port must be an integer 1-65535. You entered: '$v'."
    }
    if ($n -lt 1 -or $n -gt 65535) {
        throw "Port must be between 1 and 65535. You entered: $n."
    }
    $true
}

# FQDN: basic shape check (Day 2 will add Dns.Resolve)
$script:ValidateFQDN = {
    param($v)
    if ($v -notmatch '^[A-Za-z0-9][A-Za-z0-9.\-]+[A-Za-z0-9]$') {
        throw "FQDN must be a hostname like 'orchestrator.example.local' (not an IP). You entered: '$v'."
    }
    if ($v.Length -gt 253) {
        throw "FQDN is too long ($( $v.Length) chars; max 253)."
    }
    $true
}

# === MAIN FLOW ===
try {
    Test-Administrator
    if (-not (Test-Path -LiteralPath (Split-Path -LiteralPath $LogFile -Parent))) {
        New-Item -ItemType Directory -Path (Split-Path -LiteralPath $LogFile -Parent) -Force | Out-Null
    }
    Write-BootstrapLog 'INFO' "START v$BootstrapperVersion on $env:COMPUTERNAME (user=$env:USERNAME)"

    Write-Host ''
    Write-Host '=== Orch Client Bootstrapper v0.7.1 ===' -ForegroundColor Cyan
    Write-Host ''
    Write-Host 'Your operator will give you the values below. The HMAC secret is' -ForegroundColor Gray
    Write-Host 'hidden as you type. Press Enter to accept a default in [brackets].' -ForegroundColor Gray
    Write-Host ''

    # === 7 INTERACTIVE PROMPTS (DRAFT 1) ===
    # Each prompt is real (Draft 1 has format validators). The subsequent
    # pre-flight checks + MSI install + config write + service start +
    # enrollment poll are placeholders for Day 2-4.

    $OrchFqdn = Read-ValidatedString `
        -Prompt '1. Orchestrator FQDN (e.g. orchestrator.example.local, NOT an IP): ' `
        -Validator $script:ValidateFQDN
    Write-BootstrapLog 'INFO' "fqdn=$OrchFqdn"

    $OrchPort = Read-ValidatedString `
        -Prompt '2. Orchestrator HTTPS port [443]: ' `
        -Validator $script:ValidatePort `
        -Default '443'
    [int]$OrchPortInt = [int]$OrchPort
    Write-BootstrapLog 'INFO' "port=$OrchPortInt"

    $CertFingerprint = Read-ValidatedString `
        -Prompt '3. Orchestrator TLS cert SHA-256 fingerprint (64 hex chars, no colons): ' `
        -Validator $script:ValidateCertFingerprint
    Write-BootstrapLog 'INFO' "cert_fingerprint=$CertFingerprint"

    $AgentId = Read-ValidatedString `
        -Prompt '4. This machine agent_id (e.g. win-b-02, alphanumeric + dashes only): ' `
        -Validator $script:ValidateAgentId
    Write-BootstrapLog 'INFO' "agent_id=$AgentId"

    $KeyId = Read-ValidatedString `
        -Prompt '5. HMAC key_id (operator-assigned, alphanumeric + dashes): ' `
        -Validator $script:ValidateKeyId
    Write-BootstrapLog 'INFO' "key_id=$KeyId"

    $EnrollmentToken = Read-ValidatedString `
        -Prompt '6. One-time enrollment_token (operator-generated, starts with enroll-): ' `
        -Validator $script:ValidateEnrollmentToken
    Write-BootstrapLog 'INFO' "enrollment_token=$EnrollmentToken"

    Write-Host ''
    Write-Host '7. HMAC secret (base64; ASK OPERATOR FOR THE ENCODING; input hidden): ' -NoNewline
    $HmacSecretBytes = Read-HiddenString -Prompt ''
    Write-BootstrapLog 'INFO' "hmac_secret_len=$($HmacSecretBytes.Length)"

    # === PRE-FLIGHT (DRAFT 1: placeholders for Day 2) ===
    Write-Host ''
    Write-Host 'Pre-flight checks...' -ForegroundColor Cyan
    Write-Host '  [Day 2 work] FQDN resolves via [Net.Dns]::Resolve()'
    Write-Host '  [Day 2 work] TCP port reachable via Test-NetConnection'
    Write-Host '  [Day 2 work] Cert fingerprint format: ALREADY VALIDATED in prompt 3'
    Write-Host '  [Day 2 work] HMAC secret: ALREADY BASE64-DECODED in prompt 7'
    Write-Host '  [Day 2 work] agent_id format: ALREADY VALIDATED in prompt 4'
    Write-Host '  [Day 2 work] key_id format: ALREADY VALIDATED in prompt 5'
    Write-Host '  [Day 2 work] enrollment_token format: ALREADY VALIDATED in prompt 6'
    Write-Host '  [Day 3 work] Running as Administrator: PASS'
    Write-Host '  [Day 3 work] No existing OrchClient service (clean target)'
    Write-BootstrapLog 'INFO' 'pre-flight=DRAFT1_PLACEHOLDERS (Day 2-3 will run real checks)'

    # === INSTALL (DRAFT 1: placeholders for Day 3-4) ===
    Write-Host ''
    Write-Host 'Install OrchClient MSI...' -ForegroundColor Cyan
    Write-Host '  [Day 3 work] msiexec /i orch-client-setup.msi /qn /l*v C:\ProgramData\HermesOrchClient\install.log'
    Write-Host ''
    Write-Host 'Write config.yaml...' -ForegroundColor Cyan
    Write-Host '  [Day 3 work] Atomic Write-ConfigYaml at C:\ProgramData\HermesOrchClient\config.yaml'
    Write-Host ''
    Write-Host 'Write HMAC secret to agent-secret.bin...' -ForegroundColor Cyan
    Write-Host '  [Day 3 work] [IO.File]::WriteAllBytes + Set-Acl with SDDL D:P(A;;FA;;;SY)(A;;FA;;;BA)'
    Write-Host ''
    Write-Host 'Start service...' -ForegroundColor Cyan
    Write-Host '  [Day 3 work] Start-Service OrchClient + 30s poll for Running'
    Write-Host ''
    Write-Host 'Verify enrollment (polling for up to 60s)...' -ForegroundColor Cyan
    Write-Host '  [Day 4 work] Poll HMAC-signed /api/agents/<agent_id>/status every 5s for 60s'
    Write-BootstrapLog 'INFO' 'install=DRAFT1_PLACEHOLDERS (Day 3-4 will run real install + enroll)'

    # === SUCCESS (DRAFT 1: print the success template so the user sees the goal) ===
    Write-Host ''
    Write-Host '===========================================' -ForegroundColor Green
    Write-Host '=== SUCCESS (DRAFT 1 — actual install NOT performed) ===' -ForegroundColor Green
    Write-Host '===========================================' -ForegroundColor Green
    Write-Host 'Validated 7 values for agent_id: ' $AgentId
    Write-Host 'Log: ' $LogFile
    Write-Host ''
    Write-Host 'DRAFT 1 verifies the 7 interactive prompts + format validators.' -ForegroundColor Yellow
    Write-Host 'DRAFT 2 (Day 2) adds pre-flight checks; DRAFT 3 (Day 3) adds the' -ForegroundColor Yellow
    Write-Host 'actual install + config + secret write + service start; DRAFT 4' -ForegroundColor Yellow
    Write-Host '(Day 4) adds enrollment poll + plain-English error mapping.' -ForegroundColor Yellow
    Write-Host ''
    Write-BootstrapLog 'INFO' 'END v$BootstrapperVersion (DRAFT 1)'

} catch {
    # === DRAFT 1 error mapping (placeholder; Day 4 will replace with full table) ===
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
            Write-Host 'The HMAC secret decodes to fewer than 16 bytes.' -ForegroundColor Red
            Write-Host 'Ask your operator to regenerate the secret (must be at least 16 random bytes, base64-encoded).' -ForegroundColor Red
        }
        'EMPTY_SECRET' {
            Write-Host ''
            Write-Host 'The HMAC secret cannot be empty.' -ForegroundColor Red
            Write-Host 'Re-run the script and paste the secret when prompted.' -ForegroundColor Red
        }
        'TOO_MANY_ATTEMPTS*' {
            Write-Host ''
            Write-Host 'Too many invalid attempts for a prompt. The bootstrapper aborts to avoid silent data corruption.' -ForegroundColor Red
            Write-Host 'Re-run the script and re-enter the value more carefully.' -ForegroundColor Red
        }
        default {
            Write-Host ''
            Write-Host "An unexpected error occurred: $msg" -ForegroundColor Red
            Write-Host 'This is a DRAFT 1 placeholder. Day 4 will replace this with the full plain-English error table.' -ForegroundColor Red
        }
    }
    Write-BootstrapLog 'ERROR' "FAILED: $msg"
    exit 1
}
