"""Count profile.* audit events - for test-profile-configs.ps1 step 11."""
import sqlite3
from pathlib import Path

con = sqlite3.connect(str(Path.home() / ".hermes-orchestrator" / "hermes-orch.db"))
n = con.execute("SELECT COUNT(*) FROM audit_log WHERE event_type LIKE 'profile.%'").fetchone()[0]
print(n)
