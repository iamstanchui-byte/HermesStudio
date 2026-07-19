"""Remove the mt5-bridge-to-yahoo-fallback records that migration just
re-inserted (user had already deleted that skill, we shouldn't restore
it just because the migration ran on flat-path history)."""
import sqlite3
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
result = c.execute("""
    DELETE FROM profile_configs
    WHERE file_path = 'skills/mt5-bridge-to-yahoo-fallback/SKILL.md'
""")
c.commit()
print(f"deleted {result.rowcount} mt5-bridge folder-path records")
