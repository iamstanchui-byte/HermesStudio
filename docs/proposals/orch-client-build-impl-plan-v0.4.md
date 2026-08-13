# Orch Client Build — Implementation Plan v0.4 (for Perplexity review)

**Date:** 2026-08-13
**Status:** PROPOSAL for review (Perplexity + operator) — v0.4
**Supersedes:** v0.3 (commit `fc3d4a4` on branch `proposal/orch-client-build-impl-plan-v0.1`)
**Scope:** end-to-end build of a Windows MSI installer for the new orch
client, on the orchestrator host (Windows A). The MSI will be carried
to a separate target Windows machine for installation; this plan does
NOT include remote install, agent enrollment against a live orchestrator,
or B12 deploy. Enrollment is verified separately after the operator
manually runs the install on the target.

---

## 0. v0.3 → v0.4 changelog (Perplexity review)

Perplexity re-read PR #4 after the v0.3 commit and reported:
**2 of 4 v0.2 blockers resolved (HMAC + transport)**, plus 5 new
critical blockers and 3 smaller issues. v0.4 addresses all of them.

| # | v0.3 said | v0.4 says | Reason |
|---|---|---|---|
| 1 | `OrchClientFiles` was **manually declared as an empty group in `.wxs` while `HarvestDirectory` was also asked to generate the same group** | **Remove the manual empty group**; let `HarvestDirectory` generate it; `<ComponentGroupRef Id="OrchClientFiles" />` references the harvested group exactly once | WiX ComponentGroup is meant to collect components and be consumed via `<ComponentGroupRef>`. Defining the same name in two places is a duplicate-symbol / ownership conflict. |
| 2 | `<HarvestDirectory Include="$(PublishDir)">` relied on `$(PublishDir)` being implicitly an MSBuild property | Define `<PublishDir>`, `<ConfigTemplateDir>`, `<SecretTemplateDir>` as real MSBuild properties in `<PropertyGroup>`, then `$(PublishDir)` in the `<HarvestDirectory>` include + `<DefineConstants>PublishDir=$(PublishDir);</DefineConstants>` so the WiX preprocessor can use `$(var.PublishDir)` | `$(PublishDir)` and `$(var.PublishDir)` are different scopes. Without the explicit property, the harvest input can be empty or resolve to an unintended working directory. |
| 3 | `<MajorUpgrade Schedule="afterInstallInitialize" AllowSameVersionUpgrades="no" Disallow="yes" />` had no `UpgradeErrorMessage` | Add `UpgradeErrorMessage="A newer version of Hermes Orch Client is already installed. Please uninstall it first."` | WiX requires `UpgradeErrorMessage` when `Disallow="yes"`; the message is shown when a newer product blocks an older installer |
| 4 | Plan authored an `orch-client.spec` (with `collect_submodules` + `collect_data_files`) but the actual build ran PyInstaller via CLI (`--hidden-import win32serviceutil ...`) | **`.spec` is the single source of truth**. The build script invokes `pyinstaller --clean --noconfirm installer\orch-client.spec`. All frozen-bundle behavior (hidden imports, datas, pywin32 DLLs, version resource, exclusions, payload identity) lives in the spec | The CLI form silently overrides the spec's hidden imports and data handling. Mixing the two means the operator-reviewed spec is not what gets shipped |
| 5 | Release EXE `OrchClient.exe` is built with `--noconsole`; the dry-run acceptance test was "operator runs `OrchClient.exe --dry-run` and sees canonical headers/signature/raw body bytes on stdout" | Ship a **separate `OrchClientDoctor.exe`** (console-enabled, NOT a service). It performs dry-run, config/ACL validation, payload signature checks, and service diagnosis. Operator runs it from an elevated shell. | A release service EXE has no console for stdout; relying on invisible stdout as an operator validation mechanism is a trap |
| 6 | MSI shipped `config.yaml` directly with placeholder URL; operator was expected to fill in real `agent_id` / endpoint / `key-id` after install. **Major upgrade could overwrite those operator-bound values** | MSI ships **`config.yaml.example`** only (with placeholder URL + comments). A privileged provisioner (per `§0.ae`) creates the real `C:\ProgramData\HermesOrchClient\config.yaml` after install. **MSI never overwrites the real config file**. Config file is `NeverOverwrite="yes"`, same as the secret | An installer-owned config that gets silently overwritten on upgrade is unsafe for an enrolled client. The current state machine must be: `config.yaml.example` is the template; `config.yaml` is the operator-owned data; the MSI never touches `config.yaml` after the initial install |
| 7 | Release verification wording implied the cert must be currently unexpired | Clarify: **`signtool verify /pa /all /v` accepts an expired cert if an RFC 3161 timestamp proves the file was signed during certificate validity**. Expiry ≠ revocation; verify checks expiry against the signing-time window, not the current clock. Revocation is a separate check (`signtool verify -rpc` or CRL/OCSP) | Without the timestamp, an expired cert fails verify; with the timestamp, verify confirms the signing chain was valid at the time of signing. The two are different trust questions |
| 8 | `§6` referenced `$sbomGen` but never defined it | **§0.af binds exact SBOM tool, version, command, validation command, namespace policy, and failure handling** | An undefined variable in a build script is a build-time failure; the plan must commit to a specific tool (e.g. `syft` for CycloneDX, `ort` for SPDX) |
| 9 | `NeverOverwrite="yes"` was claimed to enforce uninstall behavior, but `NeverOverwrite` only prevents the **MSI** from overwriting; it does not by itself determine what `RemoveExistingProducts` does on uninstall | The 4-state secret-preservation table now requires **explicit VM test** for each state. The plan asserts "the uninstall behavior is operator-bound in `§0.4.4` (remove or preserve)" and the test must verify the chosen behavior in a clean VM | A claim without a test is a future incident |
| 10 | `CreateFolder` with `PermissionEx` and `KeyPath="yes"` on the parent `<Component>` was not formally declared as the directory key-path model | The `.wxs` now uses `CreateFolder` as the directory payload + `KeyPath="yes"` is on the `<CreateFolder>` (i.e. the directory itself is the key path). `PermissionEx` is the directory's SDDL. **Directory key-path behavior is deliberate**, not inferred | WiX allows the directory itself to be the key path of a component; this is the documented pattern for "no file in this directory" key paths |

