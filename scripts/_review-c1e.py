"""Review proj-c1e7286e — what went wrong with the task chain."""
import sys
import json
import re
import sqlite3
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

conn = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

proj_id = "proj-c1e7286e"

# 1. Project state
print("=== project ===")
r = cur.execute(
    "SELECT id, name, state, goal, current_iteration, max_iterations, "
    "coordinator_role, accept_criteria, deliverable_path, created_at, updated_at "
    "FROM projects WHERE id = ?", (proj_id,)
).fetchone()
if not r:
    print(f"  not found")
    sys.exit(0)
for k in r.keys():
    v = r[k] or "-"
    if isinstance(v, str) and len(v) > 250:
        v = v[:250] + "..."
    print(f"  {k:20s}: {v}")

# 2. Plan + audit (creation)
print()
print("=== plan_generated + initial replans ===")
for r in cur.execute(
    "SELECT id, created_at, event_type, actor, payload FROM audit_log "
    "WHERE project_id = ? AND event_type IN "
    "  ('project.created', 'project.plan_generated', 'project.replan_requested') "
    "ORDER BY id",
    (proj_id,),
):
    payload = (r["payload"] or "")[:200]
    print(f"  [{r['created_at']}] {r['event_type']:30s} actor={r['actor']:12s} {payload}")

# 3. Tasks in order
print()
print("=== tasks (chronological) ===")
for r in cur.execute(
    "SELECT id, name, agent_role, status, action, output_path, "
    "       created_at, updated_at, result "
    "FROM tasks WHERE project_id = ? ORDER BY created_at",
    (proj_id,),
):
    print()
    print(f"--- [{r['status']}] {r['name']} (id={r['id']}) ---")
    print(f"  role:       {r['agent_role']}")
    print(f"  action:     {(r['action'] or '')[:200]}")
    print(f"  output:     {r['output_path'] or '-'}")
    print(f"  created:    {r['created_at']}")
    print(f"  updated:    {r['updated_at']}")
    result = r["result"]
    if result:
        try:
            result_dict = json.loads(result)
            summary = result_dict.get("summary", "")
            error = result_dict.get("error", "")
            artifacts = result_dict.get("artifacts", [])
            # Strip ANSI
            summary_clean = re.sub(r"\x1b\[[0-9;]*m", "", summary)
            print(f"  summary:    {summary_clean[:300]}")
            if error:
                print(f"  ERROR:      {error[:200]}")
            if artifacts:
                names = [a.get("name") or a.get("path", "?") for a in artifacts]
                print(f"  artifacts:  {', '.join(names)}")
        except Exception as e:
            print(f"  result (raw): {result[:200]}")

# 4. Project file dir
print()
print("=== files in project dir ===")
import os
pdir = rf"C:\Project\minimax code\hermes-project\{proj_id}"
if os.path.isdir(pdir):
    for root, dirs, files in os.walk(pdir):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, pdir)
            sz = os.path.getsize(full)
            print(f"  {sz:8d}  {rel}")
else:
    print(f"  (no project dir at {pdir})")
