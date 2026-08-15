# Orch Client Install & Register Runbook (Windows target)

> **v0.4.2** — aligned with `docs/proposals/orch-client-build-impl-plan-v0.7.md`
> + v0.7 cert-pinning patch + v0.7.1 bootstrapper patch + v0.7.2 firewall
> auto-add patch + **v0.7.3 real TLS cert fingerprint pinning patch**. See
> the plan's §0.ter (bootstrapper patch), §0.quart (firewall auto-add
> patch), §0.quint (cert fingerprint pinning patch), and §0.af-bootstrap
> (bootstrapper design).
>
> **For a new operator.** Assumes the orchestrator is already running on
> another machine (the "orchestrator host"). This runbook is executed on
> the **target Windows machine** that will become a new agent host.
>
> **What this does:** installs the orch client on a fresh Windows machine,
> registers it with the orchestrator, and verifies the new agent is
> "verified".
>
> **Status:** TEMPLATE. The MSI referenced here does not yet exist; this
> runbook is finalized in plan-only phase alongside the v0.7 build plan
> and the v0.7.1 bootstrapper. Implementation begins only after the
> v0.7 §12 operator-binding phase completes (build host + signing cert
> + clean VM test matrix + agent_id bound). For the corresponding build
> plan see `docs/proposals/orch-client-build-impl-plan-v0.7.md`.

---

## Quick start (recommended for semi-technical users)

**If your operator gave you the bootstrapper script `install-orch-client.ps1`,
just run it as Administrator.** It collects the 7 required values
interactively, validates them, runs the install, and verifies enrollment.
Total time: < 2 minutes.

```powershell
# Right-click PowerShell → "Run as Administrator", then:
& 'C:\Path\To\install-orch-client.ps1'
```

The bootstrapper will prompt for:

1. Orchestrator FQDN (e.g. `orchestrator.example.local`, NOT an IP)
2. Orchestrator HTTPS port (default 443)
3. Orchestrator TLS cert SHA-256 fingerprint (64 hex chars, no colons)
4. This machine's `agent_id` (e.g. `win-b-02`)
5. HMAC `key_id` (operator-assigned)
6. One-time `enrollment_token` (operator-generated)
7. HMAC secret (base64 string; **input is hidden**)

If the bootstrapper succeeds, you will see `=== SUCCESS ===` and the
agent will be `verified` in the orchestrator. If it fails, every error
message is plain English — no stack traces.

**The 8 manual steps below are for operators and power users only.**
The bootstrapper internally implements every one of them. If you need
to understand what the bootstrapper is doing, or if you need to install
without using the bootstrapper, read on.

---

---

## 0. v0.1 → v0.2 changelog

