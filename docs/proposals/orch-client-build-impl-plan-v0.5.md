# Orch Client Build — Implementation Plan v0.5 (for Perplexity review)

**Date:** 2026-08-13
**Status:** PROPOSAL for review (Perplexity + operator) — v0.5
**Supersedes:** v0.4 (commit `c84a073` on branch `proposal/orch-client-build-impl-plan-v0.1`)
**Scope:** end-to-end build of a Windows MSI installer for the new orch
client, on the orchestrator host (Windows A). The MSI will be carried
to a separate target Windows machine for installation; this plan does
NOT include remote install, agent enrollment against a live orchestrator,
or B12 deploy. Enrollment is verified separately after the operator
manually runs the install on the target.

---

## 0. v0.4 → v0.5 changelog (Perplexity review)

Perplexity re-read PR #4 after the v0.4 commit and reported:
**the design intent of all 5 v0.3 blockers was applied, but 6 new
implementation blockers + 2 smaller issues were introduced or
retained**. v0.5 addresses all of them.

| # | v0.4 said | v0.5 says | Reason |
|---|---|---|---|
| 1 | `OrchClientDoctor.exe` was built from the **same `Analysis`** as `OrchClient.exe` (using `a.scripts` and `a.binaries` for both). Both EXEs therefore ran `__main__` (service code), not `doctor.py` | **Two separate `Analysis` objects**, one for `__main__.py` (service) and one for `doctor.py` (console). Each `EXE` references its own `.pyz` and binaries; `COLLECT` merges the payload trees | Reusing the same `Analysis.scripts` does not create a different program. A distinct entry point requires a distinct `Analysis` (or equivalent merged-spec handling) |
| 2 | `<Feature>` referenced `OrchClientConfig` / `OrchClientSecret` / `OrchClientService` / `OrchClientDoctor` but the shown `.wxs` only defined individual `<Component>` elements, **not** wrapping `<ComponentGroup>` elements | Every manually-authored component is wrapped in an explicit `<ComponentGroup Id="...">` with `<ComponentRef Id="..." />`. The `<ComponentGroupRef Id="..." />` IDs in the `<Feature>` now match | `ComponentGroup` exists to collect components for reuse via `ComponentGroupRef`. Without the wrap, the linker cannot resolve the references |
| 3 | "NeverOverwrite on the config component" — but **`config.yaml` is not in the MSI** (only `config.yaml.example` is) | The plan text now reflects: `config.yaml` is **operator-owned**, not in any MSI component, and is not subject to `NeverOverwrite` (which only applies to MSI-owned components). `config.yaml.example` is MSI-owned, lives in a `<Component>`, and can be replaced by the next MSI | The previous wording was a category error. `NeverOverwrite` is a property of a `<Component>`. There is no config component because the file is operator-owned |
| 4 | `secret_uninstall_behavior` was a `MANIFEST.json` field that claimed `remove` or `preserve`, but the WiX source implemented neither choice explicitly | **First-release default: PRESERVE on uninstall**. The MSI removes program files, MSI-owned template files, and the service registration only. An **explicit privileged cleanup script** (out of scope for v0.5; tracked as a follow-up) is required to remove the secret or the config. The `MANIFEST.json` field is dropped | An uninstall that silently deletes a provisioned secret or a real config is a footgun. The safer first-release default is to preserve and require an explicit cleanup |
| 5 | `dotnet build $WixProject` with no explicit `OutputName` / `OutputPath` in `.wixproj`; build script's `$ExpectedMsi = "OrchClient-v0.1.0-x64.msi"` could fail the exact-filename assertion | `.wixproj` now declares `<OutputName>OrchClient-v$(Version)-$(Platform)</OutputName>` and `<OutputPath>$(MSBuildProjectDirectory)\..\dist\</OutputPath>`. The build script's filename assertion is now deterministic | The exact-filename assertion is the gate against picking a stale or wrong artifact; without the explicit output policy, the build's default name might not match |
| 6 | "SBOM generator bound: `syft` is the example, operator picks" + a placeholder validator reference. The manifest did not capture SBOM provenance | **Bound**: `syft` v1.x (pinned) + `cyclonedx-py-validate` v0.x (pinned) with exact command syntax. `MANIFEST.json` adds: `sbom_filename`, `sbom_sha256`, `sbom_generator_version`, `sbom_validator_version`, `sbom_validator_result` | "Example, operator picks" is not a bound release command. SBOM provenance must be in the manifest so a downstream compliance check can verify the SBOM came from the claimed tool at the claimed version |
| 7 | `§0.x` said "Python 3.12.x" but `§0.aa` payload allowlist example said `python314.dll`. The "LTS" wording is misused (CPython does not label a release line as LTS in the same way some platform vendors do) | Use the **actual** build interpreter version consistently. Default: Python 3.14.x (matches `python314.dll`). Remove the "LTS" wording; say "current supported CPython release" with the operator-bound exact version | Version drift between the pinned-versions table and the allowlist is a documentation bug. "LTS" is a platform-vendor term, not a CPython label |
| 8 | HMAC spec was sound but did not include a server-side rule: after authenticating `key-id`, the server must map that key to an allowed `agent_id` and reject if the body's `agent_id` does not match | Added §1.4 **server-side key-id-to-agent authorization rule** (documented, not implemented in this plan): after HMAC + body-hash verification, server looks up `key-id` → authorized `agent_id`; if `body.agent_id` ≠ authorized `agent_id`, reject 403 | Without this rule, the body is cryptographically protected but the key-to-agent authorization is implicit; an attacker who learns a `key-id` could use it for an agent they do not control |