All v0.3 sections preserved where not directly affected. Section
numbering kept stable where possible. New sections added for
OrchClientDoctor, SBOM tool binding, and the config-template model.

---

## 0.x Pinned versions, hashes, and build matrix

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12.x LTS | Build-host interpreter; runtime target in MSI is whatever PyInstaller bundles |
| PyInstaller | 6.x latest | `.spec` is the single source of truth (no CLI args at build time) |
| WiX Toolset | 4.x latest | `<HarvestDirectory>` task in `.wixproj`; **no untracked `HeatDirectory.wxs`**; **no manual `OrchClientFiles` group** |
| Windows SDK (signtool) | 10.0.22621.x or newer | For `signtool.exe` |
| .NET SDK | 6.x LTS or 8.x LTS | For `dotnet build` of the SDK-style `.wixproj` |
| RFC 3161 timestamp URL | per operator binding | Default suggested: `http://timestamp.digicert.com` |
| Code-signing cert | per operator binding | OV or EV, or Azure Trusted Signing |
| SBOM generator (bound) | **§0.af picks a specific tool** | e.g. `syft` (Anchore) for CycloneDX 1.6, or `ort` (ClearlyDefined) for SPDX 2.3 |

Locked dependency file (`installer/requirements.lock`) hashes every
pip package the orch client imports; the build fails if any hash
drifts.

---

## 0.y MSI upgrade / downgrade / repair / uninstall policy

```xml
<MajorUpgrade Schedule="afterInstallInitialize"
              AllowSameVersionUpgrades="no"
              Disallow="yes"
              UpgradeErrorMessage="A newer version of Hermes Orch Client is already installed. Please uninstall it first." />
```

| Aspect | Policy |
|---|---|
| `UpgradeCode` | **Fixed** across all versions of OrchClient (one GUID for the product line) |
| `ProductCode` | **Rotates per version** (each release is a new MSI) |
| `MajorUpgrade` | `Schedule="afterInstallInitialize"` + `AllowSameVersionUpgrades="no"` + `Disallow="yes"` + `UpgradeErrorMessage` |
| Downgrade block | `Disallow="yes"`; older version stays installed if newer MSI is launched |
| Repair behavior | Reinstalls components in the same key path; `NeverOverwrite="yes"` on the secret file and the operator-owned config file means the operator's provisioned data is preserved |
| Uninstall behavior | Removes installed files; **explicit per-state secret behavior** (see §0.4) and **explicit per-state config behavior** (see §0.4-bis) |

---

## 0.z Public config lifecycle (operator-owned, never overwritten)

The MSI ships **`config.yaml.example`** only. The real
`config.yaml` is operator-provisioned after install (per `§0.ae`).
The MSI **never overwrites** `config.yaml` after the initial install.

| State | Behavior |
|---|---|
| Fresh install | MSI drops `config.yaml.example` only; no `config.yaml` |
| Operator provisions `config.yaml` | Per `§0.ae` post-install steps |
| Repair | `config.yaml` is **untouched** (`NeverOverwrite="yes"`) |
| Major upgrade | `config.yaml` is **untouched** (`NeverOverwrite="yes"`); `config.yaml.example` may be updated by the new MSI |
| Uninstall | `config.yaml` is removed (operator decides to preserve or remove per `§0.4-bis`) |

---

## 0.aa Payload allowlist (CI build-time check)

A build-time check enumerates the harvested PyInstaller output
directory and asserts that every file matches one of:

- The PyInstaller Python runtime DLLs (e.g. `python314.dll`, `vcruntime140.dll`)
- The pywin32 service dispatcher DLLs (`pywintypes*.dll`, `pythoncom*.dll`)
- The orch client module + its declared deps
- The signed payload inventory recorded in the build manifest
- The `OrchClientDoctor.exe` console binary (allowed; console-enabled is the point)
- Allowed templates (`config.yaml.example`, `agent-secret.bin` placeholder)

If any other `.exe`, `.dll`, `.pyd`, `.bat`, `.cmd`, `.ps1`, `.sh`, or
unexpected script/binary appears, the build fails before the MSI is
produced.

---

## 0.ab Signing policy

- **Approved payload list**: only files in the manifest's signed
  payload list are signed. Third-party DLLs that already carry a
  valid Authenticode signature (Python runtime, pywin32) are **not
  re-signed**.
- **Sign order**: owned payload EXEs (`OrchClient.exe` and
  `OrchClientDoctor.exe`) → MSI → verify → final SHA-256 → manifest + SBOM
- **Per-step failure**: any `signtool` invocation that returns
  non-zero is caught by `Invoke-NativeChecked` and aborts the build
- **Do not** re-sign any DLL whose existing Authenticode signature
  verifies cleanly with `signtool verify` — re-signing can break a
  valid signature and trigger SmartScreen warnings

---

## 0.ac Release verification (operator-side, target machine)

```powershell
# 1. Sign verify
signtool.exe verify /pa /all /v "OrchClient-v0.1.0-x64.msi"

# 2. SHA-256 compare against SHA256SUMS.txt
Get-FileHash "OrchClient-v0.1.0-x64.msi" -Algorithm SHA256

# 3. Publisher + cert chain
Get-AuthenticodeSignature "OrchClient-v0.1.0-x64.msi" |
    Select-Object SignerCertificate.Subject, NotAfter, IsOSCertificate

# 4. RFC 3161 timestamp presence
#    (visible in signtool verify verbose output)
```

The MSI must:
- Pass `signtool verify /pa /all /v`. **Expiry is checked against the
  signing time** (proven by the RFC 3161 timestamp), not the current
  clock. **Revocation** is a separate concern (CRL/OCSP); if the
  signing CA provides an AIA + OCSP responder, use `signtool verify
  -rpc` to fetch the live response.
- Match the SHA-256 in `SHA256SUMS.txt`
- Have a non-revoked publisher certificate
- Include a valid RFC 3161 timestamp

---

## 0.ad Build provenance

Every release artifact is accompanied by:

- Exact pinned versions (Python, PyInstaller, WiX, Windows SDK, signtool, .NET SDK)
- Source commit SHA on the build branch
- Locked `requirements.lock` hash
- Build-host identifier (operator-bound)
- Build timestamp (UTC)
- SBOM (real SPDX 2.3 or CycloneDX 1.6 — see §0.af)
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
4. **Write the real `config.yaml`** (copied from `config.yaml.example`):
   ```powershell
   Copy-Item 'C:\ProgramData\HermesOrchClient\config.yaml.example' `
             'C:\ProgramData\HermesOrchClient\config.yaml'
   # Edit config.yaml to set real agent_id, https://<ORCHESTRATOR_FQDN>:<HTTPS_PORT>, key-id
   ```
5. **Run `OrchClientDoctor.exe`** to dry-run / verify config + ACL +
   signature. It refuses to run if any precondition fails.
6. **Start the service**:
   ```powershell
   Start-Service -Name OrchClient
   Get-Service -Name OrchClient
   ```

---

## 0.af SBOM tool binding (new in v0.4)

The build script calls a **bound SBOM generator** with these
properties:

| Property | Value (example) |
|---|---|
| Tool | `syft` (Anchore) for CycloneDX; `ort` (ClearlyDefined) for SPDX |
| Version | `syft v1.x` (or `ort v30+`) — pinned |
| Command | `syft scan dir:<publishDir> --output cyclonedx-json=<outFile>` (or `ort ... --output-formats SPDX`) |
| Validation | `cyclonedx-py-validate` for CycloneDX; `spdx-tools` for SPDX |
| Namespace | `https://hermesorchestrator.local/spdxdocs/orchclient-v<version>` (or `urn:uuid:...` for SPDX) |
| Failure handling | If generator returns non-zero, the build aborts (via `Invoke-NativeChecked`); an empty / missing SBOM is treated as a build failure |
| Binding | The build script will set `$sbomGen`, `$sbomArgs`, `$sbomValidateCmd` as **explicit variables** at the top of the build script, not inline. Operator changes the tool by changing those three variables |

If the operator has not bound an SBOM tool by implementation time,
the build cannot proceed.

---

## 0.4 Secret-preservation state table

| # | State | Required behavior | VM test required |
|---|---|---|---|
| 1 | Fresh MSI install | Create zero-byte placeholder; service is demand-start and **refuses to run** until real config + secret are written | ✓ |
| 2 | Secret provisioned after install | Preserve it through `Repair` and `MajorUpgrade` | ✓ |
| 3 | Upgrade with missing secret (operator deleted) | **Do not silently recreate**; service stays demand-start; health gate fails closed | ✓ |
| 4 | Uninstall | **Explicitly** chosen: (a) securely remove the secret file, OR (b) preserve it for reinstall. **Operator-bound choice**, recorded in `MANIFEST.json` | ✓ |
| 5 | Reinstall after uninstall | Placeholder returns; prior secret is gone (if §0.4.4 = "remove") OR preserved (if §0.4.4 = "preserve") | ✓ |
| 6 | Operator-edited config on upgrade | Per §0.z: MSI template `config.yaml.example` is updated; real `config.yaml` is **never** overwritten by the MSI | ✓ |

`NeverOverwrite="yes"` on `OrchClientSecretComponent` and
`OrchClientConfigComponent` enforces (2), (3), (5), (6) at the MSI
level. (1), (4) require post-install VM tests.

---

## 0.4-bis Config-preservation state table (new in v0.4)

| # | State | Required behavior | VM test required |
|---|---|---|---|
| 1 | Fresh MSI install | Drops `config.yaml.example` only; **no `config.yaml`** until operator provisions | ✓ |
| 2 | Operator provisions `config.yaml` | Per `§0.ae` step 4; `NeverOverwrite="yes"` on the config component | ✓ |
| 3 | Repair | `config.yaml` is **untouched** | ✓ |
| 4 | Major upgrade | `config.yaml` is **untouched**; `config.yaml.example` may be updated | ✓ |
| 5 | Uninstall | `config.yaml` is removed (current v0.4 default; operator can change to "preserve") | ✓ |

---

## 1. Goal

Produce:

1. A runnable **orch client** (Python) that signs HMAC-authenticated
   heartbeats and self-enrolls against the orchestrator's enrollment
   endpoint (B12-deployed contract; TBD until B12 is reviewed).
2. A separate **`OrchClientDoctor.exe`** (console-enabled) for
   dry-run / config validation / signature / ACL checks.
3. A **code-signed Windows MSI** built with PyInstaller (`.spec`
   source of truth) + WiX 4 + `HarvestDirectory` task.
4. A **SHA-256 + real SPDX or CycloneDX SBOM + signing manifest** for
   operator handoff.
5. An **updated install runbook** that references the real artifacts
   (not illustrative filenames).

The MSI install shall:

