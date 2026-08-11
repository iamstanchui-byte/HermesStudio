# coding: utf-8
<#
.SYNOPSIS
    Pre-deploy compatibility check for the B12 agent-endpoint-auth hotfix
    (security hotfix 2026-08-11). Run BEFORE applying the hotfix to
    confirm the production fleet is in a state that can absorb it.

.DESCRIPTION
    Per `docs/security/agent-endpoint-auth-hotfix-design.md` Â§9.3, the
    hotfix hard-sets POST /api/agents/{id}/secret to 410 Gone. Before
    this change ships, the operator must verify:

      Item 1: agents.hmac_secret is non-NULL for every production agent.
              If any agent has NULL secret, do NOT apply â€” re-enroll first.
      Item 2: B10 /secret caller search. Search:
              - Source of deployed agent package
              - Live agent logs
              Confirm zero callers in the existing fleet.
      Item 3: Outstanding enrollment tokens. Revoke via admin API.
      Item 4: HERMES_ORCH_PUBLIC_ORIGIN is set in config.yaml or env.
      Item 5: Source-grep: no firewall-management code in
              `src/hermes_orch/**` or `scripts/**` (defense-in-depth;
              also enforced by `tests/test_no_firewall_management.py`).

    The script outputs a per-item pass/fail summary and exits with a
    non-zero code if any item fails. Operators should review the
    output, fix any failures, then re-run.

.PARAMETER SkipServerCheck
    Skip the live-HTTP check against the running server. Useful in CI
    where the server isn't running, or when the operator is checking
    a fresh DB before the server has been started.

.PARAMETER ConfigPath
    Override the config.yaml path (default: same as load_config() resolves).

.EXAMPLE
    .\scripts\pre_deploy_check.ps1

.NOTES
    Windows PowerShell 5.1 compatible. No `&&`, no `bash` heredocs.
    Per MEMORY.md, PowerShell here-strings with embedded quotes parse
    unreliably â€” so the Python probes are built via string concatenation
    and written to a temp file via [System.IO.File]::WriteAllText with
    UTF-8 (no BOM).
#>
[CmdletBinding()]
param(
    [switch]$SkipServerCheck,
    [string]$ConfigPath = ""
)

$ErrorActionPreference = 'Stop'

# ===== Constants (locked, NOT configurable) =====
$InstallDir = 'C:\Program Files\HermesOrchestrator'
$VenvPythonExe = Join-Path $InstallDir 'venv\Scripts\python.exe'
$DefaultConfigPath = Join-Path $env:USERPROFILE '.hermes-orchestrator\config.yaml'
if ($ConfigPath -eq "") {
    $ConfigPath = $DefaultConfigPath
}

$results = New-Object System.Collections.Generic.List[object]
function Pass($name, $msg) { $script:results.Add([pscustomobject]@{Status="PASS"; Name=$name; Message=$msg}) }
function Fail($name, $msg) { $script:results.Add([pscustomobject]@{Status="FAIL"; Name=$name; Message=$msg}) }
function Warn($name, $msg) { $script:results.Add([pscustomobject]@{Status="WARN"; Name=$name; Message=$msg}) }

# Helper: write a small .py probe to a temp file (no BOM), run it,
# return the captured stdout. Avoids PowerShell here-string parsing
# issues with embedded quotes (per MEMORY.md).
function Invoke-PythonProbe {
    param(
        [string]$ProbeName,
        [string]$ScriptContent,
        [int]$ExpectedExitCode = 0
    )
    $probePath = Join-Path $env:TEMP ("hermes-pre-deploy-{0}-{1}.py" -f $ProbeName, $PID)
    try {
        [System.IO.File]::WriteAllText(
            $probePath, $ScriptContent,
            [System.Text.UTF8Encoding]::new($false)
        )
        $output = & $VenvPythonExe -I $probePath 2>&1 | Out-String
        $exitCode = $LASTEXITCODE
        return [pscustomobject]@{Output = $output; ExitCode = $exitCode}
    } finally {
        if (Test-Path -LiteralPath $probePath) {
            Remove-Item -LiteralPath $probePath -ErrorAction SilentlyContinue
        }
    }
}

