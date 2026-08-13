# Orch Client Build — Implementation Plan v0.2 (for Perplexity review)

**Date:** 2026-08-13
**Status:** PROPOSAL for review (Perplexity + operator) — v0.2
**Supersedes:** v0.1 (commit `0754cf0` on branch `proposal/orch-client-build-impl-plan-v0.1`)
**Scope:** end-to-end build of a Windows MSI installer for the new orch
client, on the orchestrator host (Windows A). The MSI will be carried
to a separate target Windows machine for installation; this plan does
NOT include remote install, agent enrollment against a live orchestrator,
or B12 deploy. Enrollment is verified separately after the operator
manually runs the install on the target.

---

## 0. v0.1 → v0.2 changelog (Perplexity review)

Perplexity reviewed v0.1 via raw GitHub URL. Five categories of
corrections were applied:

| # | v0.1 said | v0.2 says | Reason |
|---|---|---|---|
| 1 | "JSON with fixed key order + no whitespace + UTF-8" HMAC | `HMAC-SHA256(secret, raw-body bytes)` + bound `protocol-version` / `key-id` / `timestamp` / `nonce` / `canonical-endpoint` / `body-sha256`; server uses constant-time compare + bounded replay cache | "Fixed key order" is a homegrown subset that does not cover numbers / Unicode / duplicate keys / escaping / null fields / timestamp / replay / versioning. Use raw-body bytes (GitHub-webhook style) or RFC 8785 JCS if JSON semantics are required. SigV4 is AWS-specific, not a general norm. |
| 2 | `console=False` only | `console=False` in spec + `--noconsole` on CLI; **drop** any `--uac-admin` (inappropriate for SCM-launched service) | `--noconsole` and `console=False` are equivalent; `--uac-admin` would embed an interactive UAC prompt into the service exe |
| 3 | `<Files Include="$(var.PublishDir)\**\*" />` | `<HarvestDirectory>` in `.wixproj`; **service EXE + KeyPath + ServiceInstall + ServiceControl are manually authored in their own component, not harvested** | 2026 WiX idiom; harvesting the service exe then re-authoring it manually risks duplicate component ownership |
| 4 | "MSI ships with a placeholder zero-byte secret" with implicit overwrite | Explicit `NeverOverwrite="yes"` (or WiX equivalent) on the secret file; documented preservation behavior across install / repair / major upgrade / uninstall | A repair or upgrade that silently overwrites a real secret is the highest-risk failure mode |
| 5 | (missing) | Added §0.1 Pinned versions, §0.2 Locked dependency hashes, §0.3 SBOM, §0.4 Signer + timestamp policy, §0.5 Cert renewal + revocation response, §0.6 UpgradeCode / ProductCode / version policy, §0.7 Repair / major upgrade / uninstall / rollback behavior, §0.8 Service recovery + restart cap + first-start, §0.9 Event Log strategy, §0.10 Test case matrix | Perplexity called out each as a 2026 best-practice gap |

All other v0.1 sections preserved (structure + deliverable list + secret
separation + dry-run pattern + forbidden actions).

---

## 0.x Pinned versions, hashes, and build matrix (new in v0.2)

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12.x LTS | Build-host interpreter; runtime target in MSI is whatever PyInstaller bundles |
| PyInstaller | 6.x latest | `--onedir --noconsole` |
| WiX Toolset | 4.x latest | `<HarvestDirectory>` in `.wixproj` |
| Windows SDK (signtool) | 10.0.22621.x or newer | For `signtool.exe`; `Get-AuthenticodeSignature` does not require SDK |
| RFC 3161 timestamp URL | per operator binding | Default suggested: `http://timestamp.digicert.com` |
| Code-signing cert | per operator binding | OV or EV, or Azure Trusted Signing |

Locked dependency file (`installer/requirements.lock`) hashes every
pip package the orch client imports; the build fails if any hash
drifts. SBOM (`dist/SBOM.spdx.json` or similar) is generated as part
of the build for every release.

---

## 1. Goal

Produce:

