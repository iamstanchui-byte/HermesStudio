# Orch Client Build — Implementation Plan v0.3 (for Perplexity review)

**Date:** 2026-08-13
**Status:** PROPOSAL for review (Perplexity + operator) — v0.3
**Supersedes:** v0.2 (commit `83fdb6c` on branch `proposal/orch-client-build-impl-plan-v0.1`)
**Scope:** end-to-end build of a Windows MSI installer for the new orch
client, on the orchestrator host (Windows A). The MSI will be carried
to a separate target Windows machine for installation; this plan does
NOT include remote install, agent enrollment against a live orchestrator,
or B12 deploy. Enrollment is verified separately after the operator
manually runs the install on the target.

---

## 0. v0.2 → v0.3 changelog (Perplexity review)

Perplexity read the v0.2 plan via the PR URL (`pull/4`) and returned
four critical blockers plus several security/build/lifecycle
corrections. v0.3 applies all of them.

| # | v0.2 said | v0.3 says | Reason |
|---|---|---|---|
| 1 | HMAC spec contradictory: §3.1 listed **both** `HMAC = HMAC-SHA256(secret, raw-body-bytes)` **and** `signature = HMAC-SHA256(protocol-version\|\|"\n"\|\|key-id\|\|"\n"\|\|timestamp\|\|"\n"\|\|nonce\|\|"\n"\|\|endpoint\|\|"\n"\|\|body-sha256)` | Pick the bound-metadata model; body_sha256 is computed from raw body bytes; the **server** derives method and canonical path from the actual request (does NOT trust `X-Hermes-Endpoint` as authority); key-id is a real key-rotation identifier (one agent may have old + new HMAC key during rotation) | Two different signing inputs in one spec is contradictory. The bound-metadata model is the right choice because it binds replay metadata + endpoint semantics to the signature. |
| 2 | MSI config ships `orchestrator_url: http://192.168.2.152:8765`; enrollment uses `POST /api/enrollment/anonymous` (pre-B12 contract) | MSI config ships a **placeholder** `https://<ORCHESTRATOR_FQDN>:<HTTPS_PORT>`; service **refuses to start** when endpoint is placeholder / HTTP / missing / secret is zero bytes; enrollment contract is **TBD** until B12 is deployed and reviewed; remove the "pre-B12 contract used for POC" line from §1 | B13 frozen: new enrollment over HTTP is prohibited. Shipping an HTTP endpoint in a release artifact is a regression, not a POC. |
| 3 | WiX `KeyPath="yes"` was on `<Component>`; `<ComponentGroupRef>` referenced IDs that did not exist; heat pattern mixed `HeatDirectory.wxs` + `HarvestDirectory` | `KeyPath="yes"` is on the `<File>` element, not the `<Component>`; manually-authored components are wrapped in explicit `<ComponentGroup>` elements and the `<ComponentGroupRef>` IDs match; use `HarvestDirectory` task in `.wixproj` (WiX 4 idiom); no untracked Heat fragment | Component/ComponentGroup/KeyPath placement is load-bearing for install / repair / upgrade. Mismatches cause MSI self-repair loops, broken upgrades, or missing-key components. |
| 4 | Build script used `$ErrorActionPreference = 'Stop'` but did not check native exit codes for `pip` / `pyinstaller` / `signtool` / `wix build` / `signtool verify`; MSI selected via `Get-ChildItem ... \| Select-Object -First 1` (non-deterministic); SBOM was a hand-rolled JSON labelled `SPDX-2.3` | Add `Invoke-NativeChecked` helper for every native call; use `dotnet build $WixProject -c Release -p:Platform=x64` for the SDK-style `.wixproj`; select MSI by exact expected filename `OrchClient-v<version>-x64.msi`; use a real SPDX or CycloneDX generator (not a custom JSON) | Native tools can return non-zero with PowerShell still happy; non-deterministic MSI selection can pick a stale artifact; custom JSON mislabelled as SPDX will fail any compliance check |
| 5 | §0.4 Secret-preservation table existed but `Uninstall → secret removed by design` was stated as a claim, not as a tested VM | Same 4-state table in v0.3, **plus** an explicit mandatory VM test matrix for each state; "Uninstall → secret removed" is now a **test outcome** to verify, not an assertion | A claim without a test is a future incident |
| 6 | Locked files but not directories | `util:PermissionEx` on `ProgramData\HermesOrchClient\` (full) and `ProgramData\HermesOrchClient\secrets\` (no Users ACE) in addition to the existing per-file ACLs | Directory ACLs control deletion, replacement, inherited permissions, sibling-file creation |
| 7 | "mode 0600 effectively via ACL" wording | Replaced with the explicit SDDL `D:P(A;;FA;;;SY)(A;;FA;;;BA)` (no Users ACE) on the secret file, and the matching `D:P(A;;FA;;;SY)(A;;FA;;;BA)` on the secret directory | Windows ACLs are not Unix mode bits. State the exact SDDL. |
| 8 | Source layout in §3 was reasonable, but no explicit service-dispatcher test list was present | Added §3.6 "Service-dispatcher VM validation list" — pywin32 dispatcher present in frozen bundle; `servicemanager` / `pywintypes` / `win32serviceutil` DLLs present and loadable; SCM can start `OrchClient.exe`; service stop event reaches the Python loop; no console dependency; Event Log or protected file logging captures startup failures | `console=False` / `--noconsole` suppresses the console window but does not make a `__main__.py` process a valid SCM service by itself |
| 9 | Failure actions `restart / restart / none / 60-second delay` would create a repeated restart loop for an unenrolled / zero-secret service | For the first release, **failure actions = `none`** until enrollment, rollback, uninstall, and heartbeat behavior are proven; revisit after a successful enrolled run | A never-enrolled service that restarts every 60 seconds will hammer the (placeholder) endpoint and fill Event Log |
| 10 | (missing) | Added §0.x MajorUpgrade / downgrade block policy; §0.y Public config lifecycle (installer-owned vs operator-owned); §0.z Payload allowlist (fail build on unexpected binary); §0.aa Signing policy (don't blindly re-sign every bundled DLL; use approved payload list); §0.ab Release verification; §0.ac Build provenance; §0.ad Secret provisioning runbook | Perplexity called out each as a 2026 best-practice gap |
| 11 | "V0.1" mentioned in §11 changelog as "v0.2 → v0.1" typo | §11 changelog corrected to v0.1 → v0.2 → v0.3 progression | housekeeping |

All other v0.2 sections preserved (pinned versions, locked
`requirements.lock`, sign order, SBOM-as-SPDX, never-overwrite secret,
forbidden actions, no remote install, etc.).

---

## 0.x Pinned versions, hashes, and build matrix

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12.x LTS | Build-host interpreter; runtime target in MSI is whatever PyInstaller bundles |
| PyInstaller | 6.x latest | `--onedir --noconsole`; **no `--uac-admin`** |
| WiX Toolset | 4.x latest | `<HarvestDirectory>` task in `.wixproj`; **no untracked `HeatDirectory.wxs`** |
| Windows SDK (signtool) | 10.0.22621.x or newer | For `signtool.exe` |
| RFC 3161 timestamp URL | per operator binding | Default suggested: `http://timestamp.digicert.com` |
| Code-signing cert | per operator binding | OV or EV, or Azure Trusted Signing |
| SBOM generator | per operator binding | Real SPDX 2.3 or CycloneDX 1.6 generator; **not a hand-rolled JSON** |

