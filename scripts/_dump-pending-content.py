"""Dump full content of pending skill rows to see the \r inflation."""
import sqlite3
con = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=" * 70)
print("Latest pending computer-use (win-agent02) — full content")
print("=" * 70)
r = cur.execute("""
    SELECT pc.desired_content, pc.desired_sha256, length(pc.desired_content) as sz
    FROM profile_configs pc
    JOIN agent_profiles ap ON ap.id = pc.profile_id
    WHERE ap.agent_id = 'win-local-1' AND ap.name = 'win-agent02'
      AND pc.file_path = 'skills/computer-use/SKILL.md'
    ORDER BY pc.created_at DESC LIMIT 1
""").fetchone()
if r:
    c = r['desired_content']
    print(f"  size = {r['sz']}, sha = {r['desired_sha256'][:12]}")
    print(f"  repr first 200: {c[:200]!r}")
    print(f"  repr last 200:  {c[-200:]!r}")
    # Count \r at start vs end vs middle
    leading_cr = len(c) - len(c.lstrip('\r'))
    trailing_cr = len(c) - len(c.rstrip('\r'))
    middle_cr = c.strip('\r').count('\r')
    print(f"  leading \\r: {leading_cr}")
    print(f"  trailing \\r: {trailing_cr}")
    print(f"  middle \\r: {middle_cr}")
    # Find first non-\r char
    for i, ch in enumerate(c):
        if ch != '\r':
            print(f"  first non-\\r char at offset {i}: {c[i:i+50]!r}")
            break

print()
print("=" * 70)
print("Latest pending hk-weather (win-agent01) — full content")
print("=" * 70)
r = cur.execute("""
    SELECT pc.desired_content, pc.desired_sha256, length(pc.desired_content) as sz
    FROM profile_configs pc
    JOIN agent_profiles ap ON ap.id = pc.profile_id
    WHERE ap.agent_id = 'win-local-1' AND ap.name = 'win-agent01'
      AND pc.file_path = 'skills/hk-weather/SKILL.md'
    ORDER BY pc.created_at DESC LIMIT 1
""").fetchone()
if r:
    c = r['desired_content']
    print(f"  size = {r['sz']}, sha = {r['desired_sha256'][:12]}")
    print(f"  repr first 200: {c[:200]!r}")
    print(f"  repr last 200:  {c[-200:]!r}")
    leading_cr = len(c) - len(c.lstrip('\r'))
    trailing_cr = len(c) - len(c.rstrip('\r'))
    middle_cr = c.strip('\r').count('\r')
    print(f"  leading \\r: {leading_cr}")
    print(f"  trailing \\r: {trailing_cr}")
    print(f"  middle \\r: {middle_cr}")
    for i, ch in enumerate(c):
        if ch != '\r':
            print(f"  first non-\\r char at offset {i}: {c[i:i+50]!r}")
            break