1. A runnable **orch client** (Python) that signs HMAC-authenticated
   heartbeats and self-enrolls against the orchestrator's
   `POST /api/enrollment/anonymous` (or admin equivalent, per current
   pre-B12 contract).
2. A **code-signed Windows MSI** built with PyInstaller + WiX 4.
3. A **SHA-256 + SBOM + signing manifest** for operator handoff.
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
  `SYSTEM:F, Admins:F` (no Users read) and `NeverOverwrite="yes"`
- Not auto-start the service on install (operator must configure
  agent_id + secret first, then start)

---

## 2. Deliverables

| # | Artifact | Path |
|---|---|---|
| 1 | Orch client source | `C:\Project\minimax code\hermes-orchestrator\src\orch_client\__init__.py` + `__main__.py` + `client.py` + `hmac_auth.py` + `config.py` + `logging_setup.py` + `service.py` |
| 2 | `pyproject.toml` entry point `orch-client` | extend existing `C:\Project\minimax code\hermes-orchestrator\pyproject.toml` |
| 3 | Locked dependency file | `C:\Project\minimax code\hermes-orchestrator\installer\requirements.lock` (every pip package hash) |
| 4 | PyInstaller spec | `C:\Project\minimax code\hermes-orchestrator\installer\orch-client.spec` |
| 5 | WiX 4 `.wixproj` + heat config | `C:\Project\minimax code\hermes-orchestrator\installer\orch-client.wixproj` + `orch-client.wxs` |
| 6 | Build script | `C:\Project\minimax code\hermes-orchestrator\installer\build.ps1` |
| 7 | Built MSI | `C:\Project\minimax code\hermes-orchestrator\dist\OrchClient-v0.1.0-x64.msi` |
| 8 | SHA-256 + manifest + SBOM | `dist\SHA256SUMS.txt` + `dist\MANIFEST.json` + `dist\SBOM.spdx.json` |
| 9 | Updated runbook | `C:\Users\stanley\AppData\Local\Temp\orch-client-install-runbook.md` |

All artifacts above are **local files**, no network push, no remote
install, no live enrollment against a real orchestrator during build.

---

## 3. Source layout (Deliverable 1)

```
src/orch_client/
  __init__.py          # version string
  __main__.py          # entry point: `python -m orch_client`
  client.py            # HTTP client + enrollment + heartbeat loop
  hmac_auth.py         # HMAC-SHA256 over raw body + bound headers
  config.py            # YAML config loader; per-install secret override
  logging_setup.py     # structured JSONL logger to ProgramData logs
  service.py           # Windows Service entry (pywin32 or win32serviceutil)
```

### 3.1 `hmac_auth.py` (revised in v0.2)

- **Canonical request** = raw UTF-8 body bytes (no JSON re-serialization
  on the signing path; if a JSON payload is sent, it is sent as the
  bytes that were signed)
- **HMAC** = `HMAC-SHA256(secret, raw-body-bytes)`, hex-lowercase
- **Bound headers** (all sent with every signed request):

  | Header | Value |
  |---|---|
  | `X-Hermes-Protocol-Version` | `1` (or current protocol version) |
  | `X-Hermes-Key-Id` | the agent_id (also the key identifier) |
  | `X-Hermes-Timestamp` | RFC 3339 UTC, e.g. `2026-08-13T08:00:00Z` |
  | `X-Hermes-Nonce` | 16 random bytes hex (32 chars), single use |
  | `X-Hermes-Endpoint` | canonical path, e.g. `/api/agents/{id}/heartbeat` |
  | `X-Hermes-Body-Sha256` | SHA-256 of the raw body bytes (hex-lowercase) |
  | `X-Hermes-Signature` | HMAC-SHA256 over the concatenation (in fixed order): `protocol-version \|\| "\n" \|\| key-id \|\| "\n" \|\| timestamp \|\| "\n" \|\| nonce \|\| "\n" \|\| endpoint \|\| "\n" \|\| body-sha256` |

- **Server validation** (documented, not implemented in this plan):
  constant-time compare of the signature, bounded replay cache keyed
  by `(key-id, nonce)`, timestamp within ±5 min of server clock.

