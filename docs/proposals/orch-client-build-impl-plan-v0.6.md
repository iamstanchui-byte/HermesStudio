# Orch Client Build — Implementation Plan v0.6 (for Perplexity review)

**Date:** 2026-08-13
**Status:** PROPOSAL for review (Perplexity + operator) — v0.6
**Supersedes:** v0.5 (commit `d12e430` on branch `proposal/orch-client-build-impl-plan-v0.1`)
**Scope:** end-to-end build of a Windows MSI installer for the new orch
client, on the orchestrator host (Windows A). The MSI will be carried
to a separate target Windows machine for installation; this plan does
NOT include remote install, agent enrollment against a live orchestrator,
or B12 deploy. Enrollment is verified separately after the operator
manually runs the install on the target.

---

## 0. v0.5 → v0.6 changelog (Perplexity review)

Perplexity re-read PR #4 after the v0.5 commit and reported: most of
v0.4's blockers were resolved in design, but 3 implementation
blockers + 4 smaller issues remained. v0.6 addresses all 7.

| # | v0.5 said | v0.6 says | Reason |
|---|---|---|---|
| 1 | `COLLECT(service_exe, doctor_exe, service_a.binaries, service_a.zipfiles, service_a.datas, doctor_a.binaries, doctor_a.zipfiles, doctor_a.datas, ...)` listed two complete analysis trees in a single `COLLECT`. Both analyses bundle the same `python314.dll` / pywin32 / stdlib / shared modules, which risks **duplicate destination-name collisions or accidental ownership ambiguity** in the frozen output | **`Option B`** is selected: build `OrchClient` and `OrchClientDoctor` **independently** into two temporary `--onedir` outputs, then **construct a deterministic release directory** through a deduplication + allowlist stage. A **build-time assertion** confirms exactly one copy of each shared runtime DLL appears in the final release directory | PyInstaller's multi-executable guidance does not assume two complete analyses are automatically mergeable. Option B is the safest pattern: each `Analysis` is internally consistent, the release directory is built from a reviewed merge step, and the assertion prevents future regressions |
| 2 | `NeverOverwrite="yes"` on the secret file was described as enforcing "PRESERVE on uninstall" | **`NeverOverwrite="yes"` only protects against replacement during install / repair / upgrade; it does NOT guarantee preservation at uninstall.** v0.6 explicitly states the implementation: the secret file is **never** auto-removed by uninstall, because the component is **not** marked with a `RemoveFile` / `CustomAction` and the `<ComponentGroup>` does **not** include a `RemoveFolder` directive. The `MANIFEST.json` records `secret_preserved_on_uninstall: true` and a VM test asserts this on a clean target | `NeverOverwrite` is a property of the install/repair/upgrade phases. Uninstall preservation is enforced by the **absence** of removal directives in the WiX source. The two are different mechanisms and must not be conflated |
| 3 | `config.yaml.example` was simultaneously described as `NeverOverwrite="yes"` in `§0.z` AND as a "normal MSI-owned file; the next MSI version replaces it" later. The actual WiX component had **no** `NeverOverwrite` attribute, contradicting both claims | **Single explicit policy**: `config.yaml.example` is **MSI-owned and upgradeable**. The next MSI version overwrites it as part of upgrade. Operators **must not** customize `config.yaml.example`; the real `config.yaml` is the sole operator-owned configuration file. The WiX component has no `NeverOverwrite` (consistent with "upgradeable"); the `§0.z` wording is corrected | Mixed policy claims are documentation bugs. The plan now states one clear policy and the WiX snippet matches it |
| 4 | `build.ps1` assigned `$manifest = [ordered]@{...} \| ConvertTo-Json -Depth 6` (string), then tried `$manifest.sbom_filename = ...` (object property assignment on a string), which does **not** produce the intended JSON | `build.ps1` keeps `MANIFEST.json` as a `[ordered]@{}` object throughout, sets every field, then converts to JSON **exactly once** at the end. A final read-back + parse + assert step confirms the SBOM fields are present in the written file | A PowerShell hashtable is an object; a JSON string is a string. Mutating a string with `.Property =` either fails silently or produces malformed output. The plan now uses the correct pattern |
| 5 | `syft v1.18.0` and `cyclonedx-py-validate v0.5.0` were declared as bound, but `build.ps1` did not verify the actual installed executable version | **`build.ps1` has a preflight gate** that runs `syft version` and `cyclonedx-py-validate --version`, parses the version, compares to the bound values, and **fails closed** on mismatch. Bound versions are now exact, not moving ranges | Without a version check, the build silently uses a different version than the manifest claims. The preflight gate is the only way to enforce the version claim |
| 6 | The plan said "An attacker who learns a key-id could use it to sign requests" — incorrect. `key-id` is not secret and by itself does not enable HMAC signing | The `§1.4` rationale now states: **"The mapping prevents a compromised, mis-provisioned, or incorrectly-authorized key from being used to submit a request that claims another agent_id."** The HMAC validation order is unchanged | The previous sentence was a category error. The new sentence correctly describes the threat model |
| 7 | Pinned versions were moving ranges: `Python 3.14.x`, `PyInstaller 6.x latest`, `WiX 4.x latest`, `Windows SDK 10.0.22621.x or newer`, `.NET SDK 6.x LTS or 8.x LTS` | All pinned versions are now **exact, recorded in `MANIFEST.json` and SBOM metadata, with the preflight-gate-enforced tool versions** | A range is not a pin. The preflight gate requires an exact version; the manifest records the exact version; the SBOM records the exact version |

All v0.5 sections preserved where not directly affected. Section
numbering kept stable where possible. New sections added for the
build-time dedup assertion, the preflight version gate, and the
manifest read-back gate.

---

## 0.x Pinned versions (exact, recorded in MANIFEST.json + SBOM)