All v0.4 sections preserved where not directly affected. Section
numbering kept stable where possible. New sections added for
server-side authorization rule.

---

## 0.x Pinned versions, hashes, and build matrix

| Tool | Version | Notes |
|---|---|---|
| Python | 3.14.x (current supported CPython release; **operator-bound exact version**) | Build-host interpreter; runtime target in MSI is whatever PyInstaller bundles (`python314.dll` etc.) |
| PyInstaller | 6.x latest | `.spec` is the single source of truth (no CLI args at build time) |
| WiX Toolset | 4.x latest | `<HarvestDirectory>` task in `.wixproj`; no untracked `HeatDirectory.wxs`; no manual `OrchClientFiles` group |
| Windows SDK (signtool) | 10.0.22621.x or newer | For `signtool.exe` |
| .NET SDK | 6.x LTS or 8.x LTS | For `dotnet build` of the SDK-style `.wixproj` |
| RFC 3161 timestamp URL | per operator binding | Default suggested: `http://timestamp.digicert.com` |
| Code-signing cert | per operator binding | OV or EV, or Azure Trusted Signing |
| SBOM generator | **`syft` v1.x (pinned; v0.5 binds this)** | CycloneDX 1.6 JSON output |
| SBOM validator | **`cyclonedx-py-validate` v0.x (pinned; v0.5 binds this)** | Validates CycloneDX schema |

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
| Repair behavior | Reinstalls components in the same key path; `NeverOverwrite="yes"` on the **MSI-owned** secret file and on the **MSI-owned** `config.yaml.example`; the **operator-owned** `config.yaml` is not in any MSI component and is therefore never touched by repair |
| Uninstall behavior (v0.5 default) | **PRESERVE** operator-owned files. MSI removes program files, MSI-owned template files, the service registration, and the directory trees that contain only MSI-owned files. An **explicit privileged cleanup script** (out of scope for v0.5; tracked as a follow-up) is required to remove the secret or the operator-configured `config.yaml` |

---

## 0.z Public config lifecycle (operator-owned, never in MSI)

`C:\ProgramData\HermesOrchClient\config.yaml` is **operator-owned**
and is **not** contained in any MSI component. The MSI ships
**`config.yaml.example`** only (with placeholder URL + comments);
the operator copies the example to the real `config.yaml` and fills
in real values per `§0.ae`.

| State | Behavior |
|---|---|
| Fresh MSI install | MSI drops `config.yaml.example` only; no `config.yaml` |
| Operator provisions `config.yaml` | Per `§0.ae` post-install steps |
| Repair | `config.yaml` is **untouched** (it is not in any MSI component) |
| Major upgrade | `config.yaml` is **untouched**; `config.yaml.example` may be updated by the new MSI |
| Uninstall | `config.yaml` is **preserved** (v0.5 default; explicit cleanup script required to remove) |

The `NeverOverwrite="yes"` property is applied to **`config.yaml.example` Component** (so the next MSI does not overwrite the example if the operator customized it), **not** to a `config.yaml` component (which does not exist).

---

## 0.aa Payload allowlist (CI build-time check)

A build-time check enumerates the harvested PyInstaller output
directory and asserts that every file matches one of:

- The PyInstaller Python runtime DLLs (e.g. `python314.dll`, `vcruntime140.dll`, `ucrtbase.dll`)
- The pywin32 service dispatcher DLLs (`pywintypes314.dll`, `pythoncom314.dll`)
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
  payload list are signed.
- **Owned executable** (OrchClient.exe, OrchClientDoctor.exe):
  must be signed by the release certificate.
- **Third-party signed binary** (Python runtime, pywin32): preserve
  and verify its existing signature; **do not re-sign**.
- **Third-party unsigned binary**: explicitly approved by hash,
  provenance, and SBOM entry; **do not silently treat as "already signed"**.
- **Sign order**: owned payload EXEs → MSI → verify → final SHA-256 → manifest + SBOM
- **Per-step failure**: any `signtool` invocation that returns
  non-zero is caught by `Invoke-NativeChecked` and aborts the build

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

# 5. SBOM validation
cyclonedx-py-validate "SBOM.cyclonedx.json"

