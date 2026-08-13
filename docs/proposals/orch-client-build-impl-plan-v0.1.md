# Orch Client Build — Implementation Plan v0.1 (for Perplexity review)

**Date:** 2026-08-13
**Status:** PROPOSAL for review (Perplexity + operator)
**Scope:** end-to-end build of a Windows MSI installer for the new orch
client, on the orchestrator host (Windows A). The MSI will be carried
to a separate target Windows machine for installation; this plan does
NOT include remote install, agent enrollment against a live orchestrator,
or B12 deploy. Enrollment is verified separately after the operator
manually runs the install on the target.

---

## 0. Goal

Produce:

1. A runnable **orch client** (Python) that signs HMAC-authenticated
   heartbeats and self-enrolls against the orchestrator's
   `POST /api/enrollment/anonymous` (or admin equivalent, per current
   pre-B12 contract).
2. A **code-signed Windows MSI** built with PyInstaller + WiX 4.
3. A **SHA-256 + signing manifest** for operator handoff.
4. An **updated install runbook** that references the real artifacts
   (not illustrative filenames).

The MSI install shall:

- Drop the orch client under `C:\Program Files\HermesOrchClient\`
- Register a Windows Service named `OrchClient` (start = demand)
- Drop a public config file at
  `C:\ProgramData\HermesOrchClient\config.yaml` with locked ACL
  (`SYSTEM:F, Admins:F, Users:R`)
- Drop a private secret file at
  `C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin` with ACL
  `SYSTEM:F, Admins:F` (no Users read)
- Not auto-start the service on install (operator must configure
  agent_id + secret first, then start)

---

## 1. Deliverables

| # | Artifact | Path |
|---|---|---|
| 1 | Orch client source | `C:\Project\minimax code\hermes-orchestrator\src\orch_client\__init__.py` + `__main__.py` + `client.py` + `hmac_auth.py` + `config.py` + `logging_setup.py` + `service.py` |
| 2 | `pyproject.toml` (or `setup.py`) entry point `orch-client` | `C:\Project\minimax code\hermes-orchestrator\pyproject.toml` (extend existing) |
| 3 | PyInstaller spec | `C:\Project\minimax code\hermes-orchestrator\installer\orch-client.spec` |
| 4 | WiX 4 source | `C:\Project\minimax code\hermes-orchestrator\installer\orch-client.wxs` |
| 5 | Build script | `C:\Project\minimax code\hermes-orchestrator\installer\build.ps1` |
| 6 | Built MSI | `C:\Project\minimax code\hermes-orchestrator\dist\OrchClient-v0.1.0-x64.msi` |
| 7 | SHA-256 + manifest | `C:\Project\minimax code\hermes-orchestrator\dist\SHA256SUMS.txt` + `MANIFEST.json` |
| 8 | Updated runbook | `C:\Users\stanley\AppData\Local\Temp\orch-client-install-runbook.md` (replaces illustrative content with real) |

All artifacts above are **local files**, no network push, no remote
install, no live enrollment against a real orchestrator during build.

---

## 2. Source layout (Deliverable 1)

```
src/orch_client/
  __init__.py          # version string
  __main__.py          # entry point: `python -m orch_client`
  client.py            # HTTP client + enrollment + heartbeat loop
  hmac_auth.py         # HMAC-SHA256 sign + body canonicalization
  config.py            # YAML config loader; per-install secret override
  logging_setup.py     # structured JSONL logger to ProgramData logs
  service.py           # Windows Service entry (pywin32 or win32serviceutil)
