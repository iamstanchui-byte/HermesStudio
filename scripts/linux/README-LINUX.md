# hermes-orch-agent — Linux install guide

## What you need

| Item | Where to get it |
|---|---|
| **Linux box** | Ubuntu 20.04+, Debian 11+, or similar (Python 3.10+) |
| **hermes-agent** | Installed and runnable: `hermes --version` works |
| **Wheel file** | `hermes_orchestrator-0.1.0-py3-none-any.whl` (built on the orchestrator host) |
| **Requirements** | `requirements-agent.txt` (next to the wheel) |
| **Install script** | `install-agent.sh` (this repo, `scripts/linux/`) |
| **systemd unit** | `hermes-orch-agent.service` (this repo, `packaging/systemd/`) |
| **Orchestrator URL** | e.g. `http://192.168.1.10:8765` (where `hermes-orch serve` runs) |
| **Agent ID** | Unique name, e.g. `linux-a-01` |
| **sudo** | Only for systemd install + pip on system Python |

## Quick install (5 minutes)

### 1. Copy files to the Linux box

From your **orchestrator host** (Windows or Linux), scp the wheel + scripts:

```bash
# From your Linux box, scp them in:
mkdir -p ~/hermes-orch-install
cd ~/hermes-orch-install

# 1. the wheel + requirements (from your orchestrator host)
scp <user>@<orch-host>:'dist/hermes_orchestrator-*.whl' .
scp <user>@<orch-host>:'dist/requirements-agent.txt' .

# 2. the install + uninstall scripts
scp <user>@<orch-host>:'scripts/linux/install-agent.sh' .
scp <user>@<orch-host>:'scripts/linux/uninstall-agent.sh' .

# 3. the systemd unit (place next to install-agent.sh)
scp <user>@<orch-host>:'packaging/systemd/hermes-orch-agent.service' .
```

### 2. Run install-agent.sh

```bash
chmod +x install-agent.sh uninstall-agent.sh
./install-agent.sh \
    --orchestrator http://192.168.1.10:8765 \
    --agent-id linux-a-01 \
    --user stanley
```

What it does:
1. Checks Python 3.10+
2. Creates venv at `~/.hermes-orchestrator/venv`
3. Installs deps + wheel
4. Registers this host with the orchestrator (gets a secret, writes `~/.hermes-orchestrator/.secret-linux-a-01`)
5. (with sudo) Installs systemd unit, enables + starts daemon

### 3. Edit wrapper-config.json (only profiles section)

The default wrapper-config.json has placeholder profile paths. Edit them:

```bash
nano ~/.hermes-orchestrator/wrapper-config.json
```

Set `"profiles.<role>.root"` to the actual hermes profile dir on this box:

```json
{
  "agent_id": "linux-a-01",
  "orchestrator_url": "http://192.168.1.10:8765",
  "secret_file": "/home/stanley/.hermes-orchestrator/.secret-linux-a-01",
  "profiles": {
    "data-fetch":   { "root": "/home/stanley/.local/share/hermes/profiles/data-fetch" },
    "backtest":     { "root": "/home/stanley/.local/share/hermes/profiles/backtest" }
  }
}
```

Or set `HERMES_PROFILES_DIR` env var and use the `<profiles_dir>/<role>` template in the config.

### 4. Restart the daemon (if you edited the config)

```bash
sudo systemctl restart hermes-orch-agent
# or, if you skipped systemd:
~/.hermes-orchestrator/venv/bin/hermes-orch-agent start
```

### 5. Verify

```bash
# CLI status
~/.hermes-orchestrator/venv/bin/hermes-orch-agent status

# systemd status
sudo systemctl status hermes-orch-agent

# Live logs
journalctl -u hermes-orch-agent -f
```

## What the orchestrator sees

After install, the orchestrator's `/api/agents/` will show this host as `verified`
once the daemon does its first heartbeat. The daemon's loop:

1. Heartbeat to orchestrator every 5 seconds
2. For each `assigned` task matching this host's profiles:
   - `POST /api/tasks/{id}/start` to claim
   - Run `hermes -p <profile> chat -q <action>(<params>)` in the profile's root dir
   - `POST /api/tasks/{id}/result` to submit

## Uninstall

```bash
./uninstall-agent.sh --user stanley
```

Removes systemd unit, pip package, install dir. Also need to remove
the agent from the orchestrator:

```bash
curl -X DELETE http://192.168.1.10:8765/api/agents/linux-a-01
```

## Troubleshooting

**Daemon won't start: "hermes CLI not found in PATH"**
- Install hermes-agent: `pip install hermes-agent` or follow Nous Research docs
- Verify: `hermes --version` works for the target user

**Heartbeat 401: "Missing auth headers"**
- Check that `~/.hermes-orchestrator/.secret-linux-a-01` exists and matches
  what the orchestrator has in DB
- The wrapper-config.json's `secret_file` must point to a valid file

**Tasks not picked up**
- Verify the agent's profile names match the task's `agent_role`
- Check the task list with `status` command

**`apply-configs` works but `start` fails**
- `apply-configs` only needs read access to hermes profiles
- `start` runs the daemon which also runs hermes subprocess
- Make sure hermes is in PATH for the user running the daemon

## Files

- `install-agent.sh` — main install script
- `uninstall-agent.sh` — clean removal
- `hermes-orch-agent.service` — systemd unit template (paths get templated)
- `hermes_orchestrator-*.whl` — the Python package
- `requirements-agent.txt` — Python deps
