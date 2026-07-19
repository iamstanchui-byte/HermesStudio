"""Drop all flat-path skill records (the post-migration cleanup).

The skill layout migration (commit a7516ba) inserted folder-path
records alongside the legacy flat-path ones. Now that hermes 0.17+
only reads folder layout, the flat records are dead code -- the
files at skills/<name>.md either never existed (wrapper wrote them
and hermes 0.17 silently ignored them) or got auto-cleaned up by
hermes's own skill curator. We delete them all so the dashboard
and DB are clean.

Safe to delete unconditionally because:
1. Flat records that wrote content: hermes 0.17 never read
   those files (commit a7516ba discovery). The skill was never
   effective. Deleting the record removes dead audit history.
2. Flat records with empty content (delete intent): the wrapper
   has already applied the delete (unlinked the flat file --
   which hermes wasn't reading anyway). Intent is satisfied.
   The folder-path equivalent, if any, is what hermes is using
   going forward.

After this cleanup, the DB has ONLY folder-path records for
skills -- the dashboard dedup is no longer needed.
"""
import sqlite3
DB = r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db'
c = sqlite3.connect(DB)

before = c.execute("SELECT COUNT(*) FROM profile_configs WHERE file_path LIKE 'skills/%.md' AND file_path NOT LIKE '%/SKILL.md'").fetchone()[0]
print(f'flat records before: {before}')

c.execute("DELETE FROM profile_configs WHERE file_path LIKE 'skills/%.md' AND file_path NOT LIKE '%/SKILL.md'")
deleted = c.execute('SELECT changes()').fetchone()[0]
c.commit()
print(f'deleted: {deleted}')

print()
print('total records by layout:')
for r in c.execute("""
    SELECT
      CASE WHEN file_path LIKE '%/SKILL.md' THEN 'folder' ELSE 'flat' END as layout,
      COUNT(*) as n
    FROM profile_configs WHERE file_path LIKE 'skills/%'
    GROUP BY layout
"""):
    print(f'  {r[0]}: {r[1]}')