Locked dependency file (`installer/requirements.lock`) hashes every
pip package the orch client imports; the build fails if any hash
drifts. SBOM is generated as part of the build for every release and
**must** validate against the SPDX or CycloneDX schema.

---

## 0.y MSI upgrade / downgrade / repair / uninstall policy

| Aspect | Policy |
|---|---|
| `UpgradeCode` | **Fixed** across all versions of OrchClient (one GUID for the product line) |
| `ProductCode` | **Rotates per version** (each release is a new MSI) |
| `MajorUpgrade` element | Present in `.wxs` with `Schedule="afterInstallInitialize"` and `AllowSameVersionUpgrades="no"` |
| Downgrade block | `Disallow="yes"` in `MajorUpgrade`; older version stays installed if newer MSI is launched |
| Repair behavior | Reinstalls components in the same key path; `NeverOverwrite="yes"` on the secret file means the operator's provisioned secret is preserved |
| Uninstall behavior | Removes installed files; **explicit per-state secret behavior** (see §0.4) |

---

## 0.z Public config lifecycle (installer-owned vs operator-owned)

The public config file at `C:\ProgramData\HermesOrchClient\config.yaml`
is **installer-owned** by default. Each MSI release may include a
newer `config.yaml` template; on major upgrade the installer's newer
template overwrites the operator-edited one. Operators who want
operator-owned config must move to a separate data directory (out of
scope for v0.3; flagged as a follow-up).

The secret file at `C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin`
is **always operator-owned** and is **never overwritten** by the
installer (`NeverOverwrite="yes"`).

---

## 0.aa Payload allowlist (CI build-time check)

A build-time check enumerates the harvested PyInstaller output
directory and asserts that every file matches one of:

- The PyInstaller Python runtime DLLs (e.g. `python314.dll`, `vcruntime140.dll`)
- The pywin32 service dispatcher DLLs (`pywintypes*.dll`, `pythoncom*.dll`)
- The orch client module + its declared deps
- The signed payload inventory recorded in the build manifest
- Allowed templates (`config.yaml`, `agent-secret.bin` placeholder)

If any `.exe`, `.dll`, `.pyd`, `.bat`, `.cmd`, `.ps1`, `.sh`, or other
unexpected script/binary appears in the harvested output, the build
fails before the MSI is produced.

---

## 0.ab Signing policy

- **Approved payload list**: only files in the manifest's signed
  payload list are signed. Third-party DLLs that already carry a
  valid Authenticode signature (Python runtime, pywin32) are **not
  re-signed** — re-signing can break a valid signature and trigger
  SmartScreen warnings.
- **Sign order**: payload EXEs / DLLs that we own first, then the
  final MSI, then verify, then compute the final SHA-256 and write
  the manifest + SBOM.
- **Per-step failure**: any `signtool` invocation that returns
  non-zero is caught by `Invoke-NativeChecked` and aborts the build.

---

## 0.ac Release verification (operator-side, target machine)

```powershell
# Sign verify
signtool.exe verify /pa /all /v "OrchClient-v0.1.0-x64.msi"

# SHA-256 compare against SHA256SUMS.txt
Get-FileHash "OrchClient-v0.1.0-x64.msi" -Algorithm SHA256

# Publisher + cert chain
Get-AuthenticodeSignature "OrchClient-v0.1.0-x64.msi" |
    Select-Object SignerCertificate.Subject, NotAfter, IsOSCertificate

# RFC 3161 timestamp presence
# (visible in signtool verify verbose output)
```

The MSI must:
- Pass `signtool verify /pa /all /v`
- Match the SHA-256 in `SHA256SUMS.txt`
- Have a non-expired, non-revoked publisher certificate
- Include a valid RFC 3161 timestamp

---

## 0.ad Build provenance

Every release artifact is accompanied by:

- Exact pinned versions (Python, PyInstaller, WiX, Windows SDK, signtool)
- Source commit SHA on the build branch
- Locked `requirements.lock` hash
- Build-host identifier (operator-bound)
- Build timestamp (UTC)
- SBOM (SPDX or CycloneDX)
- `MANIFEST.json` with version + SHA-256 + signer policy + timestamp

---

## 0.ae Secret provisioning runbook (post-install, privileged)

After the MSI is installed and the operator has reviewed
`MANIFEST.json` + SBOM + signature:

1. **Stop the service** (it should already be demand-start, but verify):
   ```powershell
   Stop-Service -Name OrchClient -ErrorAction SilentlyContinue
   ```
2. **Write the real HMAC secret** (out-of-band; never via the MSI):
   ```powershell
   # Operator pastes the real secret bytes into a temp file
   # then moves it into the locked location
   $secretFile = 'C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin'
   [System.IO.File]::WriteAllBytes(
       $secretFile,
       $operatorProvidedSecretBytes)
   ```
3. **Re-verify the ACL** matches `D:P(A;;FA;;;SY)(A;;FA;;;BA)` (no Users ACE):
   ```powershell
   $acl = Get-Acl $secretFile
   $acl.Sddl   # should contain O:BAG:SYD:(A;;FA;;;SY)(A;;FA;;;BA)
   ```
4. **Write the real config** with the operator-bound agent_id,
   `https://<ORCHESTRATOR_FQDN>:<HTTPS_PORT>` URL, etc.
5. **Verify service still refuses to start** if the secret is empty
   or the endpoint is HTTP / placeholder. Then start the service:
   ```powershell
   Start-Service -Name OrchClient
   Get-Service -Name OrchClient
   ```

---

## 0.4 Secret-preservation state table (v0.2 + v0.3, now with explicit VM test requirement)

| # | State | Required behavior | VM test required |
|---|---|---|---|
| 1 | Fresh MSI install | Create zero-byte placeholder; service is demand-start and **refuses to run** until real config + secret are written | ✓ |
| 2 | Secret provisioned after install | Preserve it through `Repair` and `MajorUpgrade` | ✓ |
| 3 | Upgrade with missing secret (operator deleted) | **Do not silently recreate**; service stays demand-start; health gate fails closed | ✓ |
| 4 | Uninstall | **Explicitly**: securely remove the secret file (current v0.3 default) OR preserve it for reinstall (operator choice) | ✓ |
| 5 | Reinstall after uninstall | Placeholder returns; prior secret is gone (if §0.4.4 = "remove") OR preserved (if §0.4.4 = "preserve") | ✓ |
| 6 | Operator-edited config on upgrade | Per §0.z: installer-owned → MSI template overwrites; operator-owned → no overwrite (out of scope) | ✓ |

`NeverOverwrite="yes"` on `OrchClientSecretComponent` enforces (2), (3),
and (5) at the MSI level. (1), (4), (5) require post-install VM tests.

---

## 1. Goal

Produce:

1. A runnable **orch client** (Python) that signs HMAC-authenticated
   heartbeats and self-enrolls against the orchestrator's enrollment
   endpoint (B12-deployed contract; TBD until B12 is reviewed).
2. A **code-signed Windows MSI** built with PyInstaller + WiX 4 +
   `HarvestDirectory` task.
3. A **SHA-256 + SBOM + signing manifest** for operator handoff.
4. An **updated install runbook** that references the real artifacts
   (not illustrative filenames).

The MSI install shall:

- Drop the orch client under `C:\Program Files\HermesOrchClient\`
- Register a Windows Service named `OrchClient` (start = demand)
- Drop a placeholder config file at
  `C:\ProgramData\HermesOrchClient\config.yaml` (locked ACL,
  **placeholder URL** — service refuses to start until operator
  writes a real `https://<ORCHESTRATOR_FQDN>:<HTTPS_PORT>` value)
- Drop a zero-byte placeholder secret at
  `C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin` with
  `NeverOverwrite="yes"` and ACL `D:P(A;;FA;;;SY)(A;;FA;;;BA)`
- Lock the parent directories with matching SDDLs (`util:PermissionEx`)
- **Not** auto-start the service on install
- For the first release, set service `FirstFailure=SecondFailure=ThirdFailure=none` (no restart loop on unenrolled / zero-secret state)

---

## 2. Deliverables

| # | Artifact | Path |
|---|---|---|
| 1 | Orch client source | `C:\Project\minimax code\hermes-orchestrator\src\orch_client\__init__.py` + `__main__.py` + `client.py` + `hmac_auth.py` + `config.py` + `logging_setup.py` + `service.py` |
| 2 | `pyproject.toml` entry point `orch-client` | extend existing `C:\Project\minimax code\hermes-orchestrator\pyproject.toml` |
| 3 | Locked dependency file | `C:\Project\minimax code\hermes-orchestrator\installer\requirements.lock` (every pip package hash) |
| 4 | PyInstaller spec | `C:\Project\minimax code\hermes-orchestrator\installer\orch-client.spec` |
| 5 | WiX 4 `.wixproj` + manually-authored `.wxs` | `C:\Project\minimax code\hermes-orchestrator\installer\orch-client.wixproj` + `orch-client.wxs` |
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
  hmac_auth.py         # HMAC-SHA256 over bound metadata + body hash
  config.py            # YAML config loader; per-install secret override
  logging_setup.py     # structured JSONL logger to ProgramData logs
  service.py           # Windows Service entry (pywin32 or win32serviceutil)
