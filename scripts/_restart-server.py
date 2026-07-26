"""Restart the hermes-orchestrator server cleanly.

uvicorn --reload doesn't always pick up changes (especially new modules
or DB schema changes). This script:
  1. Finds the PID listening on port 8765
  2. Kills it (and its child processes)
  3. Waits 3s for the port to clear
  4. Starts a fresh server in a new PowerShell window
  5. Polls /api/health until 200 (max 15s)
"""
import io
import os
import socket
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PORT = 8765


def find_pid_on_port(port: int) -> int | None:
    """Return the PID of the process listening on the given port, or None."""
    out = subprocess.run(
        ["netstat", "-ano"], capture_output=True, text=True, encoding="ascii", errors="ignore"
    ).stdout
    needle = f":{port} "
    for line in out.splitlines():
        if needle in line and "LISTENING" in line:
            return int(line.strip().split()[-1])
    return None


def kill_pid(pid: int) -> None:
    """Force-kill PID and all its children. /F = force, /T = tree."""
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    # Also kill any orphan python.exe that might still be holding
    # the port. We look for python processes whose parent died.
    # (Not always safe — we limit to processes matching the cmdline
    # pattern to avoid killing unrelated python work.)
    time.sleep(1)


def start_server(repo_dir: str) -> int:
    """Start the server in a new PowerShell process. Returns the new PID."""
    args = [
        "powershell",
        "-NoProfile",
        "-Command",
        ".venv\\Scripts\\hermes-orch.exe serve --reload",
    ]
    # Start-Process returns a Process object; we want its PID.
    proc = subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Start-Process powershell -ArgumentList '-NoProfile','-Command','.venv\\Scripts\\hermes-orch.exe serve --reload' -RedirectStandardOutput server.log -RedirectStandardError server.log.err -WorkingDirectory '{repo_dir}'",
        ]
    )
    proc.wait(timeout=2)
    return 0  # PID not easily recoverable cross-process; the port scan below finds it


def wait_healthy(port: int, timeout_s: float = 15.0) -> bool:
    """Poll http://127.0.0.1:{port}/api/health until 200 OK or timeout."""
    import urllib.request
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)
            if r.status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(f"repo_dir: {repo_dir}")
    pid = find_pid_on_port(PORT)
    if pid:
        print(f"killing existing server PID {pid}")
        kill_pid(pid)
    else:
        print("no existing server on port 8765")
    # Wait for port to clear
    deadline = time.time() + 5
    while time.time() < deadline:
        if not find_pid_on_port(PORT):
            break
        time.sleep(0.5)
    print("starting fresh server...")
    start_server(repo_dir)
    print("waiting for /api/health ...")
    if wait_healthy(PORT, timeout_s=15.0):
        new_pid = find_pid_on_port(PORT)
        print(f"OK — server healthy, PID={new_pid}")
        return 0
    print("FAILED — server did not become healthy within 15s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