# 6. SBOM SHA-256 + manifest cross-check
Get-FileHash "SBOM.cyclonedx.json" -Algorithm SHA256
# Compare to MANIFEST.json::sbom_sha256
```

The MSI must:
- Pass `signtool verify /pa /all /v`. **Expiry is checked against the
  signing time** (proved by the RFC 3161 timestamp), not the current
  clock. **Revocation** is a separate concern (CRL/OCSP); if the
  signing CA provides an AIA + OCSP responder, use `signtool verify
  -rpc` to fetch the live response.
- Match the SHA-256 in `SHA256SUMS.txt`
- Have a non-revoked publisher certificate
- Include a valid RFC 3161 timestamp
- The SBOM must validate against the CycloneDX schema (`cyclonedx-py-validate` exits 0) and the SBOM SHA-256 must match `MANIFEST.json::sbom_sha256`

---

## 0.ad Build provenance

Every release artifact is accompanied by:

- Exact pinned versions (Python 3.14.x, PyInstaller 6.x, WiX 4.x, Windows SDK, signtool, .NET SDK, syft, cyclonedx-py-validate)
- Source commit SHA on the build branch
- Locked `requirements.lock` hash
- Build-host identifier (operator-bound)
- Build timestamp (UTC)
- `MANIFEST.json` with version + SHA-256 + signer policy + timestamp + SBOM provenance (filename + SHA-256 + generator version + validator version + validator result)
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
3. **Re-verify the ACL** matches `D:P(A;;FA;;;SY)(A;;FA;;;BA)` (no Users ACE):
   ```powershell
   $acl = Get-Acl $secretFile
   $acl.Sddl   # should contain O:BAG:SYD:(A;;FA;;;SY)(A;;FA;;;BA)
   ```
4. **Provision the real `config.yaml`** (copied from `config.yaml.example`):
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

## 0.af SBOM tool binding (v0.5: bound)

| Property | Value (pinned; operator can change by editing build.ps1) |
|---|---|
| Tool | `syft` |
| Version | `1.x` latest at plan time (e.g. `v1.18.0`); operator pins exact version in `build.ps1` |
| Output format | `cyclonedx-json` |
| Command | `syft scan dir:<publishDir> --output cyclonedx-json=<outFile>` |
| Output filename | `SBOM.cyclonedx.json` (operator-bound in `build.ps1`) |
| Validator | `cyclonedx-py-validate` |
| Validator version | `0.x` latest at plan time (e.g. `v0.5.0`); operator pins exact version in `build.ps1` |
| Validator command | `cyclonedx-py-validate <sbomFile>` (exits 0 on valid) |
| Failure handling | `Invoke-NativeChecked` aborts the build on non-zero exit (generator or validator) |
| Manifest fields | `sbom_filename`, `sbom_sha256`, `sbom_generator_version`, `sbom_validator_version`, `sbom_validator_result` |

---

## 0.4 Secret-preservation state table (v0.5: PRESERVE on uninstall)

| # | State | Required behavior | VM test required |
|---|---|---|---|
| 1 | Fresh MSI install | Create zero-byte placeholder; service is demand-start and **refuses to run** until real config + secret are written | ✓ |
| 2 | Secret provisioned after install | Preserve it through `Repair` and `MajorUpgrade` | ✓ |
| 3 | Upgrade with missing secret (operator deleted) | **Do not silently recreate**; service stays demand-start; health gate fails closed | ✓ |
| 4 | Uninstall (v0.5 default) | **PRESERVE** the secret file. MSI removes program files + service registration only. Explicit privileged cleanup script required to remove | ✓ |
| 5 | Reinstall after uninstall | Placeholder returns; previously-provisioned secret is still present (because uninstall preserved it) | ✓ |

`NeverOverwrite="yes"` on `OrchClientSecretFileComponent` enforces
(2), (3), (5) at the MSI level. (1) and (4) require post-install VM
tests.

---

## 0.4-bis Config-preservation state table (v0.5: `config.yaml` not in MSI)

| # | State | Required behavior | VM test required |
|---|---|---|---|
| 1 | Fresh MSI install | Drops `config.yaml.example` only; **no `config.yaml`** | ✓ |
| 2 | Operator provisions `config.yaml` (per `§0.ae` step 4) | Real `config.yaml` is created by the operator, **not** by the MSI | ✓ |
| 3 | Repair | `config.yaml` is **untouched** (it is not in any MSI component) | ✓ |
| 4 | Major upgrade | `config.yaml` is **untouched**; `config.yaml.example` may be updated by the new MSI | ✓ |
| 5 | Uninstall (v0.5 default) | `config.yaml` is **preserved**. MSI removes only the program files and the MSI-owned template (`config.yaml.example` is **also** preserved on uninstall in v0.5; explicit cleanup script required to remove either file) | ✓ |

---

## 1. Goal

Produce:

1. A runnable **orch client** (Python) that signs HMAC-authenticated
   heartbeats and self-enrolls against the orchestrator's enrollment
   endpoint (B12-deployed contract; TBD until B12 is reviewed).
2. A separate **`OrchClientDoctor.exe`** (console-enabled) for
   dry-run / config validation / signature / ACL checks.
3. A **code-signed Windows MSI** built with PyInstaller (`.spec`
   source of truth, with **separate `Analysis` for service and
   doctor entry points**) + WiX 4 + `HarvestDirectory` task.
4. A **SHA-256 + real CycloneDX SBOM + signing manifest** for
   operator handoff.
5. An **updated install runbook** that references the real artifacts
   (not illustrative filenames).

The MSI install shall:

- Drop `OrchClient.exe` (service) and `OrchClientDoctor.exe` (console)
  under `C:\Program Files\HermesOrchClient\`
- Register a Windows Service named `OrchClient` (start = demand)
- Drop `config.yaml.example` (with placeholder URL) at
  `C:\ProgramData\HermesOrchClient\config.yaml.example` (locked ACL
  `SYSTEM:F, Admins:F, Users:R`); the real `config.yaml` is
  **operator-provisioned**, not in the MSI
- Drop a zero-byte placeholder secret at
  `C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin` with
  `NeverOverwrite="yes"` and ACL `D:P(A;;FA;;;SY)(A;;FA;;;BA)`
- Lock the parent directories with matching SDDLs (`util:PermissionEx`)
- **Not** auto-start the service on install
- For the first release, set service `FirstFailure=SecondFailure=ThirdFailure=none` (no restart loop on unenrolled / zero-secret state)
- On uninstall, **PRESERVE** `config.yaml` and `agent-secret.bin` (default; explicit cleanup script required to remove)

---

## 1.4 Server-side HMAC key-id-to-agent authorization rule (new in v0.5)

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

Without this rule, the body is cryptographically protected but
the key-to-agent authorization is implicit. An attacker who learns
a `key-id` could use it to sign requests for an agent they do not
control; the cryptographic check would pass.

---

## 2. Deliverables

| # | Artifact | Path |
|---|---|---|
| 1 | Orch client source | `C:\Project\minimax code\hermes-orchestrator\src\orch_client\__init__.py` + `__main__.py` + `doctor.py` + `client.py` + `hmac_auth.py` + `config.py` + `logging_setup.py` + `service.py` |
| 2 | `pyproject.toml` entry points: `orch-client` (service) + `orch-client-doctor` (console) | extend existing `C:\Project\minimax code\hermes-orchestrator\pyproject.toml` |
| 3 | Locked dependency file | `C:\Project\minimax code\hermes-orchestrator\installer\requirements.lock` (every pip package hash) |
| 4 | PyInstaller spec (single source of truth, with **separate Analysis per entry point**) | `C:\Project\minimax code\hermes-orchestrator\installer\orch-client.spec` |
| 5 | WiX 4 `.wixproj` + manually-authored `.wxs` | `C:\Project\minimax code\hermes-orchestrator\installer\orch-client.wixproj` + `orch-client.wxs` |
| 6 | Build script | `C:\Project\minimax code\hermes-orchestrator\installer\build.ps1` |
| 7 | Built MSI | `C:\Project\minimax code\hermes-orchestrator\dist\OrchClient-v0.1.0-x64.msi` |
| 8 | SHA-256 + manifest + real SBOM | `dist\SHA256SUMS.txt` + `dist\MANIFEST.json` + `dist\SBOM.cyclonedx.json` |
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

### 3.1 `hmac_auth.py` (unchanged from v0.3)

- **Body hash** = `SHA-256(exact raw UTF-8 request-body bytes)`, hex-lowercase
- **Signing input** = `protocol-version || "\n" || key-id || "\n" || timestamp || "\n" || nonce || "\n" || method || "\n" || canonical-path || "\n" || body_sha256`
- **Signature** = `HMAC-SHA256(secret, UTF-8(signing_input))`, hex-lowercase
- **Server validation**: see `§1.4` for the new key-id-to-agent rule
- **key-id** is a real rotation identifier, not necessarily `agent_id`

### 3.2 `client.py` (unchanged)

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

### 3.4 `config.py` (clarified in v0.5)

- `config.yaml.example` is the **template** that ships in the MSI
  (with placeholder URL + comments)
- The real `config.yaml` is **operator-provisioned** after install
  (per `§0.ae`); the MSI never creates it, never references it, and
  never overwrites it
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

### 3.5 `doctor.py` (separate entry point from v0.4)

`doctor.py` is a **console-enabled** Python entry point with its own
`Analysis` in the `.spec`. It performs **read-only** diagnostics:

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
because a release service EXE has no console for stdout. The two
EXEs share a frozen `COLLECT` tree but each has its own `Analysis`,
`PYZ`, and `EXE` block (see `§4`).

### 3.6 Service-dispatcher VM validation list (unchanged from v0.3)

Verified on a clean Windows 10/11 VM (the 7 items).

### 3.7 Doctor-binary VM validation list (unchanged from v0.4)

Verified on a clean Windows 10/11 VM (the 5 items).

---

## 4. PyInstaller spec (Deliverable 4) — separate Analysis per entry point (v0.5 fix)

`.spec` is the **only** build-time source of truth. The build script
calls `pyinstaller --clean --noconfirm installer\orch-client.spec`.

`orch-client.spec` (v0.5 — separate Analysis per entry point):

```python
# -*- mode: python ; coding: utf-8 -*-
import sys ; sys.setrecursionlimit(5000)
from PyInstaller.utils.hooks import collect_submodules, collect_data_files
datas = collect_data_files('orch_client')

