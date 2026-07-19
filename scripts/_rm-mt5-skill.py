"""Remove mt5-bridge-to-yahoo-fallback folder from Linux super profile."""
import subprocess
r = subprocess.run(
    ['ssh', 'stanley@192.168.2.161',
     'rm -rf /home/stanley/.hermes/profiles/super/skills/mt5-bridge-to-yahoo-fallback && echo "removed" && ls /home/stanley/.hermes/profiles/super/skills/mt5-bridge* 2>/dev/null || echo "absent (good)"'],
    capture_output=True, text=True, timeout=15
)
print('STDOUT:', r.stdout)
print('STDERR:', r.stderr)
print('rc:', r.returncode)