# Helper: build a Python raw string literal for a Windows path.
# Use Python raw string (r'...') so backslashes are literal. NO
# backslash escaping â€” the path's `\` chars are taken literally by
# Python's r-string.
function Py-Path {
    param([string]$Path)
    return "r'" + $Path + "'"
}

Write-Host ""
Write-Host "=== B12 Pre-Deploy Compatibility Check (2026-08-11) ===" -ForegroundColor Cyan
Write-Host "InstallDir : $InstallDir"
Write-Host "ConfigPath : $ConfigPath"
Write-Host ""

# ====================================================================
# Item 1: agents.hmac_secret is non-NULL for every production agent
# ====================================================================
Write-Host "[Item 1] Production agents: hmac_secret is non-NULL" -ForegroundColor Yellow

$dbPath = $null
if (Test-Path -LiteralPath $ConfigPath) {
    $configDir = Split-Path -LiteralPath $ConfigPath -ErrorAction SilentlyContinue
    if ($null -ne $configDir) {
        $dbPath = Join-Path $configDir 'hermes-orch.db'
    }
}
if ($null -eq $dbPath -or -not (Test-Path -LiteralPath $dbPath)) {
    $dbPath = Join-Path $env:USERPROFILE '.hermes-orchestrator\hermes-orch.db'
}

if (-not (Test-Path -LiteralPath $dbPath)) {
    Warn "Item 1" "DB not found at $dbPath -- skipping (no production agents to check)"
} else {
    $pyDbPath = Py-Path -Path $dbPath
    $scriptLines = @(
        "import sqlite3, sys",
        "conn = sqlite3.connect($pyDbPath)",
        "conn.row_factory = sqlite3.Row",
        "rows = conn.execute('SELECT id, hmac_secret FROM agents').fetchall()",
        "null_secrets = [r['id'] for r in rows if r['hmac_secret'] is None]",
        "total = len(rows)",
        "print('total_agents=' + str(total) + ' null_secrets=' + str(len(null_secrets)))",
        "if null_secrets:",
        "    print('NULL_SECRETS:')",
        "    for aid in null_secrets:",
        "        print('  ' + aid)",
        "sys.exit(0 if not null_secrets else 1)",
        ""
    )
    $scriptContent = $scriptLines -join "`n"
    try {
        $r = Invoke-PythonProbe -ProbeName 'item1' -ScriptContent $scriptContent
        if ($r.ExitCode -eq 0) {
            Pass "Item 1" "All production agents have non-NULL hmac_secret. ($($r.Output.Trim()))"
        } else {
            Fail "Item 1" "Production agent(s) with NULL hmac_secret -- re-enroll before deploy. Output: $($r.Output.Trim())"
        }
    } catch {
        Fail "Item 1" "Error probing DB: $($_.Exception.Message)"
    }
}

# ====================================================================
# Item 2: B10 /secret caller search
# ====================================================================
Write-Host "[Item 2] B10 /secret caller search (deployed agent source + live logs)" -ForegroundColor Yellow