# ---- Service analysis: __main__ → SCM-launched, no console ----
service_hidden = collect_submodules('orch_client') + [
    'win32serviceutil',
    'win32service',
    'win32event',
    'servicemanager',
]
service_a = Analysis(
    ['..\\src\\orch_client\\__main__.py'],
    pathex=['..\\src'],
    hiddenimports=service_hidden,
    datas=datas,
    excludes=['tkinter', 'unittest', 'pydoc'],
    noarchive=False,
)
service_pyz = PYZ(service_a.pure)

# ---- Doctor analysis: doctor.py → operator-launched, console-enabled ----
doctor_hidden = collect_submodules('orch_client')  # no pywin32 needed
doctor_a = Analysis(
    ['..\\src\\orch_client\\doctor.py'],
    pathex=['..\\src'],
    hiddenimports=doctor_hidden,
    datas=datas,
    excludes=['tkinter', 'unittest', 'pydoc'],
    noarchive=False,
)
doctor_pyz = PYZ(doctor_a.pure)

# ---- Two EXEs from two distinct Analyses ----
service_exe = EXE(
    service_pyz, service_a.scripts, [],
    exclude_binaries=True,
    name='OrchClient',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,         # service EXE: no console
)
doctor_exe = EXE(
    doctor_pyz, doctor_a.scripts, [],
    exclude_binaries=True,
    name='OrchClientDoctor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # doctor EXE: console-enabled
)