```

### 3.1 `hmac_auth.py` (revised in v0.3 — pick the bound-metadata model)

- **Body hash** = `SHA-256(exact raw UTF-8 request-body bytes)`,
  hex-lowercase
- **Signing input** (fixed-order, newline-separated):
  ```
  protocol-version || "\n" ||
  key-id          || "\n" ||
  timestamp       || "\n" ||
  nonce           || "\n" ||
  method          || "\n" ||
  canonical-path  || "\n" ||
  body_sha256
  ```
  Where:
  - `protocol-version` = `1` (or current; per operator binding)
  - `key-id` = a real key-rotation identifier, **not necessarily** the
    `agent_id` (one agent may have old + new HMAC key during rotation)
  - `timestamp` = RFC 3339 UTC, e.g. `2026-08-13T08:00:00Z`
  - `nonce` = 16 random bytes hex-lowercase (32 chars), single-use
  - `method` = uppercase HTTP method, e.g. `POST`
  - `canonical-path` = canonical request path, e.g. `/api/agents/abc/heartbeat`
  - `body_sha256` = SHA-256 of raw body bytes
- **Signature** = `HMAC-SHA256(secret, UTF-8(signing_input))`,
  hex-lowercase
- **Headers** (all sent with every signed request):

  | Header | Value |
  |---|---|
  | `X-Hermes-Protocol-Version` | `1` |
  | `X-Hermes-Key-Id` | the key-id above (not necessarily agent_id) |
  | `X-Hermes-Timestamp` | RFC 3339 UTC |
  | `X-Hermes-Nonce` | 16 random bytes hex-lowercase |
  | `X-Hermes-Endpoint` | canonical path (informational; server does NOT trust as authority) |
  | `X-Hermes-Body-Sha256` | SHA-256 of the raw body bytes (hex-lowercase) |
  | `X-Hermes-Signature` | the signature above (hex-lowercase) |

- **Server validation** (documented, not implemented in this plan):
  1. Enforce protocol version
  2. Look up key by authenticated key-id
  3. Derive method + canonical path from the actual request — do
     NOT trust `X-Hermes-Endpoint` as authority
  4. Compute SHA-256 of received raw body bytes
  5. Compare computed body hash to signed body-sha256
  6. Validate timestamp (reject outside ±5 min of server clock)
  7. Atomically reject nonce replay for the full replay-retention
     window
  8. Constant-time compare the HMAC
  9. Only then parse JSON body

- **Why this shape (not "raw body alone")**: the bound-metadata
  model binds replay metadata + endpoint semantics to the signature.
  Without endpoint binding, a captured signed request can be replayed
  against any endpoint that the same key-id is authorized for.

### 3.2 `client.py`

- `enroll(orchestrator_url, agent_id, public_key_pem) -> enrollment_receipt`
  calls the enrollment endpoint (path / method / auth: **TBD until
  B12 is deployed and reviewed**). Returns `{agent_id, hmac_secret,
  hmac_key_id}` to the caller. In the build pipeline, **enrollment
  is NOT invoked** — only a dry-run code path validates request
  shape.
- `heartbeat(orchestrator_url, agent_id, hmac_secret) -> None` is a
  loop that signs + sends a heartbeat every N seconds (configurable).
  Also a dry-run mode that prints the canonical headers + signature
  to stdout without sending.

### 3.3 `service.py`

Windows Service entry via `pywin32` (`win32serviceutil.ServiceFramework`).
The service is registered with `Start="demand"` so the operator must
explicitly start it after configuration.

**Service must refuse to start** if:
- `agent-secret.bin` is zero bytes
- `orchestrator_url` is the placeholder, HTTP, or missing
- ACL on the secret file or directory is wrong

### 3.4 `config.py`

Reads YAML at `C:\ProgramData\HermesOrchClient\config.yaml` with
fields:

```yaml
agent_id: <assigned at install time>
orchestrator_url: https://<ORCHESTRATOR_FQDN>:<HTTPS_PORT>   # placeholder; operator must fill
hmac_key_id: <key-rotation id, defaults to agent_id>
heartbeat_interval_sec: 30
log_level: info
```

The HMAC secret is read separately from
`C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin` (raw
bytes; access is gated by the file's SDDL, **not** by Unix mode bits).

### 3.5 Dry-run only at build time

The source includes a `--dry-run` flag that:
- Loads config (or uses bundled test config)
- Generates a synthetic heartbeat payload
- Computes the body SHA-256 + canonical signing input + HMAC
- Prints the canonical headers + signature to stdout

This proves the signing chain without contacting any live service.
The dry-run is what gets exercised in the build pipeline; live HTTP
calls only happen on the target machine after install.

### 3.6 Service-dispatcher VM validation list (new in v0.3)

The build is **not** considered complete until the following are
verified on a clean Windows 10/11 VM:

- [ ] `OrchClient.exe` is the entry point; pywin32 service dispatcher
  is present in the frozen bundle
- [ ] `servicemanager`, `pywintypes`, `win32serviceutil` DLLs are
  present and loadable
- [ ] SCM can start `OrchClient.exe` (the service appears as
  `OrchClient` with `Start=Demand` and the expected description)
- [ ] Service stop event reaches the Python loop (graceful stop
  completes within `StopTimeout` seconds, e.g. 15)
- [ ] No console dependency (`--noconsole` confirmed; no console
  window appears on start)
- [ ] Event Log or protected file logging captures startup failures
  (with a writeable target inside the locked `ProgramData` tree)
- [ ] `console=False` / `--noconsole` is correct for suppressing the
  console window, but **does not by itself** make `__main__.py` a
  valid SCM service — the dispatcher must be wired in `service.py`

---

## 4. PyInstaller spec (Deliverable 4)

`--onedir --noconsole` (per Perplexity's earlier guidance; preferred
over `--onefile` for service/agent). **No `--uac-admin`**: inappropriate
for an SCM-launched service.

`orch-client.spec`:

```python
# -*- mode: python ; coding: utf-8 -*-
import sys ; sys.setrecursionlimit(5000)
from PyInstaller.utils.hooks import collect_submodules, collect_data_files
hidden = collect_submodules('orch_client')
datas  = collect_data_files('orch_client')

