# Orch Client Build — Implementation Plan v0.7 (final plan-only iteration) + cert-pinning patch

**Date:** 2026-08-13
**Status:** PROPOSAL for review (Perplexity + operator) — v0.7 + cert-pinning patch
**Supersedes:** v0.6 (commit `aa49a71` on branch `proposal/orch-client-build-impl-plan-v0.1`)
**Scope:** end-to-end build of a Windows MSI installer for the new orch
client, on the orchestrator host (Windows A). The MSI will be carried
to a separate target Windows machine for installation; this plan does
NOT include remote install, agent enrollment against a live orchestrator,
or B12 deploy. Enrollment is verified separately after the operator
manually runs the install on the target.

**Note:** v0.7 is the **last plan-only iteration** before operator
binding. The acceptance gate is the §3.6 + §3.7 + §0.4 + §0.4-bis
VM test matrix on a clean Windows 10/11 target. Perplexity's review
is a planning aid, not a release gate; further doc-level iterations
would delay VM testing without adding safety. The **cert-pinning
patch** (see §0.bis) is the only post-finality amendment: it adds
the missing TLS cert verification design (§1.6 + §7 #14 + §12 #0)
without re-opening the 7-row v0.6 → v0.7 review loop.

---

## 0. v0.6 → v0.7 changelog (final plan-only corrections)

Perplexity re-read PR #4 after the v0.6 commit and reported: 2 critical
implementation blockers + 4 smaller issues + 1 design gap. v0.7
addresses all 7. **This is the final plan-only iteration.**

| # | v0.6 said | v0.7 says | Reason |
|---|---|---|---|
| 1 | Both `.spec` files used `COLLECT(..., name='OrchClient')` — both wrote to the **same** `dist\OrchClient\` directory, so the second invocation could overwrite or collide with the first | The two specs use **distinct COLLECT names**: `name='OrchClient-service'` (service spec) and `name='OrchClient-doctor'` (doctor spec). The build script also asserts both output directories exist before merging. `--distpath` is an acceptable alternative but the spec-side rename is the single source of truth | Two specs that target the same `dist` subdirectory are not independent. Distinct `COLLECT` names produce distinct `--onedir` outputs that the merge step can deterministically combine |
| 2 | "`NeverOverwrite` on the secret file preserves the secret on uninstall because the WiX source has no `RemoveFile` / `RemoveFolder` directives" | **Wrong**: Windows Installer removes files installed by `InstallFiles` when their components are removed at uninstall. `NeverOverwrite` protects install / repair / upgrade, not uninstall. **v0.7 picks Option A**: the secret component is **explicitly permanent** — the secret file is in its own component with no `RemoveFile` directive AND the component is **not** in any feature that is removed on uninstall. The major-upgrade implication is documented and tested | A claim of preservation must be represented in the MSI authoring and proven on a clean VM. The "permanent component" pattern is the documented way to keep a file across uninstall in WiX |
| 3 | The dedup stage `if (Test-Path $targetPath) { return }` silently accepts **conflicting** files (different content with the same relative path) | Replaced with a **hash-equality gate**: if the path exists, compute SHA-256 of both the existing and incoming files; if they differ, **throw** "Merge conflict: $relPath content differs". The release-directory allowlist additionally declares per-file role: `service-only` / `doctor-only` / `shared-identical` | A silent skip on conflict hides real build-environment issues. A hash-equality gate fails closed and surfaces the problem at build time |
| 4 | Preflight gate checked only `python` / `pyinstaller` / `syft` / `cyclonedx-py-validate`; `dotnet` / `wix` / `signtool` were declared in MANIFEST.json but not enforced | **All declared versions are now preflight-enforced**: `python -m pip --version`, `python -m PyInstaller --version` (using the same interpreter as `python --version`), `dotnet --version`, `wix --version`, `signtool.exe` (recorded with full path + Windows SDK install version), `syft version`, `cyclonedx-py-validate --version`. The build also binds Python / pip / PyInstaller to the same interpreter via `python -m ...` invocation | Recording an unverified version in MANIFEST.json is a documentation bug. The preflight gate is the only way to enforce the version claim |
| 5 | `MANIFEST.json` lacked `source_commit_sha` and `requirements_lock_sha256` | Both are now captured at build time: `source_commit_sha` from `git rev-parse HEAD`; `requirements_lock_sha256` from `Get-FileHash` on `installer/requirements.lock`. Both are part of the read-back + parse + assert gate | A release manifest without source commit + lockfile hash cannot be reproduced or audited |
| 6 | The template copy stage in `build.ps1` copies templates into the final PyInstaller release directory, but the WiX `<File Source="$(var.ConfigTemplate)\...">` sources templates from a separate path | **Single authoritative source**: `installer/templates/` is the only template location. `build.ps1` does NOT copy templates into the PyInstaller release directory (templates are package-time inputs, not bundle-time). The `installer/templates/config/config.yaml.example` and `installer/templates/secret/agent-secret.bin` paths are referenced by both `build.ps1` (for SHA-256 in `MANIFEST.json`) and the WiX source (for the MSI file source) | Two sources of the same file invite drift. The PyInstaller release directory carries the binaries; the MSI package sources templates at build time |
| 7 | HMAC signing input binds `method` + `canonical-path` + `body_sha256`, but does **not** bind the query string. A request with a `?foo=bar` query can pass the same auth as the canonical path-only version | **HMAC v0.7: query strings are forbidden on signed endpoints**. The client and server both reject any request with a query string. This is the simpler and safer policy. The alternative (bind a canonical query string) is documented as a future option if a real use case for query-string-based endpoints emerges | A signed request without query-string binding is replayable against any endpoint that takes a query string with the same body. The "no query strings" policy is the safest default |

All v0.6 sections preserved where not directly affected. Section
numbering kept stable. New sections added for the per-file-role
allowlist, the full preflight gate, the template-source consistency
note, and the HMAC query-string policy.

---

## 0.bis v0.7 cert-pinning patch (2026-08-13)

After v0.7 was declared the **final plan-only iteration**, an operator
review surfaced a real gap: the plan mandates HTTPS for the
orchestrator URL but does not say how the **new** orch client (this
MSI) verifies the orch server's TLS certificate. v0.7 is preserved
as the final doc-level iteration; this patch adds the missing
cert-verification design without re-opening the 7-row review loop.

| Change | Where | Why |
|---|---|---|
| New `## 1.6 Server-side TLS cert + client-side cert fingerprint pinning` | After §1.5 | Pins the cert-verification strategy: server uses the existing v3.12.0 `hermes-orch gen-cert` (self-signed RSA-2048, 365-day, SANs = hostname/localhost/127.0.0.1); each new agent's `config.yaml` carries `orchestrator_ca_fingerprint_sha256`; agent TLS client uses `CERT_REQUIRED` + post-handshake SHA-256 compare; mismatch → fail closed (no `verify=False`, no OS trust store, no `INSECURE_SKIP_TLS_VERIFY`) |
| New `§7 #14` operator-binding dependency | §7 table | Adds "Orchestrator cert SHA-256 fingerprint (per agent, for client-side pinning)" as an operator-bound prerequisite. The v3.12.0 `gen-cert` does not currently print the fingerprint — recommended follow-up: add `--print-fingerprint` so operators do not have to remember the `openssl x509 -fingerprint -sha256` incantation |
| New `§12 #0` operator-binding step | §12 prerequisites list | Adds "Operator runs `hermes-orch gen-cert` on the orch host, captures the cert fingerprint" as the **first** step in the operator-binding phase (before agent_id / cert / build host) — because every new agent's `config.yaml` needs the fingerprint at deployment time |
| Defense-in-depth note in §1.6 | §1.6 | TLS (transport) + fingerprint (MITM) + HMAC (request forgery, §1.4) + Origin/CSRF (browser session, B12/R14) — four independent layers |
| Cross-reference to runbook | §1.6 footer | `docs/runbooks/orch-client-install-runbook.md` Step 5a (config.yaml field) + Before-you-start checklist + Troubleshooting are the operator-facing surfaces for §1.6 |

**Forbidden (per §1.6):** shipping a real fingerprint in the MSI
template, hard-coding a fingerprint in
`installer/templates/config/config.yaml.example`, shipping the cert
file inside the MSI. The fingerprint is always operator-input at
deployment time.

**No other sections of v0.7 are affected.** Section numbering kept
stable. The 7-row v0.6 → v0.7 changelog above remains the canonical
"final iteration" record. This patch is the only post-finality
amendment.

---

## 0.x Pinned versions (exact, all preflight-gate enforced)

| Tool | Exact version | Preflight command |
|---|---|---|
| Python | **3.14.0** | `python --version` |
| pip (same env as Python) | per `pip --version` output | `python -m pip --version` |
| PyInstaller (same env as Python) | **6.16.0** | `python -m PyInstaller --version` |
| WiX Toolset | **4.0.6** | `wix --version` |
| Windows SDK (signtool) | **10.0.22621.4031** | `signtool.exe` (record full path + Windows SDK install version) |
| .NET SDK | **8.0.404** | `dotnet --version` |
| RFC 3161 timestamp URL | per operator binding (default: `http://timestamp.digicert.com`) | (operator-bound; recorded in MANIFEST.json) |
| Code-signing cert thumbprint | per operator binding | (operator-bound; recorded in MANIFEST.json) |
| SBOM generator | **syft v1.18.0** | `syft version` |
| SBOM validator | **cyclonedx-py-validate v0.5.0** | `cyclonedx-py-validate --version` |

**Preflight gate** (in `build.ps1`, runs before any build step):
for every tool above, run the preflight command, parse the version,
compare to the bound value, and abort the build on mismatch.

The build also binds **Python / pip / PyInstaller to the same
interpreter environment** by using `python -m pip ...` and
`python -m PyInstaller ...` (avoiding bare `pip` / `pyinstaller` from
PATH that may resolve to a different venv).

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
| `UpgradeCode` | **Fixed** across all versions |
| `ProductCode` | **Rotates per version** |
| `MajorUpgrade` | `Schedule="afterInstallInitialize"` + `AllowSameVersionUpgrades="no"` + `Disallow="yes"` + `UpgradeErrorMessage` |
| Downgrade block | `Disallow="yes"` |
| Repair behavior | `NeverOverwrite="yes"` on the MSI-owned secret file prevents the placeholder from overwriting a provisioned secret. The operator-owned `config.yaml` is not in any MSI component and is therefore never touched by repair |
| Uninstall behavior (v0.7) | **PRESERVE** operator-owned files AND the MSI-owned secret placeholder AND the MSI-owned `config.yaml.example` template. **Mechanism (v0.7)**: the secret + config components are in a separate **permanent ComponentGroup** that is **not** in the install-feature reference set; therefore `MajorUpgrade` / uninstall does not remove them. The MANIFEST.json records `secret_preserved_on_uninstall: true`, `config_preserved_on_uninstall: true`, `config_example_preserved_on_uninstall: true`. A clean-target VM test asserts all three |

The "permanent ComponentGroup" pattern is the WiX-documented way to
keep files across uninstall. The "no `RemoveFile`" claim from v0.6
is **insufficient** on its own; the component must be outside the
install/uninstall feature set.

---

## 0.z Public config lifecycle (operator-owned, never in MSI)

`C:\ProgramData\HermesOrchClient\config.yaml` is **operator-owned**
and is **not** contained in any MSI component. The MSI ships
**`config.yaml.example`** only.

| File | Ownership | Policy |
|---|---|---|
| `config.yaml.example` | **MSI-owned**, in a permanent component (not removed on uninstall) | **Upgraded** by the next MSI as part of `MajorUpgrade`. Operators **must not** customize it; the real `config.yaml` is the sole operator-owned file. No `NeverOverwrite` on the WiX component (consistent with "upgradeable") |
| `config.yaml` | **Operator-owned** (not in any MSI component) | Per `§0.ae` post-install steps. Repair / upgrade / uninstall do not touch it |

---

## 0.aa Payload allowlist (CI build-time check) — with per-file role

A build-time check enumerates the final release directory (after
the dedup stage — see `§0.ae-bis`) and asserts that every file
matches one of:

- The PyInstaller Python runtime DLLs (`python314.dll`, `vcruntime140.dll`, `ucrtbase.dll`) — **exactly one copy each**
- The pywin32 service dispatcher DLLs (`pywintypes314.dll`, `pythoncom314.dll`) — **service-only**
- The orch client module + its declared deps (service-only)
- The signed payload inventory recorded in the build manifest
- The `OrchClientDoctor.exe` console binary (doctor-only)
- Allowed templates (`config.yaml.example`, `agent-secret.bin` placeholder) — **package-time inputs** (NOT in the PyInstaller release directory; sourced from `installer/templates/` at MSI build time)

**Per-file role** (declared in the allowlist):
- `service-only` — only in the service onedir
- `doctor-only` — only in the doctor onedir
- `shared-identical` — present in both; dedup picks one; hash must match

**Dedup assertion** (v0.6 + v0.7 hash-equality gate): after the
deterministic merge step, the final release directory must contain
**exactly one copy** of each shared runtime DLL, and **all duplicates
must have identical SHA-256**. Different-content collisions throw
"Merge conflict: $relPath content differs".

If any unexpected `.exe`, `.dll`, `.pyd`, `.bat`, `.cmd`, `.ps1`,
`.sh`, or other script/binary appears, or if any shared runtime DLL
appears more than once with different content, the build fails
before the MSI is produced.

---

## 0.ab Signing policy

(unchanged from v0.6)

- **Owned executable** (`OrchClient.exe`, `OrchClientDoctor.exe`):
  must be signed by the release certificate.
- **Third-party signed binary** (Python runtime, pywin32): preserve
  and verify its existing signature; **do not re-sign**.
- **Third-party unsigned binary**: explicitly approved by hash,
  provenance, and SBOM entry.
- **Sign order**: owned EXEs → MSI → verify → final SHA-256 → manifest → SBOM → validate → re-write manifest with SBOM provenance

---

## 0.ae-bis Build-time dedup + allowlist (v0.7 hash-equality gate)

`build.ps1` runs PyInstaller **twice**:

1. `python -m PyInstaller --clean --noconfirm installer/orch-client.spec` → `dist/OrchClient-service/` (service onedir, with pywin32, `name='OrchClient-service'`)
2. `python -m PyInstaller --clean --noconfirm installer/orch-client-doctor.spec` → `dist/OrchClient-doctor/` (doctor onedir, no pywin32, `name='OrchClient-doctor'`)

The two onedir outputs use **distinct COLLECT names** so they do not
collide in `dist/`. The build script asserts both paths exist before
merging, then constructs the final release directory at
`dist/OrchClient/` with a **hash-equality dedup gate**:

```powershell
# Owned EXEs (named explicitly)
Copy-Item "$serviceOnedir\OrchClient.exe"       "$publishDir\OrchClient.exe"       -Force
Copy-Item "$doctorOnedir\OrchClientDoctor.exe"  "$publishDir\OrchClientDoctor.exe"  -Force

# All DLLs / modules from the service onedir
Copy-Item "$serviceOnedir\_internal\*" "$publishDir\_internal" -Recurse -Force

# Doctor-only modules (allowlist against service set; HASH-EQUALITY gate)
$serviceInternal = "$serviceOnedir\_internal"
$doctorInternal  = "$doctorOnedir\_internal"
Get-ChildItem $doctorInternal -Recurse -File | ForEach-Object {
    $relPath   = $_.FullName.Substring($doctorInternal.Length).TrimStart('\','/')
    $targetPath = Join-Path $publishDir $relPath
    if (Test-Path -LiteralPath $targetPath) {
        # Shared file: HASH must match
        $existing = (Get-FileHash -LiteralPath $targetPath -Algorithm SHA256).Hash
        $incoming = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
        if ($existing -ne $incoming) {
            throw "Merge conflict: $relPath has different content in service and doctor outputs (service=$existing, doctor=$incoming)"
        }
        return
    }
    $targetDir = Split-Path -LiteralPath $targetPath -Parent
    if (-not (Test-Path -LiteralPath $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }
    Copy-Item -LiteralPath $_.FullName -Destination $targetPath -Force
}
```

The two `Analysis` objects are **separate** but the final release
directory is constructed deterministically with a hash-equality
dedup check.

---

## 0.ae Secret provisioning runbook

(unchanged from v0.6)

---

## 0.af SBOM tool binding

(unchanged from v0.6; preflight gate added in §0.x)

---

## 0.4 Secret-preservation state table (v0.7: permanent component pattern)

| # | State | Required behavior | VM test required | Mechanism (v0.7) |
|---|---|---|---|---|
| 1 | Fresh MSI install | Zero-byte placeholder created; service refuses to run until real config + secret are written | ✓ | MSI drops the placeholder file |
| 2 | Secret provisioned after install | Preserve it through `Repair` and `MajorUpgrade` | ✓ | `NeverOverwrite="yes"` on the secret component |
| 3 | Upgrade with missing secret | Service fails closed; MSI does not auto-recreate | ✓ | `NeverOverwrite` is a no-op when the file is missing |
| 4 | Uninstall (v0.7 default) | **PRESERVE** the secret file. MSI removes only program files + service registration | ✓ | The secret + secret-dir components are in a **permanent ComponentGroup** that is **not** in the install/uninstall feature reference. MajorUpgrade / uninstall do not remove them. `MANIFEST.json` records `secret_preserved_on_uninstall: true`. Clean-target VM test asserts this |
| 5 | Reinstall after uninstall | Placeholder returns; previously-provisioned secret still present (because uninstall preserved it) | ✓ | Same as (4) |

---

## 0.4-bis Config-preservation state table (v0.7: permanent component pattern)

| # | State | `config.yaml` (operator-owned) | `config.yaml.example` (MSI-owned, permanent) |
|---|---|---|---|
| 1 | Fresh MSI install | Not present | MSI drops it (in a permanent component) |
| 2 | Operator provisions `config.yaml` | Created by operator per `§0.ae` | Untouched |
| 3 | Repair | Untouched (not in any MSI component) | Reinstalled from MSI template |
| 4 | Major upgrade | Untouched | **Overwritten** by the new MSI (upgradeable) |
| 5 | Uninstall (v0.7) | **PRESERVED** (not in any MSI component) | **PRESERVED** (in a permanent component) |

`MANIFEST.json` records `config_preserved_on_uninstall: true` and
`config_example_preserved_on_uninstall: true`. Clean-target VM
test asserts both.

---

## 1. Goal

(unchanged from v0.6)

1. A runnable orch client + a separate `OrchClientDoctor.exe` (console)
2. A code-signed Windows MSI built with PyInstaller (two specs, distinct COLLECT names) + WiX 4 + `HarvestDirectory`
3. A SHA-256 + real CycloneDX SBOM + signing manifest
4. An updated install runbook

The MSI install shall:
- Drop `OrchClient.exe` (service) and `OrchClientDoctor.exe` (console) under `C:\Program Files\HermesOrchClient\`
- Register `OrchClient` service (start = demand)
- Drop `config.yaml.example` (MSI-owned, in a permanent component) at `C:\ProgramData\HermesOrchClient\config.yaml.example`
- Drop a zero-byte placeholder secret at `C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin` with `NeverOverwrite="yes"` and ACL `D:P(A;;FA;;;SY)(A;;FA;;;BA)`
- Lock parent directories with matching SDDLs
- **Not** auto-start the service on install
- First-release failure actions: `none / none / none`
- On uninstall: **PRESERVE** all three: secret file, `config.yaml.example`, and operator-owned `config.yaml` (the last because it is not in any MSI component)

---

## 1.4 Server-side HMAC key-id-to-agent authorization rule

(unchanged from v0.6)

**v0.7 addition**: HMAC-signed endpoints **forbid query strings**. A
signed request must not include a `?...` query. The client does not
emit query strings on signed endpoints; the server rejects any
signed request that contains one. The signing input (§3.1) does
**not** bind a query-string canonicalization. **Future option**: if a
real use case for query-string-based endpoints emerges, the signing
input can be extended to include a canonical query string
(sorted-key=value, urlencoded); for now, no query strings.

---

## 1.5 Build provenance (v0.7: source commit + lockfile hash captured)

Every release artifact is accompanied by:

- **Exact pinned versions** (§0.x) — every tool version recorded in `MANIFEST.json::tooling` + SBOM metadata + preflight gate enforces
- **`source_commit_sha`** (v0.7) — captured at build time via `git rev-parse HEAD`
- **`requirements_lock_sha256`** (v0.7) — captured at build time via `Get-FileHash` on `installer/requirements.lock`
- Build-host identifier (operator-bound)
- Build timestamp (UTC)
- `MANIFEST.json` (built as `[ordered]@{}` object, JSON conversion once at the end, read-back + parse + assert gate)
- `SBOM.cyclonedx.json` (real CycloneDX 1.6, validated)

---

## 1.6 Server-side TLS cert + client-side cert fingerprint pinning

(v0.7 patch; applied after operator review of cert verification options)

The orch server (v3.12.0+) ships a `hermes-orch gen-cert` subcommand
that auto-generates a self-signed RSA-2048 cert with 365-day validity
and SANs = `<hostname>, localhost, 127.0.0.1`, written to
`~/.hermes-orchestrator/certs/server.{crt,key}`. The cert is
**machine-local and per-deployment**; the v0.7 MSI does NOT bundle it.

**Client-side verification (v0.7 choice: fingerprint pinning).** Each
new orch client agent's `config.yaml` carries an
`orchestrator_ca_fingerprint_sha256` field — the lower-case hex
SHA-256 of the orch server's `server.crt` DER bytes (no colons, no
spaces, 64 hex chars). The agent's TLS client sets
`ssl.SSLContext.verify_mode = CERT_REQUIRED` and compares the
server-presented cert's SHA-256 against the pinned value on every
handshake. **Mismatch → fail closed**: no connection, no fallback to
the OS trust store, no `verify=False` escape hatch, no warning-and-
continue.

| Aspect | Policy |
|---|---|
| Cert source | Orch server's `~/.hermes-orchestrator/certs/server.crt` (auto-gen at first install via `hermes-orch gen-cert`) |
| Cert type | Self-signed, RSA 2048, 365-day validity |
| Cert SANs (default) | `<hostname>`, `localhost`, `127.0.0.1` — operator MUST use the orch host's hostname in `orchestrator_url` so the cert's CN/SAN matches. Direct-IP connection is out of scope for first release (see "IP-direct" row below) |
| Agent verification | `ssl.SSLContext.verify_mode = CERT_REQUIRED` + post-handshake SHA-256 compare against `config.yaml::orchestrator_ca_fingerprint_sha256` |
| Fingerprint capture | Operator runs `openssl x509 -in ~/.hermes-orchestrator/certs/server.crt -noout -fingerprint -sha256` on the orch host (PowerShell `Get-FileHash` on the cert file gives the file's SHA-256, NOT the cert's — must use `openssl x509` or equivalent). The value after `SHA256 Fingerprint=` is the agent-side pinned value |
| Cert rotation | Server re-runs `hermes-orch gen-cert --force`; new fingerprint printed; operator updates every agent's `orchestrator_ca_fingerprint_sha256`; agent restart required. v0.7 does NOT include a server-pushed fingerprint-update flow (out of scope; tracked as a v0.7 follow-up) |
| IP-direct connection | Out of scope for first release. Operator must use the orch host's hostname and ensure DNS / `/etc/hosts` / Windows hosts file resolves it. Direct-IP would require re-gen with the IP in SANs (a server follow-up; the v3.12.0 `gen-cert` does not expose an `--ip` flag today) |
| OS trust store | Not used. The pinning model replaces the OS trust store for the orch server relationship; the cert is not added to `LocalMachine\Root` |
| `INSECURE_SKIP_TLS_VERIFY` | Not honored. The v0.7 service fails closed if pinning is requested but no fingerprint is configured (placeholder, missing, or comment-only value) |
| Defense in depth | TLS for transport encryption + fingerprint for MITM defense + HMAC (§1.4) for request forgery defense + Origin/CSRF (B12/R14) for browser-session defense — four independent layers |

`orchestrator_ca_fingerprint_sha256` is a **per-deployment** value, not
a build-time value, so it is NOT recorded in `MANIFEST.json` (which is
per-MSI-build and identical across every deployment of the same MSI).
Each agent's `config.yaml` carries its own pinned value; the value is
per-orchestrator, not per-MSI-release.

**Runbook reference:** `docs/runbooks/orch-client-install-runbook.md`
Step 5a (config.yaml field) + Before-you-start checklist (where the
operator gets the fingerprint) + Troubleshooting (rotation
mismatch).

**Forbidden:** shipping a real fingerprint in the MSI template, hard-
coding a fingerprint in `installer/templates/config/config.yaml.example`,
or shipping the cert file inside the MSI. The fingerprint is always
operator-input at deployment time.

---

## 2. Deliverables

(unchanged from v0.6)

---

## 3. Source layout (Deliverable 1)

(unchanged from v0.6)

### 3.1 `hmac_auth.py` (v0.7: query string policy added)

- **Body hash** = `SHA-256(exact raw UTF-8 request-body bytes)`, hex-lowercase
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
  - `canonical-path` is the **path-only** form (`/api/agents/abc/heartbeat`); **no query string is allowed on signed endpoints** (v0.7)
- **Signature** = `HMAC-SHA256(secret, UTF-8(signing_input))`, hex-lowercase
- **Headers** (unchanged from v0.3)
- **Server validation** (unchanged) + **key-id-to-agent authorization** (unchanged from v0.6)
- **Client policy**: do not emit query strings on signed endpoints. The SDK helper raises before signing if a query string is detected.

### 3.2 - 3.7 (unchanged from v0.5/v0.6)

---

## 4. PyInstaller specs (Deliverable 4) — v0.7: distinct COLLECT names

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
    strip=False, upx=False, upx_exclude=[], name='OrchClient-service')   # v0.7
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
    strip=False, upx=False, upx_exclude=[], name='OrchClient-doctor')   # v0.7
```

The distinct `COLLECT` names ensure the two specs produce distinct
`dist/OrchClient-service/` and `dist/OrchClient-doctor/` outputs
that the merge step combines deterministically.

---

## 5. WiX 4 source (Deliverable 5) — v0.7: permanent component pattern

`orch-client.wixproj` (unchanged from v0.6)

`orch-client.wxs` (v0.7: secret + config components in a **permanent ComponentGroup** outside the install/uninstall feature set):

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

    <!-- Install feature: program files + service + doctor. Secret and
         config example are NOT in this feature. -->
    <Feature Id="InstallFeature" Title="Hermes Orch Client" Level="1">
      <ComponentGroupRef Id="OrchClientFiles" />
      <ComponentGroupRef Id="OrchClientService" />
      <ComponentGroupRef Id="OrchClientDoctor" />
    </Feature>

    <!-- Permanent feature: secret + config example + their directories.
         Listed at the Package level (not in InstallFeature) so uninstall
         does not remove these components. MajorUpgrade and repair still
         affect them. -->
    <Feature Id="PermanentFeature" Title="Hermes Orch Client Data" Level="1" Absent="disallow">
      <ComponentGroupRef Id="OrchClientSecret" />
      <ComponentGroupRef Id="OrchClientConfig" />
    </Feature>
  </Package>

  <!-- Service + Doctor fragments (unchanged from v0.6) -->
  <Fragment>
    <DirectoryRef Id="INSTALLFOLDER">
      <Component Id="OrchClientServiceComponent" Guid="*">
        <File Id="OrchClientServiceExe" Source="$(var.PublishDir)\OrchClient.exe" KeyPath="yes" />
        <ServiceInstall Id="InstallOrchClient" Name="OrchClient"
                        DisplayName="Hermes Orch Client"
                        Description="Hermes orchestration client agent"
                        Type="ownProcess" Start="demand" ErrorControl="normal"
                        Account="LocalSystem" Interactive="no" Vital="yes">
          <util:ServiceConfig
              FirstFailureActionType="none"
              SecondFailureActionType="none"
              ThirdFailureActionType="none" />
        </ServiceInstall>
        <ServiceControl Id="ControlOrchClient" Name="OrchClient"
                        Stop="both" Remove="uninstall" Wait="yes" />
      </Component>
    </DirectoryRef>
    <ComponentGroup Id="OrchClientService">
      <ComponentRef Id="OrchClientServiceComponent" />
    </ComponentGroup>
  </Fragment>

  <Fragment>
    <DirectoryRef Id="INSTALLFOLDER">
      <Component Id="OrchClientDoctorComponent" Guid="*">
        <File Id="OrchClientDoctorExe" Source="$(var.PublishDir)\OrchClientDoctor.exe" KeyPath="yes" />
      </Component>
    </DirectoryRef>
    <ComponentGroup Id="OrchClientDoctor">
      <ComponentRef Id="OrchClientDoctorComponent" />
    </ComponentGroup>
  </Fragment>

  <!-- Secret + config (PERMANENT FEATURE, v0.7) -->
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
                Name="config.yaml.example" KeyPath="yes">
            <util:PermissionEx
              Sddl="D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)" />
          </File>
        </Component>

        <Directory Id="SECRETFOLDER" Name="secrets">
          <Component Id="OrchClientSecretDirComponent" Guid="*" KeyPath="yes">
            <CreateFolder>
              <util:PermissionEx Sddl="D:P(A;;FA;;;SY)(A;;FA;;;BA)" />
            </CreateFolder>
          </Component>

          <Component Id="OrchClientSecretFileComponent" Guid="*" NeverOverwrite="yes">
            <File Id="OrchClientSecretFile"
                  Source="$(var.SecretTemplate)\agent-secret.bin"
                  Name="agent-secret.bin" KeyPath="yes">
              <util:PermissionEx Sddl="D:P(A;;FA;;;SY)(A;;FA;;;BA)" />
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

The **PermanentFeature** at the Package level is **not** referenced
by `InstallFeature` and is therefore not removed on uninstall. This
is the WiX-documented way to keep files across uninstall while still
allowing them to be upgraded by `MajorUpgrade`.

---

## 6. Build script (Deliverable 6) — v0.7: full preflight gate + dedup hash-equality + manifest provenance + source commit + lockfile hash

`build.ps1` (key v0.7 changes):

```powershell
$ErrorActionPreference = 'Stop'

# ----- Bound operator values (exact, preflight-gate enforced) -----
$RepoRoot      = 'C:\Project\minimax code\hermes-orchestrator'
$PyInstaller   = 'pyinstaller'  # invoked via python -m
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

# ----- Preflight gate helpers -----
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

function Assert-VersionMatch {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][string]$Actual,
        [Parameter(Mandatory)][string]$Expected
    )
    if ($Actual -notmatch [regex]::Escape($Expected)) {
        throw "$Label version mismatch: expected '$Expected', got '$Actual'"
    }
}

# ============================================================================
# 0) Preflight: bound tool versions (v0.7: ALL tools)
# ============================================================================
$pythonVer = & python --version 2>&1 | Out-String
Assert-VersionMatch -Label 'python' -Actual $pythonVer -Expected $ExpectedPython

$pipVer = & python -m pip --version 2>&1 | Out-String
# pip version is bound by the same Python interpreter; just record

$pyinstVer = & python -m PyInstaller --version 2>&1 | Out-String
Assert-VersionMatch -Label 'pyinstaller' -Actual $pyinstVer -Expected $ExpectedPyInstaller

$dotnetVer = & dotnet --version 2>&1 | Out-String
Assert-VersionMatch -Label 'dotnet' -Actual $dotnetVer -Expected $ExpectedDotnet

$wixVer = & wix --version 2>&1 | Out-String
Assert-VersionMatch -Label 'wix' -Actual $wixVer -Expected $ExpectedWix

$signtoolPath = (Get-Command signtool.exe -ErrorAction SilentlyContinue).Source
if (-not $signtoolPath) { throw "signtool.exe not found on PATH" }
# signtool does not expose a clean version field; record path + version of Windows SDK
$signtoolVer = "Win SDK $([System.IO.Path]::GetFileName((Split-Path $signtoolPath -Parent) | Split-Path -Parent))"
# Compare against $ExpectedSigntool loosely; operator confirms exact Windows SDK build
if ($signtoolVer -notmatch [regex]::Escape($ExpectedSigntool)) {
    throw "signtool version mismatch: expected '$ExpectedSigntool', got '$signtoolVer' (path=$signtoolPath)"
}

$syftVer = & syft version 2>&1 | Out-String
Assert-VersionMatch -Label 'syft' -Actual $syftVer -Expected $ExpectedSyft

$cyVer = & cyclonedx-py-validate --version 2>&1 | Out-String
Assert-VersionMatch -Label 'cyclonedx-py-validate' -Actual $cyVer -Expected $ExpectedCycloneValidator

# ============================================================================
# 1) Verify locked dependency file matches (via the same Python interpreter)
# ============================================================================
Invoke-NativeChecked -FilePath 'python' `
    -Arguments @('-m','pip','install','--require-hashes','-r',
                  (Join-Path $RepoRoot 'installer/requirements.lock')) `
    -Label 'pip install --require-hashes'

# ============================================================================
# 2) Capture source commit SHA + requirements lockfile hash
# ============================================================================
$sourceCommit = & git rev-parse HEAD 2>&1 | Out-String
$sourceCommit = $sourceCommit.Trim()
$lockfileSha  = (Get-FileHash -LiteralPath (Join-Path $RepoRoot 'installer/requirements.lock') -Algorithm SHA256).Hash

# ============================================================================
# 3) PyInstaller: TWO separate specs with distinct COLLECT names (v0.7)
# ============================================================================
$serviceOnedir = Join-Path $RepoRoot 'dist\OrchClient-service'
$doctorOnedir  = Join-Path $RepoRoot 'dist\OrchClient-doctor'
$publishDir    = Join-Path $RepoRoot 'dist\OrchClient'
Remove-Item -LiteralPath $serviceOnedir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $doctorOnedir  -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $publishDir    -Recurse -Force -ErrorAction SilentlyContinue

Invoke-NativeChecked -FilePath 'python' `
    -Arguments @('-m','PyInstaller','--clean','--noconfirm',$ServiceSpec) `
    -Label 'pyinstaller (service spec)'
Invoke-NativeChecked -FilePath 'python' `
    -Arguments @('-m','PyInstaller','--clean','--noconfirm',$DoctorSpec) `
    -Label 'pyinstaller (doctor spec)'

# Both onedir outputs must exist
if (-not (Test-Path $serviceOnedir)) { throw "Service onedir missing: $serviceOnedir" }
if (-not (Test-Path $doctorOnedir))  { throw "Doctor onedir missing: $doctorOnedir"  }

# ============================================================================
# 4) Construct the release directory (deterministic merge + hash-equality dedup)
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

# Doctor-only modules (allowlist against service set; HASH-EQUALITY gate)
$serviceInternal = Join-Path $serviceOnedir '_internal'
$doctorInternal  = Join-Path $doctorOnedir  '_internal'
Get-ChildItem -Path $doctorInternal -Recurse -File | ForEach-Object {
    $relPath   = $_.FullName.Substring($doctorInternal.Length).TrimStart('\','/')
    $targetPath = Join-Path $publishDir $relPath
    if (Test-Path -LiteralPath $targetPath) {
        $existing = (Get-FileHash -LiteralPath $targetPath -Algorithm SHA256).Hash
        $incoming = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
        if ($existing -ne $incoming) {
            throw "Merge conflict: $relPath has different content in service and doctor outputs (service=$existing, doctor=$incoming)"
        }
        return
    }
    $targetDir = Split-Path -LiteralPath $targetPath -Parent
    if (-not (Test-Path -LiteralPath $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }
    Copy-Item -LiteralPath $_.FullName -Destination $targetPath -Force
}

# Dedup assertion: exactly one copy of each shared runtime DLL
$sharedDlls = @('python314.dll','vcruntime140.dll','ucrtbase.dll','pythoncom314.dll','pywintypes314.dll')
foreach ($dll in $sharedDlls) {
    $count = (Get-ChildItem -Path $publishDir -Recurse -Filter $dll -ErrorAction SilentlyContinue).Count
    if ($count -ne 1) {
        throw "Dedup assertion failed for $dll: expected 1 copy in $publishDir, got $count"
    }
}

# Templates are NOT copied into the PyInstaller release directory (v0.7).
# They are package-time inputs to the MSI build, sourced from installer/templates/.

# ============================================================================
# 5) Sign owned EXEs
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
# 6) Build MSI via dotnet build
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
# 7) Sign the MSI
# ============================================================================
Invoke-NativeChecked -FilePath 'signtool.exe' `
    -Arguments @('sign','/fd','SHA256','/tr',$TimestampUrl,'/td','SHA256',
                  '/sha1',$CertThumb,$msiPath) `
    -Label 'signtool sign MSI'

# ============================================================================
# 8) Verify
# ============================================================================
Invoke-NativeChecked -FilePath 'signtool.exe' `
    -Arguments @('verify','/pa','/all','/v',$msiPath) `
    -Label 'signtool verify MSI'

# ============================================================================
# 9) Compute MSI SHA-256 + capture template hashes (single authoritative source)
# ============================================================================
$hash = (Get-FileHash -LiteralPath $msiPath -Algorithm SHA256).Hash
"$hash  $ExpectedMsi" |
    Out-File -LiteralPath (Join-Path $msiDir 'SHA256SUMS.txt') -Encoding utf8

$configExamplePath = Join-Path $RepoRoot 'installer\templates\config\config.yaml.example'
$configExampleHash = (Get-FileHash -LiteralPath $configExamplePath -Algorithm SHA256).Hash
$secretPath        = Join-Path $RepoRoot 'installer\templates\secret\agent-secret.bin'
$secretHash        = (Get-FileHash -LiteralPath $secretPath -Algorithm SHA256).Hash

# ============================================================================
# 10) Generate SBOM (real CycloneDX)
# ============================================================================
Invoke-NativeChecked -FilePath $sbomGen `
    -Arguments @('scan',"dir=$publishDir",'--output',"cyclonedx-json=$sbomOut") `
    -Label "SBOM generator ($sbomGen $ExpectedSyft)"
$sbomHash = (Get-FileHash -LiteralPath $sbomOut -Algorithm SHA256).Hash

# ============================================================================
# 11) Validate SBOM
# ============================================================================
$sbomValidateResult = & $sbomValidate $sbomOut 2>&1
$sbomValidateExit   = $LASTEXITCODE
if ($sbomValidateExit -ne 0) {
    throw "SBOM validator ($sbomValidate $ExpectedCycloneValidator) failed (exit $sbomValidateExit): $sbomValidateResult"
}

# ============================================================================
# 12) Build MANIFEST as an OBJECT throughout, write JSON once, read back + assert
# ============================================================================
$payloadInventory = @()
Get-ChildItem $publishDir -Recurse -File | ForEach-Object {
    $payloadInventory += [PSCustomObject]@{
        path   = (Resolve-Path -LiteralPath $_ -Relative).ToString()
        sha256 = (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash
    }
}

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
    source_commit_sha      = $sourceCommit
    requirements_lock_sha256 = $lockfileSha
    signing        = [ordered]@{
        tool        = 'signtool.exe'
        timestamp   = $TimestampUrl
        cert_sha1   = $CertThumb
        digest      = 'SHA256'
    }
    tooling        = [ordered]@{
        python_version        = $ExpectedPython
        pip_version           = ($pipVer.Trim() -split ' ')[1]
        pyinstaller_version   = $ExpectedPyInstaller
        wix_version           = $ExpectedWix
        signtool_version      = $ExpectedSigntool
        signtool_path         = $signtoolPath
        dotnet_version        = $ExpectedDotnet
        sbom_generator        = $sbomGen
        sbom_generator_version = $ExpectedSyft
        sbom_validator        = $sbomValidate
        sbom_validator_version = $ExpectedCycloneValidator
    }
    templates       = [ordered]@{
        config_yaml_example_sha256 = $configExampleHash
        agent_secret_bin_sha256    = $secretHash
        source_path                = 'installer/templates/'
    }
    payload_inventory = $payloadInventory
    secret_preserved_on_uninstall     = $true
    config_preserved_on_uninstall     = $true
    config_example_preserved_on_uninstall = $true
    sbom_filename    = Split-Path -Leaf $sbomOut
    sbom_sha256      = $sbomHash
    sbom_generator   = $sbomGen
    sbom_generator_version = $ExpectedSyft
    sbom_validator   = $sbomValidate
    sbom_validator_version = $ExpectedCycloneValidator
    sbom_validator_result = 'pass'
}

$manifestJson = $manifest | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText(
    (Join-Path $msiDir 'MANIFEST.json'),
    $manifestJson,
    [System.Text.UTF8Encoding]::new($false))

# ============================================================================
# 13) Read-back + parse + assert gate
# ============================================================================
$manifestReadback = Get-Content -LiteralPath (Join-Path $msiDir 'MANIFEST.json') -Raw |
    ConvertFrom-Json
foreach ($required in @('product','version','msi_sha256','msi_filename',
                        'source_commit_sha','requirements_lock_sha256',
                        'sbom_filename','sbom_sha256','sbom_generator_version',
                        'sbom_validator_version','sbom_validator_result',
                        'secret_preserved_on_uninstall',
                        'config_preserved_on_uninstall',
                        'config_example_preserved_on_uninstall',
                        'tooling','templates','payload_inventory')) {
    if (-not ($manifestReadback.PSObject.Properties.Name -contains $required)) {
        throw "MANIFEST.json read-back missing field: $required"
    }
}
if ($manifestReadback.msi_sha256 -ne $hash) { throw "MANIFEST.json::msi_sha256 mismatch" }
if ($manifestReadback.sbom_sha256 -ne $sbomHash) { throw "MANIFEST.json::sbom_sha256 mismatch" }
if ($manifestReadback.source_commit_sha -ne $sourceCommit) { throw "MANIFEST.json::source_commit_sha mismatch" }
if ($manifestReadback.requirements_lock_sha256 -ne $lockfileSha) { throw "MANIFEST.json::requirements_lock_sha256 mismatch" }
if ($manifestReadback.templates.config_yaml_example_sha256 -ne $configExampleHash) { throw "MANIFEST.json::templates.config_yaml_example_sha256 mismatch" }
if ($manifestReadback.templates.agent_secret_bin_sha256 -ne $secretHash) { throw "MANIFEST.json::templates.agent_secret_bin_sha256 mismatch" }
if ($manifestReadback.sbom_validator_result -ne 'pass') { throw "MANIFEST.json::sbom_validator_result is not 'pass'" }

Write-Host "[+] Build complete: $msiPath"
Write-Host "[+] MSI SHA-256: $hash"
Write-Host "[+] SBOM: $sbomOut (sha256: $sbomHash)"
Write-Host "[+] MANIFEST read-back + assert gate: PASS"
Write-Host "[+] Source commit: $sourceCommit"
Write-Host "[+] requirements.lock sha256: $lockfileSha"
```

---

## 7. Known gaps & explicit dependencies (must be resolved before build)

(unchanged from v0.6; v0.7 binds all exact versions)

| # | Dependency | Owner | Status |
|---|---|---|---|
| 1 | Orchestrator contract | operator | **TBD until B12 deployed and reviewed** |
| 2 | Code-signing cert thumbprint | operator | not yet bound |
| 3 | WiX 4 + .NET SDK on build host | operator | not yet installed |
| 4 | PyInstaller in build Python | operator | assumed present |
| 5 | Target machine `agent_id` | operator | not yet assigned |
| 6 | Target machine HMAC secret | operator | not yet generated |
| 7 | HMAC `key-id` (rotation support) | operator | not yet bound |
| 8 | `<ORCHESTRATOR_FQDN>` and `<HTTPS_PORT>` (B13-transport-closed) | operator | not yet bound |
| 9 | All tool versions (exact, preflight-gate enforced) | operator | **bound in v0.7**: Python 3.14.0, pip per env, PyInstaller 6.16.0, WiX 4.0.6, Windows SDK 10.0.22621.4031, .NET SDK 8.0.404, syft v1.18.0, cyclonedx-py-validate v0.5.0 |
| 10 | Cert renewal policy + revoked-cert response | operator | bind at runtime |
| 11 | UpgradeCode GUID + ProductCode rotation | operator | bind at runtime |
| 12 | VM test environment (clean Windows 10/11) | operator | required before implementation approval |
| 13 | Explicit privileged cleanup script (out of scope; follow-up) | operator | for removing `config.yaml` / `agent-secret.bin` on uninstall |
| 14 | Orchestrator cert SHA-256 fingerprint (per agent, for client-side pinning) | operator | not yet bound — see §1.6. Operator runs `hermes-orch gen-cert` on the orch host, captures the lower-case hex SHA-256 of `server.crt` (via `openssl x509 -fingerprint -sha256`, NOT `Get-FileHash` on the cert file), pastes the value into each agent's `config.yaml::orchestrator_ca_fingerprint_sha256`. The v3.12.0 `gen-cert` does not currently print the fingerprint; a `--print-fingerprint` follow-up is recommended so the operator does not have to remember the openssl incantation |

---

## 8. Forbidden actions (no exceptions)

(unchanged from v0.6; v0.7 adds: "no shared runtime DLL may have content-hash conflict in the merge stage")

- ❌ No remote install on the target Windows machine
- ❌ No live HTTP calls during build (dry-run only)
- ❌ No real HMAC secret embedded in the MSI
- ❌ No `orchestrator_url: http://...` shipped in MSI config
- ❌ No `config.yaml` shipped in MSI (only `config.yaml.example`)
- ❌ No modification of B12 deploy (held at watchdog REFUSE)
- ❌ No modification of watchdog task (held)
- ❌ No push to remote git remote
- ❌ No firewall / enrollment / agent-mutation action on the orchestrator
- ❌ No repair / upgrade that clobbers a provisioned secret (`NeverOverwrite="yes"`)
- ❌ No uninstall that removes the secret file, `config.yaml.example`, or operator-owned `config.yaml` (PermanentFeature pattern; v0.7)
- ❌ No blind re-signing of third-party DLLs that already carry a valid Authenticode signature
- ❌ No labelling a hand-rolled JSON as SPDX or CycloneDX
- ❌ No CLI args for frozen-bundle behavior
- ❌ No single COLLECT name for two specs (v0.7 fix; distinct names)
- ❌ No manual `OrchClientFiles` group
- ❌ No requiring unexpired cert at verify time
- ❌ No silent dedup on hash conflict (v0.7 fix; throw)
- ❌ No skipping the preflight version gate or the manifest read-back gate
- ❌ No query strings on HMAC-signed endpoints (v0.7 fix; client + server reject)
- ❌ No two-source-of-truth for templates (v0.7 fix; `installer/templates/` is authoritative)

---

## 9. Acceptance criteria

A-M: same as v0.5 (file system layout, ACLs, service registration, doctor binary, secret-preservation, config-preservation, HMAC validation, runbook, side effects, VM validation, test case matrix, server-side rule, manifest field set)

**N (v0.7 added)**:
- Preflight version gate passes for **all** declared tools (Python, pip, PyInstaller, WiX, SignTool, .NET, syft, cyclonedx-py-validate)
- PyInstaller `--onedir` outputs land in `dist/OrchClient-service/` and `dist/OrchClient-doctor/` (distinct COLLECT names)
- Dedup hash-equality gate finds exactly one copy of each shared runtime DLL with identical SHA-256
- `MANIFEST.json` includes `source_commit_sha` and `requirements_lock_sha256`
- `MANIFEST.json` includes `templates.config_yaml_example_sha256` and `templates.agent_secret_bin_sha256`
- Read-back + parse + assert gate passes
- `PermanentFeature` (secret + config) survives uninstall on a clean VM

---

## 10. What I will NOT do (without separate approval)

(unchanged from v0.5)

---

## 11. v0.1 → v0.7 changelog (final summary)

| # | v0.1 | v0.7 |
|---|---|---|
| HMAC | "JSON fixed key order" | Bound-metadata model (raw body + canonical headers), no query strings, key-id-to-agent server rule |
| Orchestrator URL | (n/a) | HTTPS placeholder; service fail-closed on placeholder/HTTP/missing |
| PyInstaller | (n/a) | Two separate specs with distinct COLLECT names (`OrchClient-service` / `OrchClient-doctor`); Option B deterministic merge with hash-equality dedup gate; one copy of each shared runtime DLL |
| WiX KeyPath | (n/a) | On `<File>` (and on `<CreateFolder>` for dirs) |
| WiX ComponentGroup wrap | (n/a) | Every manual Component wrapped in explicit ComponentGroup + ComponentRef |
| WiX MajorUpgrade | (n/a) | `Disallow="yes"` + `UpgradeErrorMessage` |
| WiX uninstall preservation | (n/a) | PermanentFeature at Package level (secret + config) + ABSENCE of `RemoveFile` / `RemoveFolder` for operator-owned files; both required, not just ABSENCE |
| Doctor EXE | (n/a) | Separate `Analysis` + separate `.spec` + console-enabled; not a service |
| Config lifecycle | (n/a) | MSI ships `config.yaml.example` only; real `config.yaml` is operator-owned and outside MSI; MSI-owned example is upgradeable |
| Secret preservation | (n/a) | NeverOverwrite (install/repair/upgrade) + PermanentFeature (uninstall); VM-tested per state |
| SBOM | (n/a) | Real CycloneDX 1.6, validated by cyclonedx-py-validate v0.5.0; preflight gate enforced; manifest has full provenance |
| Tool versions | (n/a) | Exact pinned: Python 3.14.0, PyInstaller 6.16.0, WiX 4.0.6, Windows SDK 10.0.22621.4031, .NET SDK 8.0.404, syft v1.18.0, cyclonedx-py-validate v0.5.0 |
| Build provenance | (n/a) | `source_commit_sha` + `requirements_lock_sha256` + templates hashes + payload inventory |
| Templates | (n/a) | Single authoritative source: `installer/templates/`; PyInstaller release directory does NOT contain template copies |
| HMAC query strings | (n/a) | Forbidden on signed endpoints (client + server reject) |
| Preflight gate | (n/a) | All declared tool versions verified before any build step |
| Dedup | (n/a) | Hash-equality gate (silent skip is forbidden) |
| Manifest handling | (n/a) | Object throughout, JSON once, read-back + parse + assert gate |

---

## 12. Next steps (operator-binding phase, post-plan)

The plan-only phase is **done**. v0.7 is the final plan-only iteration.
The acceptance gate is the **VM test matrix on a clean Windows
10/11 target**, not further Perplexity reviews.

Operator-binding prerequisites for the implementation phase:

0. Operator runs `hermes-orch gen-cert` on the **orch host** (or
   confirms an existing cert is in place) and captures the
   lower-case hex SHA-256 fingerprint (see §1.6). The fingerprint is
   per-orchestrator-deployment; every new agent's
   `config.yaml::orchestrator_ca_fingerprint_sha256` is set from
   this value. If the orch server is a fresh install, this is a
   one-line `hermes-orch gen-cert` + `openssl x509 -fingerprint
   -sha256` step. The v0.7 implementation includes a
   `--print-fingerprint` follow-up on `gen-cert` so operators do
   not have to remember the openssl incantation
1. Operator picks `agent_id` and `key-id` for the target machine
2. Operator picks the code-signing cert / Azure Trusted Signing
3. Operator installs WiX 4 + .NET SDK + Python 3.14.0 + PyInstaller 6.16.0 + Windows SDK 10.0.22621.4031 + .NET SDK 8.0.404 on the build host
4. Operator verifies `syft v1.18.0` and `cyclonedx-py-validate v0.5.0` are installed on PATH
5. Operator generates the target machine's HMAC secret (out-of-band)
6. Operator generates the UpgradeCode GUID
7. Operator runs the §3.6 + §3.7 + §0.4 + §0.4-bis VM tests on a clean
   Windows 10/11 target, produces a signed report
8. Operator drafts the §9 test case matrix (separate work item)
9. Operator drafts the explicit privileged cleanup script (out of
   scope for v0.7; tracked as a follow-up) for removing
   `config.yaml` / `agent-secret.bin` on uninstall

After the VM test report is signed, the implementation phase can
begin under a separate "implementation approved" gate.
