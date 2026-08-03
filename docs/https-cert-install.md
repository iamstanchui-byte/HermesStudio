# HTTPS / TLS Certificate Install Guide (v3.12.0)

End-to-end runbook for enabling HTTPS on the orchestrator and getting
every client (browser, wrapper, CLI tool) to trust the self-signed
cert.

## 0. Overview

The orchestrator's TLS is **optional** — default is plain HTTP for
dev / LAN. When you flip the toggle in Settings, uvicorn boots with
the cert + key you point it at, and every HTTP client needs to either
**trust the cert** (pin) or **skip verification** (dev only).

```
┌──────────────────┐    HTTPS     ┌──────────────────┐
│  Browser         │──────────────│  Orchestrator    │
│  (dashboard user)│ cert pin     │  (uvicorn + ssl) │
└──────────────────┘              └──────────────────┘
                                        ▲    ▲
                                  cert pin   cert pin
                                        │    │
┌──────────────────┐              ┌─────┴────┴─────┐
│  Wrapper         │              │                 │
│  (agent host)    │──────────────│  linux-a-01     │
│  cert pin via    │              │  win-local-1    │
│  ORCHESTRATOR_CA_BUNDLE          │                 │
└──────────────────┘              └─────────────────┘
```

Two layers of "trust":

1. **Transport** — TLS handshake. Either trust the cert, or don't
   verify. Skipping verify is dev-only (MITM risk).
2. **Application** — HMAC over the body (unchanged by TLS). Even
   over HTTPS, the wrapper still signs every request with its
   shared secret. TLS + HMAC = belt + suspenders.

The cert in this doc is a **self-signed** cert. The browser will
show "Not Secure" by default; installing it in the OS trust store
removes the warning. For production with a public domain, swap
to a Let's Encrypt / internal CA cert — the orchestrator config
doesn't change, just point `https.ssl_cert_path` /
`https.ssl_key_path` at the new files.

---

## 1. Server setup (one-time, on the orchestrator host)

### 1.1 Generate the cert

The orchestrator ships a `gen-cert` subcommand. Run it from the
project root:

```bash
hermes-orch gen-cert
```

By default this writes:

- `~/.hermes-orchestrator/certs/server.crt` (cert, 1200 bytes)
- `~/.hermes-orchestrator/certs/server.key` (private key, mode 0600)

The cert is RSA 2048, valid 365 days, with SANs:
`hostname`, `localhost`, `127.0.0.1`. Adjust with flags:

```bash
hermes-orch gen-cert \
    --hostname hermes-win \
    --days 365 \
    --out-dir /custom/cert/path \
    --force            # overwrite existing files
```

`gen-cert` prints a ready-to-paste `config.yaml` snippet, so you
don't have to type the paths by hand.

### 1.2 Enable HTTPS in Settings