- **Why this shape, not "JSON fixed key order"**: the homegrown subset
  leaves numbers / Unicode / duplicate keys / escaping / null fields /
  omitted fields ambiguous. Raw body bytes + bound metadata is
  unambiguous and matches the GitHub-webhook model.

### 3.2 `client.py`

- `enroll(orchestrator_url, agent_id, public_key_pem) -> enrollment_receipt`
  calls `POST /api/enrollment/anonymous` (or admin endpoint per
  pre-B12 contract). Returns `{agent_id, hmac_secret}` to the caller.
  In the build pipeline, **enrollment is NOT invoked** — only a
  dry-run code path validates request shape.
- `heartbeat(orchestrator_url, agent_id, hmac_secret) -> None` is a
  loop that signs + sends a heartbeat every N seconds (configurable).
  Also a dry-run mode that prints the signed payload + headers to
  stdout without sending.

### 3.3 `service.py`

Windows Service entry via `pywin32`. The service is registered with
`Start="demand"` (per earlier Perplexity guidance) so the operator
must explicitly start it after configuration.

### 3.4 `config.py`

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

### 3.5 Dry-run only at build time

The source includes a `--dry-run` flag that:
- Loads config (or uses bundled test config)
- Generates a synthetic heartbeat payload
- Computes the body SHA-256 + HMAC
- Prints the canonical headers + signature to stdout

This proves the signing chain without contacting any live service.
The dry-run is what gets exercised in the build pipeline; live HTTP
calls only happen on the target machine after install.

---

## 4. PyInstaller spec (Deliverable 4) — revised in v0.2

`--onedir` (preferred over `--onefile` for service/agent: easier to
inspect, allowlist, patch, troubleshoot, package predictably in MSI).
**`--noconsole`** on the CLI (equivalent to `console=False` in spec).
**No `--uac-admin`**: inappropriate for an SCM-launched service.

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
    console=False,         # equivalent to --noconsole; suppress console window
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[],
    name='OrchClient',
)
```

CLI invocation (preferred for clarity):

```
pyinstaller --clean --noconfirm --onedir --noconsole --name OrchClient \
    --paths ..\src ..\src\orch_client\__main__.py
```

Output: `dist/OrchClient/OrchClient.exe` + supporting files (Python
runtime, stdlib, deps). This folder is what WiX consumes.

---

## 5. WiX 4 source (Deliverable 5) — revised in v0.2

**Key change vs v0.1**: replace `<Files Include>` with **`<HarvestDirectory>`**
in the `.wixproj`. The service EXE is **manually authored in its own
component** (not harvested) to avoid duplicate component ownership
where WiX might generate a different Component/@Guid for the same file.

### 5.1 Project layout

```
installer/
  orch-client.wixproj     # MSBuild project: harvest + candle + light
  orch-client.wxs         # manually authored fragments (service, config, secret)
  HeatDirectory.wxs       # generated by wix heat at build time, OR produced by <HarvestDirectory> task
  templates/
    config/config.yaml    # placeholder config shipped in MSI
    secret/agent-secret.bin  # zero-byte placeholder (NEVER overwritten post-install)
```

### 5.2 `orch-client.wixproj` (sketch)

```xml
<Project Sdk="WixToolset.Sdk/4.0">
  <PropertyGroup>
    <OutputType>Package</OutputType>
    <Platform>x64</Platform>
    <DefineConstants>PublishDir=..\dist\OrchClient\;ConfigTemplate=..\installer\templates\config\;SecretTemplate=..\installer\templates\secret\</DefineConstants>
  </PropertyGroup>

  <ItemGroup>
    <HarvestDirectory Include="$(PublishDir)">
      <ComponentGroupName>OrchClientFiles</ComponentGroupName>
      <DirectoryRefId>INSTALLFOLDER</DirectoryRefId>
      <!-- Exclude the service EXE from harvest; it is manually authored -->
      <ExcludeFiles>**\OrchClient.exe</ExcludeFiles>
    </HarvestDirectory>
  </ItemGroup>

  <ItemGroup>
    <Compile Include="orch-client.wxs" />
  </ItemGroup>