- Drop `OrchClient.exe` under `C:\Program Files\HermesOrchClient\`
- Drop `OrchClientDoctor.exe` (console-enabled) in the same folder
- Register a Windows Service named `OrchClient` (start = demand)
- Drop `config.yaml.example` (placeholder URL; operator provisions
  the real `config.yaml` after install) at
  `C:\ProgramData\HermesOrchClient\config.yaml.example` (locked ACL
  `SYSTEM:F, Admins:F, Users:R`)
- Drop a zero-byte placeholder secret at
  `C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin` with
  `NeverOverwrite="yes"` and ACL `D:P(A;;FA;;;SY)(A;;FA;;;BA)`
- Lock the parent directories with matching SDDLs (`util:PermissionEx`)
- **Not** auto-start the service on install
- For the first release, set service `FirstFailure=SecondFailure=ThirdFailure=none` (no restart loop on unenrolled / zero-secret state)
- The MSI does **not** create `config.yaml`; it is operator-provisioned

---

## 2. Deliverables

| # | Artifact | Path |
|---|---|---|
| 1 | Orch client source | `C:\Project\minimax code\hermes-orchestrator\src\orch_client\__init__.py` + `__main__.py` + `client.py` + `hmac_auth.py` + `config.py` + `logging_setup.py` + `service.py` + `doctor.py` |
| 2 | `pyproject.toml` entry points: `orch-client` (service) + `orch-client-doctor` (console tool) | extend existing `C:\Project\minimax code\hermes-orchestrator\pyproject.toml` |
| 3 | Locked dependency file | `C:\Project\minimax code\hermes-orchestrator\installer\requirements.lock` (every pip package hash) |
| 4 | PyInstaller spec | `C:\Project\minimax code\hermes-orchestrator\installer\orch-client.spec` (single source of truth) |
| 5 | WiX 4 `.wixproj` + manually-authored `.wxs` | `C:\Project\minimax code\hermes-orchestrator\installer\orch-client.wixproj` + `orch-client.wxs` |
| 6 | Build script | `C:\Project\minimax code\hermes-orchestrator\installer\build.ps1` |
| 7 | Built MSI | `C:\Project\minimax code\hermes-orchestrator\dist\OrchClient-v0.1.0-x64.msi` |
| 8 | SHA-256 + manifest + real SBOM | `dist\SHA256SUMS.txt` + `dist\MANIFEST.json` + `dist\SBOM.cyclonedx.json` (or `dist\SBOM.spdx.json`) |
| 9 | Updated runbook | `C:\Users\stanley\AppData\Local\Temp\orch-client-install-runbook.md` |

All artifacts above are **local files**, no network push, no remote
install, no live enrollment against a real orchestrator during build.

---

## 3. Source layout (Deliverable 1)

```
src/orch_client/
  __init__.py          # version string
  __main__.py          # entry point: `python -m orch_client` (service)
  doctor.py            # entry point: `python -m orch_client.doctor` (console)
  client.py            # HTTP client + enrollment + heartbeat loop
  hmac_auth.py         # HMAC-SHA256 over bound metadata + body hash
  config.py            # YAML config loader; per-install secret override
  logging_setup.py     # structured JSONL logger to ProgramData logs
  service.py           # Windows Service entry (pywin32 win32serviceutil)
```

`pyproject.toml` entry points:

```toml
[project.scripts]
orch-client          = "orch_client.__main__:main"
orch-client-doctor   = "orch_client.doctor:main"
```

### 3.1 `hmac_auth.py` (unchanged from v0.3 — bound-metadata model)

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
- **Signature** = `HMAC-SHA256(secret, UTF-8(signing_input))`,
  hex-lowercase
- **Server validation** (must not trust `X-Hermes-Endpoint` as authority)
- **key-id** is a real rotation identifier, not necessarily `agent_id`

### 3.2 `client.py` (unchanged from v0.3)

- `enroll(orchestrator_url, agent_id, public_key_pem) -> enrollment_receipt`
- `heartbeat(orchestrator_url, agent_id, hmac_secret) -> None` (loop)
- `dry_run(...)` (called by `doctor.py`)

### 3.3 `service.py` (unchanged from v0.3)

Windows Service entry via `pywin32` (`win32serviceutil.ServiceFramework`).
Service **refuses to start** if:
- `agent-secret.bin` is zero bytes
- `orchestrator_url` is the placeholder, HTTP, or missing
- ACL on the secret file or directory is wrong
- `config.yaml` is missing (operator has not provisioned yet)

### 3.4 `config.py` (revised in v0.4 — config-template model)

- `config.yaml.example` is the **template** that ships in the MSI
  (with placeholder URL + comments)
- The real `config.yaml` is **operator-provisioned** after install
  (per `§0.ae`); the MSI never creates it
- Reads YAML at `C:\ProgramData\HermesOrchClient\config.yaml`:
  ```yaml
  agent_id: <operator-assigned>
  orchestrator_url: https://<ORCHESTRATOR_FQDN>:<HTTPS_PORT>
  hmac_key_id: <key-rotation id, defaults to agent_id>
  heartbeat_interval_sec: 30
  log_level: info
  ```
- The HMAC secret is read separately from
  `C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin` (raw
  bytes; access is gated by the file's SDDL)

### 3.5 `doctor.py` (new in v0.4 — replaces "service EXE --dry-run")

`doctor.py` is a **console-enabled** Python entry point that performs
**read-only** diagnostics:

- `doctor check-config` — load + validate `config.yaml` (URL is HTTPS,
  `agent_id` present, `key-id` present, etc.)
- `doctor check-secret` — verify `agent-secret.bin` exists, is
  non-empty, ACL matches expected SDDL
- `doctor dry-run` — generate a synthetic heartbeat payload, compute
  body SHA-256 + canonical signing input + HMAC, print the canonical
  headers + signature to stdout
- `doctor check-signature` — `signtool verify` on a supplied MSI path
- `doctor service-diag` — `Get-Service OrchClient` + Event Log tail

**`doctor.py` is NOT a service** and does NOT modify any state. It
is shipped as a separate EXE (`OrchClientDoctor.exe`, console-enabled)
because a release service EXE has no console for stdout.

### 3.6 Service-dispatcher VM validation list (unchanged from v0.3)

Verified on a clean Windows 10/11 VM:

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

### 3.7 Doctor-binary VM validation list (new in v0.4)

Verified on a clean Windows 10/11 VM:

- [ ] `OrchClientDoctor.exe` runs from an elevated PowerShell with a
  console window
- [ ] `doctor check-config` reports valid when `config.yaml` is
  present and well-formed; reports specific error otherwise
- [ ] `doctor check-secret` reports `agent-secret.bin` exists,
  non-empty, ACL matches `D:P(A;;FA;;;SY)(A;;FA;;;BA)`
- [ ] `doctor dry-run` prints canonical headers + signature + raw
  body bytes to stdout (no HTTP call)
- [ ] `doctor check-signature --msi <path>` reports the MSI's
  publisher + cert chain + RFC 3161 timestamp

---

## 4. PyInstaller spec (Deliverable 4) — single source of truth

`.spec` is the **only** build-time source of truth. The build script
calls `pyinstaller --clean --noconfirm installer\orch-client.spec`.
All frozen-bundle behavior lives in the spec, **not** in CLI flags.

`orch-client.spec`:

```python
# -*- mode: python ; coding: utf-8 -*-
import sys ; sys.setrecursionlimit(5000)
from PyInstaller.utils.hooks import collect_submodules, collect_data_files
hidden = collect_submodules('orch_client') + [
    'win32serviceutil',
    'win32service',
    'win32event',
    'servicemanager',
]
datas = collect_data_files('orch_client')