# ---- COLLECT merges the two payload trees ----
coll = COLLECT(
    service_exe, doctor_exe,
    service_a.binaries, service_a.zipfiles, service_a.datas,
    doctor_a.binaries,  doctor_a.zipfiles,  doctor_a.datas,
    strip=False, upx=False, upx_exclude=[],
    name='OrchClient',
)
```

The build script invokes the spec:

```powershell
pyinstaller --clean --noconfirm installer\orch-client.spec
```

**No CLI flags** for frozen-bundle behavior. The spec is the source
of truth.

---

## 5. WiX 4 source (Deliverable 5) — revised in v0.5

**Key changes vs v0.4**:
- **OutputName** and **OutputPath** declared explicitly in `.wixproj`
- Every manually-authored `<Component>` is wrapped in an explicit
  `<ComponentGroup>` with `<ComponentRef>` (so the `<ComponentGroupRef>`
  in the `<Feature>` resolves)
- `OrchClientConfigDirComponent` (directory) + `OrchClientConfigExampleComponent`
  (file) are in `OrchClientConfig` group
- `OrchClientSecretDirComponent` + `OrchClientSecretFileComponent` are
  in `OrchClientSecret` group
- `OrchClientServiceComponent` is in `OrchClientService` group
- `OrchClientDoctorComponent` is in `OrchClientDoctor` group
- `config.yaml.example` is the only MSI-owned config file; the
  operator-owned `config.yaml` is not in any MSI component

### 5.1 `orch-client.wixproj` (revised in v0.5)

```xml
<Project Sdk="WixToolset.Sdk/4.0">
  <PropertyGroup>
    <OutputType>Package</OutputType>
    <Version>0.1.0</Version>
    <Platform>x64</Platform>

    <!-- Real MSBuild properties -->
    <PublishDir>$(MSBuildProjectDirectory)\..\dist\OrchClient</PublishDir>
    <ConfigTemplateDir>$(MSBuildProjectDirectory)\templates\config</ConfigTemplateDir>
    <SecretTemplateDir>$(MSBuildProjectDirectory)\templates\secret</SecretTemplateDir>

    <!-- v0.5: explicit MSI output policy (deterministic filename) -->
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

### 5.2 `orch-client.wxs` (revised in v0.5 — ComponentGroups wrap every manual Component)

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
      <ComponentGroupRef Id="OrchClientService" />   <!-- manual -->
      <ComponentGroupRef Id="OrchClientDoctor" />    <!-- manual (new in v0.4) -->
      <ComponentGroupRef Id="OrchClientConfig" />    <!-- manual -->
      <ComponentGroupRef Id="OrchClientSecret" />    <!-- manual -->
    </Feature>
  </Package>

  <!-- Manually authored service component + group wrap (v0.5) -->
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

  <!-- Manually authored doctor component + group wrap (v0.5) -->
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

  <!-- Manually authored config + secret components + group wraps (v0.5) -->
  <Fragment>
    <StandardDirectory Id="ProgramDataFolder">
      <Directory Id="CONFIGFOLDER" Name="HermesOrchClient">
        <Component Id="OrchClientConfigDirComponent" Guid="*" KeyPath="yes">
          <CreateFolder>
            <util:PermissionEx
              Sddl="D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)(A;;FX;;;BU)" />
          </CreateFolder>
        </Component>

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

Notes (v0.5):
- Every manually-authored component is wrapped in an explicit
  `<ComponentGroup>` with `<ComponentRef>`
- `<Feature>` references resolve correctly
- Directory key-paths use `CreateFolder` (deliberate, not inferred)
- Secret file `NeverOverwrite="yes"` preserves provisioned secret through repair/upgrade
- `config.yaml.example` is **also** a normal MSI-owned file (not `NeverOverwrite`; the next MSI version replaces it as part of upgrade — see §0.z for the v0.5 uninstall behavior)

---

## 6. Build script (Deliverable 6) — v0.5

`build.ps1` (key v0.5 changes: bound `syft` + `cyclonedx-py-validate` versions, real SBOM provenance in manifest, drop `config_uninstall_behavior` / `secret_uninstall_behavior` since v0.5 default is PRESERVE for both):