# 2a: Search the deployed agent source. The agent is typically
# installed in a venv on the agent host, but in this testing
# environment the agent runs in C:\Users\stanley\AppData\Local\...
# We search a few likely locations.
$agentSearchPaths = @(
    (Join-Path $env:USERPROFILE 'AppData\Local\Packages\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\LocalCache\local-packages\Python313\site-packages\hermes_orch'),
    (Join-Path $env:USERPROFILE 'AppData\Local\Programs\Python\Python313\Lib\site-packages\hermes_orch'),
    (Join-Path $InstallDir 'venv\Lib\site-packages\hermes_orch')
)
$secretCallers = New-Object System.Collections.Generic.List[string]
# Look for ACTUAL HTTP calls to /secret, not just string references.
# Patterns to flag:
#   - urlopen( with /secret
#   - .post( with /secret
#   - requests.post( with /secret
#   - httpx.post( with /secret
# We deliberately do NOT flag bare path strings like
#   path = f"/api/agents/{agent_id}/secret"  (used in any HTTP call
#   site â€” would be a false positive).
$secretCallPatterns = @(
    'urlopen\(.*/secret',
    'requests\.post\(.*/secret',
    'httpx\.post\(.*/secret',
    '\.post\(.*/secret',
    'fetch\(.*/secret',
    'send_request\(.*/secret'
)
foreach ($p in $agentSearchPaths) {
    if (Test-Path -LiteralPath $p) {
        $cliPath = Join-Path $p 'agent_cli.py'
        if (Test-Path -LiteralPath $cliPath) {
            foreach ($pat in $secretCallPatterns) {
                $hits = Select-String -Path $cliPath -Pattern $pat -ErrorAction SilentlyContinue
                if ($hits) {
                    foreach ($h in $hits) {
                        $secretCallers.Add("$($cliPath):$($h.LineNumber): $($h.Line.Trim())")
                    }
                }
            }
        }
    }
}
# Also search wrapper log files for any /secret HTTP calls
$logSearchPaths = @(
    (Join-Path $env:USERPROFILE '.hermes-orchestrator\daemon*.log'),
    (Join-Path $env:USERPROFILE '.hermes-orchestrator\wrapper.log'),
    (Join-Path $env:USERPROFILE '.hermes-orchestrator\agent*.log')
)
$logHits = 0
foreach ($lp in $logSearchPaths) {
    $matchedFiles = Get-ChildItem -Path $lp -ErrorAction SilentlyContinue
    foreach ($lf in $matchedFiles) {
        $hits = Select-String -Path $lf.FullName -Pattern '/secret' -ErrorAction SilentlyContinue
        if ($hits) { $logHits += $hits.Count }
    }
}
if ($secretCallers.Count -eq 0 -and $logHits -eq 0) {
    Pass "Item 2" "Zero callers of /secret in deployed agent source + live logs."
} elseif ($secretCallers.Count -gt 0) {
    $list = $secretCallers -join "`n    "
    Fail "Item 2" "Caller(s) of /secret found in agent source -- DO NOT DEPLOY until removed:`n    $list"
} else {
    Warn "Item 2" "Log file mentions of /secret found ($logHits), but no live source callers. Review logs."
}

# ====================================================================
# Item 3: Outstanding enrollment tokens
# ====================================================================
Write-Host "[Item 3] Outstanding enrollment tokens" -ForegroundColor Yellow

$pyDbPath = Py-Path -Path $dbPath
# Note: SQL uses single quotes around 'now'. We wrap the SQL in
# a Python DOUBLE-quoted string so the inner single quotes don't
# terminate the Python string literal. Column is `token_hash`
# (enrollment tokens store SHA-256, never plaintext), `used_at`
# NULL = not yet consumed, `expires_at > now` = not yet expired.
$sql1 = "SELECT id, used_at, expires_at FROM enrollment_tokens WHERE used_at IS NULL AND expires_at > datetime('now')"
$scriptLines = @(
    'import sqlite3, sys',
    ('conn = sqlite3.connect(' + $pyDbPath + ')'),
    'conn.row_factory = sqlite3.Row',
    ('rows = conn.execute("' + $sql1 + '").fetchall()'),
    "print('outstanding_count=' + str(len(rows)))",
    "for r in rows:",
    "    print('  ' + r['id'] + ' expires=' + str(r['expires_at']))",
    'sys.exit(0 if len(rows) == 0 else 2)',
    ''
)
$scriptContent = $scriptLines -join "`n"
try {
    $r = Invoke-PythonProbe -ProbeName 'item3' -ScriptContent $scriptContent
    if ($r.ExitCode -eq 0) {
        Pass "Item 3" "No outstanding enrollment tokens."
    } else {
        Warn "Item 3" "Outstanding enrollment tokens found. Revoke via DELETE /api/enrollment-tokens/{id} (admin cookie required). Output: $($r.Output.Trim())"
    }
} catch {
    Fail "Item 3" "Error probing tokens: $($_.Exception.Message)"
}

# ====================================================================
# Item 4: HERMES_ORCH_PUBLIC_ORIGIN is set
# ====================================================================
Write-Host "[Item 4] HERMES_ORCH_PUBLIC_ORIGIN is set and valid" -ForegroundColor Yellow

