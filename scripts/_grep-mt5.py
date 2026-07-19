"""Check daemon log for mt5-bridge events."""
import subprocess
r = subprocess.run(
    ['ssh', 'stanley@192.168.2.161',
     "egrep 'mt5-bridge|deleted folder' /tmp/hermes-daemon.log"],
    capture_output=True, text=True, timeout=15
)
print(r.stdout)
print('STDERR:', r.stderr)
