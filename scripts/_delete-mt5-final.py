"""Re-delete the mt5-bridge folder that the migration restored.
Insert an empty-content record with the folder path; the wrapper
will pick it up and rmtree the folder."""
import sqlite3
import hashlib
import uuid
c = sqlite3.connect(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
# Find both profile_ids that had mt5-bridge
profiles = [r[0] for r in c.execute("""
    SELECT DISTINCT profile_id FROM profile_configs
    WHERE file_path = 'skills/mt5-bridge-to-yahoo-fallback.md'
""").fetchall()]
print(f"profiles with mt5-bridge: {[p[:8]+'..' for p in profiles]}")
for pid in profiles:
    cfg_id = str(uuid.uuid4())
    empty_sha = hashlib.sha256(b'').hexdigest()
    c.execute("""
        INSERT INTO profile_configs
          (id, profile_id, file_path, desired_sha256, desired_content, status)
        VALUES (?, ?, 'skills/mt5-bridge-to-yahoo-fallback/SKILL.md', ?, '', 'pending')
    """, (cfg_id, pid, empty_sha))
    print(f"  inserted delete record for profile={pid[:8]}.. id={cfg_id[:8]}..")
c.commit()