| # | v0.1 said | v0.2 says | Reason |
|---|---|---|---|
| 1 | `Test-NetConnection 192.168.2.152 -Port 8765` and `orchestrator_url: http://192.168.2.152:8765` | All URL and reachability examples use **`https://<ORCHESTRATOR_FQDN>:<HTTPS_PORT>/`** as a placeholder; the operator fills in the bound values from the v0.7 plan §7 #8. The `192.168.2.152:8765` IP+port pair is removed entirely | v0.7 §8 forbidden actions: "No `orchestrator_url: http://...` shipped in MSI config". B13 transport is HTTPS-or-closed; HTTP exposes session cookie + admin password + login + HMAC replay. The service fails closed on HTTP / missing / placeholder |
| 2 | Step 5: paste `hmac_secret: <paste-secret-here>` into `agent.yaml` (plaintext) | Step 5 is split: **(a)** edit `C:\ProgramData\HermesOrchClient\config.yaml` with `agent_id` / `orchestrator_url` / `key_id` / `enrollment_token` (NO secret in the file), then **(b)** write the HMAC secret to `C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin` (the MSI-owned zero-byte placeholder installed by the MSI in a permanent component with SDDL `D:P(A;;FA;;;SY)(A;;FA;;;BA)`) | v0.7 §0.4 / §0.4-bis / §0.z: secret is a separate file, not in `config.yaml`. The placeholder is `NeverOverwrite="yes"` so repair / MajorUpgrade does not clobber a provisioned secret. B11 secret-at-rest is deferred but this split is already the v0.7 layout |
| 3 | Uninstall: `Remove-Item -LiteralPath 'C:\Program Files\OrchClient' -Recurse -Force` | Uninstall uses the same MSI: `Start-Process -FilePath $installer -ArgumentList '/uninstall','/qn' -Wait`. **No `Remove-Item` of the install dir or the data dir.** The PermanentFeature at Package level preserves `config.yaml`, `config.yaml.example`, and `agent-secret.bin`. Explicit removal of those three is a separate, follow-up "privileged cleanup script" (v0.7 §7 #13, out of scope) that requires the operator to type an explicit `YES` confirmation | v0.7 §0.y / §0.4 / §0.4-bis: uninstall PRESERVES the 3 operator-or-MSI-owned data files. A blanket `Remove-Item -Recurse -Force` would defeat the PermanentFeature pattern and could destroy a provisioned HMAC secret that the orchestrator still trusts |
| 4 | Service install path: `C:\Program Files\OrchClient\` | Service install path: `C:\Program Files\HermesOrchClient\` (matches v0.7 §1 install list) | v0.7 §1 / §5 locked install path |
| 5 | `C:\Program Files\OrchClient\config\agent.yaml` | `C:\ProgramData\HermesOrchClient\config.yaml` (operator-owned, NOT in any MSI component); MSI ships `C:\ProgramData\HermesOrchClient\config.yaml.example` only | v0.7 §0.z locked: `config.yaml` is operator-owned, `config.yaml.example` is the only MSI-shipped template, both live under `%ProgramData%\HermesOrchClient\` |
| 6 | `Get-Service | Where-Object { $_.Name -like '*orch*' -or $_.Name -like '*hermes*' }` for pre-install check | Same query, but documented as "should NOT return `OrchClient`" (the legacy `HermesOrchServer` on the orchestrator host is a different machine, not the target) | Avoids confusion on the orchestrator host where `HermesOrchServer` is the server, not a client |
| 7 | Step 7: "The orch client should auto-enroll on first start" | Step 7: "On first start the service calls the orchestrator's anonymous-enroll endpoint using the `enrollment_token` from `config.yaml`. On success, the orchestrator records the agent and binds `agent_id` ↔ HMAC secret. The HMAC secret in `agent-secret.bin` is used for all subsequent signed requests" | v0.7 §1.4 + §1.5: enrollment-token-first; HMAC for subsequent; signed endpoints forbid query strings |
| 8 | Troubleshooting: "Auth / 401 / 403 errors → re-paste `hmac_secret` from operator" | Same troubleshooting but rooted in `agent-secret.bin` (not `config.yaml`); add a row for "agent stays `pending` > 2 min" → "check `enrollment_token` is the value the operator recorded on the orchestrator side and is not yet consumed" | v0.7 §1.4 enrollment flow + secret-at-rest layout |

All v0.1 sections preserved where not directly affected. Section
numbering kept stable. New section `0` added for the changelog.

### 0.1 v0.2 → v0.3 cert-pinning patch (2026-08-13)

| # | v0.2 said | v0.3 says | Reason |
|---|---|---|---|
| 9 | "Before you start" checklist did not list the orch server's TLS cert; `orchestrator_url` was the only HTTPS-related item | Checklist adds **Orchestrator cert SHA-256 fingerprint** (lower-case hex, no colons, 64 hex chars; operator runs `openssl x509 -in server.crt -noout -fingerprint -sha256` on the orch host and pastes the value after `SHA256 Fingerprint=`) | v0.7 §1.6 + §0.bis: the new client (this MSI) uses cert fingerprint pinning, not OS trust store. Without the fingerprint, the service fails closed (placeholder / missing / comment-only value) |
| 10 | `config.yaml` example did not include the cert fingerprint field | Step 5a adds `orchestrator_ca_fingerprint_sha256: <PASTE_FINGERPRINT_HERE>` field with a comment pointing to v0.7 §1.6 | The pinned value is per-deployment, not in `MANIFEST.json`; each agent's `config.yaml` carries its own pinned value. Operator pastes once at install time |
| 11 | Troubleshooting table had no row for "TLS handshake fails" / "certificate verify failed" | New row: "TLS handshake fails" → "`orchestrator_ca_fingerprint_sha256` in `config.yaml` does not match the orch server's current cert. Re-fetch the fingerprint from the operator (orch server may have re-gen'd cert since first install); paste the new value; restart the service. Do NOT switch to `verify=False` or trust-store fallback — pinning is the only trust model" | Pinning is the only trust model in v0.7 §1.6. The troubleshooting row makes the rotation-flow explicit |
| 12 | "What to report back" did not include the cert fingerprint | Adds "the cert fingerprint you used (so the operator can confirm it matches the orch server's current cert at audit time)" | Audit trail: the operator can verify at any time that every agent's pinned fingerprint matches the orch server's current cert |

**Forbidden (per v0.7 §1.6):** shipping a real fingerprint in the MSI
template, hard-coding a fingerprint in `config.yaml.example`, shipping
the cert file inside the MSI, adding the cert to the OS trust store,
or setting `INSECURE_SKIP_TLS_VERIFY=1`. The fingerprint is always
operator-input at deployment time; the cert never leaves the orch
host.

### 0.2 v0.3 → v0.4 bootstrapper patch (2026-08-13)

| # | v0.3 said | v0.4 says | Reason |
|---|---|---|---|
| 13 | No Quick-start section; the runbook jumped straight into 8 manual PowerShell steps | New "Quick start" section at the top: "if your operator gave you `install-orch-client.ps1`, just run it as Administrator. The 8 manual steps below are for operators and power users only." The bootstrapper collects the same 7 values interactively, validates them, runs the install, and verifies enrollment | User profile (locked): target audience is semi-technical, NOT developers. A 8-step PowerShell runbook with a manual base64 secret write is a 7-place footgun. The bootstrapper reduces 8 steps + 7 manual YAML edits + 1 base64 secret write to one interactive script with plain-English error messages at every failure |
| 14 | Step 1-8 presented as the user-facing path | All 8 steps demoted to a "Manual install (advanced)" section at the bottom, with a note that "every step below corresponds to a section of the bootstrapper source code (`installer/bootstrapper/install-orch-client.ps1`)" | Manual install is still the reference for operators / power users; it is NOT deleted. The Quick start supersedes it as the user-facing path |

### 0.3 v0.4 → v0.4.1 firewall auto-add patch (2026-08-15)

| # | v0.4 said | v0.4.1 says | Reason |
|---|---|---|---|
| 15 | User has to manually add a Windows Firewall outbound allow rule for the orchestrator port (TCP 8765 or whatever port the operator chose) before the agent can reach the orchestrator. The bootstrapper's `PORT_UNREACHABLE` error tells the user to "check firewall" but doesn't fix it | The bootstrapper auto-adds a Windows Firewall outbound allow rule via `netsh advfirewall firewall add rule` as part of the install flow, right after pre-flight and before MSI install. The rule name is `HermesOrchestrator Agent (Outbound) - <fqdn>:<port>` (unique per orch target, so multiple orchs can coexist). The add is idempotent: if the rule already exists, it's a no-op. A new `FIREWALL_RULE_FAILED` plain-English error case handles the rare netsh failure. If a later install step fails after the rule was added, the catch block best-effort removes the rule (rollback) before showing the error | The "user manually edits Windows Firewall" step is the single biggest friction for a regular user — 4-5 dialog clicks, they have to know "Outbound Rules", "Port", "TCP", the right port number. Auto-add removes that step entirely. The user's mental model collapses to: "I ran the script, it asked me 7 questions, it said SUCCESS in 2 minutes." Matches the user-profile principle: "東西都齊, 就是怎樣方便新user 安裝" |
| 16 | 13 plain-English error cases (12 from v0.7.1 + EXISTING_SERVICE* wildcard) | 14 plain-English error cases (added FIREWALL_RULE_FAILED) | New failure mode needs a new error mapping. The error message includes 3 fallback options so the user can self-recover without operator intervention |
| 15 | Header / `## 0. v0.1 → v0.2 changelog` did not mention bootstrapper | Header bumped to v0.4; changelog gets a `### 0.2` subsection listing the Quick-start + manual-demotion | Change is structural, not a content correction |

### 0.4 v0.4.1 → v0.4.2 real TLS cert fingerprint pinning patch (2026-08-15)

| # | v0.4.1 said | v0.4.2 says | Reason |
|---|---|---|---|
| 17 | The bootstrapper's `Wait-ForEnrollment` uses `Invoke-WebRequest` for the HMAC-signed poll. The pre-flight `Test-CertFingerprint` is a FORMAT check (regex `^[0-9a-f]{64}$` on the user-pasted 64-char hex) only — it does NOT verify the actual TLS cert the orch presents. For a self-signed orch cert, the default cert validation FAILS outright, so the poll loop never actually worked end-to-end against a self-signed orch (the regex was a hopeful check, not a real pin) | `Wait-ForEnrollment` now uses `[System.Net.Http.HttpClient]` with a custom `HttpClientHandler.ServerCertificateCustomValidationCallback` that: (1) computes SHA-256 of `cert.Export([X509ContentType]::Cert)` (the cert's DER bytes), (2) compares lowercase hex to the user-pasted `$CertFingerprint`, (3) returns `$true` only if the hashes match. Force `[Net.ServicePointManager]::SecurityProtocol = Tls12` for the TLS handshake stage (PS 5.1 / .NET Framework 4.x default is SSL3 / TLS 1.0, which the orch's cert rejects). New `-CertFingerprint` parameter passed at the call site. Improved `CERT_MISMATCH` error message to cover both cert mismatch AND TLS handshake failure (e.g. older TLS versions on the orch) | The format check was a hopeful lie. With this patch, the bootstrapper's `Wait-ForEnrollment` actually verifies the orch's cert against the pinned fingerprint at TLS handshake time, before any HMAC header is sent. This makes the cert-pinning contract per v0.7 §1.6 enforceable from the client side. The PS 5.1 .NET bug (Invoke-WebRequest can't do TLS 1.2) was the proximate cause; the deeper cause is the cert-pinning spec was designed but never implemented for self-signed orchs |
| 18 | PowerShell `Invoke-WebRequest` is used for the HTTPS enrollment poll | PowerShell `[System.Net.Http.HttpClient]` is used (force TLS 1.2 in handler init) | PS 5.1 .NET Framework 4.x has a known SChannel / HttpClient bug that breaks `Invoke-WebRequest` for TLS 1.2+ endpoints. `HttpClient` with explicit `SecurityProtocol = Tls12` is the fix; same pattern documented in the agent's C# / Python / PowerShell HMAC codebases |
| 19 | `Wait-ForEnrollment` did not explicitly dispose `HttpClient` / `HttpClientHandler` | Explicit `$client.Dispose()` and `$handler.Dispose()` in the deadline-exceeded branch; `HttpRequestMessage` is disposed in `finally` | Bootstrapper runs once per install; not a long-lived leak, but the explicit dispose is correct hygiene and matches the codebase pattern |

