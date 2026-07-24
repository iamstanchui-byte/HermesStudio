"""Kill the old server (parent + all children) and start a fresh one.
v3: use taskkill /IM (image name) for atomic tree kill. The
previous Stop-Process approach left orphan uvicorn reloader
watchers when the launcher died but the watcher was still
parented to it (orphaned = no parent, still alive).

taskkill /F /T /IM hermes-orch.exe kills the launcher AND every
descendant (uvicorn reloader watcher, uvicorn worker, the
multiprocessing.spawn helper). One command, atomic.

Usage:
    python scripts/restart-server.py
        # full kill + restart
    python scripts/restart-server.py --help
        # show this docstring
"""
import subprocess
import time
import os
import sys
import urllib.request

proj = r'C:\Project\minimax code\hermes-orchestrator'
exe = os.path.join(proj, '.venv', 'Scripts', 'hermes-orch.exe')

if '--help' in sys.argv or '-h' in sys.argv:
    print(__doc__)
    sys.exit(0)

# 1. Kill the server tree (launcher + descendants)
# The launcher is hermes-orch.exe; its children (uvicorn reloaders
# etc) get caught by /T. The actual app server is a grandchild
# running as plain `python.exe` (no hermes-orch in name), so /IM
# hermes-orch.exe doesn't catch it. We catch the launcher with
# /IM, and the /T cascade takes the rest of the tree.
print('killing old server (taskkill /F /T /IM hermes-orch.exe)...')
r = subprocess.run(
    ['taskkill', '/F', '/T', '/IM', 'hermes-orch.exe'],
    capture_output=True, text=True, timeout=15,
)
print(f'  rc={r.returncode}')
print('  stdout:', r.stdout.strip()[:200])
print('  stderr:', r.stderr.strip()[:200])

# 2. Wait for port 8765 to free
print('waiting for port 8765 to free...')
for i in range(15):
    r = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         'Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count'],
        capture_output=True, text=True, timeout=10,
    )
    if r.stdout.strip() == '0':
        print(f'  port free after {i}s')
        break
    time.sleep(1)
else:
    print('  WARNING: port 8765 still busy after 15s')
    # Show who has it
    r = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         'Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object OwningProcess | Format-Table -AutoSize'],
        capture_output=True, text=True, timeout=10)
    print(f'  owners: {r.stdout}')
    sys.exit(1)

# 3. Start fresh
log_out = os.path.join(proj, 'server.log')
log_err = os.path.join(proj, 'server.log.err')
print(f'starting new server...')
with open(log_out, 'ab') as fo, open(log_err, 'ab') as fe:
    p = subprocess.Popen(
        [exe, 'serve', '--reload'],
        cwd=proj, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
        creationflags=0x00000008,  # DETACHED_PROCESS
    )
print(f'  PID={p.pid}')

# 4. Wait for health
for i in range(20):
    try:
        r = urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=2)
        print(f'  up after {i}s: {r.read().decode()}')
        sys.exit(0)
    except Exception:
        time.sleep(1)
print('  ERROR: server did not come up within 20s')
sys.exit(1)