| Tool | Exact version | Recorded in |
|---|---|---|
| Python | **3.14.0** (operator-pinned; current supported CPython release) | `MANIFEST.json::tooling::python_version` + SBOM `distro` field |
| PyInstaller | **6.16.0** (operator-pinned) | `MANIFEST.json::tooling::pyinstaller_version` + preflight gate |
| WiX Toolset | **4.0.6** (operator-pinned) | `MANIFEST.json::tooling::wix_version` + preflight gate |
| Windows SDK (signtool) | **10.0.22621.4031** (operator-pinned) | `MANIFEST.json::tooling::signtool_version` + preflight gate |
| .NET SDK | **8.0.404** (operator-pinned) | `MANIFEST.json::tooling::dotnet_version` + preflight gate |
| RFC 3161 timestamp URL | per operator binding (default: `http://timestamp.digicert.com`) | `MANIFEST.json::signing::timestamp` |
| Code-signing cert thumbprint | per operator binding | `MANIFEST.json::signing::cert_sha1` |
| SBOM generator | **syft v1.18.0** (operator-pinned) | `MANIFEST.json::sbom_generator_version` + preflight gate |
| SBOM validator | **cyclonedx-py-validate v0.5.0** (operator-pinned) | `MANIFEST.json::sbom_validator_version` + preflight gate |

**Preflight gate** (in `build.ps1`, runs before any build step): for
each tool above, run `<tool> --version` (or the tool's native
version flag), parse the version, compare to the bound value, and
`Invoke-NativeChecked` (or a similar fail-closed check) on mismatch.

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
| `UpgradeCode` | **Fixed** across all versions of OrchClient |
| `ProductCode` | **Rotates per version** |
| `MajorUpgrade` | `Schedule="afterInstallInitialize"` + `AllowSameVersionUpgrades="no"` + `Disallow="yes"` + `UpgradeErrorMessage` |
| Downgrade block | `Disallow="yes"`; older version stays installed |
| Repair behavior | Reinstalls components in the same key path; `NeverOverwrite="yes"` on the **MSI-owned** secret file (prevents placeholder from overwriting a provisioned secret during repair); the **operator-owned** `config.yaml` is not in any MSI component and is therefore never touched by repair |
| Uninstall behavior (v0.6 default) | **PRESERVE** operator-owned files. MSI removes program files, MSI-owned template files, and the service registration only. **Preservation is enforced by the ABSENCE of `RemoveFile` / `RemoveFolder` directives in the WiX source — NOT by `NeverOverwrite`.** A separate, explicit privileged cleanup script is the only supported removal path |

`MANIFEST.json` records `secret_preserved_on_uninstall: true` and
`config_preserved_on_uninstall: true`. A **VM test** asserts these on
a clean target before any release is tagged.

---

## 0.z Public config lifecycle (operator-owned, never in MSI)

`C:\ProgramData\HermesOrchClient\config.yaml` is **operator-owned**
and is **not** contained in any MSI component. The MSI ships
**`config.yaml.example`** only.

| File | Ownership | Policy |
|---|---|---|
| `config.yaml.example` | **MSI-owned** | **Upgraded** by the next MSI as part of `MajorUpgrade`. The next version overwrites the example. Operators **must not** customize `config.yaml.example`; the real `config.yaml` is the sole operator-owned file. The WiX component has **no** `NeverOverwrite` (consistent with "upgradeable") |
| `config.yaml` | **Operator-owned** (not in any MSI component) | Per `§0.ae` post-install steps. Repair / upgrade / uninstall do not touch it. Uninstall preserves it (enforced by ABSENCE of removal directives) |

---

## 0.aa Payload allowlist (CI build-time check)

A build-time check enumerates the final release directory (after
the dedup stage — see `§0.ae-bis`) and asserts that every file
matches one of:

- The PyInstaller Python runtime DLLs (e.g. `python314.dll`, `vcruntime140.dll`, `ucrtbase.dll`) — **exactly one copy each**
- The pywin32 service dispatcher DLLs (`pywintypes314.dll`, `pythoncom314.dll`) — only in the service onedir; the doctor onedir does **not** bundle pywin32 (Option B)
- The orch client module + its declared deps (service onedir only)
- The signed payload inventory recorded in the build manifest
- The `OrchClientDoctor.exe` console binary (allowed; console-enabled is the point)
- Allowed templates (`config.yaml.example`, `agent-secret.bin` placeholder)

**Dedup assertion** (v0.6): after the deterministic merge step, the
final release directory must contain **exactly one copy** of each
shared runtime DLL (`python314.dll`, `vcruntime140.dll`, `ucrtbase.dll`,
etc.). The assertion fails the build if duplicates are found.

If any unexpected `.exe`, `.dll`, `.pyd`, `.bat`, `.cmd`, `.ps1`,
`.sh`, or other script/binary appears, or if any shared runtime DLL
appears more than once, the build fails before the MSI is produced.

---

## 0.ab Signing policy

- **Owned executable** (`OrchClient.exe`, `OrchClientDoctor.exe`):
  must be signed by the release certificate.
- **Third-party signed binary** (Python runtime, pywin32): preserve
  and verify its existing signature; **do not re-sign**.
- **Third-party unsigned binary**: explicitly approved by hash,
  provenance, and SBOM entry; **do not silently treat as "already signed"**.
- **Sign order**: owned EXEs → MSI → verify → final SHA-256 → manifest → SBOM → validate → re-write manifest with SBOM provenance
- **Per-step failure**: any `signtool` invocation that returns
  non-zero is caught by `Invoke-NativeChecked` and aborts the build

---

## 0.ae-bis Build-time dedup + allowlist (v0.6 new)

`build.ps1` runs PyInstaller **twice** (Option B):

1. `pyinstaller installer/orch-client.spec` → `dist/OrchClient-service/`
   (service onedir, with pywin32)
2. `pyinstaller installer/orch-client-doctor.spec` (separate spec) →
   `dist/OrchClient-doctor/` (doctor onedir, no pywin32)

Then `build.ps1` constructs the final release directory at
`dist/OrchClient/` by:

1. Copying `OrchClient.exe` from `dist/OrchClient-service/`
2. Copying `OrchClientDoctor.exe` from `dist/OrchClient-doctor/`
3. Copying the **service onedir's** Python runtime, pywin32, and
   service-only modules (allowlist)
4. Copying the **doctor onedir's** modules (allowlist, with name
   deduplication against the service set)
5. **Dedup assertion**: exactly one copy of each shared DLL
   (`python314.dll`, `vcruntime140.dll`, `ucrtbase.dll`, …). If
   duplicates exist, the build aborts.
6. Copying templates (`config.yaml.example`, `agent-secret.bin`)

