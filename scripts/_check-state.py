"""Check DB state and template render for the current config."""
import sqlite3, json
con = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
con.row_factory = sqlite3.Row
print("=== current state of all profiles ===")
for r in con.execute("SELECT name, capabilities FROM agent_profiles ORDER BY name"):
    print(f"  {r['name']:15}  {r['capabilities']!r}")
con.close()
