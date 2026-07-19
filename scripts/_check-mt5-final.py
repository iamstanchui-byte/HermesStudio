"""Check mt5-bridge folder status after delete-record insert."""
import subprocess
r = subprocess.run(
    ['ssh', 'stanley@192.168.2.161',
     "ls /home/stanley/.hermes/profiles/super/skills/mt5-bridge* 2>/dev/null || echo 'absent (good)' ; ls /home/stanley/.hermes/profiles/super-b/skills/mt5-bridge* 2>/dev/null || echo 'absent (good)'"],
    capture_output=True, text=True, timeout=15
)
print('STDOUT:', r.stdout)
print('STDERR:', r.stderr)