Open `https://localhost:8765/settings` (or `http://...` if HTTPS
isn't on yet), scroll to **HTTPS / TLS**, fill in:

- Enable HTTPS: checked
- Cert file path: `C:/Users/stanley/.hermes-orchestrator/certs/server.crt`
  (or wherever you generated it)
- Key file path: same dir + `server.key`
- Click **Save**

Or hand-edit `~/.hermes-orchestrator/config.yaml`:

```yaml
https:
  enabled: true
  ssl_cert_path: C:/Users/stanley/.hermes-orchestrator/certs/server.crt
  ssl_key_path:  C:/Users/stanley/.hermes-orchestrator/certs/server.key
```

### 1.3 Restart the orchestrator

The watchdog only restarts the server when it sees the port go
down. To force a config reload:

```powershell
# Easiest — touch the watchdog's restart-now flag
"C:\Users\stanley\.hermes-orchestrator\restart-orch.bat" --wait

# Or use the PowerShell control script
C:\Users\stanley\.hermes-orchestrator\orch-ctl.ps1 restart -Wait
```

The watchdog will kill the old server and spawn a new one in
~20s. The new server will:

- Read `config.yaml` and pick up `https.enabled=true`
- Bind to port 8765 with TLS (uvicorn `--ssl-keyfile --ssl-certfile`)
- Set the `Secure` flag on every `Set-Cookie` response

If the cert or key path is bad / unreadable, the watchdog logs
a `WARN:` and falls back to plain HTTP. The Settings page will
show a "configured but not ready" yellow state.

### 1.4 Verify the server is on HTTPS

```powershell
C:\Users\stanley\.hermes-orchestrator\orch-ctl.ps1 status
```

Output:

```
  server: https://127.0.0.1:8765  (PID 12345)
  config https:
    enabled: true
    ssl_cert_path: C:/Users/stanley/.hermes-orchestrator/certs/server.crt
    ssl_key_path:  C:/Users/stanley/.hermes-orchestrator/certs/server.key
  watchdog: armed
```

If you see `server: http://...` instead, the server didn't pick
up the config — check the watchdog log:

```powershell
Get-Content "C:\Project\minimax code\hermes-orchestrator\watchdog\watchdog-hermes.log" -Tail 5
Get-Content "C:\Project\minimax code\hermes-orchestrator\watchdog\server.stdout.log" -Tail 10
```

The `server.stdout.log` should show `Starting Hermes Orchestrator on https://...`.

---

## 2. Client setup (per client type)

### 2.1 Browser (dashboard user)

The browser needs to trust the cert. Three OSes, three methods.

#### Windows (Chrome, Edge, Firefox all use the system store)

Run this in an **Administrator** PowerShell:

```powershell
Import-Certificate -FilePath "C:\Users\stanley\.hermes-orchestrator\certs\server.crt" `
    -CertStoreLocation Cert:\LocalMachine\Root
```

Restart the browser (Chrome keeps certs cached; Firefox uses its
own store and needs `about:config` trust override OR a manual
add via Settings → Privacy & Security → Certificates → View
Certificates → Authorities → Import).

Verify: close all browser windows, reopen, navigate to
`https://hermes-win:8765`. Padlock should be green (Chrome
might take 30s to refresh the trust store; Firefox needs a
manual one-time add).

#### macOS (Safari, Chrome, Firefox all use the Keychain)

```bash
sudo security add-trusted-cert -d -r trustRoot \
    -k /Library/Keychains/System.keychain \
    server.crt
```

Restart the browser. Verify with `https://hermes-win:8765`.

#### Linux (apt-based distros)

```bash
sudo cp server.crt /usr/local/share/ca-certificates/hermes-orch.crt
sudo update-ca-certificates
```

For per-user trust (no sudo needed, only affects your account):

```bash
certutil -d sql:$HOME/.pki/nssdb -A -t C -n hermes-orch \
    -i server.crt    # Firefox (NSS store)
```

Chrome and Edge pick up the system store automatically after
restart.

#### iOS / iPadOS

1. AirDrop / email the cert to the device
2. Settings → General → VPN & Device Management → tap the profile → Install
3. Settings → General → About → Certificate Trust Settings → enable the cert

#### Android

1. Email the cert to yourself, download attachment
2. Settings → Security → Encryption & credentials → Install a certificate → CA certificate

