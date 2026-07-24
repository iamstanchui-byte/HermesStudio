"""Push the latest wrapper code to a Linux agent host, restart it,
and trigger a skill sync. Run from Windows after committing
changes to src/hermes_orch/agent_cli.py (or agent_paths.py).

What it does:
1. SCP src/hermes_orch/{agent_cli,agent_paths}.py to the Linux host's
   site-packages. Backups the originals first (suffix .bak.<ts>).
2. Kill the running hermes-orch-agent (pkill -f).
3. Start a new one via setsid + nohup so it survives the SSH session
   (Linux has no equivalent of NSSM/Windows service for the wrapper).
4. Wait for heartbeat to come back (max 30s).
5. POST /api/agents/{id}/profiles/{name}/skills/sync for each profile
   so the new code immediately syncs subfolder skills (the old
   1-level iterdir scan missed them).

Usage:
    python scripts/deploy-wrapper-linux.py HOST USER [PROFILE...]

    # defaults: HOST=192.168.2.161 USER=stanley PROFILE=super super-b
    python scripts/deploy-wrapper-linux.py
    # custom host + user
    python scripts/deploy-wrapper-linux.py 192.168.2.161 stanley

The host + user + profile list are also overrideable via env:
    HERMES_LINUX_HOST, HERMES_LINUX_USER, HERMES_LINUX_PROFILES
"""
import os
import subprocess
import sys
import time
import urllib.request
import json

DEFAULT_HOST = "192.168.2.161"
DEFAULT_USER = "stanley"
DEFAULT_PROFILES = ["super", "super-b"]
# Layout on the Linux install (a pip install in a venv):
#   /home/stanley/.hermes-orchestrator/                  <- WRAPPER_DIR (venv root's parent)
#     venv/                                              <- venv root
#       bin/hermes-orch-agent                            <- the wrapper exe shim
#       lib/python3.12/site-packages/hermes_orch/        <- SITE_DIR (the actual code)
#         agent_cli.py
#         agent_paths.py
# os.path.dirname(SITE_DIR) gives the venv root, not the
# wrapper install dir. The wrapper exe is at WRAPPER_DIR +
# '/venv/bin/hermes-orch-agent'. The wrapper config is at
# WRAPPER_DIR + '/wrapper-config.json'.
WRAPPER_DIR = "/home/stanley/.hermes-orchestrator"
SITE_DIR = WRAPPER_DIR + "/venv/lib/python3.12/site-packages/hermes_orch"
SRC = (
    r"C:\Project\minimax code\hermes-orchestrator\src\hermes_orch"
    if os.name == "nt"
    else os.path.join(os.path.dirname(__file__), "..", "src", "hermes_orch")
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _ssh(host_user, *args, timeout=30):
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host_user, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _scp(local, remote, timeout=30):
    return subprocess.run(
        ["scp", "-o", "BatchMode=yes", local, remote],
        capture_output=True, text=True, timeout=timeout,
    )


def deploy(host: str, user: str, profiles: list[str]) -> int:
    ssh_dest = f"{user}@{host}"
    site = SITE_DIR

    # 1. Backup existing files on Linux
    print("--- 1. Backup existing wrapper files on Linux ---")
    for f in ["agent_cli.py", "agent_paths.py"]:
        r = _ssh(
            ssh_dest,
            f'cp {site}/{f} {site}/{f}.bak.$(date +%Y%m%d_%H%M%S)',
            timeout=10,
        )
        print(f"  backup {f}: rc={r.returncode}")

    # 2. SCP new files
    print("--- 2. SCP new files to Linux ---")
    for f in ["agent_cli.py", "agent_paths.py"]:
        src = os.path.join(SRC, f)
        dst = f"{ssh_dest}:{site}/{f}"
        r = _scp(src, dst)
        print(f"  scp {f}: rc={r.returncode}")
        if r.returncode != 0:
            print(f"    stderr: {r.stderr[:300]}")
            return 1

    # 3. Verify the files have the new code
    print("--- 3. Verify rglob/iterdir counts in deployed files ---")
    r = _ssh(ssh_dest, f'grep -c "rglob" {site}/agent_cli.py', timeout=10)
    rglob = r.stdout.strip()
    r = _ssh(ssh_dest, f'grep -c "iterdir" {site}/agent_cli.py', timeout=10)
    iterdir = r.stdout.strip()
    print(f"  rglob={rglob}  iterdir={iterdir}")
    if int(rglob) < 1:
        print("  WARN: rglob not found in deployed agent_cli.py — something is wrong")

    # 4. Kill old wrapper
    print("--- 4. Kill old wrapper ---")
    r = _ssh(ssh_dest, 'pkill -f "hermes-orch-agent"', timeout=10)
    print(f"  pkill rc={r.returncode}")
    time.sleep(2)
    r = _ssh(ssh_dest, 'pgrep -fa "hermes-orch-agent"', timeout=10)
    alive = [x for x in r.stdout.split() if x.strip()]
    if alive:
        print(f"  WARN: still alive after pkill: {alive}")

    # 5. Start new wrapper, fully detached
    print("--- 5. Start new wrapper ---")
    # WRAPPER_DIR is the venv's PARENT (the install root). The exe
    # is at venv/bin/, the config is at the install root.
    start_cmd = (
        f'setsid nohup {WRAPPER_DIR}/venv/bin/hermes-orch-agent start '
        f'--config {WRAPPER_DIR}/wrapper-config.json --interval 5 '
        f'> /tmp/hermes-orch-agent.log 2>&1 < /dev/null &'
    )
    r = _ssh(ssh_dest, start_cmd, timeout=10)
    print(f"  start rc={r.returncode}")
    time.sleep(3)
    r = _ssh(ssh_dest, 'pgrep -fa "hermes-orch-agent"', timeout=10)
    print("  new processes:")
    for line in r.stdout.splitlines():
        if line.strip():
            print(f"    {line.strip()}")

    # 6. Wait for heartbeat
    print("--- 6. Wait for heartbeat ---")
    for i in range(30):
        time.sleep(1)
        try:
            r = urllib.request.urlopen("http://127.0.0.1:8765/api/agents", timeout=3)
            data = json.loads(r.read())
            for a in data.get("agents", []):
                if a["id"] == "linux-a-01":
                    if a.get("status") == "verified" and a.get("last_heartbeat_at"):
                        print(f"  verified after {i+1}s")
                        break
            else:
                continue
            break
        except Exception as e:
            print(f"  t+{i+1}s: {e}")
    else:
        print("  WARN: did not see verified status in 30s")
        return 1

    # 7. Trigger skill sync for each profile
    print("--- 7. Trigger skill sync ---")
    for prof in profiles:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:8765/api/agents/linux-a-01/profiles/{prof}/skills/sync",
                method="POST",
            )
            r = urllib.request.urlopen(req, timeout=5)
            print(f"  sync {prof}: status={r.status}")
        except Exception as e:
            print(f"  sync {prof}: error {e}")
    return 0


if __name__ == "__main__":
    host = os.environ.get("HERMES_LINUX_HOST", DEFAULT_HOST)
    user = os.environ.get("HERMES_LINUX_USER", DEFAULT_USER)
    profiles = os.environ.get("HERMES_LINUX_PROFILES", "").split() or DEFAULT_PROFILES
    if len(sys.argv) > 1:
        host = sys.argv[1]
    if len(sys.argv) > 2:
        user = sys.argv[2]
    if len(sys.argv) > 3:
        profiles = sys.argv[3:]

    print(f"deploying to {user}@{host} (profiles: {profiles})")
    sys.exit(deploy(host, user, profiles))