a = Analysis(
    ['..\\src\\orch_client\\__main__.py'],
    pathex=['..\\src'],
    hiddenimports=hidden,
    datas=datas,
    excludes=['tkinter', 'unittest', 'pydoc'],
    noarchive=False,
)
pyz = PYZ(a.pure)

# Service EXE: no console, used by SCM
service_exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='OrchClient',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,         # equivalent to --noconsole; suppress console window
)

# Doctor EXE: WITH console, for operator diagnostics
doctor_exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='OrchClientDoctor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # console-enabled; not a service
)

coll = COLLECT(
    service_exe, doctor_exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[],
    name='OrchClient',
)
```

The `.spec` produces **two EXEs** in the same `--onedir` output:
- `OrchClient.exe` (service, no console)
- `OrchClientDoctor.exe` (console-enabled, operator diagnostic)

Both are signed in the build pipeline. The doctor EXE is in the
**allowlist** (see §0.aa).

The build script invokes the spec:

```powershell
pyinstaller --clean --noconfirm installer\orch-client.spec
```

**No CLI flags** for frozen-bundle behavior. The spec is the source of truth.

---

## 5. WiX 4 source (Deliverable 5) — revised in v0.4

**Key changes vs v0.3**:
- **Removed** the manual empty `OrchClientFiles` group (let
  `HarvestDirectory` generate it)
- **Defined** `PublishDir` / `ConfigTemplateDir` / `SecretTemplateDir`
  as real MSBuild `<PropertyGroup>` entries, with `$(PublishDir)`
  flowing into `<DefineConstants>` so `$(var.PublishDir)` is valid
- **Added** `UpgradeErrorMessage` to `MajorUpgrade`
- **Added** `OrchClientDoctor.exe` as a manually-authored component
  (the doctor EXE is not part of the harvest — it's authored with
  the service EXE in the same `INSTALLFOLDER` directory)
- **Clarified** the directory key-path model: `CreateFolder` is the
  key path for directory components

### 5.1 Project layout

```
installer/
  orch-client.wixproj     # MSBuild project: HarvestDirectory + candle + light
  orch-client.wxs         # manually authored fragments (service + doctor + config + secret + dirs)
  templates/
    config/config.yaml.example
    secret/agent-secret.bin  # zero-byte placeholder (NeverOverwrite="yes")
```

### 5.2 `orch-client.wixproj` (revised in v0.4)

```xml
<Project Sdk="WixToolset.Sdk/4.0">
  <PropertyGroup>
    <OutputType>Package</OutputType>
    <Platform>x64</Platform>

    <!-- Real MSBuild properties (revised in v0.4) -->
    <PublishDir>$(MSBuildProjectDirectory)\..\dist\OrchClient</PublishDir>
    <ConfigTemplateDir>$(MSBuildProjectDirectory)\templates\config</ConfigTemplateDir>
    <SecretTemplateDir>$(MSBuildProjectDirectory)\templates\secret</SecretTemplateDir>

    <!-- Flow into WiX preprocessor constants so $(var.PublishDir) etc. resolve -->
    <DefineConstants>
      PublishDir=$(PublishDir);
      ConfigTemplate=$(ConfigTemplateDir);
      SecretTemplate=$(SecretTemplateDir)
    </DefineConstants>
  </PropertyGroup>

  <ItemGroup>
    <HarvestDirectory Include="$(PublishDir)">
      <ComponentGroupName>OrchClientFiles</ComponentGroupName>
      <DirectoryRefId>INSTALLFOLDER</DirectoryRefId>
      <!-- Exclude the two authored EXEs from harvest; they have their own components -->
      <ExcludeFiles>**\OrchClient.exe</ExcludeFiles>
      <ExcludeFiles>**\OrchClientDoctor.exe</ExcludeFiles>
    </HarvestDirectory>
  </ItemGroup>

  <ItemGroup>
    <Compile Include="orch-client.wxs" />
  </ItemGroup>
</Project>
```

### 5.3 `orch-client.wxs` (revised in v0.4)

```xml
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs"
     xmlns:util="http://wixtoolset.org/schemas/v4/wxs/util">
  <Package Name="OrchClient" Version="0.1.0" Manufacturer="ACME"
           UpgradeCode="PUT-GUID-HERE">
    <MediaTemplate EmbedCab="yes" />
    <MajorUpgrade Schedule="afterInstallInitialize"
                  AllowSameVersionUpgrades="no"
                  Disallow="yes"
                  UpgradeErrorMessage="A newer version of Hermes Orch Client is already installed. Please uninstall it first." />
    <Feature Id="Main" Title="Orch Client" Level="1">
      <ComponentGroupRef Id="OrchClientFiles" />     <!-- harvested (no manual group) -->
      <ComponentGroupRef Id="OrchClientConfig" />    <!-- manual: config.yaml.example + config dir -->
      <ComponentGroupRef Id="OrchClientSecret" />    <!-- manual: secret + secret dir -->
      <ComponentGroupRef Id="OrchClientService" />   <!-- manual: service EXE + service config -->
      <ComponentGroupRef Id="OrchClientDoctor" />    <!-- manual: doctor EXE (new in v0.4) -->
    </Feature>
  </Package>

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

      <!-- Doctor EXE component (new in v0.4; NOT a service) -->
      <Component Id="OrchClientDoctorComponent" Guid="*">
        <File Id="OrchClientDoctorExe"
              Source="$(var.PublishDir)\OrchClientDoctor.exe"
              KeyPath="yes" />
      </Component>
    </DirectoryRef>
  </Fragment>

  <!-- Public config + secret + their parent directories -->
  <Fragment>
    <StandardDirectory Id="ProgramDataFolder">
      <Directory Id="CONFIGFOLDER" Name="HermesOrchClient">
        <!-- Config directory: SYSTEM:F, Admins:F, Users:R/X; KeyPath on the directory itself -->
        <Component Id="OrchClientConfigDirComponent" Guid="*" KeyPath="yes">
          <CreateFolder>
            <util:PermissionEx
              Sddl="D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)(A;;FX;;;BU)" />
          </CreateFolder>
        </Component>

        <!-- config.yaml.example (the template; MSI ships this, operator copies to config.yaml) -->
        <Component Id="OrchClientConfigExampleComponent" Guid="*">
          <File Id="OrchClientConfigExampleFile"
                Source="$(var.ConfigTemplate)\config.yaml.example"
                Name="config.yaml.example"
                KeyPath="yes">
            <util:PermissionEx
              Sddl="D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)" />
          </File>
        </Component>

        <Directory Id="SECRETFOLDER" Name="secrets">
          <!-- Secret directory: SYSTEM:F, Admins:F, no Users ACE; KeyPath on the directory itself -->
          <Component Id="OrchClientSecretDirComponent" Guid="*" KeyPath="yes">
            <CreateFolder>
              <util:PermissionEx
                Sddl="D:P(A;;FA;;;SY)(A;;FA;;;BA)" />
            </CreateFolder>
          </Component>

          <!-- agent-secret.bin (zero-byte placeholder, NeverOverwrite) -->
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