(Cert install on mobile is OS-version-dependent; if this section
goes stale, search the latest Android / iOS docs for "install CA
cert".)

### 2.2 Linux agent host (wrapper)

The wrapper on `linux-a-01` (or any non-orchestrator host) needs
to trust the cert. Use the **cert pin** approach — the wrapper
points `ORCHESTRATOR_CA_BUNDLE` at the cert file, and httpx
verifies only that one cert (not the whole system trust store).

#### 2.2.1 Copy the cert to the agent host

```powershell
# From the orchestrator host (Windows)
scp -i C:\Users\stanley\.ssh\id_ed25519 `
    C:\Users\stanley\.hermes-orchestrator\certs\server.crt `
    stanley@192.168.2.161:/home/stanley/.hermes-orchestrator/certs/server.crt
```

If the certs dir doesn't exist on the agent yet:

```bash
# On the agent host
mkdir -p ~/.hermes-orchestrator/certs
chmod 755 ~/.hermes-orchestrator/certs
```

Verify the certs match:

```bash
# On the agent
sha256sum /home/stanley/.hermes-orchestrator/certs/server.crt
```

Should match the orchestrator's SHA256 (visible via `certutil -hash server.crt SHA256` on Windows).

#### 2.2.2 Set the env var in the wrapper startup

The `scripts/restart_linux_wrapper.sh` script (in the project
repo) already sets `ORCHESTRATOR_CA_BUNDLE` before launching the
wrapper. If you use the user's customized
`~/.hermes-orchestrator/_restart_linux.sh`, it has the same
change. Just make sure to re-run your restart script after a
cert rotation.

Verify the env var is set on the running wrapper:

```bash
# On the agent
cat /proc/$(cat /home/stanley/.hermes-orchestrator/agent.pid)/environ | tr '\0' '\n' | grep ORCHESTRATOR
```

Should print:

```
ORCHESTRATOR_CA_BUNDLE=/home/stanley/.hermes-orchestrator/certs/server.crt
```

#### 2.2.3 Restart the wrapper

```bash
bash /home/stanley/.hermes-orchestrator/_restart_linux.sh
# or, from the orchestrator host:
"C:\Users\stanley\.hermes-orchestrator\restart-orch.bat" --wait
# (this only restarts the orchestrator, not the agent — use the first command)
```

Within 5s, the agent's heartbeat should reach the orchestrator
and `is_alive` flips back to `verified`. Verify in the
orchestrator's Settings page (Agents section) or the orch-ctl.ps1
status output.

### 2.3 Windows agent host (wrapper on the same machine as orchestrator)

If the wrapper runs on the orchestrator's own host (e.g. you have
a single Windows box that hosts the orchestrator + 1 wrapper),
the cert is already at `C:\Users\stanley\.hermes-orchestrator\certs\server.crt`
and the wrapper just needs the env var.

#### 2.3.1 For a one-off Start-Process launch

Set the env var in your PowerShell session before launching:

```powershell
$env:ORCHESTRATOR_CA_BUNDLE = "C:\Users\stanley\.hermes-orchestrator\certs\server.crt"
$env:USERPROFILE = "C:\Users\stanley"
$env:HOME = "C:\Users\stanley"

Start-Process -FilePath "C:\Project\minimax code\hermes-orchestrator\.venv\Scripts\python.exe" `
    -ArgumentList @("-m", "hermes_orch.agent_cli", "start",
                    "--config", "C:\Users\stanley\.hermes-orchestrator\wrapper-config.json",
                    "--interval", "5") `
    -WorkingDirectory "C:\Project\minimax code\hermes-orchestrator" `
    -WindowStyle Hidden `
    -RedirectStandardOutput "C:\Users\stanley\.hermes-orchestrator\daemon.out.log" `
    -RedirectStandardError  "C:\Users\stanley\.hermes-orchestrator\daemon.err.log"
