"""E2E test for workflow package Stage 1 (promote + list + get + patch + delete).

Pipeline:
  1. Find a completed project (uses proj-e4c9e5dd "Google Drive Access" which
     has 4 completed tasks — good evidence for the LLM).
  2. POST /api/workflows/from-project/{id} with name + description.
     LLM synthesizes a workflow package (60-120s).
  3. GET /api/workflows/ to confirm it appears.
  4. GET /api/workflows/{id} to fetch detail, verify shape.
  5. PATCH the description (test the edit path).
  6. DELETE the workflow (cleanup).
  7. Verify it's gone.

Run: .venv\Scripts\python.exe scripts/_test-workflow-e2e.py
"""
import sys
import time
import json
import httpx

BASE = "http://localhost:8765"
SOURCE_PROJECT_ID = "proj-e4c9e5dd"  # Google Drive Access, 4 completed tasks
WF_NAME = f"gdrive-list-and-summarize-{int(time.time())}"  # unique per run


def main() -> int:
    # Use locals to avoid UnboundLocalError on early-return paths
    # (Python's scoping treats `+=` on module-level names as local).
    pass_count = 0
    fail_count = 0

    def expect(name: str, cond: bool, detail: str = "") -> None:
        nonlocal pass_count, fail_count
        if cond:
            pass_count += 1
            print(f"  PASS  {name}")
        else:
            fail_count += 1
            print(f"  FAIL  {name}  ->  {detail}")

    print(f"=== workflow package Stage 1 E2E ===")
    print(f"source project: {SOURCE_PROJECT_ID}")
    print(f"workflow name:  {WF_NAME}")
    print()

    # ---- 0. Source project must be in terminal state ----
    print("[0] verify source project is terminal")
    r = httpx.get(f"{BASE}/api/projects/{SOURCE_PROJECT_ID}", timeout=10)
    expect("source project fetchable", r.status_code == 200,
           f"status={r.status_code}, body={r.text[:200]}")
    if r.status_code != 200:
        print()
        print(f"=== {pass_count} pass / {fail_count} fail ===")
        return 1
    proj = r.json()
    state = proj.get("state", "?")
    expect("source project is in terminal state", state in ("completed", "failed", "cancelled", "interrupted"),
           f"got state={state!r}")
    if state not in ("completed", "failed", "cancelled", "interrupted"):
        print()
        print(f"=== {pass_count} pass / {fail_count} fail ===")
        return 1
    print(f"  source state: {state}")
    print()

    # ---- 1. POST promote (this calls the LLM, 60-120s) ----
    print(f"[1] POST /api/workflows/from-project/{SOURCE_PROJECT_ID}")
    print(f"    (LLM call — up to 180s)")
    t0 = time.time()
    r = httpx.post(
        f"{BASE}/api/workflows/from-project/{SOURCE_PROJECT_ID}",
        json={"name": WF_NAME, "description": "E2E test workflow — list and summarize Google Drive files."},
        timeout=200,
    )
    dt = time.time() - t0
    print(f"    LLM call took {dt:.1f}s, status={r.status_code}")
    if r.status_code == 422:
        # validation failed — print the LLM output for diagnosis
        print(f"    422 body: {r.text[:1000]}")
        fail_count += 1
        print("  FAIL  LLM-produced workflow failed validation")
        print()
        print(f"=== {pass_count} pass / {fail_count} fail ===")
        return 1
    expect("promote returns 200", r.status_code == 200,
           f"status={r.status_code}, body={r.text[:500]}")
    if r.status_code != 200:
        print()
        print(f"=== {pass_count} pass / {fail_count} fail ===")
        return 1
    wf = r.json()
    wf_id = wf.get("id", "")
    print(f"    created: id={wf_id}, name={wf.get('name')}, steps={wf.get('step_count')}, vars={wf.get('variable_count')}")
    print()

    # ---- 2. Schema checks ----
    print("[2] verify workflow shape")
    expect("id present", bool(wf.get("id")))
    expect("name matches request", wf.get("name") == WF_NAME)
    expect("description present", bool(wf.get("description")))
    expect("version present", bool(wf.get("version")))
    expect("step_template is a list", isinstance(wf.get("step_template"), list))
    expect("variables is a list", isinstance(wf.get("variables"), list))
    expect("step_count matches", wf.get("step_count") == len(wf.get("step_template", [])))
    expect("variable_count matches", wf.get("variable_count") == len(wf.get("variables", [])))
    expect("source_project_id matches", wf.get("source_project_id") == SOURCE_PROJECT_ID)
    print()

    # ---- 3. Step-level shape ----
    print("[3] verify step_template shape")
    for i, step in enumerate(wf.get("step_template", [])):
        if not isinstance(step, dict):
            FAIL_COUNT += 1
            print(f"  FAIL  step {i} is not a dict: {step}")
            continue
        ok_name = isinstance(step.get("name"), str) and step["name"]
        ok_action = isinstance(step.get("action"), str) and step["action"]
        ok_role = isinstance(step.get("agent_role"), str) and step["agent_role"]
        ok_deps = isinstance(step.get("depends_on"), list)
        expect(f"step[{i}] '{step.get('name', '?')}' has name/action/role/deps",
               ok_name and ok_action and ok_role and ok_deps)
    print()

    # ---- 4. Variable checks ----
    print("[4] verify variables shape")
    for i, var in enumerate(wf.get("variables", [])):
        if not isinstance(var, dict):
            FAIL_COUNT += 1
            print(f"  FAIL  var {i} is not a dict: {var}")
            continue
        ok_name = isinstance(var.get("name"), str) and var["name"]
        ok_type = var.get("type") in ("string", "number", "path", "choice", "boolean")
        ok_desc = isinstance(var.get("description"), str)
        expect(f"var[{i}] '{var.get('name', '?')}' has name/type/description",
               ok_name and ok_type and ok_desc)
    print()

    # ---- 5. Cross-checks: every {{var}} in step_template has a variable entry ----
    print("[5] cross-check: every {{var}} in step_template has a variables entry")
    import re
    var_re = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
    found: set[str] = set()
    def walk(v):
        if isinstance(v, str):
            for m in var_re.finditer(v):
                found.add(m.group(1))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    for step in wf.get("step_template", []):
        for f in ("params_template", "output_path", "name", "action", "agent_role"):
            if f in step:
                walk(step[f])
    declared = {v.get("name") for v in wf.get("variables", [])}
    expect("no undeclared {{var}} in step_template", not (found - declared),
           f"undeclared={found - declared}")
    expect("no orphan variable entries", not (declared - found),
           f"orphans={declared - found}")
    print(f"  found {len(found)} unique {{var}}: {sorted(found)}")
    print(f"  declared {len(declared)} variables: {sorted(declared)}")
    print()

    # ---- 6. GET /api/workflows/ ----
    print("[6] GET /api/workflows/ — verify it appears in list")
    r = httpx.get(f"{BASE}/api/workflows/", timeout=10)
    expect("list returns 200", r.status_code == 200)
    listed = [w for w in r.json() if w.get("id") == wf_id]
    expect("our workflow appears in list", len(listed) == 1)
    if listed:
        expect("list summary has step_count", "step_count" in listed[0])
        expect("list summary has variable_count", "variable_count" in listed[0])
    print()

    # ---- 7. GET /api/workflows/{id} ----
    print("[7] GET /api/workflows/{id}")
    r = httpx.get(f"{BASE}/api/workflows/{wf_id}", timeout=5)
    expect("detail returns 200", r.status_code == 200)
    if r.status_code == 200:
        wf2 = r.json()
        expect("detail step_template matches", wf2.get("step_template") == wf.get("step_template"))
        expect("detail variables matches", wf2.get("variables") == wf.get("variables"))
    # also by name
    r2 = httpx.get(f"{BASE}/api/workflows/{WF_NAME}", timeout=5)
    expect("lookup by name returns 200", r2.status_code == 200)
    print()

    # ---- 8. PATCH the description ----
    print("[8] PATCH /api/workflows/{id} — update description")
    new_desc = "E2E test (description updated by PATCH)"
    r = httpx.patch(
        f"{BASE}/api/workflows/{wf_id}",
        json={"description": new_desc},
        timeout=5,
    )
    expect("patch returns 200", r.status_code == 200,
           f"status={r.status_code}, body={r.text[:200]}")
    if r.status_code == 200:
        expect("description was updated", r.json().get("description") == new_desc)
    print()

    # ---- 9. DELETE ----
    print("[9] DELETE /api/workflows/{id}")
    r = httpx.delete(f"{BASE}/api/workflows/{wf_id}", timeout=5)
    expect("delete returns 200", r.status_code == 200)
    r = httpx.get(f"{BASE}/api/workflows/{wf_id}", timeout=5)
    expect("workflow is gone after delete", r.status_code == 404)
    print()

    # ---- 10. 409 on duplicate name ----
    print("[10] re-create to test 409 (skipped — we already deleted)")
    print()

    # ---- 11. /workflows HTML page renders ----
    print("[11] /workflows HTML page renders")
    r = httpx.get(f"{BASE}/workflows", timeout=5)
    expect("HTML page returns 200", r.status_code == 200)
    expect("page contains 'Workflow packages' heading", "Workflow packages" in r.text)
    expect("page has the nav link", 'href="/workflows"' in r.text)
    print()

    # ---- summary ----
    print()
    print(f"=== {pass_count} pass / {fail_count} fail ===")
    if fail_count:
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