```

### 2.1 `hmac_auth.py`

Canonical body = JSON of {agent_id, ts, nonce} in that key order, UTF-8,
no whitespace, then HMAC-SHA256 with the secret. Header:
`X-Hermes-Agent-Id`, `X-Hermes-Ts`, `X-Hermes-Nonce`, `X-Hermes-Signature`.

### 2.2 `client.py`

- `enroll(orchestrator_url, agent_id, public_key_pem) -> enrollment_receipt`
  calls `POST /api/enrollment/anonymous` (or admin endpoint per
  pre-B12 contract). Returns `{agent_id, hmac_secret}` to the caller.
  In the build pipeline, **enrollment is NOT invoked** — only a
  dry-run code path validates request shape.
- `heartbeat(orchestrator_url, agent_id, hmac_secret) -> None` is a
  loop that signs + sends a heartbeat every N seconds (configurable).
  Also a dry-run mode that prints the signed payload without sending.

### 2.3 `service.py`

Windows Service entry via `pywin32`. The service is registered with
`Start="demand"` (per Perplexity's earlier guidance) so the operator
must explicitly start it after configuration.

### 2.4 `config.py`

Reads YAML at `C:\ProgramData\HermesOrchClient\config.yaml` with
fields:

```yaml
agent_id: <assigned at install time>
orchestrator_url: http://192.168.2.152:8765
heartbeat_interval_sec: 30
log_level: info
```

The HMAC secret is read separately from
`C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin` (raw bytes,
mode 0600 effectively via ACL).

### 2.5 Dry-run only at build time

The source includes a `--dry-run` flag that:
- Loads config
- Generates a synthetic heartbeat payload
- Signs it
- Prints the canonical body + signature + headers to stdout

This proves the signing chain without contacting any live service.
The dry-run is what gets exercised in the build pipeline; live HTTP
calls only happen on the target machine after install.

---

## 3. PyInstaller spec (Deliverable 3)

`--onedir` (per Perplexity's earlier guidance; preferred over
`--onefile` for service/agent: easier to inspect, allowlist, patch,
troubleshoot, and package predictably in MSI components).

`orch-client.spec`:

```python
# -*- mode: python ; coding: utf-8 -*-
import sys ; sys.setrecursionlimit(5000)
from PyInstaller.utils.hooks import collect_submodules
hidden = collect_submodules('orch_client')

a = Analysis(
    ['..\\src\\orch_client\\__main__.py'],
    pathex=['..\\src'],
    hiddenimports=hidden,
    excludes=['tkinter', 'unittest', 'pydoc'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='OrchClient',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,         # GUI-less service; no console window
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[],
    name='OrchClient',
)
```

Output: `dist/OrchClient/OrchClient.exe` + supporting files (Python
runtime, stdlib, deps). This folder is what WiX packages.

---

## 4. WiX 4 source (Deliverable 4)

Per Perplexity's earlier advice:
- `ServiceInstall` + `ServiceControl` + `util:ServiceConfig` in one
  component, key path = EXE
- `Start="demand"` (register but do not auto-start)
- `util:ServiceConfig`: FirstFailure=restart, SecondFailure=restart,
  ThirdFailure=none, RestartServiceDelayInSeconds=60,
  ResetPeriodInDays=1
- `util:PermissionEx` on the config File with SDDL
  `D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)`
- **No** users-R for the secret file — separate component with
  `D:P(A;;FA;;;SY)(A;;FA;;;BA)` (no Users ACE)

`orch-client.wxs` (sketch):

```xml
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs"
     xmlns:util="http://wixtoolset.org/schemas/v4/wxs/util">
  <Package Name="OrchClient" Version="0.1.0" Manufacturer="ACME"
           UpgradeCode="PUT-GUID-HERE">
    <MediaTemplate EmbedCab="yes" />
    <Feature Id="Main" Title="Orch Client" Level="1">
      <ComponentGroupRef Id="OrchClientFiles" />
      <ComponentGroupRef Id="OrchClientConfig" />
      <ComponentGroupRef Id="OrchClientSecret" />
      <ComponentGroupRef Id="OrchClientService" />
    </Feature>
  </Package>

  <Fragment>
    <StandardDirectory Id="ProgramFiles64Folder">
      <Directory Id="INSTALLFOLDER" Name="HermesOrchClient">
        <ComponentGroup Id="OrchClientFiles">
          <!-- Copy from PyInstaller --onedir output -->
          <Files Include="$(var.PublishDir)\**\*" />
        </ComponentGroup>
      </Directory>
    </StandardDirectory>
  </Fragment>

  <Fragment>
    <StandardDirectory Id="ProgramDataFolder">
      <Directory Id="CONFIGFOLDER" Name="HermesOrchClient">
        <Component Id="OrchClientConfigComponent" Guid="*" KeyPath="yes">
          <File Id="OrchClientConfigFile"
                Source="$(var.ConfigTemplate)\config.yaml"
                Name="config.yaml">
            <util:PermissionEx
              Sddl="D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)" />
          </File>
        </Component>
        <Directory Id="SECRETFOLDER" Name="secrets">
          <Component Id="OrchClientSecretComponent" Guid="*" KeyPath="yes">
            <File Id="OrchClientSecretFile"
                  Source="$(var.SecretTemplate)\agent-secret.bin"
                  Name="agent-secret.bin">
              <util:PermissionEx
                Sddl="D:P(A;;FA;;;SY)(A;;FA;;;BA)" />
            </File>
          </Component>
        </Directory>
      </Directory>
    </StandardDirectory>
  </Fragment>

  <Fragment>
    <DirectoryRef Id="INSTALLFOLDER">
      <Component Id="OrchClientServiceComponent" Guid="*" KeyPath="yes">
        <File Id="OrchClientServiceExe"
              Source="$(var.PublishDir)\OrchClient.exe" />
        <ServiceInstall Id="InstallOrchClient"
                        Name="OrchClient"
                        DisplayName="Hermes Orch Client"
                        Description="Hermes orchestration client agent"
                        Type="ownProcess"
                        Start="demand"
                        ErrorControl="normal"
                        Account="LocalSystem"
                        Interactive="no"
                        Vital="yes">
          <util:ServiceConfig
              FirstFailureActionType="restart"
              SecondFailureActionType="restart"
              ThirdFailureActionType="none"
              RestartServiceDelayInSeconds="60"
              ResetPeriodInDays="1" />
        </ServiceInstall>
        <ServiceControl Id="ControlOrchClient"
                        Name="OrchClient"
                        Stop="both"
                        Remove="uninstall"
                        Wait="yes" />
      </Component>
    </DirectoryRef>
  </Fragment>