The service and doctor `Analysis` objects are **separate** but the
final release directory is constructed deterministically with an
allowlist + dedup check. Each `Analysis` is internally consistent;
the merge step is explicit and testable.

---

## 0.ac Release verification (operator-side, target machine)

```powershell
# 1. Sign verify (expiry checked against signing time, not now)
signtool.exe verify /pa /all /v "OrchClient-v0.1.0-x64.msi"

# 2. SHA-256 compare against SHA256SUMS.txt
Get-FileHash "OrchClient-v0.1.0-x64.msi" -Algorithm SHA256

# 3. Publisher + cert chain
Get-AuthenticodeSignature "OrchClient-v0.1.0-x64.msi" |
    Select-Object SignerCertificate.Subject, NotAfter, IsOSCertificate

# 4. RFC 3161 timestamp presence
#    (visible in signtool verify verbose output)

# 5. SBOM validation
cyclonedx-py-validate "SBOM.cyclonedx.json"

# 6. SBOM SHA-256 + manifest cross-check
Get-FileHash "SBOM.cyclonedx.json" -Algorithm SHA256
# Compare to MANIFEST.json::sbom_sha256

# 7. Tool version cross-check
python --version            # should match MANIFEST.json::tooling::python_version
pyinstaller --version       # should match MANIFEST.json::tooling::pyinstaller_version
syft version                # should match MANIFEST.json::sbom_generator_version
cyclonedx-py-validate --version  # should match MANIFEST.json::sbom_validator_version
```

---

## 0.ad Build provenance

Every release artifact is accompanied by:

- **Exact pinned versions** (§0.x) — every tool version recorded in
  `MANIFEST.json::tooling` and SBOM metadata
- Source commit SHA on the build branch
- Locked `requirements.lock` hash
- Build-host identifier (operator-bound)
- Build timestamp (UTC)
- `MANIFEST.json` (v0.6: built as a `[ordered]@{}` object throughout;
  converted to JSON exactly once at the end; read-back + parse +
  assert gate at the end)
- `SBOM.cyclonedx.json` (real CycloneDX 1.6 document, validated)

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
3. **Re-verify the ACL** matches `D:P(A;;FA;;;SY)(A;;FA;;;BA)` (no Users ACE)
4. **Provision the real `config.yaml`** (copied from `config.yaml.example`)
5. **Run `OrchClientDoctor.exe`** to dry-run / verify config + ACL + signature
6. **Start the service**

---

## 0.af SBOM tool binding (v0.6: preflight gate enforced)

| Property | Value (pinned, exact) |
|---|---|
| Tool | `syft` |
| Version | **v1.18.0** (pinned; preflight-gate enforced) |
| Output format | `cyclonedx-json` |
| Command | `syft scan dir:<publishDir> --output cyclonedx-json=<outFile>` |
| Output filename | `SBOM.cyclonedx.json` (operator-bound in `build.ps1`) |
| Validator | `cyclonedx-py-validate` |
| Validator version | **v0.5.0** (pinned; preflight-gate enforced) |
| Validator command | `cyclonedx-py-validate <sbomFile>` (exits 0 on valid) |
| Preflight gate | `build.ps1` runs `syft version` + `cyclonedx-py-validate --version`; parses version; compares to bound values; aborts the build on mismatch (via `Invoke-NativeChecked` or a similar fail-closed check) |
| Manifest fields | `sbom_filename`, `sbom_sha256`, `sbom_generator`, `sbom_generator_version`, `sbom_validator`, `sbom_validator_version`, `sbom_validator_result` |

---

## 0.4 Secret-preservation state table (v0.6: NeverOverwrite ≠ preserve-on-uninstall)

| # | State | Required behavior | VM test required | Mechanism |
|---|---|---|---|---|
| 1 | Fresh MSI install | Create zero-byte placeholder; service is demand-start and **refuses to run** until real config + secret are written | ✓ | MSI drops the file with size 0; service self-check rejects |
| 2 | Secret provisioned after install | Preserve it through `Repair` and `MajorUpgrade` | ✓ | `NeverOverwrite="yes"` on `OrchClientSecretFileComponent` |
| 3 | Upgrade with missing secret (operator deleted) | **Do not silently recreate**; service stays demand-start; health gate fails closed | ✓ | `NeverOverwrite` is a no-op when the file is missing; MSI does not auto-recreate |
| 4 | Uninstall | **PRESERVE** the secret file. MSI removes only program files + service registration | ✓ | **Mechanism**: the WiX component has **no** `RemoveFile` directive and the parent `<ComponentGroup>` has **no** `RemoveFolder` directive. The `MANIFEST.json` records `secret_preserved_on_uninstall: true`. A clean-target VM test asserts this |
| 5 | Reinstall after uninstall | Placeholder returns; previously-provisioned secret is still present (because uninstall preserved it) | ✓ | Same as (4); install does not remove the operator-written file because there's no `RemoveFile` |

---

## 0.4-bis Config-preservation state table (v0.6: explicit `config.yaml.example` policy)

| # | State | `config.yaml` (operator-owned) | `config.yaml.example` (MSI-owned) |
|---|---|---|---|
| 1 | Fresh MSI install | Not present (operator creates per `§0.ae`) | MSI drops it |
| 2 | Operator provisions `config.yaml` | Created by operator per `§0.ae` | Untouched |
| 3 | Repair | Untouched (not in any MSI component) | Reinstalled from MSI template |
| 4 | Major upgrade | Untouched (not in any MSI component) | **Overwritten** by the new MSI (per the v0.6 explicit "MSI-owned, upgradeable" policy) |
| 5 | Uninstall | **PRESERVED** (no `RemoveFile` directive) | **PRESERVED** (no `RemoveFile` directive) |

`MANIFEST.json` records `config_preserved_on_uninstall: true` and
`config_example_preserved_on_uninstall: true`. A clean-target VM
test asserts both.

---

## 1. Goal

Produce:

1. A runnable **orch client** (Python) that signs HMAC-authenticated
   heartbeats and self-enrolls against the orchestrator's enrollment
   endpoint (B12-deployed contract; TBD until B12 is reviewed).
2. A separate **`OrchClientDoctor.exe`** (console-enabled) for
   dry-run / config validation / signature / ACL checks.
