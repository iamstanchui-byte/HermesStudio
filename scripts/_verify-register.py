"""Verify register: agent in DB, secret file, secret hash format."""
import sqlite3
import os
from pathlib import Path

DB = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
SECRET_FILE = Path.home() / ".hermes-orchestrator" / ".secret-win-local-1"
AGENT_ID = "win-local-1"

print("--- 1. Agent in DB ---")
con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row
row = con.execute("SELECT id, secret_hash, status, os_type, ip FROM agents WHERE id = ?", (AGENT_ID,)).fetchone()
if not row:
    print("  NOT FOUND")
else:
    print(f"  id: {row['id']}")
    print(f"  status: {row['status']}")
    print(f"  os_type: {row['os_type']}")
    print(f"  ip: {row['ip']}")
    print(f"  secret_hash: {row['secret_hash'][:16]}...")
    print(f"  hash length: {len(row['secret_hash'])} (sha256 should be 64)")

print()
print("--- 2. Secret file on disk ---")
if SECRET_FILE.exists():
    content = SECRET_FILE.read_text(encoding="utf-8")
    print(f"  path: {SECRET_FILE}")
    print(f"  size: {len(content)} bytes")
    print(f"  first 8: {content[:8]}")
    print(f"  last 4: {content[-4:]}")
else:
    print(f"  NOT FOUND at {SECRET_FILE}")

print()
print("--- 3. Profiles created ---")
profiles = con.execute("SELECT name FROM agent_profiles WHERE agent_id = ?", (AGENT_ID,)).fetchall()
for p in profiles:
    print(f"  - {p['name']}")