**The 8 manual steps (Step 1 through Step 8 + Uninstall + Troubleshooting
+ Quick reference + Report back + Cross-references) are all preserved
verbatim.** The bootstrapper internally implements every one of them.
The Quick start adds ONE thing: a pointer to the bootstrapper as the
recommended path for semi-technical users.

**Forbidden (per v0.7.1 §0.af-bootstrap):** shipping a real fingerprint
in the MSI template, hard-coding a fingerprint in
`config.yaml.example`, shipping the cert file inside the MSI, adding
the cert to the OS trust store, or setting `INSECURE_SKIP_TLS_VERIFY=1`
(unchanged from v0.3). Plus, NEW: shipping the bootstrapper with a
statically-baked cert fingerprint or agent_id (the bootstrapper must
collect all 7 values at runtime).

---

## Manual install (advanced) — for operators and power users

The 8 steps in this section are what the bootstrapper (Quick start above)
implements internally. If the bootstrapper is not available, or if you
need to install without it (e.g. you are running an older MSI build that
does not ship the bootstrapper), follow these steps by hand. Every step
below corresponds to a section of the bootstrapper source code (see
`installer/bootstrapper/install-orch-client.ps1`).

**Prerequisites (same as the bootstrapper's pre-flight):**

- [ ] Admin PowerShell (right-click → "Run as Administrator")
- [ ] The 7 values from the operator (FQDN, port, cert fingerprint,
      agent_id, key_id, enrollment_token, HMAC secret)
- [ ] The MSI file (`orch-client-setup.msi`) on a staging path

---


## Before you start (checklist)

You need:

- [ ] **Target machine:** Windows 10 or 11, 64-bit
- [ ] **Admin / install privileges** on the target machine
- [ ] **HTTPS network access** from the target to the orchestrator host.
      Orchestrator URL pattern is **`https://<ORCHESTRATOR_FQDN>:<HTTPS_PORT>/`** —
      the operator gives you the exact values (B13 transport is HTTPS-or-closed;
      the v0.7 service fails closed on HTTP / missing / placeholder)
- [ ] **Orchestrator FQDN + HTTPS port** (ask your operator; v0.7 §7 #8)
- [ ] **A new `agent_id`** (e.g. `win-b-02`; must NOT match any existing
      agent — your operator will tell you which one to use)
- [ ] **HMAC `key_id`** (your operator will assign; v0.7 §1.4)
- [ ] **A one-time `enrollment_token`** for this agent (your operator
      generates it on the orchestrator and pastes it to you out-of-band;
      v0.7 §1.4 — the token is consumed on first successful enroll)
- [ ] **HMAC secret** (32+ random bytes; your operator generates and
      pastes to you out-of-band; you will write it into
      `agent-secret.bin`, not into `config.yaml`; v0.7 §0.4)
- [ ] **Orchestrator cert SHA-256 fingerprint** (the orch server's
      self-signed cert, lower-case hex, **no colons, 64 hex chars**;
      your operator runs `openssl x509 -in server.crt -noout
      -fingerprint -sha256` on the orch host and pastes you the value
      after `SHA256 Fingerprint=`; v0.7 §1.6 + §0.bis). **Do NOT**
      substitute `Get-FileHash` on the cert file — that gives the
      file's SHA-256, not the cert's
- [ ] **Orch client installer file** (.msi) + the matching SHA-256 hash
      + the signing-cert fingerprint (your operator provides these —
      DO NOT download from anywhere else)
- [ ] **A staging folder** for the installer (e.g. `C:\Install\orch-client\`)

> If you do NOT have the installer, SHA-256, signing-cert, FQDN, port,
> `agent_id`, `key_id`, `enrollment_token`, and HMAC secret from your
> operator, **STOP HERE** and ask. Do not download from random sites.

---

## Step 1 — Get the installer onto the target

1. Open **PowerShell as Administrator** on the target machine.
2. Create a staging folder:
   ```powershell
   New-Item -ItemType Directory -Path 'C:\Install\orch-client' -Force
   Set-Location 'C:\Install\orch-client'
   ```
3. Copy the installer into the staging folder. Use the exact file the
   operator provided. Do not rename.

   Example:
   ```powershell
   # Assuming the operator dropped it on a share
   Copy-Item '\\fileserver\shares\orch-client-setup.msi' 'C:\Install\orch-client\'
   ```

---

## Step 2 — Verify the installer (this MUST match)

This step is the security gate. The installer hash MUST match the value
your operator provided. If it does not match, **DO NOT INSTALL**.

```powershell
# Show the file you have
Get-ChildItem 'C:\Install\orch-client\*' | Select-Object Name, Length

# Compute SHA-256
$installer = Get-ChildItem 'C:\Install\orch-client\*.msi','C:\Install\orch-client\*.exe' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256
```

The hash output will look like:

```
Algorithm       Hash                                                                   Path
--------       ----                                                                   ----
SHA256         AABBCCDD11223344...                                                    C:\Install\orch-client\orch-client-setup.msi
```

Compare the **Hash** column with the value your operator gave you.
They must match **exactly** (case-insensitive).

- ✅ **Match** → continue to Step 3.
- ❌ **Mismatch** → **STOP. Do not run the installer.** Contact your operator.

(If your operator also gave you a signing certificate fingerprint, you
can additionally verify the Authenticode signature, but the SHA-256
match is the primary gate.)

---

## Step 3 — Pre-install checks

```powershell
# 3a. Can you reach the orchestrator over HTTPS?
# Replace <ORCHESTRATOR_FQDN> and <HTTPS_PORT> with the values the operator gave you
$orchHost = '<ORCHESTRATOR_FQDN>'
$orchPort = <HTTPS_PORT>
Test-NetConnection -ComputerName $orchHost -Port $orchPort
```

Expected: `TcpTestSucceeded : True`. If False, the target cannot reach
the orchestrator — check the network / firewall before continuing.

> Do NOT use plain-HTTP `http://` for the URL. B13 transport is closed
> for new enrollment over plain HTTP; the v0.7 client fails closed on
> any non-HTTPS URL. If the orchestrator only exposes HTTP, **STOP** and
> ask the operator to enable HTTPS first.

```powershell
# 3b. Is there already an orch client installed? (avoid double-install)
# Should NOT return 'OrchClient' (the orchestrator host runs 'HermesOrchServer', not 'OrchClient')
Get-Service | Where-Object { $_.Name -like 'OrchClient' }
```

Expected: empty. If `OrchClient` is listed, you may be on the wrong
machine, or the client was already installed — check with your operator
before continuing.

```powershell
# 3c. (Informational — the MSI installs a machine-owned CPython if needed)
python --version
```

If Python is installed, note the version. The orch client installer
will install a machine-owned CPython at
`C:\Program Files\HermesOrchClient\venv\` (using
`C:\Program Files\Python314\python.exe` as the base) regardless, so
you don't need to install Python manually.

---

## Step 4 — Install the orch client

Run the installer (the exact file you verified in Step 2):

```powershell
# Adjust the filename to match what your operator provided
Start-Process -FilePath 'C:\Install\orch-client\orch-client-setup.msi' -ArgumentList '/qn' -Wait
```

The MSI runs silently and returns. The orch client is now installed.

Verify:

```powershell
# The service should exist (start = demand, NOT auto-start)
Get-Service | Where-Object { $_.Name -like 'OrchClient' }
```

Note the service name (`OrchClient`). You will need it in Step 6.

Verify the MSI-owned data drop:

```powershell
# These should exist (dropped by the MSI in permanent components)
Get-ChildItem 'C:\ProgramData\HermesOrchClient\' -ErrorAction SilentlyContinue
#   config.yaml.example         — MSI-owned template (permanent)
#   secrets\                    — MSI-owned directory
#   secrets\agent-secret.bin    — zero-byte placeholder (permanent; NeverOverwrite)
```

`config.yaml` (operator-owned) does **NOT** exist yet — you will create
it in Step 5.

---

## Step 5 — Configure the agent (split: config + secret file)

The installer creates two MSI-owned data files at fixed paths:

| File | Owner | What to do |
|---|---|---|
| `C:\ProgramData\HermesOrchClient\config.yaml.example` | MSI (permanent) | Reference only. **Do not edit.** Copy to `config.yaml` and edit the copy |
| `C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin` | MSI (permanent) | Write the real HMAC secret bytes here. Do NOT put the secret in `config.yaml` |

### 5a — Create `config.yaml` from the example

```powershell
Copy-Item -LiteralPath 'C:\ProgramData\HermesOrchClient\config.yaml.example' `
          -Destination 'C:\ProgramData\HermesOrchClient\config.yaml' -Force
notepad 'C:\ProgramData\HermesOrchClient\config.yaml'
```

Fill in these fields. **No secret in this file.**

```yaml
# Operator-owned. Not in any MSI component.
# v0.7 layout: secret lives in agent-secret.bin, NOT here.
agent_id:        <AGENT_ID>          # e.g. win-b-02
key_id:          <KEY_ID>            # operator-assigned; used for HMAC key rotation
orchestrator_url: https://<ORCHESTRATOR_FQDN>:<HTTPS_PORT>/
orchestrator_ca_fingerprint_sha256: <PASTE_FINGERPRINT_HERE>  # 64 hex chars, no colons; v0.7 §1.6
enrollment_token: <ENROLLMENT_TOKEN> # one-time; consumed on first enroll
log_level:       info
```

> **Do NOT** put `hmac_secret` in this file. The secret lives in
> `agent-secret.bin` with SDDL `D:P(A;;FA;;;SY)(A;;FA;;;BA)` (only
> SYSTEM and BUILTIN\Admins). See v0.7 §0.4.

Save and close.

### 5b — Write the HMAC secret to `agent-secret.bin`

The operator gave you the HMAC secret out-of-band (a base64 string, or
hex, or raw bytes — confirm the encoding with the operator). Write it
to `agent-secret.bin` exactly once. The placeholder is zero bytes and
has `NeverOverwrite="yes"`, so a future repair / MajorUpgrade will not
clobber your real secret.

```powershell
# Example: secret is base64 (operator confirms the encoding)
$secretB64 = '<PASTE_BASE64_SECRET_HERE>'
[System.IO.File]::WriteAllBytes(
    'C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin',
    [System.Convert]::FromBase64String($secretB64))

# Verify the size and ACL
Get-ChildItem 'C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin' | Select-Object Name, Length
Get-Acl    'C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin' | Select-Object -ExpandProperty Sddl
```

Expected SDDL on the file: `O:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)`. If you
see extra ACEs (e.g. `BUILTIN\Users` with any access), **STOP** and
contact the operator.

> **Important:** the secret file contains the HMAC key. Treat it like
> a password. Do not share, screenshot, or commit it to a repo. The
> file is readable only by SYSTEM and BUILTIN\Admins.

---

## Step 6 — Start the service

```powershell
# Use the service name from Step 4
Start-Service -Name 'OrchClient'
Start-Sleep -Seconds 3
Get-Service -Name 'OrchClient'
```

Expected: `Status : Running`. If `Stopped` or another status, check the
service's event log:

```powershell
Get-EventLog -LogName Application -Source 'OrchClient' -Newest 20
```

(or use Event Viewer: `eventvwr.msc` → Windows Logs → Application)

First-release failure actions are `none / none / none` (v0.7 §1): if
the service fails to enroll or auth, it does NOT auto-restart-loop.
Check the event log and ask the operator.

---

## Step 7 — Enroll with the orchestrator

On first start the service calls the orchestrator's anonymous-enroll
endpoint using the `enrollment_token` from `config.yaml`. On success:

1. Orchestrator records the new agent with the bound `agent_id`
2. Orchestrator binds `agent_id` ↔ HMAC secret (the one in
   `agent-secret.bin`; only the SHA-256 is stored on the server side
   per v0.7 §1.4)
3. The `enrollment_token` is consumed (cannot be reused)
4. The service switches to HMAC-signed requests for all subsequent
   heartbeats (signed endpoints forbid query strings; v0.7 §1.4)

To verify the enrollment reached the orchestrator, ask your operator to
run this on the orchestrator host:

```powershell
# Operator runs this on the orchestrator host
Get-AgentList  # (or whatever admin CLI / endpoint your operator uses)
```

You should see your new `agent_id` (`win-b-02` in this example) in the
list, with `status = verified`.

If the agent does NOT appear after 30 seconds:

```powershell
# On the target, check the agent's log
Get-Content 'C:\ProgramData\HermesOrchClient\logs\orch-client.log' -Tail 50 -ErrorAction SilentlyContinue
```

Look for:
- "agent enrolled" / "enroll success" (good)
- "enrollment_token rejected" / "agent_id already exists" / "auth failed" (bad — see Troubleshooting)

---

## Step 8 — Verify the registration

End-to-end verification (operator-side, on the orchestrator host):

```powershell
# Operator checks the agent's status + recent heartbeat
Get-AgentStatus -Id '<AGENT_ID>'   # operator's actual command
```

Expected output:

```
agent_id        : <AGENT_ID>
status          : verified
last_heartbeat  : <recent timestamp>
profiles        : <list>
```

If `status` is `pending` instead of `verified`, wait 30 seconds and
re-check. If still `pending` after 2 minutes, see Troubleshooting.

---

## Uninstall / rollback (v0.7 PermanentFeature pattern)

The v0.7 MSI uses a **PermanentFeature** at the Package level for the
3 data files (`config.yaml`, `config.yaml.example`,
`agent-secret.bin`). The standard uninstall via the same MSI removes
**only** the program files and the service registration. The 3 data
files are **preserved**.

```powershell
# 1. Stop the service
Stop-Service -Name 'OrchClient'

# 2. Uninstall via the same installer (preserves the 3 data files)
Start-Process -FilePath 'C:\Install\orch-client\orch-client-setup.msi' `
    -ArgumentList '/uninstall','/qn' -Wait

# 3. (Optional, recommended) Stash a copy of the 3 data files for the next install
#    The next install will create a new placeholder secret; the operator
#    can re-paste the original secret into the new placeholder.
$backup = 'C:\Install\orch-client\stashed-{0:yyyyMMdd-HHmmss}' -f (Get-Date)
New-Item -ItemType Directory -Path $backup -Force | Out-Null
Copy-Item 'C:\ProgramData\HermesOrchClient\config.yaml'          $backup
Copy-Item 'C:\ProgramData\HermesOrchClient\config.yaml.example' $backup
Copy-Item 'C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin' $backup
"Stashed data files to $backup"
```

After uninstall, the install dir `C:\Program Files\HermesOrchClient\`
is gone, but the data dir `C:\ProgramData\HermesOrchClient\` still
contains the 3 preserved files (plus `logs\` if it exists).

### Explicit removal of the preserved data files (v0.7 follow-up)

Removing `config.yaml` and `agent-secret.bin` after uninstall is a
**separate, follow-up** operation in v0.7 (the "explicit privileged
cleanup script" — v0.7 §7 #13). It is **not** part of the standard
uninstall. Reasons:

- `agent-secret.bin` is the HMAC key. Until the operator also removes
  the agent record on the orchestrator (DELETE `/api/agents/{id}` with
  admin auth), destroying the file can leave the orchestrator trusting
  a key that no longer exists on any target — a forced re-enrollment
  hazard.
- `config.yaml` may contain operator-specific overrides the operator
  wants to inspect before deleting.
- Both files are ACL-restricted; a blanket `Remove-Item -Recurse` by
  an admin who has not confirmed intent is exactly the kind of footgun
  the PermanentFeature pattern exists to prevent.

The explicit cleanup script is a separate work item and will ship under
a v0.7 follow-up. **Do not script or run a manual `Remove-Item` of
`C:\ProgramData\HermesOrchClient\*` against a provisioned production
agent without first deleting the agent record on the orchestrator.**

> Do not delete the data dir until the operator has removed the agent
> record from the orchestrator — otherwise the orchestrator may keep
> trusting an HMAC key for an agent that no longer exists.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Installer SHA-256 mismatch | Wrong / tampered / partially-downloaded file | Re-fetch from the operator; do not run |
| `Test-NetConnection` to orchestrator fails | Network / firewall | Check FQDN resolves, HTTPS port open, certificate valid; ask operator to verify firewall rules. Do **not** fall back to HTTP |
| Service won't start | Config error / port conflict / Python venv broken | Check event log; verify `config.yaml` syntax (no tabs, valid YAML); check `netstat` for port conflicts |
| `enrollment_token rejected` in log | Token already consumed, or wrong agent_id, or wrong FQDN | Ask operator to issue a new token; verify `agent_id` in `config.yaml` matches the operator's record |
| Agent not in orchestrator list after 30s | Enroll endpoint unreachable, or auth failed | Check agent log for `4xx` / `5xx`; verify `orchestrator_url` is reachable from target AND uses `https://` |
| `status = pending` > 2 min | HMAC mismatch: the `agent-secret.bin` you wrote does not match what the orchestrator stored on enroll | Operator re-checks the SHA-256 of the secret that was bound during enroll; if different, re-enroll (operator issues a new token) |
| `connection refused` in agent log | Orchestrator down, or wrong port, or wrong scheme | Operator verifies orchestrator is up and listening on the **HTTPS** port (not HTTP); verify scheme in `orchestrator_url` |
| Auth / 401 / 403 errors after enroll | HMAC key bytes don't match, or query string on signed endpoint | Re-paste secret from operator into `agent-secret.bin`; do not edit by hand. Per v0.7 §1.4, signed endpoints forbid query strings — do not add `?...` to a signed URL |
| Service crashes repeatedly | `agent-secret.bin` is missing / empty / unreadable (SDDL stripped, or wrong owner) | Re-create the secret file with the correct SDDL; restart the service. The service fails closed on a missing or wrong-DACL secret |
| Uninstall removes the 3 data files | v0.7 PermanentFeature was not applied (wrong MSI / wrong build) | This is a build bug, not an operator error. **STOP**, restore from your stash backup (above), and ask the operator to rebuild and re-test on a clean VM |
| TLS handshake fails / "certificate verify failed" | `orchestrator_ca_fingerprint_sha256` in `config.yaml` does not match the orch server's current cert (orch server may have re-gen'd cert since first install) | Re-fetch the fingerprint from the operator (`openssl x509 -in server.crt -noout -fingerprint -sha256` on the orch host), paste the new value into `config.yaml`, restart the service. Do **NOT** switch to `verify=False`, do **NOT** fall back to the OS trust store, do **NOT** set `INSECURE_SKIP_TLS_VERIFY=1` — pinning is the only trust model in v0.7 §1.6 |
| `CERTIFICATE_VERIFY_FAILED` with "Hostname mismatch" | The cert's CN/SAN does not include the FQDN you used in `orchestrator_url` (default cert SANs are `hostname, localhost, 127.0.0.1` only) | Either: (a) use the orch host's hostname (not its IP) in `orchestrator_url` and ensure DNS / `hosts` file resolves it, OR (b) ask the operator to re-gen the cert with the FQDN (or IP) in SANs. Per v0.7 §1.6, direct-IP is out of scope for first release |

---

## Quick reference (copy-paste summary)

```powershell
# 1. Stage
New-Item -ItemType Directory -Path 'C:\Install\orch-client' -Force
Set-Location 'C:\Install\orch-client'
# copy installer here (operator-provided .msi)

# 2. Verify hash (must match operator's value)
$installer = Get-ChildItem 'C:\Install\orch-client\*' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256

# 3. Pre-check (HTTPS only)
$orchHost = '<ORCHESTRATOR_FQDN>'
$orchPort = <HTTPS_PORT>
Test-NetConnection -ComputerName $orchHost -Port $orchPort
Get-Service | Where-Object { $_.Name -like 'OrchClient' }

# 4. Install
Start-Process -FilePath $installer.FullName -ArgumentList '/qn' -Wait

# 5a. Configure (no secret in this file)
Copy-Item 'C:\ProgramData\HermesOrchClient\config.yaml.example' 'C:\ProgramData\HermesOrchClient\config.yaml' -Force
notepad 'C:\ProgramData\HermesOrchClient\config.yaml'

# 5b. Write HMAC secret to the dedicated file (SDDL = SY+BA only)
[System.IO.File]::WriteAllBytes(
    'C:\ProgramData\HermesOrchClient\secrets\agent-secret.bin',
    [System.Convert]::FromBase64String('<PASTE_BASE64_SECRET_HERE>'))

# 6. Start
Start-Service -Name 'OrchClient'
Get-Service -Name 'OrchClient'

# 7. Check log
Get-Content 'C:\ProgramData\HermesOrchClient\logs\orch-client.log' -Tail 50 -ErrorAction SilentlyContinue

# 8. (Operator-side) verify in orchestrator
# Get-AgentStatus -Id '<AGENT_ID>'

# Uninstall (preserves the 3 data files; explicit cleanup is a follow-up)
Stop-Service -Name 'OrchClient'
Start-Process -FilePath $installer.FullName -ArgumentList '/uninstall','/qn' -Wait
```

---

## What to report back to the operator

After you finish, tell the operator:

- The **`agent_id`** you used
- The **target hostname** (so they can identify the machine)
- The **installer filename + SHA-256** you used
- The **orchestrator FQDN + HTTPS port** you targeted
- The **cert fingerprint** you used (so the operator can confirm it
  matches the orch server's current cert at audit time; v0.7 §1.6)
- The **service status** (Running / Stopped / etc.)
- The **last 20 lines of `orch-client.log`** if there were any errors
- (If available) the **status** the operator sees in the orchestrator

The operator will confirm "verified" or troubleshoot.

---

## Cross-references

- `docs/proposals/orch-client-build-impl-plan-v0.7.md` — build plan this
  runbook is aligned with. See §0.bis (v0.7 cert-pinning patch), §0.z,
  §0.4, §0.4-bis, §0.y, §1, §1.4, §1.5, §1.6 (cert fingerprint
  pinning — the design behind Step 5a's
  `orchestrator_ca_fingerprint_sha256` field and the Before-you-start
  checklist's cert-fingerprint line), §7 (operator-binding
  dependencies, including the new #14 cert fingerprint), §8 (forbidden
  actions), §12 (next steps).