3. A **code-signed Windows MSI** built with PyInstaller (two
   separate `.spec` files + deterministic release-directory merge +
   allowlist + dedup assertion) + WiX 4 + `HarvestDirectory` task.
4. A **SHA-256 + real CycloneDX SBOM + signing manifest** for
   operator handoff.
5. An **updated install runbook** that references the real artifacts
   (not illustrative filenames).

The MSI install shall:

- Drop `OrchClient.exe` (service) and `OrchClientDoctor.exe` (console)
  under `C:\Program Files\HermesOrchClient\`
- Register a Windows Service named `OrchClient` (start = demand)
- Drop `config.yaml.example` (MSI-owned, upgradeable) at
  `C:\ProgramData\HermesOrchClient\config.yaml.example`; the real
  `config.yaml` is **operator-provisioned**, not in the MSI
- Drop a zero-byte placeholder secret at
  `C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin` with
  `NeverOverwrite="yes"` and ACL `D:P(A;;FA;;;SY)(A;;FA;;;BA)`
- Lock the parent directories with matching SDDLs (`util:PermissionEx`)
- **Not** auto-start the service on install
- For the first release, set service `FirstFailure=SecondFailure=ThirdFailure=none`
- On uninstall, **PRESERVE** `config.yaml` and `agent-secret.bin` (no `RemoveFile` / `RemoveFolder` directives in WiX source)

---

## 1.4 Server-side HMAC key-id-to-agent authorization rule (v0.6: corrected rationale)

Documented for the server implementation (out of scope for this
plan; server is the B12-deployed code). After the server has
verified the HMAC + body-hash signature per `§3.1`, the server
**must** apply the following authorization rule:

```text
1. Look up the key-id (X-Hermes-Key-Id) in the agent registry.
2. The registry entry maps key-id -> authorized agent_id.
3. Parse the raw body to extract body.agent_id.
4. Reject with 403 if body.agent_id != authorized agent_id
   (or if the key-id is not registered, or if the agent is
   disabled / pending).