</Wix>
```

The actual `agent-secret.bin` shipped in the MSI is a **placeholder
zero-byte file** with a comment in the runbook instructing the
operator to overwrite it after install. The MSI never carries a
real secret.

---

## 5. Build script (Deliverable 5)

`build.ps1`:

```powershell
$ErrorActionPreference = 'Stop'

$RepoRoot      = 'C:\Project\minimax code\hermes-orchestrator'
$PyInstaller   = 'pyinstaller'
$WixPath       = 'C:\wix\v4\'
$TimestampUrl  = 'http://timestamp.digicert.com'
$CertThumb     = '<TBD by operator — code-signing cert thumbprint>'
$Version       = '0.1.0'

# 1) Build Python wheel / install
pip install -e "$RepoRoot[client]"

# 2) PyInstaller --onedir
$publishDir = Join-Path $RepoRoot 'dist\OrchClient'
Remove-Item -LiteralPath $publishDir -Recurse -Force -ErrorAction SilentlyContinue
& $PyInstaller --clean --noconfirm "$RepoRoot\installer\orch-client.spec"

# 3) WiX: candle + light
$msi = Join-Path $RepoRoot "dist\OrchClient-v$Version-x64.msi"
& (Join-Path $WixPath 'wix.exe') build `
    -arch x64 `
    -bindpath "PublishDir=$publishDir" `
    -bindpath "ConfigTemplate=$RepoRoot\installer\templates\config" `
    -bindpath "SecretTemplate=$RepoRoot\installer\templates\secret" `
    -out "$RepoRoot\dist\" `
    "$RepoRoot\installer\orch-client.wxs"

# 4) Sign MSI with signtool (Azure Trusted Signing or local cert)
& signtool.exe sign `
    /fd SHA256 `
    /tr $TimestampUrl `
    /td SHA256 `
    /sha1 $CertThumb `
    "$msi"

# 5) Verify
& signtool.exe verify /pa /all /v "$msi"

# 6) Compute SHA-256 + write manifest
$hash = (Get-FileHash -LiteralPath $msi -Algorithm SHA256).Hash
$manifest = @{
    product       = 'OrchClient'
    version       = $Version
    architecture  = 'x64'
    msi_path      = $msi
    msi_sha256    = $hash
    msi_size      = (Get-Item $msi).Length
    built_at_utc  = (Get-Date).ToUniversalTime().ToString('o')
    built_by      = $env:USERNAME
    signing       = @{
        tool        = 'signtool.exe'
        timestamp   = $TimestampUrl
        cert_sha1   = $CertThumb
        digest      = 'SHA256'
    }
} | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText(
    (Join-Path $RepoRoot 'dist\MANIFEST.json'),
    $manifest,
    [System.Text.UTF8Encoding]::new($false))
"$hash  OrchClient-v$Version-x64.msi" |
    Out-File -LiteralPath (Join-Path $RepoRoot 'dist\SHA256SUMS.txt') -Encoding utf8

Write-Host "[+] Build complete: $msi"
Write-Host "[+] SHA-256: $hash"
```

---

## 6. Known gaps & explicit dependencies (must be resolved before build)

