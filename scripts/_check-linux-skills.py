"""Verify Linux hermes skills folder state after delete."""
import subprocess
r = subprocess.run(
    ['ssh', 'stanley@192.168.2.161',
     "ls /home/stanley/.hermes/profiles/super/skills/ ; echo --- ; ls /home/stanley/.hermes/profiles/super/skills/ridge-multicollinearity-on-small-n/ 2>/dev/null && echo 'STILL THERE' || echo 'absent (good)'"],
    capture_output=True, text=True, timeout=15
)
print(r.stdout)
print('STDERR:', r.stderr)