```powershell
$ErrorActionPreference = 'Stop'

# ----- Bound operator values -----
$RepoRoot      = 'C:\Project\minimax code\hermes-orchestrator'
$PyInstaller   = 'pyinstaller'
$WixProject    = Join-Path $RepoRoot 'installer\orch-client.wixproj'
$SpecFile      = Join-Path $RepoRoot 'installer\orch-client.spec'
$TimestampUrl  = 'http://timestamp.digicert.com'
$CertThumb     = '<TBD by operator — code-signing cert thumbprint>'
$Version       = '0.1.0'
$Arch          = 'x64'
$ExpectedMsi   = "OrchClient-v${Version}-${Arch}.msi"

# Bound SBOM tool (v0.5 §0.af) — pinned versions
$sbomGen       = 'syft'
$sbomVersion   = 'v1.18.0'                                  # operator-pinned
$sbomArgs      = @('scan',"dir:$($RepoRoot)\dist\OrchClient",
                   '--output',"cyclonedx-json=$($RepoRoot)\dist\SBOM.cyclonedx.json")
$sbomValidate  = 'cyclonedx-py-validate'
$sbomValidatorVersion = 'v0.5.0'                           # operator-pinned
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

# 0) Verify locked dependency file matches
Invoke-NativeChecked -FilePath 'pip' `
    -Arguments @('install','--require-hashes','-r',
                  (Join-Path $RepoRoot 'installer/requirements.lock')) `
    -Label 'pip install --require-hashes'

# 1) PyInstaller — .spec is the single source of truth (no CLI args)
$publishDir = Join-Path $RepoRoot 'dist\OrchClient'
Remove-Item -LiteralPath $publishDir -Recurse -Force -ErrorAction SilentlyContinue
Invoke-NativeChecked -FilePath $PyInstaller `
    -Arguments @('--clean','--noconfirm',$SpecFile) `
    -Label 'pyinstaller (spec-driven)'

# 2) Sign owned EXEs
$ownedExes = @(Get-ChildItem -Recurse -File -Path $publishDir |
    Where-Object { $_.Name -in @('OrchClient.exe','OrchClientDoctor.exe') })
foreach ($f in $ownedExes) {
    Invoke-NativeChecked -FilePath 'signtool.exe' `
        -Arguments @('sign','/fd','SHA256','/tr',$TimestampUrl,'/td','SHA256',
                      '/sha1',$CertThumb,$f.FullName) `
        -Label "signtool sign $($f.Name)"
}

# 3) Build MSI via `dotnet build` (SDK-style .wixproj with explicit OutputName + OutputPath)
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

# 6) Compute SHA-256 + write manifest
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
    # Note: v0.5 default is PRESERVE on uninstall for both config.yaml and
    # agent-secret.bin; no per-MSI behavior field is needed in the manifest.
} | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText(
    (Join-Path $msiDir 'MANIFEST.json'),
    $manifest,
    [System.Text.UTF8Encoding]::new($false))
"$hash  $ExpectedMsi" |
    Out-File -LiteralPath (Join-Path $msiDir 'SHA256SUMS.txt') -Encoding utf8

# 7) Real CycloneDX SBOM (v0.5 §0.af)
Invoke-NativeChecked -FilePath $sbomGen `
    -Arguments $sbomArgs `
    -Label "SBOM generator ($sbomGen $sbomVersion)"
$sbomHash = (Get-FileHash -LiteralPath $sbomOut -Algorithm SHA256).Hash

# 8) Validate the SBOM
$sbomValidateResult = & $sbomValidate $sbomOut 2>&1
$sbomValidateExit   = $LASTEXITCODE
if ($sbomValidateExit -ne 0) {
    throw "SBOM validator ($sbomValidate $sbomValidatorVersion) failed (exit $sbomValidateExit): $sbomValidateResult"
}

# 9) Add SBOM provenance to MANIFEST.json
$manifest.sbom_filename         = Split-Path -Leaf $sbomOut
$manifest.sbom_sha256           = $sbomHash
$manifest.sbom_generator        = $sbomGen
$manifest.sbom_generator_version = $sbomVersion
$manifest.sbom_validator        = $sbomValidate
$manifest.sbom_validator_version = $sbomValidatorVersion
$manifest.sbom_validator_result = 'pass'
[System.IO.File]::WriteAllText(
    (Join-Path $msiDir 'MANIFEST.json'),
    ($manifest | ConvertTo-Json -Depth 6),
    [System.Text.UTF8Encoding]::new($false))

Write-Host "[+] Build complete: $msiPath"
Write-Host "[+] MSI SHA-256: $hash"
Write-Host "[+] SBOM: $sbomOut (sha256: $sbomHash)"
```