```

#### 2.3.2 For a persistent NSSM service

The `register-windows-service-v2.ps1` script (v3.12.0+) already
includes `ORCHESTRATOR_CA_BUNDLE` in the service's
`AppEnvironmentExtra`. After install, the env var is set for
the service every time it starts.

```powershell
# Run as Administrator
cd "C:\Project\minimax code\hermes-orchestrator\scripts"
.\register-windows-service-v2.ps1
```

This installs `HermesOrchAgent` as a Windows service that
auto-starts on boot, auto-restarts on crash, and always picks up
the latest `ORCHESTRATOR_CA_BUNDLE` from the cert file.

#### 2.3.3 Restart the running wrapper

If the wrapper is already running (e.g. as a Start-Process
background), restart it so the new env var takes effect:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*hermes_orch.agent_cli*start*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Then re-launch per 2.3.1 (or 2.3.2 if you want it persistent).

### 2.4 CLI tools (curl, Postman, Python scripts)

For **curl**:

```bash
# Pin to the cert (preferred)
curl --cacert /path/to/server.crt https://hermes-win:8765/api/health
# -k for one-off insecure (dev only — accepts MITM)
curl -k https://hermes-win:8765/api/health
```

For **Postman**: Settings → Certificates → CA Certificates → Add
the `server.crt`. Or per-request: the request → SSL certificate
verification → OFF (insecure).

For **Python httpx** (your own scripts):

```python
import httpx
r = httpx.get("https://hermes-win:8765/api/health", verify="/path/to/server.crt")
# or set globally:
import os
os.environ["SSL_CERT_FILE"] = "/path/to/server.crt"
# (httpx + urllib3 honor SSL_CERT_FILE for `verify=True`)
```

For **Python requests**:

```python
import requests
r = requests.get("https://hermes-win:8765/api/health", verify="/path/to/server.crt")
```

---

## 3. URL hostname (matters for cert verification)

The cert is generated with these SANs by default:
`hostname`, `localhost`, `127.0.0.1` (where `hostname` is whatever
`socket.gethostname()` returns on the orchestrator host — usually
the machine's Windows name, e.g. `hermes-win`).

The wrapper / browser must use **one of these** in the URL.
Going to `https://192.168.2.152:8765` instead of
`https://hermes-win:8765` will fail cert verification (IP
addresses are not in the SANs by default).

If you need to use the IP, either:

1. Re-generate the cert with the IP in SANs:
   ```bash
   # Easiest: temporarily add the IP to /etc/hosts pointing at the
   # hostname, then re-gen. Or use a custom script.
   ```
2. Edit `gen-cert` in `src/hermes_orch/cli.py` to add the IP
   before signing.
3. Use `INSECURE_SKIP_TLS_VERIFY=1` on the wrapper (not recommended).

For DNS, ensure `hermes-win` (or whatever your hostname is)
resolves from every client. On Linux, add to `/etc/hosts` if no
DNS server does it automatically.

---

## 4. Cert rotation

Self-signed certs expire (default 365 days). 30 days before
expiry, the Settings page shows the days remaining in red.
Re-generate and re-deploy:

### 4.1 Re-generate

```bash
hermes-orch gen-cert --force
```

The new cert overwrites the old. Same path (`~/.hermes-orchestrator/certs/server.crt`).

### 4.2 Restart the orchestrator

```powershell
"C:\Users\stanley\.hermes-orchestrator\restart-orch.bat" --wait
```

### 4.3 Re-deploy the new cert to all clients

**Browsers**: re-install the cert (the OS trust store replaces
the old one automatically). Restart the browser.

**Linux agent host**: `scp` the new file (overwriting), then
restart the wrapper:

```powershell
scp -i C:\Users\stanley\.ssh\id_ed25519 `
    C:\Users\stanley\.hermes-orchestrator\certs\server.crt `
    stanley@192.168.2.161:/home/stanley/.hermes-orchestrator/certs/server.crt
```

```bash
# On the agent
bash /home/stanley/.hermes-orchestrator/_restart_linux.sh
```

**Windows wrapper** (same machine): restart the wrapper
process — the cert path is the same, so no other change.

### 4.4 Verify

The Settings page should now show the new expiry date (~365 days
from now). Both agents should flip to `verified` within 30s of
their restart. The browser should show the new cert (no warning
if the old one was previously trusted and the new one has the
same subject — or one click-through if the subject changed
because you regenerated with a different hostname).

---

## 5. Troubleshooting

### 5.1 "Your connection is not private" (browser)

The cert isn't in the OS / browser trust store. Install it per
section 2.1.

### 5.2 "Server disconnected without sending a response" (wrapper log)

The wrapper is sending **HTTP** to an **HTTPS** port (or vice
versa). Two causes:

- `orchestrator_url` in `wrapper-config.json` is `http://` but
  the orchestrator is on `https://`. Update to `https://`.
- The URL is correct but the cert is being rejected (see 5.3).

### 5.3 "CERTIFICATE_VERIFY_FAILED" (wrapper log)