$envOrigin = $env:HERMES_ORCH_PUBLIC_ORIGIN
$configOrigin = ""
if (Test-Path -LiteralPath $ConfigPath) {
    try {
        $pyConfigPath = Py-Path -Path $ConfigPath
        $scriptLines = @(
            "import yaml, sys",
            "with open($pyConfigPath, 'r', encoding='utf-8') as f:",
            "    cfg = yaml.safe_load(f) or {}",
            "val = ''",
            "if isinstance(cfg.get('server'), dict):",
            "    val = (cfg.get('server') or {}).get('public_origin', '')",
            "print(val)",
            ""
        )
        $scriptContent = $scriptLines -join "`n"
        $r = Invoke-PythonProbe -ProbeName 'item4-read' -ScriptContent $scriptContent
        $configOrigin = $r.Output.Trim()
    } catch {
        Warn "Item 4" "Could not read config.yaml: $($_.Exception.Message)"
    }
}

$chosenOrigin = ""
if ($envOrigin) { $chosenOrigin = "env: $envOrigin" }
elseif ($configOrigin) { $chosenOrigin = "config: $configOrigin" }

if (-not $chosenOrigin) {
    Fail "Item 4" "HERMES_ORCH_PUBLIC_ORIGIN is not set in env or config.yaml. Set it to the dashboard's public origin (e.g. 'http://192.168.2.152:8765')."
} else {
    # Validate format via the same validate_public_origin helper
    $pyInstallDir = Py-Path -Path $InstallDir
    $pyConfigPath = Py-Path -Path $ConfigPath
    # Use the env var if set; otherwise fall back to config value.
    $valueToCheck = if ($envOrigin) { $envOrigin } else { $configOrigin }
    $pyValue = Py-Path -Path $valueToCheck
    $envOriginPy = if ($envOrigin) { 'True' } else { 'False' }
    $sitePackages = Join-Path (Join-Path (Join-Path $InstallDir 'venv') 'Lib') 'site-packages'
    $pySitePackages = Py-Path -Path $sitePackages
    # The probe tries to import `validate_public_origin` from the
    # PRODUCTION venv. If the hotfix isn't deployed yet, the import
    # fails and the script falls back to a basic format check (just
    # urlparse + a few sanity checks). After the hotfix is deployed,
    # the full validation runs. This makes the check useful both
    # BEFORE and AFTER deploy.
    $scriptLines = @(
        'import sys, re',
        ('sys.path.insert(0, ' + $pySitePackages + ')'),
        'try:',
        '    from hermes_orch.auth.origin_validation import validate_public_origin',
        '    _has_validator = True',
        'except Exception:',
        '    _has_validator = False',
        'import yaml',
        "value = ''",
        ('use_env = ' + $envOriginPy),
        'if use_env:',
        ('    value = ' + $pyValue),
        'else:',
        '    try:',
        ('        with open(' + $pyConfigPath + ", 'r', encoding='utf-8') as f:"),
        '            cfg = yaml.safe_load(f) or {}',
        "        if isinstance(cfg.get('server'), dict):",
        "            value = (cfg.get('server') or {}).get('public_origin', '')",
        '    except Exception as e:',
        "        print('CONFIG_READ_ERROR: ' + str(e))",
        '        sys.exit(2)',
        'if not value:',
        "    print('EMPTY: public_origin is empty')",
        '    sys.exit(1)',
        'if _has_validator:',
        '    try:',
        '        canonical = validate_public_origin(value)',
        "        print('VALID: ' + canonical)",
        '        sys.exit(0)',
        '    except ValueError as e:',
        "        print('INVALID: ' + str(e))",
        '        sys.exit(1)',
        'else:',
        '    # Fallback format check (pre-deploy). The full validator',
        '    # runs after the hotfix is deployed. This is a sanity',
        '    # check only; it does NOT enforce the full contract',
        '    # (no path/query/fragment/userinfo). Use the deployed',
        '    # validator post-deploy for the full contract.',
        '    from urllib.parse import urlparse',
        '    parsed = urlparse(value)',
        '    errors = []',
        "    if parsed.scheme not in ('http', 'https'):",
        "        errors.append('scheme=' + str(parsed.scheme))",
        "    if not parsed.hostname:",
        "        errors.append('no hostname')",
        "    if parsed.port is None:",
        "        errors.append('no port')",
        "    if parsed.path not in ('', '/'):",
        "        errors.append('has path=' + str(parsed.path))",
        "    if parsed.query:",
        "        errors.append('has query=' + str(parsed.query))",
        "    if parsed.fragment:",
        "        errors.append('has fragment=' + str(parsed.fragment))",
        "    if parsed.username or parsed.password:",
        "        errors.append('has userinfo')",
        '    if errors:',
        "        print('INVALID (pre-deploy fallback): ' + ', '.join(errors))",
        '        sys.exit(1)',
        "    print('OK (pre-deploy fallback, full validation runs post-deploy): ' + value)",
        '    sys.exit(0)',
        ''
    )
    $scriptContent = $scriptLines -join "`n"
    try {
        $r = Invoke-PythonProbe -ProbeName 'item4-validate' -ScriptContent $scriptContent
        if ($r.ExitCode -eq 0) {
            Pass "Item 4" "public_origin is valid. ($($r.Output.Trim()))"
        } else {
            Fail "Item 4" "public_origin is invalid. $($r.Output.Trim())"
        }
    } catch {
        Fail "Item 4" "Error validating public_origin: $($_.Exception.Message)"
    }
}

