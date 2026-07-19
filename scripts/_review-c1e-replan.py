"""Review proj-c1e7286e after replan — show project state, all tasks, and recent audit."""
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
    print("  not found")
    sys.exit(0)
for k in r.keys():
    v = r[k] or "-"
    if isinstance(v, str) and len(v) > 200:
        v = v[:200] + "..."
    print(f"  {k:20s}: {v}")

# 2. Replan audit events
print()
print("=== replan events ===")
for r in cur.execute(
    "SELECT id, created_at, event_type, actor, payload FROM audit_log "
    "WHERE project_id = ? AND event_type IN "
    "  ('project.replan_requested', 'project.plan_generated', "
    "   'project.created', 'task.created', 'project.iteration_dispatched', "
    "   'project.iteration_completed', 'task.completed', 'task.failed') "
    "ORDER BY id DESC LIMIT 30",
    (proj_id,),
):
    payload = (r["payload"] or "")[:250]
    print(f"  [{r['created_at']}] {r['event_type']:32s} actor={r['actor']:12s} {payload}")

# 3. All tasks
print()
print("=== tasks (chronological) ===")
for r in cur.execute(
    "SELECT id, name, agent_role, status, action, output_path, "
    "       created_at, updated_at, depends_on "
    "FROM tasks WHERE project_id = ? ORDER BY created_at",
    (proj_id,),
):
    deps = r['depends_on'] or '[]'
    try:
        deps_list = json.loads(deps) if isinstance(deps, str) else deps
    except Exception:
        deps_list = []
    deps_str = f" depends_on={deps_list}" if deps_list else ""
    print(f"  [{r['status']:10s}] {r['name'][:40]:40s} role={r['agent_role']:14s} op={r['output_path'] or '-'}{deps_str}")
    print(f"      action: {(r['action'] or '')[:120]}")
    print(f"      created: {r['created_at']}  updated: {r['updated_at']}")
    print()