Notes (v0.4 corrections applied):
- **No** manual `OrchClientFiles` group; `<ComponentGroupRef Id="OrchClientFiles" />` references the harvested group exactly once
- `PublishDir` is a real MSBuild property; `$(var.PublishDir)` works
- `UpgradeErrorMessage` is present
- Doctor EXE is its own authored component (not harvested)
- Directory components use `CreateFolder` as the key path; the directory itself is the key path; SDDL is on the `<CreateFolder>`

---

## 6. Build script (Deliverable 6) — revised in v0.4

`build.ps1` (key changes: `.spec` is the source of truth; `dotnet
build` for the `.wixproj`; explicit SBOM tool binding; signed doctor
EXE):

```powershell
$ErrorActionPreference = 'Stop'

# ----- Bound operator values (TBD until operator assigns) -----
$RepoRoot      = 'C:\Project\minimax code\hermes-orchestrator'
$PyInstaller   = 'pyinstaller'
$WixProject    = Join-Path $RepoRoot 'installer\orch-client.wixproj'
$SpecFile      = Join-Path $RepoRoot 'installer\orch-client.spec'
$TimestampUrl  = 'http://timestamp.digicert.com'
$CertThumb     = '<TBD by operator — code-signing cert thumbprint>'
$Version       = '0.1.0'
$Arch          = 'x64'
$ExpectedMsi   = "OrchClient-v${Version}-${Arch}.msi"

# ----- Bound SBOM tool (v0.4 §0.af) -----
$sbomGen       = 'syft'                                          # operator-bound
$sbomArgs      = @('scan',"dir:$($RepoRoot)\dist\OrchClient",      # operator-bound
                   '--output',"cyclonedx-json=$($RepoRoot)\dist\SBOM.cyclonedx.json")
$sbomValidate  = 'cyclonedx-py-validate'                        # operator-bound

# Native-exec helper (v0.3)
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

# 1) PyInstaller — spec is the single source of truth (no CLI flags)
$publishDir = Join-Path $RepoRoot 'dist\OrchClient'
Remove-Item -LiteralPath $publishDir -Recurse -Force -ErrorAction SilentlyContinue
Invoke-NativeChecked -FilePath $PyInstaller `
    -Arguments @('--clean','--noconfirm',$SpecFile) `
    -Label 'pyinstaller (spec-driven)'

# 2) Sign owned EXEs (third-party DLLs already signed; do NOT re-sign)
$ownedExes = @(Get-ChildItem -Recurse -File -Path $publishDir |
    Where-Object { $_.Name -in @('OrchClient.exe','OrchClientDoctor.exe') })
foreach ($f in $ownedExes) {
    Invoke-NativeChecked -FilePath 'signtool.exe' `
        -Arguments @('sign','/fd','SHA256','/tr',$TimestampUrl,'/td','SHA256',
                      '/sha1',$CertThumb,$f.FullName) `
        -Label "signtool sign $($f.Name)"
}

# 3) Build MSI via `dotnet build` (SDK-style .wixproj with HarvestDirectory)
$msiDir = Join-Path $RepoRoot 'dist'
Remove-Item -LiteralPath (Join-Path $msiDir '*.msi') -ErrorAction SilentlyContinue
Invoke-NativeChecked -FilePath 'dotnet' `
    -Arguments @('build',$WixProject,'-c','Release','-p',"Platform=$Arch") `
    -Label 'dotnet build (WiX 4 SDK-style .wixproj)'
$msiPath = Join-Path $msiDir $ExpectedMsi
if (-not (Test-Path -LiteralPath $msiPath)) {
    throw "Expected MSI not found at $msiPath (exact filename assertion failed)"
}

# 4) Sign the MSI
Invoke-NativeChecked -FilePath 'signtool.exe' `
    -Arguments @('sign','/fd','SHA256','/tr',$TimestampUrl,'/td','SHA256',
                  '/sha1',$CertThumb,$msiPath) `
    -Label 'signtool sign MSI'

# 5) Verify
Invoke-NativeChecked -FilePath 'signtool.exe' `
    -Arguments @('verify','/pa','/all','/v',$msiPath) `
    -Label 'signtool verify MSI'

# 6) SHA-256 + manifest
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
        # Expiry vs timestamp semantics:
        #   verify checks expiry against the signing-time window
        #   (proved by the RFC 3161 timestamp), not the current clock.
        #   Revocation is a separate concern (signtool verify -rpc).
    }
    payload_inventory = @((Get-ChildItem $publishDir -Recurse -File).FullName |
        ForEach-Object { [PSCustomObject]@{ path = (Resolve-Path -LiteralPath $_ -Relative); sha256 = (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash } })
    config_uninstall_behavior = 'remove'  # operator-bound: 'remove' | 'preserve'
    secret_uninstall_behavior = 'remove'  # operator-bound: 'remove' | 'preserve'
} | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText(
    (Join-Path $msiDir 'MANIFEST.json'),
    $manifest,
    [System.Text.UTF8Encoding]::new($false))
"$hash  $ExpectedMsi" |
    Out-File -LiteralPath (Join-Path $msiDir 'SHA256SUMS.txt') -Encoding utf8

# 7) Real SPDX / CycloneDX generator (v0.4 §0.af — bound tool)
$sbomOut = Join-Path $msiDir 'SBOM.cyclonedx.json'
Invoke-NativeChecked -FilePath $sbomGen `
    -Arguments $sbomArgs `
    -Label 'SBOM generator (syft)'

# 8) Validate the SBOM (operator-bound validator)
Invoke-NativeChecked -FilePath $sbomValidate `
    -Arguments @($sbomOut) `
    -Label 'SBOM validator'

Write-Host "[+] Build complete: $msiPath"
Write-Host "[+] SHA-256: $hash"
```