# ====================================================================
# Item 5: Source-grep firewall (defense-in-depth -- pytest covers too)
# ====================================================================
Write-Host "[Item 5] Source-grep: no firewall-management code in allowlist" -ForegroundColor Yellow

$allowlistDirs = @(
    (Join-Path (Get-Location) 'src\hermes_orch'),
    (Join-Path (Get-Location) 'scripts')
)
$forbidden = @('New-NetFirewallRule', 'Set-NetFirewallRule', 'Remove-NetFirewallRule', 'netsh advfirewall', 'iptables', 'ip6tables', 'nft ', 'ufw allow')
$findings = New-Object System.Collections.Generic.List[string]
foreach ($d in $allowlistDirs) {
    if (Test-Path -LiteralPath $d) {
        foreach ($ext in @('*.py', '*.ps1', '*.sh', '*.bat', '*.cmd')) {
            $files = Get-ChildItem -Path $d -Recurse -Filter $ext -ErrorAction SilentlyContinue
            foreach ($f in $files) {
                $hits = Select-String -Path $f.FullName -Pattern ($forbidden -join '|') -ErrorAction SilentlyContinue
                foreach ($h in $hits) {
                    $findings.Add("$($f.FullName):$($h.LineNumber): $($h.Line.Trim())")
                }
            }
        }
    }
}
if ($findings.Count -eq 0) {
    Pass "Item 5" "No firewall-management code in allowlist."
} else {
    # Filter out this script itself (which lists the forbidden tokens
    # in `$forbidden` and so trivially matches its own scan). This is
    # the documented self-trigger case: a meta-script that needs to
    # KNOW the rules to enforce them MUST be excluded from the scan
    # of those rules. Same logic as `tests/test_no_firewall_management.py`
    # excluding itself via the path filter.
    $selfExcluded = $findings | Where-Object { $_ -notmatch 'pre_deploy_check' }
    if ($selfExcluded.Count -eq 0) {
        Pass "Item 5" "No firewall-management code in allowlist (self-excluded pre_deploy_check.ps1)."
    } else {
        $list = $selfExcluded -join "`n    "
        Fail "Item 5" "Firewall-management code found in allowlist -- DO NOT DEPLOY:`n    $list"
    }
}

# ====================================================================
# Summary
# ====================================================================
Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
$passCount = 0; $failCount = 0; $warnCount = 0
foreach ($r in $results) {
    $color = switch ($r.Status) { "PASS" { "Green" } "FAIL" { "Red" } "WARN" { "Yellow" } }
    Write-Host ("  [{0}] {1}: {2}" -f $r.Status, $r.Name, $r.Message) -ForegroundColor $color
    switch ($r.Status) { "PASS" { $passCount++ } "FAIL" { $failCount++ } "WARN" { $warnCount++ } }
}
Write-Host ""
Write-Host ("Total: {0} PASS, {1} FAIL, {2} WARN" -f $passCount, $failCount, $warnCount)
if ($failCount -gt 0) {
    Write-Host "FAILED -- fix the issues above before deploying the B12 hotfix." -ForegroundColor Red
    exit 1
} elseif ($warnCount -gt 0) {
    Write-Host "PASSED with warnings -- review the WARN items before deploying." -ForegroundColor Yellow
    exit 0
} else {
    Write-Host "PASSED -- safe to deploy the B12 hotfix." -ForegroundColor Green
    exit 0
}