| # | Dependency | Owner | Status |
|---|---|---|---|
| 1 | Orchestrator contract (enrollment + heartbeat + cleanup endpoint paths, methods, auth, CSRF) | operator | partially TBD until B12 is deployed; pre-B12 contract used for POC |
| 2 | Code-signing cert thumbprint (or Azure Trusted Signing endpoint) | operator | not yet bound |
| 3 | WiX 4 install path | operator | not yet installed (default assumed: `C:\wix\v4\`) |
| 4 | PyInstaller availability in build Python | operator | assumed present |
| 5 | Target machine agent_id (e.g. `win-b-02`) | operator | not yet assigned |
| 6 | Target machine HMAC secret | operator | not yet generated; **MSI ships with placeholder zero-byte secret** |
| 7 | Orchestrator-side agent record (pre-created or auto-enroll?) | operator | pre-B12 supports auto-enroll; B12 hotfix path is separate |

---

## 7. Forbidden actions (no exceptions)

- ❌ No remote install on the target Windows machine
- ❌ No live HTTP calls during build (dry-run only)
- ❌ No real HMAC secret embedded in the MSI
- ❌ No modification of B12 deploy (held at watchdog REFUSE)
- ❌ No modification of watchdog task (held)
- ❌ No push to remote git remote
- ❌ No commit to local git (operator's explicit approval required first)
- ❌ No firewall / enrollment / agent-mutation action on the orchestrator

---

## 8. Acceptance criteria

A. The MSI installs cleanly on a clean Windows 10/11 VM:
   - File system layout matches §0
   - ACL on config file: `SYSTEM:F, Admins:F, Users:R` (verified via
     `Get-Acl`)
   - ACL on secret file: `SYSTEM:F, Admins:F` (no Users)
   - Service is registered with `Start=Demand`, `State=Stopped`
   - Service is NOT auto-started

B. The MSI is code-signed:
   - `signtool verify /pa /all /v` reports valid
   - SHA-256 in `SHA256SUMS.txt` matches `Get-FileHash`
   - `MANIFEST.json` is consistent with the MSI

C. The orch client `--dry-run` produces a canonical signed payload:
   - Operator can run `OrchClient.exe --dry-run` and see the JSON
     body + signature without any HTTP call

D. The updated runbook (`orch-client-install-runbook.md`) uses
   the real MSI filename and references §6 binding values.

E. No side-effecting action against the live orchestrator.

---

## 9. What I will NOT do (without separate approval)

- Push the built MSI to any remote location
- Modify the live orchestrator config / DB / NSSM
- Install on any machine other than a local test VM
- Generate a real HMAC secret for production
- Sign with anything other than a cert explicitly bound by the operator
- Touch the B12 deploy script (`apply-r7c-rebuild.ps1`) — already
  locally patched to v4.1 + v5.1, no further changes planned

---

## 10. Perplexity review questions (next step)

When this plan is sent to Perplexity for review, ask:

1. **Source layout**: is splitting `hmac_auth.py` / `client.py` /
   `service.py` the right level of granularity for a 2026 PyInstaller
   build, or should I consolidate?
2. **PyInstaller `--onedir` flag list**: are there any flags I'm
   missing for a Windows Service (e.g. `--uac-admin`, manifest
   embedding, version-info)? Does `--console=False` actually suppress
   the console window for a Windows Service?
3. **WiX 4 schema correctness**: is the `<ComponentGroup>` / `<Files
   Include="$(var.PublishDir)\**\*" />` pattern the right way to
   include a whole PyInstaller output directory, or should I use a
   heat-harvested fragment?
4. **util:PermissionEx with SDDL**: any 2026 caveats? Does the SDDL
   `D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)` correctly produce
   `SYSTEM:F, Admins:F, Users:R` on Windows 11?
5. **signtool command shape**: with `signtool.exe sign /fd SHA256 /tr
   <URL> /td SHA256 /sha1 <THUMB>`, what is the exact 2026 syntax
   for a hardware-backed cert? Do I need `/kcsp` or `/csp` flags?
6. **HMAC body canonicalization**: JSON in fixed key order with
   `separators=(',', ':')` and UTF-8 — is this still the recommended
   2026 pattern, or has HMAC-SHA256 + structured header (e.g. AWS
   SigV4-style) become the norm for agent auth?
7. **Dry-run pattern**: is `OrchClient.exe --dry-run` the right
   shape for an operator sanity check, or should it be a separate
   `OrchClientDoctor.exe` companion tool?
8. **Missing pieces**: is there anything obvious this plan is missing
   that I should add before I start coding?