**Sign order** (v0.5): owned EXEs (`OrchClient.exe` + `OrchClientDoctor.exe`) → MSI → verify → final SHA-256 → manifest → SBOM → validate → re-write manifest with SBOM provenance. Each step is `Invoke-NativeChecked`-gated.

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
| 8 | Orchestrator `<ORCHESTRATOR_FQDN>` and `<HTTPS_PORT>` (B13-transport-closed) | operator | not yet bound; MSI ships `config.yaml.example` containing placeholder |
| 9 | SBOM generator version + validator version | operator | **bound in v0.5**: `syft v1.18.0` + `cyclonedx-py-validate v0.5.0`; operator can change in `build.ps1` |
| 10 | Cert renewal policy + revoked-cert response | operator | bind at runtime |
| 11 | UpgradeCode GUID (fixed across versions) + ProductCode rotation per version | operator | bind at runtime |
| 12 | Test case matrix | operator | new in v0.5; to be drafted separately |
| 13 | VM test environment (clean Windows 10/11) for §3.6, §3.7, §0.4, §0.4-bis | operator | required before implementation approval |
| 14 | Explicit privileged cleanup script (out of scope; follow-up) | operator | for removing `config.yaml` / `agent-secret.bin` on uninstall (v0.5 default: PRESERVE) |

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
- ❌ No repair / upgrade that clobbers a provisioned secret (`NeverOverwrite="yes"` on the MSI-owned secret file; the operator-owned `config.yaml` is not in any MSI component)
- ❌ No uninstall that removes `config.yaml` or `agent-secret.bin` (v0.5 default: PRESERVE)
- ❌ No blind re-signing of third-party DLLs that already carry a valid Authenticode signature
- ❌ No labelling a hand-rolled JSON as SPDX or CycloneDX
- ❌ No CLI args for frozen-bundle behavior; `.spec` is the single source of truth
- ❌ No manual `OrchClientFiles` group; `HarvestDirectory` is the only source
- ❌ No requiring unexpired cert at verify time; expiry is checked against the signing-time window (proved by RFC 3161 timestamp)
- ❌ No single `Analysis` for two EXEs with different entry points (v0.5 fix)
- ❌ No unwrapped `<Component>` in WiX (v0.5 fix; every manual component has a `<ComponentGroup>` wrap)

---

## 9. Acceptance criteria

A. The MSI installs cleanly on a clean Windows 10/11 VM:
   - File system layout matches §1
   - `OrchClient.exe` (service) and `OrchClientDoctor.exe` (console) are both present, each from its own `Analysis`
   - ACL on `config.yaml.example`: `SYSTEM:F, Admins:F, Users:R`
   - ACL on config directory: `SYSTEM:F, Admins:F, Users:R/X`
   - ACL on `agent-secret.bin`: `SYSTEM:F, Admins:F` (no Users)
   - ACL on secret directory: `SYSTEM:F, Admins:F` (no Users)
   - Service is registered with `Start=Demand`, `State=Stopped`
   - Service is NOT auto-started
   - Service refuses to start when `agent-secret.bin` is zero bytes, `orchestrator_url` is the placeholder / HTTP / missing, secret file ACL is wrong, or `config.yaml` is missing
   - `OrchClientDoctor.exe` runs from an elevated PowerShell with a console window and refuses to run if any precondition fails

B. The MSI is code-signed:
   - `signtool verify /pa /all /v` reports valid (with RFC 3161 timestamp)
   - Expiry is checked against the signing-time window, not the current clock
   - SHA-256 in `SHA256SUMS.txt` matches `Get-FileHash`
   - `MANIFEST.json` is consistent with the MSI
   - SBOM is a real CycloneDX 1.6 document, validated by `cyclonedx-py-validate` (exits 0), and the SHA-256 matches `MANIFEST.json::sbom_sha256`
   - Owned payload files are signed; third-party DLLs retain their own signatures (not blindly re-signed)

C. The doctor binary works:
   - `OrchClientDoctor.exe check-config` — passes for a well-formed `config.yaml`
   - `OrchClientDoctor.exe check-secret` — passes when `agent-secret.bin` exists, non-empty, ACL matches
   - `OrchClientDoctor.exe dry-run` — prints canonical headers + signature + raw body bytes to stdout
   - `OrchClientDoctor.exe check-signature --msi <path>` — verifies the MSI

D. Secret-preservation behavior (5 states, all VM-tested):
   - Fresh install → zero-byte placeholder; service refuses to run
   - Secret provisioned → preserved through repair and major upgrade
   - Upgrade with missing secret → service fails closed
   - **Uninstall → PRESERVED** (v0.5 default; explicit cleanup script required to remove)
   - Reinstall after uninstall → previously-provisioned secret still present (because uninstall preserved it)

E. Config-preservation behavior (5 states, all VM-tested): per §0.4-bis
   - Fresh install → only `config.yaml.example`; no `config.yaml`
   - Operator provisions `config.yaml` (per `§0.ae` step 4)
   - Repair → `config.yaml` untouched
   - Major upgrade → `config.yaml` untouched; `config.yaml.example` may be updated
   - **Uninstall → PRESERVED** (v0.5 default)

F. HMAC server validation (documented, not implemented in this plan):
   - All 9 server validation steps from v0.3 §3.1
   - Plus key-id-to-agent authorization rule from §1.4

G. The updated runbook uses the real MSI filename and references §7 binding values

H. No side-effecting action against the live orchestrator

I. §3.6 + §3.7 VM validation lists: every checkbox verified on a clean Windows 10/11 VM

