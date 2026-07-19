"""One-time cleanup: strip \\r from corrupted skill files (left over from the
newline=None-on-Windows bug fixed in agent_cli.py:_atomic_write).

The DB has the same \\r\\n corruption because the wrapper round-tripped the
file content. We:
  1. Read each file as binary
  2. Decode UTF-8
  3. Strip \\r (the file originally had \\n line endings)
  4. Re-write in binary mode (preserves \\n exactly, no translation)
  5. SHA-compare before/after so we don't touch already-clean files

After this runs, the next wrapper auto-sync (post-restart with the
newline='' fix) will see file SHA != DB SHA, POST the clean content,
and the loop converges on \\n-only content.
"""
import sys
from pathlib import Path
import hashlib

PROFILE_ROOTS = {
    "win-agent01": Path("C:/Users/stanley/AppData/Local/hermes/profiles/win-agent01/skills"),
    "win-agent02": Path("C:/Users/stanley/AppData/Local/hermes/profiles/win-agent02/skills"),
}

# Only the two files that got into the runaway loop
TARGETS = [
    ("win-agent02", "computer-use"),
    ("win-agent01", "hk-weather"),
]

for pname, sname in TARGETS:
    fp = PROFILE_ROOTS[pname] / sname / "SKILL.md"
    if not fp.exists():
        print(f"  SKIP {fp} (not found)")
        continue
    raw = fp.read_bytes()
    n_cr = raw.count(b"\r")
    n_lf = raw.count(b"\n")
    if n_cr == 0:
        print(f"  OK   {fp}  ({n_lf} lines, no \\r — already clean)")
        continue
    # Strip \r. The original content used \n line endings; the corruption
    # was just \r prefixed before every \n.
    cleaned = raw.replace(b"\r", b"")
    # Write atomically via .tmp + replace. Use binary mode so no
    # translation happens. SHA before/after for sanity.
    tmp = fp.with_suffix(fp.suffix + ".tmp")
    tmp.write_bytes(cleaned)
    tmp.replace(fp)
    after = fp.read_bytes()
    sha = hashlib.sha256(after).hexdigest()[:12]
    print(f"  FIXED {fp}")
    print(f"        before: {len(raw)} bytes  ({n_cr} \\r, {n_lf} \\n)")
    print(f"        after:  {len(after)} bytes  (0 \\r, {n_lf} \\n, sha={sha})")

print()
print("Done. Restart the wrapper to trigger an auto-sync that will re-POST")
print("the cleaned content to the orchestrator, breaking the corruption loop.")
