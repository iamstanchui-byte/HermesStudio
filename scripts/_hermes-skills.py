"""Query hermes's skills table via SSH."""
import subprocess
# List tables
r = subprocess.run(
    ['ssh', 'stanley@192.168.2.161',
     "python3 -c \"import sqlite3; c=sqlite3.connect('/home/stanley/.hermes/state.db'); cur=c.execute(\\\"SELECT name FROM sqlite_master WHERE type='table'\\\"); [print(r[0]) for r in cur.fetchall()]\""],
    capture_output=True, text=True, timeout=15
)
print('STDOUT:', r.stdout)
print('STDERR:', r.stderr)