J. The test case matrix covers: offline, cert mismatch, missing-secret, invalid-secret, repair, major upgrade, uninstall (PRESERVE behavior), partial-install, revocation, payload allowlist failure, SBOM schema validation, signing round-trip

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

## 11. v0.1 → v0.5 changelog (cross-reference)

| # | Section | v0.1 | v0.2 | v0.3 | v0.4 | v0.5 |
|---|---|---|---|---|---|---|
| 1 | §3.1 HMAC | "JSON fixed key order" | raw-body + bound (contradictory) | bound-metadata model | (unchanged) | (unchanged) |
| 2 | §1 orchestrator_url | (n/a) | HTTP | HTTPS placeholder | (unchanged) | (unchanged) |
| 3 | §4 .spec entry point(s) | (n/a) | single (CLI) | single (CLI) | single (.spec) but **same Analysis for both EXEs** | **two separate Analyses per entry point** |
| 4 | §5 WiX KeyPath | (n/a) | on `<Component>` | on `<File>` | (unchanged) | (unchanged) |
| 5 | §5 WiX ComponentGroup wrap | (n/a) | missing | manual wrap | (unchanged) | **every manual Component wrapped in ComponentGroup with ComponentRef** |
| 6 | §5 WiX harvest | `<Files Include>` | `<HarvestDirectory>` | (same) | (same) | (same) |
| 7 | §5 WiX directory ACLs | (missing) | (missing) | added | (unchanged) | (unchanged) |
| 8 | §5 WiX MajorUpgrade | (missing) | (missing) | Disallow="yes" | + `UpgradeErrorMessage` | (unchanged) |
| 9 | §5 WiX doctor EXE | (missing) | (missing) | (missing) | added component | (unchanged) |
| 10 | §5 WiX PublishDir | (n/a) | DefineConstants only | (same) | real PropertyGroup | (unchanged) + `OutputName` + `OutputPath` |
| 11 | §5 WiX MSI output policy | (n/a) | (n/a) | (n/a) | (missing) | `OutputName=OrchClient-v$(Version)-$(Platform)`, `OutputPath=.../dist/` |
| 12 | §3.5 doctor | (n/a) | service EXE stdout | service EXE stdout | separate OrchClientDoctor.exe | (unchanged) + **separate Analysis per entry point** |
| 13 | §3.4 config lifecycle | (n/a) | installer ships config.yaml | installer ships config.yaml | installer ships config.yaml.example | (unchanged) + explicit "config.yaml NOT in any MSI component" wording |
| 14 | §0.4 secret-preservation | (n/a) | 4-state table | VM tests per state | + `config.yaml` NeverOverwrite | **default: PRESERVE on uninstall**; no `secret_uninstall_behavior` field |
| 15 | §0.af SBOM | (missing) | hand-rolled JSON | real generator | bound `syft` + `cyclonedx-py-validate` as example | **bound: `syft v1.18.0` + `cyclonedx-py-validate v0.5.0`; manifest adds sbom_filename/sha256/generator_version/validator_version/result** |
| 16 | §0.x Python version | (n/a) | 3.12 | 3.12 | 3.12 + payload allowlist says `python314.dll` | **3.14.x consistent; "LTS" wording dropped** |
| 17 | §1.4 HMAC key-id-to-agent | (n/a) | (n/a) | (n/a) | (n/a) | **server-side rule: key-id → authorized agent_id; reject if body.agent_id mismatches** |
| 18 | §3.6 + §3.7 VM validation | (missing) | (missing) | service-dispatcher | + doctor-binary | (unchanged) |
| 19 | §7 dependencies | 7 | 10 | 13 | 14 | **14** (removed "config_uninstall_behavior" from required binding) |
| 20 | §8 forbidden actions | 7 | 8 | 11 | 15 | **17** (added "no uninstall that removes config.yaml/agent-secret.bin", "no single Analysis for two EXEs", "no unwrapped Component") |
| 21 | §9 acceptance | 6 | 8 | 10 | 11 | **10** (preserved, with v0.5 default PRESERVE behavior) |

---

## 12. Outstanding Perplexity questions for v0.6 (if any)

The v0.5 review addressed all 6 v0.4 implementation blockers and 2
smaller issues. Remaining open items are operator-binding:

- Operator must pick `agent_id` and `key-id` for the target machine
- Operator must pick the code-signing cert / Azure Trusted Signing
- Operator must install WiX 4 + `dotnet` SDK on the build host
- Operator must generate the target machine's HMAC secret (out-of-band)
- Operator must generate the UpgradeCode GUID
- Operator must run the §3.6 + §3.7 + §0.4 + §0.4-bis VM tests, and
  produce a signed report before implementation approval
- Operator must draft the §9.J test case matrix (separate work item,
  not a code change)
- Operator must bind the `syft` and `cyclonedx-py-validate` versions
  in `build.ps1` (defaults provided)
- Operator must draft the **explicit privileged cleanup script**
  (out of scope for v0.5; tracked as a follow-up) for removing
  `config.yaml` / `agent-secret.bin` on uninstall (v0.5 default
  PRESERVE; cleanup script is the only supported way to remove)
