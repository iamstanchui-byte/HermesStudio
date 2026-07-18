"""Cleanup script for test-profile-configs.ps1 - mark leftover pending/applying as failed."""
import sqlite3
import sys
from pathlib import Path

db = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"
con = sqlite3.connect(str(db))
n = con.execute(
    "UPDATE profile_configs SET status='failed', error='cleaned by test' "
    "WHERE status IN ('pending','applying')"
).rowcount
con.commit()
print(f"cleaned {n} leftover pending/applying configs")