The wrapper can't verify the cert. Check:

1. **Is `ORCHESTRATOR_CA_BUNDLE` set in the wrapper's env?**
   ```bash
   cat /proc/$(cat /home/stanley/.hermes-orchestrator/agent.pid)/environ | tr '\0' '\n' | grep ORCHESTRATOR_CA_BUNDLE
   ```
   If empty, the wrapper startup script didn't set it. Update
   `_restart_linux.sh` (or `register-windows-service-v2.ps1`)
   per the v3.12.0 commit.

2. **Does the file exist and match?**
   ```bash
   sha256sum /home/stanley/.hermes-orchestrator/certs/server.crt
   # Compare to orchestrator's
   ```
   If different, the certs have rotated. Re-scp the latest.

3. **Is the wrapper actually using the env var?** The wrapper
   reads `ORCHESTRATOR_CA_BUNDLE` at **import time** of
   `hermes_orch.agent_http`. The wrapper process must be
   restarted after a cert change, not just the orchestrator.

### 5.4 "function' object has no attribute 'set_alpn_protocols" (wrapper log)

Pre-v3.12.0 bug — the `httpx.Client(...)` call sites didn't
pass `verify=`. Fixed in `bb7afb0` (`verify=_agent_http_verify()`
with the parens). Deploy the latest agent_cli.py and restart
the wrapper.

### 5.5 "Server did not pick up HTTPS" (orch-ctl.ps1 status shows http://)

The server is still the pre-HTTPS process. Either:

- The restart-now flag wasn't picked up (check the watchdog
  log for `[FORCE]`)
- The new server started on a different port (check
  `server.stdout.log` for `bind: address already in use`)

Force a clean restart by killing the port owner first (admin
PowerShell required for session-0 processes):

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

Then run `orch-ctl.ps1 restart -Wait` and the watchdog will
spawn a fresh server with HTTPS.

### 5.6 "agents went stale" after cert rotation

The cert changed but a wrapper is still using the old SHA in
its pinned bundle. Restart all wrappers; they re-read the
cert file on next import.

---

## 6. Quick reference

| Action | Command |
|--------|---------|
| Generate cert | `hermes-orch gen-cert [--force]` |
| Re-generate (overwrite) | `hermes-orch gen-cert --force` |
| Enable HTTPS | Settings page → HTTPS → toggle + cert paths → Save |
| Restart orchestrator | `C:\Users\stanley\.hermes-orchestrator\restart-orch.bat --wait` |
| Check server status | `C:\Users\stanley\.hermes-orchestrator\orch-ctl.ps1 status` |
| Restart Linux wrapper | `bash _restart_linux.sh` (on the agent host) |
| Restart Windows wrapper | Stop + Start-Process (see 2.3.1) |
| Install NSSM service | `cd scripts; .\register-windows-service-v2.ps1` (admin) |
| Install cert on Windows | `Import-Certificate -FilePath server.crt -CertStoreLocation Cert:\LocalMachine\Root` |
| Install cert on macOS | `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain server.crt` |
| Install cert on Linux | `sudo cp server.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates` |
| Test HTTPS with curl | `curl --cacert server.crt https://hermes-win:8765/api/health` |
| Force cert trust on wrapper | Set `INSECURE_SKIP_TLS_VERIFY=1` (dev only) |
| Pin cert on wrapper | Set `ORCHESTRATOR_CA_BUNDLE=/path/to/server.crt` |

---

## 7. Related

- `docs/install-spec.md` — initial orchestrator install (separate
  from HTTPS, but the watchdog script is the same one)
- `docs/hmac-agent-auth.md` — the HMAC layer that runs ON TOP of
  TLS. Independent of cert; unchanged by enabling HTTPS.
- `docs/sse-events-v1.8.md` — SSE events also work over HTTPS
  (browser shows the cert, not a separate stream cert)
- `docs/api/README.md` (todo) — `POST /api/settings/https` and
  `POST /api/settings/https/upload` endpoints