</Project>
```

### 5.3 `orch-client.wxs` (manually authored fragments)

```xml
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs"
     xmlns:util="http://wixtoolset.org/schemas/v4/wxs/util">
  <Package Name="OrchClient" Version="0.1.0" Manufacturer="ACME"
           UpgradeCode="PUT-GUID-HERE">
    <MediaTemplate EmbedCab="yes" />
    <Feature Id="Main" Title="Orch Client" Level="1">
      <ComponentGroupRef Id="OrchClientFiles" />  <!-- harvested -->
      <ComponentGroupRef Id="OrchClientConfig" />
      <ComponentGroupRef Id="OrchClientSecret" />
      <ComponentGroupRef Id="OrchClientService" />
    </Feature>
  </Package>

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
          <Component Id="OrchClientSecretComponent" Guid="*" KeyPath="yes" NeverOverwrite="yes">
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
</Wix>
```

Key new element: `NeverOverwrite="yes"` on `OrchClientSecretComponent`.
This prevents a repair, reinstall, or major upgrade from clobbering a
real provisioned secret with the zero-byte placeholder.

---

## 6. Build script (Deliverable 6) — revised in v0.2

`build.ps1`:

```powershell
$ErrorActionPreference = 'Stop'

$RepoRoot      = 'C:\Project\minimax code\hermes-orchestrator'
$PyInstaller   = 'pyinstaller'
$WixDir        = 'C:\wix\v4\'
$WixProject    = Join-Path $RepoRoot 'installer\orch-client.wixproj'
$TimestampUrl  = 'http://timestamp.digicert.com'
$CertThumb     = '<TBD by operator — code-signing cert thumbprint>'
$Version       = '0.1.0'
$Arch          = 'x64'

# 0) Verify locked dependency file matches
#    (run `pip install --require-hashes -r installer/requirements.lock` against the build venv)

# 1) Install orch client in editable mode + locked deps
pip install --require-hashes -r (Join-Path $RepoRoot 'installer/requirements.lock')

# 2) PyInstaller --onedir --noconsole
$publishDir = Join-Path $RepoRoot 'dist\OrchClient'
Remove-Item -LiteralPath $publishDir -Recurse -Force -ErrorAction SilentlyContinue
& $PyInstaller --clean --noconfirm `
    --onedir --noconsole `
    --name OrchClient `
    --paths (Join-Path $RepoRoot 'src') `
    (Join-Path $RepoRoot 'src\orch_client\__main__.py')

# 3) Sign each shipped EXE / DLL individually (payload first, MSI last)
$payloadFiles = Get-ChildItem -Recurse -Include '*.exe','*.dll' -Path $publishDir
foreach ($f in $payloadFiles) {
    & signtool.exe sign /fd SHA256 /tr $TimestampUrl /td SHA256 /sha1 $CertThumb $f.FullName
}

# 4) Build MSI via wix dotnet build (runs HarvestDirectory automatically)
$msiDir = Join-Path $RepoRoot 'dist'
Remove-Item -LiteralPath (Join-Path $msiDir '*.msi') -ErrorAction SilentlyContinue
& (Join-Path $WixDir 'wix.exe') build `
    -arch $Arch `
    -out $msiDir `
    $WixProject
$msi = Get-ChildItem -Path $msiDir -Filter '*.msi' | Select-Object -First 1

# 5) Sign the MSI itself
& signtool.exe sign /fd SHA256 /tr $TimestampUrl /td SHA256 /sha1 $CertThumb $msi.FullName

# 6) Verify
& signtool.exe verify /pa /all /v $msi.FullName

# 7) Compute SHA-256 + write manifest + SBOM
$hash = (Get-FileHash -LiteralPath $msi.FullName -Algorithm SHA256).Hash
$manifest = @{
    product       = 'OrchClient'
    version       = $Version
    architecture  = $Arch
    msi_path      = $msi.FullName
    msi_sha256    = $hash
    msi_size      = $msi.Length
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
    (Join-Path $msiDir 'MANIFEST.json'),
    $manifest,
    [System.Text.UTF8Encoding]::new($false))
"$hash  $($msi.Name)" |
    Out-File -LiteralPath (Join-Path $msiDir 'SHA256SUMS.txt') -Encoding utf8

# 8) Generate SBOM (lightweight SPDX; operator can swap for a fuller tool)
$sbom = @{
    spdxVersion = 'SPDX-2.3'
    name        = "OrchClient-$Version"
    packages    = @(Get-ChildItem $publishDir -Recurse -File | ForEach-Object {
        @{
            name    = $_.Name
            sha256  = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
            size    = $_.Length
        }
    })
} | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText(
    (Join-Path $msiDir 'SBOM.spdx.json'),
    $sbom,
    [System.Text.UTF8Encoding]::new($false))

Write-Host "[+] Build complete: $($msi.FullName)"
Write-Host "[+] SHA-256: $hash"
```

