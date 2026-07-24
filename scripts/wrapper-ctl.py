"""Control the hermes-orch-agent wrapper WITHOUT NSSM / admin rights.

Replaces the NSSM-based "HermesOrchAgent" Windows service. Why we
ditched NSSM (2026-07-24):
- NSSM runs the wrapper as LocalSystem. Restarting it requires
  admin (Stop-Service / nssm stop / sc stop all return
  AccessDenied on HKLM\\...\\Parameters).
- The wrapper process can be killed (taskkill /F /T /PID) but the
  NSSM service itself can't be stopped or removed without admin,
  so a crash leaves the service in 'Stopped' state with no easy
  way to bring it back.
- For the dev/test loop we need to restart the wrapper every few
  minutes as we iterate on agent_cli.py. The NSSM friction
  (UAC prompt each time) made the loop painful.

This script owns the wrapper process directly: start it as a
detached process with logs redirected to files, kill it via
taskkill when stopping. No admin required for any of the
operations below.

Usage:
    .venv\Scripts\python.exe scripts\wrapper-ctl.py start
    .venv\Scripts\python.exe scripts\wrapper-ctl.py stop
    .venv\Scripts\python.exe scripts\wrapper-ctl.py restart
    .venv\Scripts\python.exe scripts\wrapper-ctl.py status

Logs:
    %USERPROFILE%\\.hermes-orchestrator\\wrapper.out.log   (stdout, append)
    %USERPROFILE%\\.hermes-orchestrator\\wrapper.err.log   (stderr, append)

The wrapper's self-restart watchdog (agent_cli.py) is still active
in this mode: editing src\\hermes_orch\\agent_cli.py causes the
wrapper to exit cleanly so the next `start` (or the
`auto-restart-on-exit` behavior, if you wrap start in a loop)
picks up the new code. With NSSM gone, you can run
`wrapper-ctl.py restart` instead of bouncing the service.

If you want auto-restart-on-crash (like NSSM did), wrap this in
a simple while loop or schedule it as a Task Scheduler entry.
"""
import os
import subprocess
import sys
import time
import urllib.request
import json
from pathlib import Path

# Force UTF-8 on stdout/stderr so we can print ✓/✗ without cp1252
# choking (Windows console default is cp1252 even though the file
# is UTF-8). errors="replace" avoids UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ----- paths (read-only; tweak here if your install is elsewhere) -----
PROJ = Path(__file__).resolve().parent.parent
EXE = PROJ / ".venv" / "Scripts" / "hermes-orch-agent.exe"
CONFIG = Path(r"C:\Users\stanley\.hermes-orchestrator\wrapper-config.json")
LOG_DIR = Path(r"C:\Users\stanley\.hermes-orchestrator")
LOG_OUT = LOG_DIR / "wrapper.out.log"
LOG_ERR = LOG_DIR / "wrapper.err.log"
PID_FILE = LOG_DIR / "wrapper.pid"
SERVER_URL = "http://127.0.0.1:8765"
AGENT_ID = "win-local-1"  # which agent to check heartbeat for

# Process creation flags (Windows)
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _is_running() -> int | None:
    """Return PID of a running hermes-orch-agent, or None."""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process hermes-orch-agent -ErrorAction SilentlyContinue | "
         "Select-Object -ExpandProperty Id"],
        capture_output=True, text=True, timeout=10,
    )
    pids = [int(x) for x in r.stdout.strip().split() if x.strip().isdigit()]
    return pids[0] if pids else None


def _heartbeat_status() -> tuple[str, str | None]:
    """Return (agent_status, last_heartbeat_at) for our agent."""
    try:
        r = urllib.request.urlopen(f"{SERVER_URL}/api/agents", timeout=3)
        data = json.loads(r.read())
        for a in data.get("agents", []):
            if a["id"] == AGENT_ID:
                return a["status"], a.get("last_heartbeat_at")
    except Exception:
        pass
    return "?", None