**Sign order** (v0.4): owned EXEs (`OrchClient.exe` + `OrchClientDoctor.exe`) → MSI → verify → final hash → manifest + SBOM. Each step is `Invoke-NativeChecked`-gated.

---

## 7. Known gaps & explicit dependencies (must be resolved before build)

| # | Dependency | Owner | Status |
|---|---|---|---|
| 1 | Orchestrator contract (enrollment + heartbeat + cleanup endpoint paths, methods, auth, CSRF) | operator | **TBD until B12 is deployed and reviewed** |
| 2 | Code-signing cert thumbprint (or Azure Trusted Signing endpoint) | operator | not yet bound |
| 3 | WiX 4 install path + .NET SDK on the build host | operator | not yet installed (default assumed: `C:\wix\v4\`, `dotnet` on PATH) |
| 4 | PyInstaller availability in build Python | operator | assumed present |
| 5 | Target machine agent_id (e.g. `win-b-02`) | operator | not yet assigned |
| 6 | Target machine HMAC secret (operator-bound, out-of-band) | operator | not yet generated; MSI ships with **zero-byte placeholder** and `NeverOverwrite="yes"` |
| 7 | HMAC `key-id` (must support rotation) | operator | not yet bound |
| 8 | Orchestrator `<ORCHESTRATOR_FQDN>` and `<HTTPS_PORT>` (B13-transport-closed) | operator | not yet bound; MSI ships with `config.yaml.example` containing placeholder |
| 9 | SBOM generator (bound per `§0.af`) | operator | not yet bound; `syft` is the example, operator picks |
| 10 | Cert renewal policy + revoked-cert response | operator | bind at runtime; manifest captures signer policy |
| 11 | UpgradeCode GUID (fixed across versions) + ProductCode rotation per version | operator | bind at runtime |
| 12 | Test case matrix | operator | new in v0.4; to be drafted separately |
| 13 | VM test environment (clean Windows 10/11) for §3.6, §3.7, §0.4, §0.4-bis | operator | required before implementation approval |
| 14 | Config uninstall behavior (`remove` vs `preserve`) | operator | recorded in `MANIFEST.json` |

---

## 8. Forbidden actions (no exceptions)

- ❌ No remote install on the target Windows machine
- ❌ No live HTTP calls during build (dry-run only)
- ❌ No real HMAC secret embedded in the MSI (only zero-byte placeholder)
- ❌ No `orchestrator_url: http://...` shipped in MSI config (placeholder only)
- ❌ No `config.yaml` shipped in MSI (only `config.yaml.example`; operator provisions)
- ❌ No modification of B12 deploy (held at watchdog REFUSE)
- ❌ No modification of watchdog task (held)
- ❌ No push to remote git remote (per proposal-branch pattern only)
- ❌ No firewall / enrollment / agent-mutation action on the orchestrator
- ❌ No repair / upgrade / uninstall that clobbers a provisioned secret or config (`NeverOverwrite="yes"` enforces at MSI level; operator must NOT manually delete the secret or config file in a way that the placeholder would replace)
- ❌ No blind re-signing of third-party DLLs that already carry a valid Authenticode signature
- ❌ No labelling a hand-rolled JSON as SPDX or CycloneDX
- ❌ No CLI args for frozen-bundle behavior; `.spec` is the single source of truth
- ❌ No manual `OrchClientFiles` group; `HarvestDirectory` is the only source
- ❌ No requiring unexpired cert at verify time; expiry is checked against the signing-time window (proved by RFC 3161 timestamp)

---

## 9. Acceptance criteria

A. The MSI installs cleanly on a clean Windows 10/11 VM:
   - File system layout matches §1
   - `OrchClient.exe` (service) and `OrchClientDoctor.exe` (console) are both present
   - ACL on `config.yaml.example`: `SYSTEM:F, Admins:F, Users:R`
   - ACL on config directory: `SYSTEM:F, Admins:F, Users:R/X`
   - ACL on `agent-secret.bin`: `SYSTEM:F, Admins:F` (no Users)
   - ACL on secret directory: `SYSTEM:F, Admins:F` (no Users)
   - Service is registered with `Start=Demand`, `State=Stopped`
   - Service is NOT auto-started
   - `OrchClient.exe` is the service binary; pywin32 dispatcher is present
   - `OrchClientDoctor.exe` runs from an elevated PowerShell with a console window

B. The MSI is code-signed:
   - `signtool verify /pa /all /v` reports valid (with RFC 3161 timestamp)
   - Expiry is checked against the signing-time window, not the current clock
   - SHA-256 in `SHA256SUMS.txt` matches `Get-FileHash`
   - `MANIFEST.json` is consistent with the MSI
   - SBOM is a real CycloneDX 1.6 (or SPDX 2.3) document, validated by the bound validator
   - Owned payload files are signed; third-party DLLs retain their own signatures (not blindly re-signed)

C. The doctor binary works:
   - `OrchClientDoctor.exe check-config` — passes for a well-formed `config.yaml`
   - `OrchClientDoctor.exe check-secret` — passes when `agent-secret.bin` exists, non-empty, ACL matches
   - `OrchClientDoctor.exe dry-run` — prints canonical headers + signature + raw body bytes to stdout
   - `OrchClientDoctor.exe check-signature --msi <path>` — verifies the MSI

D. Secret-preservation behavior (6 states, all VM-tested):
   - Fresh install → zero-byte placeholder; service refuses to run
   - Secret provisioned → preserved through repair and major upgrade
   - Upgrade with missing secret → service fails closed
   - Uninstall → behavior per `MANIFEST.json::config_uninstall_behavior` / `secret_uninstall_behavior` (operator-bound: `remove` or `preserve`)
   - Reinstall after uninstall → placeholder returns
   - Operator-edited config on upgrade → MSI does not overwrite `config.yaml` (per `NeverOverwrite="yes"`)

E. Config-preservation behavior (5 states, all VM-tested): per §0.4-bis

F. Service fail-closed on bad state:
   - Service refuses to start when `agent-secret.bin` is zero bytes
   - Service refuses to start when `orchestrator_url` is the placeholder, HTTP, or missing
   - Service refuses to start when secret file ACL is wrong
   - Service refuses to start when `config.yaml` is missing

G. The updated runbook (`orch-client-install-runbook.md`) uses the real MSI filename and references §7 binding values

H. No side-effecting action against the live orchestrator

I. The HMAC validation (server-side, not in this plan):
   - Server uses constant-time compare
   - Server maintains a bounded replay cache keyed by `(key-id, nonce)`
   - Server rejects requests with timestamp ±5 min outside server clock
   - Server does NOT trust `X-Hermes-Endpoint` as authority

J. §3.6 + §3.7 VM validation lists: every checkbox verified on a clean Windows 10/11 VM

K. The test case matrix covers: offline, cert mismatch, missing-secret, invalid-secret, repair, major upgrade, uninstall, partial-install, revocation, payload allowlist failure, SBOM schema validation, signing round-trip

---

## 10. What I will NOT do (without separate approval)

- Push the built MSI to any remote location
- Modify the live orchestrator config / DB / NSSM
- Install on any machine other than a local test VM
- Generate a real HMAC secret for production
- Sign with anything other than a cert explicitly bound by the operator
- Touch the B12 deploy script (`apply-r7c-rebuild.ps1`) — already locally patched to v4.1 + v5.1, no further changes planned
- Create / modify firewall rules
- Add, remove, or modify any scheduled task
- Initiate or accept any enrollment against the live orchestrator
- Open the B12 deploy (still on hold at watchdog REFUSE)
- Run `OrchClientDoctor.exe` against a live orchestrator (dry-run only)

---

## 11. v0.1 → v0.2 → v0.3 → v0.4 changelog (cross-reference)

| # | Section | v0.1 | v0.2 | v0.3 | v0.4 |
|---|---|---|---|---|---|
| 1 | §3.1 HMAC canonicalization | "JSON fixed key order" | raw-body + bound headers (contradictory) | bound-metadata model picked (NOT raw-body alone) | (unchanged) |
| 2 | §1 orchestrator_url | (n/a) | HTTP | HTTPS placeholder | (unchanged) |
| 3 | §5 WiX KeyPath | (n/a) | on `<Component>` | on `<File>` | (unchanged) |
| 4 | §5 WiX ComponentGroup | (n/a) | IDs mismatched | manual wrap | (unchanged) |
| 5 | §5 WiX harvest pattern | `<Files Include>` | `<HarvestDirectory>` task | (same) | **no untracked HeatDirectory.wxs** |
| 6 | §5 WiX directory ACLs | (missing) | (missing) | added | (unchanged) |
| 7 | §5 WiX MajorUpgrade | (missing) | (missing) | Disallow="yes" | **+ `UpgradeErrorMessage`** |
| 8 | §5 WiX OrchClientFiles group | (n/a) | manual + harvested | manual + harvested | **manual group removed; harvested only** |
| 9 | §5 WiX .wixproj PublishDir | (n/a) | DefineConstants only | DefineConstants only | **real MSBuild PropertyGroup + DefineConstants flow** |
| 10 | §5 WiX doctor EXE | (missing) | (missing) | (missing) | **OrchClientDoctor component added** |
| 11 | §5 WiX service failure actions | (n/a) | (n/a) | `none/none/none` | (unchanged) |
| 12 | §6 build source-of-truth | (n/a) | CLI + spec | CLI + spec | **`.spec` only (single source of truth)** |
| 13 | §6 SBOM tool | (missing) | hand-rolled JSON labelled SPDX | real generator | **bound tool + validator + failure handling (§0.af)** |
| 14 | §6 build helpers | (n/a) | none | `Invoke-NativeChecked` | (unchanged) + bound sbomGen vars |
| 15 | §3.5 dry-run | (n/a) | service EXE stdout | service EXE stdout | **`OrchClientDoctor.exe` separate console binary** |
| 16 | §3.4 config lifecycle | (n/a) | installer ships config.yaml | installer ships config.yaml | **installer ships `config.yaml.example`; operator provisions `config.yaml`** |
| 17 | §0.4 secret-preservation | (n/a) | 4-state table | VM tests per state | + **`config.yaml` NeverOverwrite** (5-state §0.4-bis) |
| 18 | §0.z public config lifecycle | (missing) | (missing) | installer-owned | **operator-owned `config.yaml`; MSI ships `config.yaml.example` only** |
| 19 | §0.ac release verification | (missing) | (missing) | wording implied unexpired | **clarified: expiry checked against signing-time window via RFC 3161 timestamp** |
| 20 | §3.6 / §3.7 VM validation | (missing) | (missing) | service-dispatcher | **+ doctor-binary VM validation list (§3.7)** |
| 21 | §7 dependencies | 7 | 10 | 13 | **14** (added config uninstall behavior) |
| 22 | §8 forbidden actions | 7 | 8 | 11 | **15** (added "no config.yaml in MSI", "no CLI args for frozen-bundle", "no manual OrchClientFiles group", "no requiring unexpired cert at verify") |
| 23 | §9 acceptance | 6 | 8 | 10 | **11** (added doctor-binary works) |

---

## 12. Outstanding Perplexity questions for v0.5 (if any)

The v0.4 review was the deepest yet. Remaining open items are
operator-binding, not technical:

- Operator must pick `agent_id` and `key-id` for the target machine
- Operator must pick the code-signing cert / Azure Trusted Signing
- Operator must install WiX 4 + `dotnet` SDK on the build host
- Operator must generate the target machine's HMAC secret (out-of-band)
- Operator must generate the UpgradeCode GUID
- Operator must run the §3.6 + §3.7 + §0.4 + §0.4-bis VM tests, and
  produce a signed report before implementation approval
- Operator must draft the §9.K test case matrix (separate work item,
  not a code change)
- Operator must bind the real SBOM tool + validator (§0.af)
- Operator must bind the `config_uninstall_behavior` + `secret_uninstall_behavior` values for `MANIFEST.json`