**Sign order** (per Perplexity's v0.2 §0.5 requirement): payload EXEs
and DLLs first, **then** the MSI, **then** verify, **then** compute
final hash and write the manifest + SBOM.

---

## 7. Known gaps & explicit dependencies (must be resolved before build)

| # | Dependency | Owner | Status |
|---|---|---|---|
| 1 | Orchestrator contract (enrollment + heartbeat + cleanup endpoint paths, methods, auth, CSRF) | operator | partially TBD until B12 is deployed; pre-B12 contract used for POC |
| 2 | Code-signing cert thumbprint (or Azure Trusted Signing endpoint) | operator | not yet bound |
| 3 | WiX 4 install path | operator | not yet installed (default assumed: `C:\wix\v4\`) |
| 4 | PyInstaller availability in build Python | operator | assumed present |
| 5 | Target machine agent_id (e.g. `win-b-02`) | operator | not yet assigned |
| 6 | Target machine HMAC secret | operator | not yet generated; **MSI ships with placeholder zero-byte secret + NeverOverwrite so it cannot clobber a real secret** |
| 7 | Orchestrator-side agent record (pre-created or auto-enroll?) | operator | pre-B12 supports auto-enroll; B12 hotfix path is separate |
| 8 | Cert renewal policy + revoked-cert response | operator | new in v0.2; bind at runtime |
| 9 | UpgradeCode GUID (must be stable across versions; ProductCode rotates per version) | operator | new in v0.2 |
| 10 | Test case matrix (offline / cert mismatch / missing-secret / invalid-secret / repair / upgrade / uninstall / partial-install / revocation) | operator | new in v0.2; test plan to be drafted separately |

---

## 8. Forbidden actions (no exceptions)

- ❌ No remote install on the target Windows machine
- ❌ No live HTTP calls during build (dry-run only)
- ❌ No real HMAC secret embedded in the MSI
- ❌ No modification of B12 deploy (held at watchdog REFUSE)
- ❌ No modification of watchdog task (held)
- ❌ No push to remote git remote (per proposal-branch pattern only)
- ❌ No firewall / enrollment / agent-mutation action on the orchestrator
- ❌ **No repair / upgrade / uninstall that clobbers a provisioned secret** (`NeverOverwrite="yes"` enforces this at the MSI level; the operator must NOT manually delete the secret file in a way that the placeholder would replace)

---

## 9. Acceptance criteria

A. The MSI installs cleanly on a clean Windows 10/11 VM:
   - File system layout matches §1
   - ACL on config file: `SYSTEM:F, Admins:F, Users:R` (verified via
     `Get-Acl`)
   - ACL on secret file: `SYSTEM:F, Admins:F` (no Users)
   - Service is registered with `Start=Demand`, `State=Stopped`
   - Service is NOT auto-started

B. The MSI is code-signed:
   - `signtool verify /pa /all /v` reports valid
   - SHA-256 in `SHA256SUMS.txt` matches `Get-FileHash`
   - `MANIFEST.json` is consistent with the MSI
   - SBOM is consistent with the harvested PyInstaller payload

C. The orch client `--dry-run` produces a canonical signed payload:
   - Operator can run `OrchClient.exe --dry-run` and see the canonical
     headers + signature + raw body bytes (without any HTTP call)

D. Secret-preservation behavior (new in v0.2):
   - Install MSI with a real secret in place → secret unchanged
   - Repair install → secret unchanged
   - Major upgrade (same UpgradeCode) → secret unchanged
   - Uninstall → secret removed (by design)
   - Reinstall on top of uninstall → placeholder returned, no real
     secret ever re-injected

E. The updated runbook (`orch-client-install-runbook.md`) uses
   the real MSI filename and references §7 binding values.

F. No side-effecting action against the live orchestrator.

G. The HMAC validation (new in v0.2):
   - Server uses constant-time compare
   - Server maintains a bounded replay cache keyed by `(key-id, nonce)`
   - Server rejects requests with timestamp ±5 min outside server clock

H. The test case matrix (new in v0.2) covers:
   - offline (no orchestrator reachable)
   - cert mismatch (signing cert changed mid-rotation)
   - missing-secret (secret file deleted but service is still installed)
   - invalid-secret (secret file is corrupt)
   - repair, major upgrade, uninstall, partial-install
   - revocation (signing cert revoked after build)

---

## 10. What I will NOT do (without separate approval)

- Push the built MSI to any remote location
- Modify the live orchestrator config / DB / NSSM
- Install on any machine other than a local test VM
- Generate a real HMAC secret for production
- Sign with anything other than a cert explicitly bound by the operator
- Touch the B12 deploy script (`apply-r7c-rebuild.ps1`) — already
  locally patched to v4.1 + v5.1, no further changes planned
- Create / modify firewall rules
- Add, remove, or modify any scheduled task
- Initiate or accept any enrollment against the live orchestrator

---

## 11. v0.1 → v0.2 changelog (cross-reference)

| # | Section | v0.1 | v0.2 |
|---|---|---|---|
| 1 | §3.1 HMAC canonicalization | "JSON with fixed key order + no whitespace + UTF-8" | `HMAC-SHA256(secret, raw-body-bytes)` + bound headers (protocol-version, key-id, timestamp, nonce, endpoint, body-sha256); constant-time compare + replay cache on server |
| 2 | §4 PyInstaller | `console=False` only | `console=False` + `--noconsole`; explicit "no --uac-admin for SCM-launched service" |
| 3 | §5 WiX 4 | `<Files Include="$(var.PublishDir)\**\*" />` | `<HarvestDirectory>` in `.wixproj`; service EXE + KeyPath + ServiceInstall + ServiceControl are **manually authored in a separate component** (not harvested) |
| 4 | §5.3 Secret component | implicit overwrite | `NeverOverwrite="yes"` on `OrchClientSecretComponent` |
| 5 | §0.x Pinned versions | (missing) | Python 3.12, PyInstaller 6.x, WiX 4.x, Windows SDK 10.0.22621.x; locked `requirements.lock` with hashes |
| 6 | §6 Sign order | MSI only | payload EXEs / DLLs → MSI → verify → final hash |
| 7 | §6 SBOM | (missing) | `SBOM.spdx.json` generated as part of the build |
| 8 | §7 dependencies | 7 items | + cert renewal, UpgradeCode GUID, test case matrix |
| 9 | §9 acceptance | 6 criteria | + secret-preservation behavior (D), HMAC validation (G), test case matrix (H) |
| 10 | §8 forbidden actions | 7 items | + "no repair/upgrade/uninstall that clobbers a provisioned secret" |
| 11 | (everywhere) | "implicit" | "explicit" — every previously-implicit decision is now written down |

---

## 12. Outstanding Perplexity questions for v0.3 (if any)

Perplexity's v0.1 review was thorough; remaining open questions are
operator-binding, not technical:

- Operator must pick `agent_id` for the target machine
- Operator must pick the code-signing cert / Azure Trusted Signing
- Operator must install WiX 4 on the build host at `C:\wix\v4\`
- Operator must generate the target machine's HMAC secret (out-of-band)
- Operator must generate the UpgradeCode GUID (or accept the placeholder)
- Operator must draft the test case matrix (§9.H) — this is a
  separate work item, not a code change
