"""Migrate flat-path skill records to folder layout (hermes 0.17+).

For each (profile_id, name) pair whose latest non-empty record is
flat-path (skills/<name>.md), insert a new pending record with
folder-path (skills/<name>/SKILL.md) + same content. The wrapper
will pick it up on next heartbeat, write the folder file, and ack.
"""
import sqlite3
import hashlib
import uuid

DB = r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db'
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

# Find latest non-deleted (non-empty content) record per (profile_id, name).
# We want rows where the LATEST record (by created_at) is flat-path
# AND has content. We do this in two steps: get all (profile_id, name)
# pairs, then find the latest row per pair.
pairs = c.execute("""
    SELECT DISTINCT profile_id, file_path
    FROM profile_configs
    WHERE file_path LIKE 'skills/%.md' AND file_path NOT LIKE '%/SKILL.md'
""").fetchall()

# Get the skill name from each flat path: 'skills/<name>.md' -> <name>
def name_from_path(p):
    if p.startswith('skills/') and p.endswith('.md'):
        return p[len('skills/'):-len('.md')]
    return None

# Group pairs by name, dedupe
by_name_profile = {}
for r in pairs:
    name = name_from_path(r['file_path'])
    if not name:
        continue
    key = (r['profile_id'], name)
    if key not in by_name_profile:
        by_name_profile[key] = []

# For each (profile_id, name), find the latest non-empty record
migrated = 0
skipped = 0
already_migrated = 0
for (profile_id, name) in by_name_profile:
    # Get latest record for this (profile_id, name) under either flat
    # OR folder path -- if folder path is more recent, already migrated.
    latest_folder = c.execute("""
        SELECT id, file_path, desired_content, status, created_at
        FROM profile_configs
        WHERE profile_id = ? AND file_path = ?
        ORDER BY created_at DESC LIMIT 1
    """, (profile_id, f"skills/{name}/SKILL.md")).fetchone()
    if latest_folder and latest_folder['desired_content']:
        # Folder path already exists with content, no migration needed
        already_migrated += 1
        continue
    # Find latest non-empty flat record
    latest_flat = c.execute("""
        SELECT id, file_path, desired_content, status, created_at
        FROM profile_configs
        WHERE profile_id = ? AND file_path = ?
        AND desired_content != ''
        ORDER BY created_at DESC LIMIT 1
    """, (profile_id, f"skills/{name}.md")).fetchone()
    if not latest_flat:
        # No content to migrate
        skipped += 1
        continue
    # Insert new record with folder path
    content = latest_flat['desired_content']
    sha = hashlib.sha256(content.encode()).hexdigest()
    new_id = str(uuid.uuid4())
    c.execute("""
        INSERT INTO profile_configs
          (id, profile_id, file_path, desired_sha256, desired_content, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
    """, (new_id, profile_id, f"skills/{name}/SKILL.md", sha, content))
    print(f"  migrated: profile={profile_id[:8]}.. name={name!r} "
          f"({len(content)} bytes)")
    migrated += 1

c.commit()
print()
print(f"migrated: {migrated}")
print(f"already migrated: {already_migrated}")
print(f"skipped (no content): {skipped}")
