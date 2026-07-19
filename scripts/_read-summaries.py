"""Read summary_v2.md with proper encoding + check if 2nd run reused 1st run's data."""
import sys
import sqlite3
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Read summary_v2.md
with open(r"C:\Project\minimax code\hermes-project\proj-c1e7286e\summary_v2.md",
          encoding='utf-8') as f:
    v2 = f.read()
print(f"=== summary_v2.md (len={len(v2)}) ===")
print()
# Print first 50 lines
for i, line in enumerate(v2.split('\n')[:50], 1):
    print(f"{i:3d}  {line}")

print()
print("=" * 70)
print()

# Check if 2nd run's tasks reference old summary.md or its data
conn = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# 2nd run tasks
print("=== 2nd run tasks: action + params + result snippet ===")
for r in cur.execute("""
    SELECT id, name, action, params, result FROM tasks
    WHERE project_id = 'proj-c1e7286e'
    AND created_at > '2026-07-19 13:00:00'
    ORDER BY created_at
"""):
    print(f"\n--- {r['id']} {r['name']} ---")
    print(f"  action: {r['action']}")
    print(f"  params: {(r['params'] or '')[:200]}")
    result = r['result'] or ''
    if result:
        try:
            import json
            rd = json.loads(result)
            s = rd.get('summary', '')
            s = s.replace('\x1b[2;3m', '').replace('\x1b[0m', '')
            s = s.replace('\x1b[1;38;2;255;215;0m', '').replace('\x1b[38;2;255;248;220m', '')
            s = s.replace('\x1b[38;2;255;191;0m', '')
            print(f"  result (first 400): {s[:400]}")
        except Exception as e:
            print(f"  result (raw, first 200): {result[:200]}")

# Check if 1st-run summary.md is referenced by anything in 2nd run
print()
print("=" * 70)
print("=== Search for old-data references in 2nd-run artifacts ===")
import os
for f in ['summary_v2.md', 'ridge_result.md', 'yuanbao_analysis.md']:
    full = rf"C:\Project\minimax code\hermes-project\proj-c1e7286e\{f}"
    if not os.path.exists(full):
        continue
    with open(full, encoding='utf-8') as fh:
        text = fh.read()
    refs = []
    for marker in ['summary.md', 'proj-c1e7286e/summary.md', 't-d4432279',
                   't-13168a54', 't-de46a9bd', 't-170e688d', 't-73c27382',
                   't-9e2c6109', 't-4292ccc0', 'Spain 50.0', 'Argentina 56.8']:
        if marker in text:
            refs.append(marker)
    print(f"  {f}: references = {refs or 'NONE'}")