5. Only then dispatch to the request handler.
```

**Rationale (v0.6 corrected)**: `key-id` is **not** a secret. The
mapping rule prevents a **compromised, mis-provisioned, or
incorrectly-authorized key** from being used to submit a request that
claims another `agent_id`. Without this rule, the body is
cryptographically protected but the key-to-agent authorization is
implicit; an attacker who learns a `key-id` plus the corresponding
HMAC secret can sign requests for any agent the same key is
authorized for.

---

## 2. Deliverables

(unchanged from v0.5 — Deliverables 1-9)

| # | Artifact | Path |
|---|---|---|
| 1 | Orch client source | `C:\Project\minimax code\hermes-orchestrator\src\orch_client\` (7 modules) |
| 2 | `pyproject.toml` entry points | extend existing `C:\Project\minimax code\hermes-orchestrator\pyproject.toml` |
| 3 | Locked dependency file | `C:\Project\minimax code\hermes-orchestrator\installer\requirements.lock` |
| 4 | **Two** PyInstaller specs (v0.6) | `C:\Project\minimax code\hermes-orchestrator\installer\orch-client.spec` (service) + `orch-client-doctor.spec` (doctor) |
| 5 | WiX 4 `.wixproj` + manually-authored `.wxs` | `C:\Project\minimax code\hermes-orchestrator\installer\orch-client.wixproj` + `orch-client.wxs` |
| 6 | Build script | `C:\Project\minimax code\hermes-orchestrator\installer\build.ps1` |
| 7 | Built MSI | `C:\Project\minimax code\hermes-orchestrator\dist\OrchClient-v0.1.0-x64.msi` |
| 8 | SHA-256 + manifest + real SBOM | `dist\SHA256SUMS.txt` + `dist\MANIFEST.json` + `dist\SBOM.cyclonedx.json` |
| 9 | Updated runbook | `C:\Users\stanley\AppData\Local\Temp\orch-client-install-runbook.md` |

---

## 3. Source layout (Deliverable 1)

(unchanged from v0.5)

---

## 4. PyInstaller specs (Deliverable 4) — two separate specs, Option B dedup merge

`installer/orch-client.spec` (service):

```python
# -*- mode: python ; coding: utf-8 -*-
import sys ; sys.setrecursionlimit(5000)
from PyInstaller.utils.hooks import collect_submodules, collect_data_files
datas = collect_data_files('orch_client')
hidden = collect_submodules('orch_client') + [
    'win32serviceutil', 'win32service', 'win32event', 'servicemanager',
]
a = Analysis(
    ['..\\src\\orch_client\\__main__.py'],
    pathex=['..\\src'],
    hiddenimports=hidden,
    datas=datas,
    excludes=['tkinter', 'unittest', 'pydoc'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [],
    exclude_binaries=True, name='OrchClient',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[], name='OrchClient')
```

`installer/orch-client-doctor.spec` (doctor, separate spec, no pywin32):

```python
# -*- mode: python ; coding: utf-8 -*-
import sys ; sys.setrecursionlimit(5000)
from PyInstaller.utils.hooks import collect_submodules, collect_data_files
datas = collect_data_files('orch_client')
hidden = collect_submodules('orch_client')
a = Analysis(
    ['..\\src\\orch_client\\doctor.py'],
    pathex=['..\\src'],
    hiddenimports=hidden,
    datas=datas,
    excludes=['tkinter', 'unittest', 'pydoc', 'win32serviceutil', 'win32service', 'win32event', 'servicemanager'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [],
    exclude_binaries=True, name='OrchClientDoctor',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[], name='OrchClient')
```

**Build script** (`build.ps1`) runs both specs in sequence; the
release directory is constructed by the deterministic merge step
(`§0.ae-bis`).

---

## 5. WiX 4 source (Deliverable 5) — v0.6: `config.yaml.example` policy explicit

`orch-client.wixproj` (unchanged from v0.5):

```xml
<Project Sdk="WixToolset.Sdk/4.0">
  <PropertyGroup>
    <OutputType>Package</OutputType>
    <Version>0.1.0</Version>
    <Platform>x64</Platform>
    <PublishDir>$(MSBuildProjectDirectory)\..\dist\OrchClient</PublishDir>
    <ConfigTemplateDir>$(MSBuildProjectDirectory)\templates\config</ConfigTemplateDir>
    <SecretTemplateDir>$(MSBuildProjectDirectory)\templates\secret</SecretTemplateDir>
    <OutputName>OrchClient-v$(Version)-$(Platform)</OutputName>
    <OutputPath>$(MSBuildProjectDirectory)\..\dist\</OutputPath>
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
      <ExcludeFiles>**\OrchClient.exe</ExcludeFiles>
      <ExcludeFiles>**\OrchClientDoctor.exe</ExcludeFiles>
    </HarvestDirectory>
  </ItemGroup>

  <ItemGroup>
    <Compile Include="orch-client.wxs" />
  </ItemGroup>
</Project>
```

`orch-client.wxs` (v0.6: explicit `config.yaml.example` policy; no `RemoveFile` / `RemoveFolder` for the operator-owned files):

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
      <ComponentGroupRef Id="OrchClientFiles" />
      <ComponentGroupRef Id="OrchClientService" />
      <ComponentGroupRef Id="OrchClientDoctor" />
      <ComponentGroupRef Id="OrchClientConfig" />
      <ComponentGroupRef Id="OrchClientSecret" />
    </Feature>
  </Package>

  <!-- Service + Doctor + Config + Secret fragments (ComponentGroup wrap per component) -->

  <!-- Service: remove="uninstall" ensures SCM removal; no RemoveFile for /ProgramData -->
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
    <ComponentGroup Id="OrchClientService">
      <ComponentRef Id="OrchClientServiceComponent" />
    </ComponentGroup>
  </Fragment>

  <Fragment>
    <DirectoryRef Id="INSTALLFOLDER">
      <Component Id="OrchClientDoctorComponent" Guid="*">
        <File Id="OrchClientDoctorExe"
              Source="$(var.PublishDir)\OrchClientDoctor.exe"
              KeyPath="yes" />
      </Component>
    </DirectoryRef>
    <ComponentGroup Id="OrchClientDoctor">
      <ComponentRef Id="OrchClientDoctorComponent" />
    </ComponentGroup>
  </Fragment>

  <!--
    v0.6 NOTE: there are NO <RemoveFile> or <RemoveFolder> directives in
    this WiX source. Uninstall therefore preserves every ProgramData file
    that the MSI did not explicitly remove via <RemoveFile>. Program files
    under INSTALLFOLDER are removed by WiX's default InstallExecuteSequence
    (RemoveFiles + RemoveFolders). config.yaml, config.yaml.example,
    agent-secret.bin, and the ProgramData directories are PRESERVED on
    uninstall. The MANIFEST.json records this as secret_preserved_on_uninstall
    and config_preserved_on_uninstall; a clean-target VM test asserts it.
  -->
  <Fragment>
    <StandardDirectory Id="ProgramDataFolder">
      <Directory Id="CONFIGFOLDER" Name="HermesOrchClient">
        <Component Id="OrchClientConfigDirComponent" Guid="*" KeyPath="yes">
          <CreateFolder>
            <util:PermissionEx
              Sddl="D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)(A;;FX;;;BU)" />
          </CreateFolder>
        </Component>

        <!-- config.yaml.example: MSI-owned, upgradeable, NO NeverOverwrite -->
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

    <ComponentGroup Id="OrchClientConfig">
      <ComponentRef Id="OrchClientConfigDirComponent" />
      <ComponentRef Id="OrchClientConfigExampleComponent" />
    </ComponentGroup>
    <ComponentGroup Id="OrchClientSecret">
      <ComponentRef Id="OrchClientSecretDirComponent" />
      <ComponentRef Id="OrchClientSecretFileComponent" />
    </ComponentGroup>
  </Fragment>
</Wix>
```

---

## 6. Build script (Deliverable 6) — v0.6: preflight gate + manifest object + read-back gate + dedup merge

`build.ps1` (key v0.6 changes: preflight tool-version gate, two PyInstaller runs with dedup merge, manifest as object throughout, read-back + parse + assert gate):

```powershell
$ErrorActionPreference = 'Stop'

# ----- Bound operator values (exact, preflight-gate enforced) -----
$RepoRoot      = 'C:\Project\minimax code\hermes-orchestrator'
$PyInstaller   = 'pyinstaller'
$WixProject    = Join-Path $RepoRoot 'installer\orch-client.wixproj'
$ServiceSpec   = Join-Path $RepoRoot 'installer\orch-client.spec'
$DoctorSpec    = Join-Path $RepoRoot 'installer\orch-client-doctor.spec'
$TimestampUrl  = 'http://timestamp.digicert.com'
$CertThumb     = '<TBD by operator — code-signing cert thumbprint>'
$Version       = '0.1.0'
$Arch          = 'x64'
$ExpectedMsi   = "OrchClient-v${Version}-${Arch}.msi"

# Bound exact tool versions (must match preflight-gate checks)
$ExpectedPython           = '3.14.0'
$ExpectedPyInstaller      = '6.16.0'
$ExpectedWix              = '4.0.6'
$ExpectedSigntool         = '10.0.22621.4031'
$ExpectedDotnet           = '8.0.404'
$ExpectedSyft             = 'v1.18.0'
$ExpectedCycloneValidator = 'v0.5.0'

# SBOM tool
$sbomGen       = 'syft'
$sbomValidate  = 'cyclonedx-py-validate'
$sbomOut       = Join-Path $RepoRoot 'dist\SBOM.cyclonedx.json'

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

# Helper: get a tool's reported version (or 'unknown' if not parseable)
function Get-ToolVersion {
    param([string]$ToolPath, [string[]]$VersionArgs)
    $out = & $ToolPath @VersionArgs 2>&1 | Out-String
    return $out.Trim()
}

# ============================================================================
# 0) Preflight: bound tool versions (v0.6)
# ============================================================================
$pythonVer = Get-ToolVersion -ToolPath 'python' -VersionArgs @('--version')
if ($pythonVer -notmatch [regex]::Escape($ExpectedPython)) {
    throw "Python version mismatch: expected '$ExpectedPython', got '$pythonVer'"
}
$pyinstVer = Get-ToolVersion -ToolPath $PyInstaller -VersionArgs @('--version')
if ($pyinstVer -notmatch [regex]::Escape($ExpectedPyInstaller)) {
    throw "PyInstaller version mismatch: expected '$ExpectedPyInstaller', got '$pyinstVer'"
}
$syftVer = Get-ToolVersion -ToolPath $sbomGen -VersionArgs @('version')
if ($syftVer -notmatch [regex]::Escape($ExpectedSyft)) {
    throw "syft version mismatch: expected '$ExpectedSyft', got '$syftVer'"
}
$cyVer = Get-ToolVersion -ToolPath $sbomValidate -VersionArgs @('--version')
if ($cyVer -notmatch [regex]::Escape($ExpectedCycloneValidator)) {
    throw "cyclonedx-py-validate version mismatch: expected '$ExpectedCycloneValidator', got '$cyVer'"
}

# ============================================================================
# 1) Verify locked dependency file matches
# ============================================================================
Invoke-NativeChecked -FilePath 'pip' `
    -Arguments @('install','--require-hashes','-r',
                  (Join-Path $RepoRoot 'installer/requirements.lock')) `
    -Label 'pip install --require-hashes'

# ============================================================================
# 2) PyInstaller: TWO separate specs (Option B)
# ============================================================================
$serviceOnedir = Join-Path $RepoRoot 'dist\OrchClient-service'
$doctorOnedir  = Join-Path $RepoRoot 'dist\OrchClient-doctor'
$publishDir    = Join-Path $RepoRoot 'dist\OrchClient'
Remove-Item -LiteralPath $serviceOnedir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $doctorOnedir  -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $publishDir    -Recurse -Force -ErrorAction SilentlyContinue

Invoke-NativeChecked -FilePath $PyInstaller `
    -Arguments @('--clean','--noconfirm',$ServiceSpec) `
    -Label 'pyinstaller (service spec)'
Invoke-NativeChecked -FilePath $PyInstaller `
    -Arguments @('--clean','--noconfirm',$DoctorSpec) `
    -Label 'pyinstaller (doctor spec)'

# ============================================================================
# 3) Construct the release directory (deterministic merge + dedup assertion)
# ============================================================================
New-Item -ItemType Directory -Path $publishDir -Force | Out-Null

# Owned EXEs
Copy-Item -LiteralPath (Join-Path $serviceOnedir 'OrchClient.exe') `
            -Destination (Join-Path $publishDir 'OrchClient.exe') -Force
Copy-Item -LiteralPath (Join-Path $doctorOnedir  'OrchClientDoctor.exe') `
            -Destination (Join-Path $publishDir 'OrchClientDoctor.exe') -Force

# All DLLs / modules from the service onedir (Python runtime + pywin32 + service deps)
Copy-Item -Path (Join-Path $serviceOnedir '_internal\*') `
            -Destination (Join-Path $publishDir '_internal') -Recurse -Force

# Doctor-only modules (allowlist against service set; skip duplicates)
$serviceInternal = Join-Path $serviceOnedir '_internal'
$doctorInternal  = Join-Path $doctorOnedir  '_internal'
Get-ChildItem -Path $doctorInternal -Recurse -File | ForEach-Object {
    $relPath   = $_.FullName.Substring($doctorInternal.Length).TrimStart('\','/')
    $targetPath = Join-Path $publishDir $relPath
    if (Test-Path -LiteralPath $targetPath) {
        # Duplicate (shared runtime / stdlib). Skip; service onedir already has it.
        return
    }
    $targetDir = Split-Path -LiteralPath $targetPath -Parent
    if (-not (Test-Path -LiteralPath $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }
    Copy-Item -LiteralPath $_.FullName -Destination $targetPath -Force
}

# Dedup assertion (v0.6): exactly one copy of each shared runtime DLL
$sharedDlls = @('python314.dll','vcruntime140.dll','ucrtbase.dll','pythoncom314.dll','pywintypes314.dll')
foreach ($dll in $sharedDlls) {
    $count = (Get-ChildItem -Path $publishDir -Recurse -Filter $dll -ErrorAction SilentlyContinue).Count
    if ($count -ne 1) {
        throw "Dedup assertion failed for $dll: expected 1 copy in $publishDir, got $count"
    }
}

# Templates
$configDir = Join-Path $RepoRoot 'installer\templates\config'
$secretDir = Join-Path $RepoRoot 'installer\templates\secret'
New-Item -ItemType Directory -Path (Join-Path $publishDir 'templates\config') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $publishDir 'templates\secret') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $configDir 'config.yaml.example') `
            -Destination (Join-Path $publishDir 'templates\config\config.yaml.example') -Force
Copy-Item -LiteralPath (Join-Path $secretDir 'agent-secret.bin') `
            -Destination (Join-Path $publishDir 'templates\secret\agent-secret.bin') -Force

# ============================================================================
# 4) Sign owned EXEs (third-party DLLs already signed; do NOT re-sign)
# ============================================================================
$ownedExes = @(Get-ChildItem -Path $publishDir -File |
    Where-Object { $_.Name -in @('OrchClient.exe','OrchClientDoctor.exe') })
foreach ($f in $ownedExes) {
    Invoke-NativeChecked -FilePath 'signtool.exe' `
        -Arguments @('sign','/fd','SHA256','/tr',$TimestampUrl,'/td','SHA256',
                      '/sha1',$CertThumb,$f.FullName) `
        -Label "signtool sign $($f.Name)"
}

# ============================================================================
# 5) Build MSI via dotnet build (SDK-style .wixproj)
# ============================================================================
$msiDir = Join-Path $RepoRoot 'dist'
Remove-Item -LiteralPath (Join-Path $msiDir '*.msi') -ErrorAction SilentlyContinue
Invoke-NativeChecked -FilePath 'dotnet' `
    -Arguments @('build',$WixProject,'-c','Release','-p',"Platform=$Arch") `
    -Label 'dotnet build (WiX 4 SDK-style .wixproj)'
$msiPath = Join-Path $msiDir $ExpectedMsi
if (-not (Test-Path -LiteralPath $msiPath)) {
    throw "Expected MSI not found at $msiPath (exact filename assertion failed)"
}

# ============================================================================
# 6) Sign the MSI
# ============================================================================
Invoke-NativeChecked -FilePath 'signtool.exe' `
    -Arguments @('sign','/fd','SHA256','/tr',$TimestampUrl,'/td','SHA256',
                  '/sha1',$CertThumb,$msiPath) `
    -Label 'signtool sign MSI'

# ============================================================================
# 7) Verify
# ============================================================================
Invoke-NativeChecked -FilePath 'signtool.exe' `
    -Arguments @('verify','/pa','/all','/v',$msiPath) `
    -Label 'signtool verify MSI'

# ============================================================================
# 8) Compute MSI SHA-256
# ============================================================================
$hash = (Get-FileHash -LiteralPath $msiPath -Algorithm SHA256).Hash
"$hash  $ExpectedMsi" |
    Out-File -LiteralPath (Join-Path $msiDir 'SHA256SUMS.txt') -Encoding utf8

# ============================================================================
# 9) Generate SBOM (real CycloneDX, preflight-gate-enforced version)
# ============================================================================
Invoke-NativeChecked -FilePath $sbomGen `
    -Arguments @('scan',"dir=$publishDir",'--output',"cyclonedx-json=$sbomOut") `
    -Label "SBOM generator ($sbomGen $ExpectedSyft)"
$sbomHash = (Get-FileHash -LiteralPath $sbomOut -Algorithm SHA256).Hash

# ============================================================================
# 10) Validate SBOM
# ============================================================================
$sbomValidateResult = & $sbomValidate $sbomOut 2>&1
$sbomValidateExit   = $LASTEXITCODE
if ($sbomValidateExit -ne 0) {
    throw "SBOM validator ($sbomValidate $ExpectedCycloneValidator) failed (exit $sbomValidateExit): $sbomValidateResult"
}

# ============================================================================
# 11) Build MANIFEST as an OBJECT throughout, write JSON once, read back + assert
# ============================================================================
$manifest = [ordered]@{
    product        = 'OrchClient'
    version        = $Version
    architecture   = $Arch
    msi_path       = $msiPath
    msi_filename   = $ExpectedMsi
    msi_sha256     = $hash
    msi_size       = (Get-Item $msiPath).Length
    built_at_utc   = (Get-Date).ToUniversalTime().ToString('o')
    built_by       = $env:USERNAME
    signing        = [ordered]@{
        tool        = 'signtool.exe'
        timestamp   = $TimestampUrl
        cert_sha1   = $CertThumb
        digest      = 'SHA256'
    }
    tooling        = [ordered]@{
        python_version        = $ExpectedPython
        pyinstaller_version   = $ExpectedPyInstaller
        wix_version           = $ExpectedWix
        signtool_version      = $ExpectedSigntool
        dotnet_version        = $ExpectedDotnet
        sbom_generator        = $sbomGen
        sbom_generator_version = $ExpectedSyft
        sbom_validator        = $sbomValidate
        sbom_validator_version = $ExpectedCycloneValidator
    }
    payload_inventory = @((Get-ChildItem $publishDir -Recurse -File).FullName |
        ForEach-Object { [PSCustomObject]@{ path = (Resolve-Path -LiteralPath $_ -Relative); sha256 = (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash } })
    secret_preserved_on_uninstall = $true
    config_preserved_on_uninstall = $true
    config_example_preserved_on_uninstall = $true
    sbom_filename    = Split-Path -Leaf $sbomOut
    sbom_sha256      = $sbomHash
    sbom_generator   = $sbomGen
    sbom_generator_version = $ExpectedSyft
    sbom_validator   = $sbomValidate
    sbom_validator_version = $ExpectedCycloneValidator
    sbom_validator_result = 'pass'
}

# Write the manifest ONCE
$manifestJson = $manifest | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText(
    (Join-Path $msiDir 'MANIFEST.json'),
    $manifestJson,
    [System.Text.UTF8Encoding]::new($false))

# ============================================================================
# 12) Read-back + parse + assert gate (catches missing fields, mismatched hashes)
# ============================================================================
$manifestReadback = Get-Content -LiteralPath (Join-Path $msiDir 'MANIFEST.json') -Raw |
    ConvertFrom-Json
foreach ($required in @('product','version','msi_sha256','msi_filename',
                        'sbom_filename','sbom_sha256','sbom_generator_version',
                        'sbom_validator_version','sbom_validator_result',
                        'secret_preserved_on_uninstall',
                        'config_preserved_on_uninstall',
                        'config_example_preserved_on_uninstall',
                        'tooling')) {
    if (-not ($manifestReadback.PSObject.Properties.Name -contains $required)) {
        throw "MANIFEST.json read-back missing field: $required"
    }
}
if ($manifestReadback.msi_sha256 -ne $hash) {
    throw "MANIFEST.json::msi_sha256 does not match Get-FileHash: $($manifestReadback.msi_sha256) vs $hash"
}
if ($manifestReadback.sbom_sha256 -ne $sbomHash) {
    throw "MANIFEST.json::sbom_sha256 does not match Get-FileHash(SBOM)"
}
if ($manifestReadback.sbom_validator_result -ne 'pass') {
    throw "MANIFEST.json::sbom_validator_result is '$($manifestReadback.sbom_validator_result)' (expected 'pass')"
}

Write-Host "[+] Build complete: $msiPath"
Write-Host "[+] MSI SHA-256: $hash"
Write-Host "[+] SBOM: $sbomOut (sha256: $sbomHash)"
Write-Host "[+] MANIFEST read-back + assert gate: PASS"
```

---

## 7. Known gaps & explicit dependencies (must be resolved before build)

(unchanged from v0.5; v0.6 adds: exact pinned versions are now bound, not ranges)

| # | Dependency | Owner | Status |
|---|---|---|---|
| 1 | Orchestrator contract | operator | **TBD until B12 deployed and reviewed** |
| 2 | Code-signing cert thumbprint | operator | not yet bound |
| 3 | WiX 4 + .NET SDK on build host | operator | not yet installed |
| 4 | PyInstaller availability in build Python | operator | assumed present |
| 5 | Target machine `agent_id` | operator | not yet assigned |
| 6 | Target machine HMAC secret | operator | not yet generated |
| 7 | HMAC `key-id` (rotation support) | operator | not yet bound |
| 8 | `<ORCHESTRATOR_FQDN>` and `<HTTPS_PORT>` (B13-transport-closed) | operator | not yet bound |
| 9 | SBOM tool versions | operator | **bound (v0.6)**: `syft v1.18.0` + `cyclonedx-py-validate v0.5.0`; preflight gate enforces |
| 10 | All other tool versions | operator | **bound (v0.6, exact)**: Python 3.14.0, PyInstaller 6.16.0, WiX 4.0.6, Windows SDK 10.0.22621.4031, .NET SDK 8.0.404 |
| 11 | Cert renewal policy + revoked-cert response | operator | bind at runtime |
| 12 | UpgradeCode GUID + ProductCode rotation | operator | bind at runtime |
| 13 | VM test environment (clean Windows 10/11) | operator | required before implementation approval |
| 14 | Explicit privileged cleanup script (out of scope; follow-up) | operator | for removing `config.yaml` / `agent-secret.bin` on uninstall |

---

## 8. Forbidden actions (no exceptions)

(unchanged from v0.5; v0.6 adds: "no `RemoveFile` / `RemoveFolder` for operator-owned files")

- ❌ No remote install on the target Windows machine
- ❌ No live HTTP calls during build (dry-run only)
- ❌ No real HMAC secret embedded in the MSI
- ❌ No `orchestrator_url: http://...` shipped in MSI config
- ❌ No `config.yaml` shipped in MSI (only `config.yaml.example`; operator provisions)
- ❌ No modification of B12 deploy (held at watchdog REFUSE)
- ❌ No modification of watchdog task (held)
- ❌ No push to remote git remote (per proposal-branch pattern only)
- ❌ No firewall / enrollment / agent-mutation action on the orchestrator
- ❌ No repair / upgrade that clobbers a provisioned secret (`NeverOverwrite="yes"` on the MSI-owned secret file; the operator-owned `config.yaml` is not in any MSI component)
- ❌ No `RemoveFile` / `RemoveFolder` directives in WiX source for operator-owned files
- ❌ No uninstall that removes `config.yaml` or `agent-secret.bin`
- ❌ No blind re-signing of third-party DLLs that already carry a valid Authenticode signature
- ❌ No labelling a hand-rolled JSON as SPDX or CycloneDX
- ❌ No CLI args for frozen-bundle behavior; the two `.spec` files are the single source of truth (one per entry point)
- ❌ No manual `OrchClientFiles` group
- ❌ No requiring unexpired cert at verify time
- ❌ No single `Analysis` for two EXEs with different entry points
- ❌ No unwrapped `<Component>` in WiX
- ❌ No moving-range pinned versions; all tools have exact versions recorded in MANIFEST.json
- ❌ No skipping the preflight version gate or the manifest read-back gate
- ❌ No silent dedup failures; the dedup assertion aborts the build

---

## 9. Acceptance criteria

(unchanged from v0.5; v0.6 adds: preflight gate, dedup assertion, manifest read-back gate, exact pinned versions)

A-M: same as v0.5 (file system layout, ACLs, service registration, doctor binary, secret-preservation, config-preservation, HMAC validation, runbook, side effects, VM validation, test case matrix, server-side rule, manifest field set)

**N (new in v0.6)**: preflight version gate passes; dedup assertion finds exactly one copy of each shared runtime DLL; manifest read-back + parse + assert gate passes; every recorded tool version matches the installed executable version.

---

## 10. What I will NOT do (without separate approval)

(unchanged from v0.5)

---

## 11. v0.1 → v0.6 changelog (cross-reference)

| # | Section | v0.5 | v0.6 |
|---|---|---|---|
| 1 | §4 PyInstaller | two `Analysis` merged via one `COLLECT` (duplicate risk) | **two `Analysis` + two specs + Option B deterministic merge + dedup assertion** |
| 2 | §0.4 / §0.y secret-preservation | "`NeverOverwrite` = preserve on uninstall" (incorrect) | **`NeverOverwrite` ≠ preserve-on-uninstall; preserve-on-uninstall enforced by ABSENCE of `RemoveFile` / `RemoveFolder`; explicit VM test for the uninstall state** |
| 3 | §0.z config.yaml.example policy | contradictory (claimed both `NeverOverwrite` and "upgradeable") | **single explicit policy: MSI-owned, upgradeable, no `NeverOverwrite` on the WiX component; real `config.yaml` is the sole operator-owned file** |
| 4 | §6 build.ps1 MANIFEST object | `| ConvertTo-Json` too early → string mutation | **kept as `[ordered]@{}` object throughout, `ConvertTo-Json` exactly once, then read-back + parse + assert gate** |
| 5 | §0.af SBOM tool version | bound but not verified | **preflight gate runs `<tool> version` and aborts on mismatch** |
| 6 | §1.4 HMAC key-id-to-agent rationale | "attacker learns key-id to sign requests" (incorrect) | **"prevents a compromised, mis-provisioned, or incorrectly-authorized key from being used to submit a request that claims another agent_id"** |
| 7 | §0.x pinned versions | moving ranges (3.14.x, 6.x latest, 4.x latest) | **exact pinned: Python 3.14.0, PyInstaller 6.16.0, WiX 4.0.6, Windows SDK 10.0.22621.4031, .NET SDK 8.0.404, syft v1.18.0, cyclonedx-py-validate v0.5.0** |
| 8 | §6 dedup | (n/a) | **`sharedDlls` count assertion** (e.g. `python314.dll`, `vcruntime140.dll` must appear exactly once) |
| 9 | §6 read-back gate | (n/a) | **Read `MANIFEST.json` back, parse as JSON, assert required fields + SHA-256 matches + SBOM validator result is 'pass'** |

---

## 12. Outstanding Perplexity questions for v0.7 (if any)

The v0.6 review addressed all 3 v0.5 implementation blockers and 4
smaller issues. Remaining open items are operator-binding:

- Operator must pick `agent_id` and `key-id` for the target machine
- Operator must pick the code-signing cert / Azure Trusted Signing
- Operator must install WiX 4 + `dotnet` SDK on the build host
- Operator must generate the target machine's HMAC secret (out-of-band)
- Operator must generate the UpgradeCode GUID
- Operator must run the §3.6 + §3.7 + §0.4 + §0.4-bis VM tests, and
  produce a signed report before implementation approval
- Operator must draft the §9 test case matrix (separate work item)
- Operator must draft the **explicit privileged cleanup script**
  (out of scope; tracked as a follow-up) for removing
  `config.yaml` / `agent-secret.bin` on uninstall