def _wait_for_heartbeat(timeout_s: int = 30) -> bool:
    """Poll the server until the agent is back to 'verified', or timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status, hb = _heartbeat_status()
        if status == "verified":
            return True
        time.sleep(2)
    return False


def start() -> int:
    pid = _is_running()
    if pid:
        print(f"wrapper already running (PID {pid})")
        return 0

    if not EXE.exists():
        print(f"ERROR: wrapper exe not found at {EXE}")
        return 1
    if not CONFIG.exists():
        print(f"ERROR: config not found at {CONFIG}")
        return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"starting wrapper...")
    print(f"  exe:    {EXE}")
    print(f"  config: {CONFIG}")
    print(f"  logs:   {LOG_OUT} + {LOG_ERR}")

    cmd = [str(EXE), "start", "--config", str(CONFIG), "--interval", "5"]
    env = os.environ.copy()
    # These three were set in NSSM's AppEnvironmentExtra. The wrapper
    # uses USERPROFILE for ~/.hermes-orchestrator/ paths; LOCALAPPDATA
    # for %LOCALAPPDATA%\\hermes-...; HOME for any unix-style config
    # lookup (e.g. when hermes_cli tries ~/.config/). Without these
    # the wrapper can pick up a different agent's config.
    env.update({
        "LOCALAPPDATA": r"C:\Users\stanley\AppData\Local",
        "USERPROFILE": r"C:\Users\stanley",
        "HOME": r"C:\Users\stanley",
    })

    # Open logs in append mode so multiple restarts preserve history.
    # line_buffering=1 + buffered file IO gives us crash-recovery: if
    # the wrapper crashes mid-line we still see the partial output.
    fo = open(LOG_OUT, "ab", buffering=0)
    fe = open(LOG_ERR, "ab", buffering=0)
    try:
        p = subprocess.Popen(
            cmd, cwd=str(PROJ),
            stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
            env=env,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    finally:
        # Keep handles open in the child; we just don't need them
        # in the parent anymore. The OS will close them when the
        # wrapper exits. (Closing in the parent would just close
        # our copy, not the child's.)
        pass

    PID_FILE.write_text(str(p.pid))
    print(f"  PID={p.pid}")

    if _wait_for_heartbeat(30):
        print(f"  ✓ heartbeat OK")
        return 0
    print(f"  WARNING: no heartbeat after 30s, check {LOG_ERR.name}")
    return 1


def stop(timeout_s: int = 10) -> int:
    pid = _is_running()
    if not pid:
        print("wrapper not running")
        if PID_FILE.exists():
            PID_FILE.unlink()
        return 0
    print(f"stopping wrapper (PID {pid})...")
    # taskkill /F /T kills the process tree (the exe shim + any python
    # children it spawned). Windows requires /F for child processes
    # that don't handle a kill signal — without /F, taskkill prints
    # "This process can only be terminated forcefully" and skips it
    # (rc=128). We use /F directly because there's no useful cleanup
    # we need the wrapper to run; the OS will free the file handles,
    # the heartbeat thread dies, the child python processes go with
    # the parent.
    r = subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode == 0:
        # Wait for OS to release the PID (usually instant)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if _is_running() is None:
                print(f"  stopped (rc=0)")
                if PID_FILE.exists():
                    PID_FILE.unlink()
                return 0
            time.sleep(0.5)
        print(f"  WARNING: PID still alive after {timeout_s}s")
    else:
        # rc=128 is "some processes need /F" (we already used /F, so
        # the most likely cause is "access denied" — the wrapper is
        # running as a different user). Surface the error so the
        # operator can intervene.
        print(f"  taskkill failed (rc={r.returncode})")
        for line in r.stderr.strip().splitlines():
            print(f"    {line}")
    if PID_FILE.exists():
        PID_FILE.unlink()
    return 1


def restart() -> int:
    stop()
    time.sleep(2)
    return start()


def status() -> int:
    pid = _is_running()
    if pid:
        print(f"wrapper: running (PID {pid})")
        status, hb = _heartbeat_status()
        print(f"  agent: {status}, last heartbeat: {hb}")
        if LOG_OUT.exists():
            size = LOG_OUT.stat().st_size
            print(f"  log:   {LOG_OUT} ({size} bytes)")
        return 0
    else:
        print("wrapper: not running")
        return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1].lower()
    if cmd == "start":
        sys.exit(start())
    elif cmd == "stop":
        sys.exit(stop())
    elif cmd == "restart":
        sys.exit(restart())
    elif cmd == "status":
        sys.exit(status())
    else:
        print(f"unknown command: {cmd!r}")
        print("valid: start | stop | restart | status")
        sys.exit(1)
