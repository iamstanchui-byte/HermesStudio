"""Run T2 task-status tests in a clean DB state.

Why this exists: the live server's supervisor queries
`status='running' AND last_liveness_at IS NULL OR < 180s ago`
and marks them failed. Our tests deliberately seed tasks with
old/missing liveness_at, so they'd race with the supervisor.

Stop the server → run the tests → restart the server. This keeps
test data deterministic without us needing a temp DB fixture
that bypasses the supervisor's stuck-task sweep.

Usage:
    python scripts/run-t2-tests.py
"""
import os
import subprocess
import sys
import time

proj = r'C:\Project\minimax code\hermes-orchestrator'
exe = os.path.join(proj, '.venv', 'Scripts', 'hermes-orch.exe')
py = os.path.join(proj, '.venv', 'Scripts', 'python.exe')

# 1. Stop server
print("=" * 60)
print("STEP 1: stop server (supervisor would race our test data)")
print("=" * 60)
r = subprocess.run(
    ['taskkill', '/F', '/T', '/IM', 'hermes-orch.exe'],
    capture_output=True, text=True, timeout=15,
)
print(f"  taskkill rc={r.returncode}")
print(f"  {r.stdout.strip()[:150]}")

# Wait for port to free
for i in range(15):
    r = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         'Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count'],
        capture_output=True, text=True, timeout=10,
    )
    if r.stdout.strip() == '0':
        print(f"  port 8765 free after {i}s")
        break
    time.sleep(1)
else:
    print("  WARNING: port still busy, tests may race")
    sys.exit(1)

# 2. Run T2 tests
print()
print("=" * 60)
print("STEP 2: run tests")
print("=" * 60)
test_files = [
    "tests/test_loop_status.py",   # T1 (just to confirm it still passes)
    "tests/test_task_status_endpoints.py",  # T2
]
env = os.environ.copy()
r = subprocess.run(
    [py, "-m", "pytest", "-v"] + test_files,
    cwd=proj, env=env,
)
test_rc = r.returncode

# 3. Restart server
print()
print("=" * 60)
print(f"STEP 3: restart server (tests rc={test_rc})")
print("=" * 60)
log_out = os.path.join(proj, 'server.log')
log_err = os.path.join(proj, 'server.log.err')
with open(log_out, 'ab') as fo, open(log_err, 'ab') as fe:
    p = subprocess.Popen(
        [exe, 'serve', '--reload'],
        cwd=proj, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
        creationflags=0x00000008,
    )
print(f"  new server PID={p.pid}")

# Wait for health
for i in range(20):
    try:
        import urllib.request
        r = urllib.request.urlopen(
            'http://127.0.0.1:8765/api/health', timeout=2
        )
        print(f"  up after {i}s: {r.read().decode().strip()}")
        sys.exit(test_rc)
    except Exception:
        time.sleep(1)
print("  ERROR: server did not come up within 20s")
sys.exit(1)
