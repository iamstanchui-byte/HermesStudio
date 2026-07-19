"""Find hermes skill layout in Linux install."""
import subprocess
r = subprocess.run(
    ['ssh', 'stanley@192.168.2.161',
     "find /home/stanley/.local -name SKILL.md -path '*/skills/*' 2>/dev/null | head -3"],
    capture_output=True, text=True, timeout=15
)
print('SKILL.md paths:')
print(r.stdout)
print('STDERR:', r.stderr)
