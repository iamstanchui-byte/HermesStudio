"""Check DB state of mt5-bridge-to-yahoo-fallback skill."""
import sqlite3
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
c.row_factory = sqlite3.Row

print('=== profile_configs for mt5-bridge ===')
for r in c.execute("SELECT id, profile_id, file_path, status, created_at FROM profile_configs WHERE file_path LIKE '%mt5-bridge%' ORDER BY created_at DESC LIMIT 10"):
    print(dict(r))

print()
print('=== agent_profiles (showing names) ===')
for r in c.execute('SELECT id, agent_id, name FROM agent_profiles'):
    print(dict(r))