a = Analysis(
    ['..\\src\\orch_client\\__main__.py'],
    pathex=['..\\src'],
    hiddenimports=hidden + ['win32serviceutil', 'servicemanager'],
    datas=datas,
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
    console=False,         # equivalent to --noconsole
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
    --paths ..\src \
    --hidden-import win32serviceutil --hidden-import servicemanager \
    ..\src\orch_client\__main__.py
```

Output: `dist/OrchClient/OrchClient.exe` + supporting files (Python
runtime, stdlib, deps, pywin32 DLLs). This folder is what WiX consumes.

---

## 5. WiX 4 source (Deliverable 5) — revised in v0.3

**Key changes vs v0.2**:
- `KeyPath="yes"` is on the **`<File>`** element, not the `<Component>`
- Manually-authored components are wrapped in explicit
  **`<ComponentGroup>`** elements, with matching `<ComponentGroupRef>`
  IDs
- Use `<HarvestDirectory>` task in `.wixproj`; **no untracked
  `HeatDirectory.wxs`**
- Directory ACLs added (`util:PermissionEx` on
  `ProgramData\HermesOrchClient\` and `…\secrets\`)
- Secret file SDDL has no Users ACE: `D:P(A;;FA;;;SY)(A;;FA;;;BA)`
- First-release failure actions: `none / none / none` (no restart
  loop on unenrolled / zero-secret state)

### 5.1 Project layout

```
installer/
  orch-client.wixproj     # MSBuild project: HarvestDirectory + candle + light
  orch-client.wxs         # manually authored fragments (service + config + secret + dirs)
  templates/
    config/config.yaml    # placeholder config (URL is https://<ORCHESTRATOR_FQDN>:<HTTPS_PORT>)
    secret/agent-secret.bin  # zero-byte placeholder (NeverOverwrite="yes")
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

### 5.3 `orch-client.wxs` (manually authored fragments — revised in v0.3)

```xml
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs"
     xmlns:util="http://wixtoolset.org/schemas/v4/wxs/util">
  <Package Name="OrchClient" Version="0.1.0" Manufacturer="ACME"
           UpgradeCode="PUT-GUID-HERE">
    <MediaTemplate EmbedCab="yes" />
    <MajorUpgrade Schedule="afterInstallInitialize"
                  AllowSameVersionUpgrades="no"
                  Disallow="yes" />
    <Feature Id="Main" Title="Orch Client" Level="1">
      <ComponentGroupRef Id="OrchClientFiles" />     <!-- harvested -->
      <ComponentGroupRef Id="OrchClientConfig" />    <!-- manual -->
      <ComponentGroupRef Id="OrchClientSecret" />    <!-- manual -->
      <ComponentGroupRef Id="OrchClientService" />   <!-- manual -->
    </Feature>
  </Package>

  <!-- Harvested ComponentGroup: Python runtime + pywin32 + module deps -->
  <Fragment>
    <ComponentGroup Id="OrchClientFiles">
      <DirectoryRef Id="INSTALLFOLDER" />
    </ComponentGroup>
  </Fragment>

  <!-- Manually authored service component (KeyPath on File, not Component) -->
  <Fragment>
    <DirectoryRef Id="INSTALLFOLDER">
      <Component Id="OrchClientServiceComponent" Guid="*">
        <File Id="OrchClientServiceExe"
              Source="$(var.PublishDir)\OrchClient.exe"
              KeyPath="yes" />
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
          <!-- First release: failure actions = none (no restart loop on
               unenrolled / zero-secret state). Revisit after a successful
               enrolled run. -->
          <util:ServiceConfig
              FirstFailureActionType="none"
              SecondFailureActionType="none"
              ThirdFailureActionType="none" />
        </ServiceInstall>
        <ServiceControl Id="ControlOrchClient"
                        Name="OrchClient"
                        Stop="both"
                        Remove="uninstall"
                        Wait="yes" />
      </Component>
    </DirectoryRef>
  </Fragment>

  <!-- Public config component + directory ACL -->
  <Fragment>
    <StandardDirectory Id="ProgramDataFolder">
      <Directory Id="CONFIGFOLDER" Name="HermesOrchClient">
        <!-- Directory ACL: SYSTEM:F, Admins:F, Users:R/X as required -->
        <Component Id="OrchClientConfigDirComponent" Guid="*" KeyPath="yes">
          <CreateFolder>
            <util:PermissionEx
              Sddl="D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)(A;;FX;;;BU)" />
          </CreateFolder>
        </Component>
        <Component Id="OrchClientConfigFileComponent" Guid="*">
          <File Id="OrchClientConfigFile"
                Source="$(var.ConfigTemplate)\config.yaml"
                Name="config.yaml"
                KeyPath="yes">
            <util:PermissionEx
              Sddl="D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)" />
          </File>
        </Component>
        <Directory Id="SECRETFOLDER" Name="secrets">
          <!-- Secret directory ACL: SYSTEM:F, Admins:F, no Users ACE -->
          <Component Id="OrchClientSecretDirComponent" Guid="*" KeyPath="yes">
            <CreateFolder>
              <util:PermissionEx
                Sddl="D:P(A;;FA;;;SY)(A;;FA;;;BA)" />
            </CreateFolder>
          </Component>
          <Component Id="OrchClientSecretFileComponent" Guid="*"
                     NeverOverwrite="yes">
            <File Id="OrchClientSecretFile"
                  Source="$(var.SecretTemplate)\agent-secret.bin"
                  Name="agent-secret.bin"
                  KeyPath="yes">
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

Key elements (revised in v0.3):
- `<MajorUpgrade ... Disallow="yes" />` to block downgrade
- `KeyPath="yes"` is on the **`<File>`** element (and on
  `<CreateFolder>` for the directory components)
- `<util:PermissionEx>` with explicit SDDL on each file and on the
  parent directories
- `NeverOverwrite="yes"` on the secret **Component**
- First-release failure actions: `none / none / none` (no restart
  loop on unenrolled / zero-secret state)

---

## 6. Build script (Deliverable 6) — revised in v0.3

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
$ExpectedMsi   = "OrchClient-v${Version}-${Arch}.msi"

# Native-exec helper: every external command goes through this so a
# non-zero exit code actually fails the script. $ErrorActionPreference
# does NOT auto-fail on native errors; you must check $LASTEXITCODE.
function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$Label
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE. Command: $FilePath $($Arguments -join ' ')"
    }
}

# 0) Verify locked dependency file matches
Invoke-NativeChecked -FilePath 'pip' `
    -Arguments @('install','--require-hashes','-r',
                  (Join-Path $RepoRoot 'installer/requirements.lock')) `
    -Label 'pip install --require-hashes'

# 1) PyInstaller --onedir --noconsole (already installed in step 0)
$publishDir = Join-Path $RepoRoot 'dist\OrchClient'
Remove-Item -LiteralPath $publishDir -Recurse -Force -ErrorAction SilentlyContinue
Invoke-NativeChecked -FilePath $PyInstaller `
    -Arguments @('--clean','--noconfirm','--onedir','--noconsole','--name','OrchClient',
                 '--paths',(Join-Path $RepoRoot 'src'),
                 '--hidden-import','win32serviceutil',
                 '--hidden-import','servicemanager',
                 (Join-Path $RepoRoot 'src\orch_client\__main__.py')) `
    -Label 'pyinstaller --onedir --noconsole'

# 2) Sign each shipped EXE / DLL we own. Third-party DLLs that already
#    carry a valid Authenticode signature (Python runtime, pywin32) are
#    NOT re-signed (re-signing can break a valid signature).
#    "Owned" = produced by our PyInstaller invocation, not vendored.
$ownedExes = @(Get-ChildItem -Recurse -File -Path $publishDir |
    Where-Object { $_.Name -eq 'OrchClient.exe' })
foreach ($f in $ownedExes) {
    Invoke-NativeChecked -FilePath 'signtool.exe' `
        -Arguments @('sign','/fd','SHA256','/tr',$TimestampUrl,'/td','SHA256',
                      '/sha1',$CertThumb,$f.FullName) `
        -Label "signtool sign $($f.Name)"
}

# 3) Build MSI via `dotnet build` (SDK-style .wixproj with
#    HarvestDirectory task). The exact output filename is asserted.
$msiDir = Join-Path $RepoRoot 'dist'
Remove-Item -LiteralPath (Join-Path $msiDir '*.msi') -ErrorAction SilentlyContinue
Invoke-NativeChecked -FilePath 'dotnet' `
    -Arguments @('build',$WixProject,'-c','Release','-p',"Platform=$Arch") `
    -Label 'dotnet build (WiX 4 SDK-style .wixproj)'
$msiPath = Join-Path $msiDir $ExpectedMsi
if (-not (Test-Path -LiteralPath $msiPath)) {
    throw "Expected MSI not found at $msiPath (exact filename assertion failed)"
}

# 4) Sign the MSI itself
Invoke-NativeChecked -FilePath 'signtool.exe' `
    -Arguments @('sign','/fd','SHA256','/tr',$TimestampUrl,'/td','SHA256',
                  '/sha1',$CertThumb,$msiPath) `
    -Label 'signtool sign MSI'

# 5) Verify
Invoke-NativeChecked -FilePath 'signtool.exe' `
    -Arguments @('verify','/pa','/all','/v',$msiPath) `
    -Label 'signtool verify MSI'

# 6) Compute SHA-256 + write manifest + SPDX SBOM
$hash = (Get-FileHash -LiteralPath $msiPath -Algorithm SHA256).Hash
$manifest = [ordered]@{
    product       = 'OrchClient'
    version       = $Version
    architecture  = $Arch
    msi_path      = $msiPath
    msi_filename  = $ExpectedMsi
    msi_sha256    = $hash
    msi_size      = (Get-Item $msiPath).Length
    built_at_utc  = (Get-Date).ToUniversalTime().ToString('o')
    built_by      = $env:USERNAME
    signing       = [ordered]@{
        tool        = 'signtool.exe'
        timestamp   = $TimestampUrl
        cert_sha1   = $CertThumb
        digest      = 'SHA256'
    }
    payload_inventory = @((Get-ChildItem $publishDir -Recurse -File).FullName |
        ForEach-Object { [PSCustomObject]@{ path = (Resolve-Path -LiteralPath $_ -Relative); sha256 = (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash } })
} | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText(
    (Join-Path $msiDir 'MANIFEST.json'),
    $manifest,
    [System.Text.UTF8Encoding]::new($false))
"$hash  $ExpectedMsi" |
    Out-File -LiteralPath (Join-Path $msiDir 'SHA256SUMS.txt') -Encoding utf8

# 7) Generate a real SPDX 2.3 SBOM. Use a real generator
#    (operator-bound); never a hand-rolled JSON labelled SPDX.
#    The v0.3 script calls the operator-bound generator here.
#    (Example shape — operator replaces with their tool of choice.)
& $sbomGen -inputDir $publishDir -outputFile (Join-Path $msiDir 'SBOM.spdx.json') `
           -documentNamespace "https://hermesorchestrator.local/spdxdocs/orchclient-$Version" `
           -packageName "OrchClient-$Version" `
           -packageVersion $Version
if ($LASTEXITCODE -ne 0) {
    throw "SBOM generator failed with exit code $LASTEXITCODE"
}

Write-Host "[+] Build complete: $msiPath"
Write-Host "[+] SHA-256: $hash"
```

**Sign order** (per v0.2 + v0.3): owned EXEs / DLLs → MSI → verify →
final hash → manifest + SBOM. Each step is `Invoke-NativeChecked`-gated.

---

## 7. Known gaps & explicit dependencies (must be resolved before build)

| # | Dependency | Owner | Status |
|---|---|---|---|
| 1 | Orchestrator contract (enrollment + heartbeat + cleanup endpoint paths, methods, auth, CSRF) | operator | **TBD until B12 is deployed and reviewed**; v0.3 plan explicitly does NOT assume pre-B12 HTTP contract |
| 2 | Code-signing cert thumbprint (or Azure Trusted Signing endpoint) | operator | not yet bound |
| 3 | WiX 4 install path + `dotnet` SDK on the build host | operator | not yet installed (default assumed: `C:\wix\v4\`, `dotnet` on PATH) |
| 4 | PyInstaller availability in build Python | operator | assumed present |
| 5 | Target machine agent_id (e.g. `win-b-02`) | operator | not yet assigned |
| 6 | Target machine HMAC secret (operator-bound, out-of-band) | operator | not yet generated; MSI ships with **zero-byte placeholder** and `NeverOverwrite="yes"` so it cannot clobber a real secret |
| 7 | HMAC `key-id` (must support rotation: agent may have old + new key) | operator | not yet bound |
| 8 | Orchestrator `<ORCHESTRATOR_FQDN>` and `<HTTPS_PORT>` (B13-transport-closed) | operator | not yet bound; MSI ships with placeholder URL; service refuses to start until operator writes a real value |
| 9 | Real SPDX / CycloneDX generator tool + namespace | operator | not yet bound; plan asserts "real generator", not "hand-rolled JSON" |
| 10 | Cert renewal policy + revoked-cert response | operator | bind at runtime; manifest captures signer policy |
| 11 | UpgradeCode GUID (fixed across versions) + ProductCode rotation per version | operator | bind at runtime; v0.3 `.wxs` has `<MajorUpgrade Disallow="yes" />` |
| 12 | Test case matrix (offline / cert mismatch / missing-secret / invalid-secret / repair / upgrade / uninstall / partial-install / revocation) | operator | new in v0.3; test plan to be drafted separately |
| 13 | VM test environment (clean Windows 10/11) for §3.6 + §0.4 states | operator | required before implementation approval |

---

## 8. Forbidden actions (no exceptions)

- ❌ No remote install on the target Windows machine
- ❌ No live HTTP calls during build (dry-run only)
- ❌ No real HMAC secret embedded in the MSI (only zero-byte placeholder)
- ❌ No `orchestrator_url: http://...` shipped in MSI config (placeholder only)
- ❌ No modification of B12 deploy (held at watchdog REFUSE)
- ❌ No modification of watchdog task (held)
- ❌ No push to remote git remote (per proposal-branch pattern only)
- ❌ No firewall / enrollment / agent-mutation action on the orchestrator
- ❌ No repair / upgrade / uninstall that clobbers a provisioned secret (`NeverOverwrite="yes"` enforces at MSI level; operator must NOT manually delete the secret file in a way that the placeholder would replace)
- ❌ No blind re-signing of third-party DLLs that already carry a valid Authenticode signature
- ❌ No labelling a hand-rolled JSON as SPDX or CycloneDX

---

## 9. Acceptance criteria

A. The MSI installs cleanly on a clean Windows 10/11 VM:
   - File system layout matches §1
   - ACL on config file: `SYSTEM:F, Admins:F, Users:R` (verified via
     `Get-Acl`)
   - ACL on config directory: `SYSTEM:F, Admins:F, Users:R/X`
   - ACL on secret file: `SYSTEM:F, Admins:F` (no Users)
   - ACL on secret directory: `SYSTEM:F, Admins:F` (no Users)
   - Service is registered with `Start=Demand`, `State=Stopped`
   - Service is NOT auto-started
   - `OrchClient.exe` is the service binary; pywin32 dispatcher is
     present; service can be started by SCM

B. The MSI is code-signed:
   - `signtool verify /pa /all /v` reports valid
   - SHA-256 in `SHA256SUMS.txt` matches `Get-FileHash`
   - `MANIFEST.json` is consistent with the MSI
   - SBOM is a real SPDX 2.3 (or CycloneDX 1.6) document, not a
     custom JSON mislabelled
   - Owned payload files are signed; third-party DLLs retain their
     own signatures (not blindly re-signed)

C. The orch client `--dry-run` produces a canonical signed payload:
   - Operator can run `OrchClient.exe --dry-run` and see the canonical
     headers + signature + raw body bytes (without any HTTP call)

D. Secret-preservation behavior:
   - Install MSI with a real secret in place → secret unchanged
   - Repair install → secret unchanged
   - Major upgrade (same UpgradeCode) → secret unchanged
   - Uninstall → secret removed (per §0.4.4 default; operator can
     override to "preserve" if needed)
   - Reinstall on top of uninstall → placeholder returned, no real
     secret re-injected
   - All 6 §0.4 states verified in a VM

E. Service fail-closed on bad state:
   - Service refuses to start when `agent-secret.bin` is zero bytes
   - Service refuses to start when `orchestrator_url` is the
     placeholder, HTTP, or missing
   - Service refuses to start when secret file ACL is wrong

F. The updated runbook (`orch-client-install-runbook.md`) uses
   the real MSI filename and references §7 binding values.

G. No side-effecting action against the live orchestrator.

H. The HMAC validation:
   - Server uses constant-time compare
   - Server maintains a bounded replay cache keyed by `(key-id, nonce)`
   - Server rejects requests with timestamp ±5 min outside server clock
   - Server does NOT trust `X-Hermes-Endpoint` as authority; derives
     method + canonical path from the actual request

I. §3.6 Service-dispatcher VM validation list: every checkbox
   verified on a clean Windows 10/11 VM

J. The test case matrix covers:
   - offline, cert mismatch, missing-secret, invalid-secret
   - repair, major upgrade, uninstall, partial-install
   - revocation
   - payload allowlist fails on unexpected binary
   - SBOM schema validates
   - signing round-trip (sign → verify → hash) on a clean target

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
- Open the B12 deploy (still on hold at watchdog REFUSE)

---

## 11. v0.1 → v0.2 → v0.3 changelog (cross-reference)

| # | Section | v0.1 | v0.2 | v0.3 |
|---|---|---|---|---|
| 1 | §3.1 HMAC canonicalization | "JSON with fixed key order" | `HMAC-SHA256(secret, raw-body bytes)` + bound headers | Bound-metadata model picked (NOT raw-body alone); server derives method + canonical path from actual request; key-id is a real rotation identifier |
| 2 | §1 / §1.2 orchestrator_url | (n/a) | `http://192.168.2.152:8765` | **`https://<ORCHESTRATOR_FQDN>:<HTTPS_PORT>` placeholder**; service refuses to start on placeholder / HTTP / missing / zero-secret |
| 3 | §5 WiX KeyPath | `KeyPath="yes"` on `<Component>` | (same) | **`KeyPath="yes"` on `<File>`** (and on `<CreateFolder>` for dirs) |
| 4 | §5 WiX ComponentGroup | `<ComponentGroupRef>` to non-existent IDs | (same) | Manually-authored components wrapped in explicit `<ComponentGroup>` with matching IDs |
| 5 | §5 WiX harvest pattern | `<Files Include>` | `<HarvestDirectory>` in `.wixproj` | (same; explicit "no untracked HeatDirectory.wxs") |
| 6 | §5 WiX directories | (only files) | (only files) | **`util:PermissionEx` on `…\HermesOrchClient\` and `…\secrets\`** with SDDL |
| 7 | §5 WiX service failure actions | `restart / restart / none / 60s` | (same) | **`none / none / none` for first release** (no restart loop on unenrolled / zero-secret state) |
| 8 | §5 WiX secret SDDL | `D:P(A;;FA;;;SY)(A;;FA;;;BA)` | (same) | (same) + drop the "mode 0600" analogy; state the exact SDDL |
| 9 | §6 Build pipeline | Native exit codes not checked | (same) | **`Invoke-NativeChecked` helper for every native call**; `dotnet build` for the SDK-style `.wixproj`; exact filename assertion for the MSI; real SPDX/CycloneDX generator (not hand-rolled JSON) |
| 10 | §5 / §0.y MSI upgrade policy | (missing) | (missing) | `UpgradeCode` fixed; `ProductCode` per version; `MajorUpgrade Disallow="yes"` |
| 11 | §0.z Public config lifecycle | (implicit) | (implicit) | **Explicit**: installer-owned by default; operator-owned out of scope (follow-up) |
| 12 | §0.aa Payload allowlist | (missing) | (missing) | **CI build-time check** for unexpected binary in harvested output |
| 13 | §0.ab Signing policy | (MSI only) | (payload + MSI) | Owned payload only; **don't re-sign third-party DLLs** with valid signatures |
| 14 | §0.ac Release verification | (missing) | (missing) | `signtool verify` / SHA-256 compare / publisher + cert chain / RFC 3161 timestamp |
| 15 | §0.ad Build provenance | (missing) | (missing) | Versions + commit SHA + locked hashes + build-host + timestamp |
| 16 | §0.ae Secret provisioning runbook | (missing) | (missing) | Post-install privileged steps + ACL re-verify + service-stays-stopped |
| 17 | §0.4 Secret-preservation states | (implicit) | (explicit 4-state table) | (same) + **VM test required** per state; expanded to 6 states |
| 18 | §3.6 Service-dispatcher VM validation | (missing) | (missing) | **Explicit list** of pywin32 dispatcher / DLLs / SCM / stop event / console / Event Log |
| 19 | §9 Acceptance | 6 criteria | 8 criteria | **10 criteria** (added service fail-closed, dispatcher validation) |
| 20 | §7 dependencies | 7 items | 10 items | **13 items** (added B13 URL, key-id rotation, SPDX tool, UpgradeCode, VM env) |
| 21 | §8 forbidden actions | 7 items | 8 items | **11 items** (added "no HTTP URL in MSI", "no blind re-signing", "no hand-rolled SBOM") |
| 22 | §1.3 secret SDDL phrasing | "mode 0600 effectively" | (same) | **Dropped "mode 0600"**; explicit SDDL only |

---

## 12. Outstanding Perplexity questions for v0.4 (if any)

The v0.3 review was the most thorough yet. Remaining open items are
operator-binding, not technical:

- Operator must pick `agent_id` and `key-id` for the target machine
- Operator must pick the code-signing cert / Azure Trusted Signing
- Operator must install WiX 4 + `dotnet` SDK on the build host
- Operator must generate the target machine's HMAC secret (out-of-band)
- Operator must generate the UpgradeCode GUID (or accept the placeholder)
- Operator must run the §3.6 dispatcher validation + §0.4 secret-state
  VM tests, and produce a signed report before implementation approval
- Operator must draft the §9.J test case matrix (separate work item,
  not a code change)
- Operator must bind the real SPDX / CycloneDX generator (not
  hand-rolled JSON)
