"""E2E for Plan B (2026-07-23): LLM-synthesize plan.md when mark-template.

Pipeline:
  1. Pick a completed project (proj-e4c9e5dd — Google Drive Access)
  2. POST /api/schedules/project/{id}/mark-template with regenerate=True
  3. Verify response: plan_regenerated=True, plan_regen_error=None
  4. Read the new plan.md and check:
     - size > 1KB (sane upper bound for a clean doc)
     - has YAML frontmatter
     - has ## Steps section
     - has ## Variables section with {{var}} placeholders
     - NO L3 noise: no [cite:, coord_pickup, iteration_completed@, etc.
  5. Audit log: project.plan_regenerated event present
  6. Cleanup: unmark template, restore plan.md from the project dir
     backup? Actually we just leave the new clean plan.md in place —
     it's strictly better than the L3 dump.

Run: .venv\Scripts\python.exe scripts\_test-plan-synth-e2e.py
"""
import sys
import httpx
import re

BASE = "http://localhost:8765"
SOURCE = "proj-e4c9e5dd"

PASS_COUNT = 0
FAIL_COUNT = 0


def expect(name: str, cond: bool, detail: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"  PASS  {name}")
    else:
        FAIL_COUNT += 1
        print(f"  FAIL  {name}  ->  {detail}")


def main() -> int:
    print(f"=== Plan B (plan.md LLM synth) E2E ===")
    print(f"source project: {SOURCE}")
    print()

    # 1. Unmark template (idempotent — ignore 204/404)
    r = httpx.delete(f"{BASE}/api/schedules/project/{SOURCE}/mark-template", timeout=5)
    print(f"[0] cleanup: unmark template (status={r.status_code})")
    print()

    # 2. POST mark-template with regenerate=True
    print("[1] POST mark-template with regenerate=True")
    r = httpx.post(
        f"{BASE}/api/schedules/project/{SOURCE}/mark-template",
        json={"description": "E2E test template", "regenerate": True},
        timeout=300,
    )
    expect("mark-template returns 200", r.status_code == 200,
           f"status={r.status_code}, body={r.text[:200]}")
    if r.status_code != 200:
        return 1
    j = r.json()
    print(f"    response: is_template={j.get('is_template')}, "
          f"plan_regenerated={j.get('plan_regenerated')}, "
          f"plan_regen_error={j.get('plan_regen_error')}")
    expect("plan was regenerated", j.get("plan_regenerated") is True)
    expect("no plan_regen_error", j.get("plan_regen_error") is None)
    print()

    # 3. Read the new plan.md
    print("[2] verify the new plan.md")
    pdir = f"C:/Project/minimax code/hermes-project/{SOURCE}"
    import os
    plan_path = f"{pdir}/plan.md"
    expect("plan.md file exists", os.path.exists(plan_path))
    if not os.path.exists(plan_path):
        return 1
    with open(plan_path, encoding="utf-8") as f:
        plan = f.read()
    print(f"    plan.md size: {len(plan)} bytes")

    expect("plan.md > 1KB", len(plan) > 1000,
           f"got {len(plan)} bytes")
    expect("plan.md < 60KB (hard cap)", len(plan) < 60_000,
           f"got {len(plan)} bytes")
    expect("plan.md starts with --- (frontmatter)", plan.startswith("---"))
    expect("plan.md has ## Steps section", "## Steps" in plan)
    expect("plan.md has ## Variables section", "## Variables" in plan)
    expect("plan.md has {{var}} placeholders", "{{" in plan and "}}" in plan)
    print()

    # 4. L3-noise guards
    print("[3] L3-noise guards (anti-dump assertions)")
    bad_tokens = [
        "[cite:", "task.completed@", "DECISION: PASS", "DECISION: FAIL",
        "coord_pickup", "iteration_completed@", "_iteration_review:",
        "## Plan History", "## Coord Verdicts",
    ]
    for tok in bad_tokens:
        expect(f"plan.md does NOT contain {tok!r}", tok not in plan)
    print()

    # 5. Audit log
    print("[4] audit log: project.plan_regenerated present")
    import sqlite3
    db = sqlite3.connect(r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db")
    row = db.execute(
        "SELECT created_at, payload FROM audit_log "
        "WHERE project_id=? AND event_type='project.plan_regenerated' "
        "ORDER BY created_at DESC LIMIT 1", (SOURCE,)
    ).fetchone()
    expect("plan_regenerated audit present", row is not None)
    if row:
        print(f"    latest: {row[0]}")
        print(f"    payload: {row[1][:200]}")
        expect("audit payload has plan_size_bytes",
               "plan_size_bytes" in (row[1] or ""))
    print()

    # 6. Test regenerate=False — should NOT overwrite
    print("[5] regenerate=False (opt-out) does NOT overwrite plan.md")
    plan_before = open(plan_path, encoding="utf-8").read()
    r = httpx.post(
        f"{BASE}/api/schedules/project/{SOURCE}/mark-template",
        json={"description": "no-regen", "regenerate": False},
        timeout=10,
    )
    expect("mark-template returns 200", r.status_code == 200)
    if r.status_code == 200:
        j2 = r.json()
        expect("plan NOT regenerated when regenerate=False",
               j2.get("plan_regenerated") is False)
    plan_after = open(plan_path, encoding="utf-8").read()
    expect("plan.md unchanged (bytes match)", plan_before == plan_after)
    print()

    # summary
    print(f"=== {PASS_COUNT} pass / {FAIL_COUNT} fail ===")
    if FAIL_COUNT:
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
