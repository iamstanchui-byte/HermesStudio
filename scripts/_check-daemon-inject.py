"""Check Linux daemon log for USER RECENT / PROJECT STATE / PROJECT MEMORY inject markers."""
import subprocess
r = subprocess.run(
    ['ssh', 'stanley@192.168.2.161',
     'grep', '-E', 'USER RECENT|PROJECT STATE|PROJECT MEMORY',
     '/tmp/hermes-daemon.log'],
    capture_output=True, text=True, timeout=15
)
print('count of matching lines:', len(r.stdout.splitlines()))
print()
print('--- matches (last 10) ---')
for line in r.stdout.splitlines()[-10:]:
    print(line[:200])
