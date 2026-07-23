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
from typing import Any

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

    # ---- 9. Stage 2b: POST /api/workflows/{id}/run (BEFORE delete) ----
    print("[9] Stage 2b: run a workflow with variables")
    # Build variables dict from declared variables
    run_variables: dict[str, Any] = {}
    for v in wf.get("variables", []):
        if v.get("required", False) or v.get("default") is not None:
            if v["type"] in ("string", "path", "choice"):
                run_variables[v["name"]] = f"e2e-{v['name']}"
            elif v["type"] == "number":
                run_variables[v["name"]] = 42
            elif v["type"] == "boolean":
                run_variables[v["name"]] = True
    r = httpx.post(
        f"{BASE}/api/workflows/{wf_id}/run",
        json={"variables": run_variables,
              "project_name": f"e2e-run-{WF_NAME}"},
        timeout=30,
    )
    expect("run returns 201", r.status_code == 201,
           f"status={r.status_code}, body={r.text[:500]}")
    if r.status_code == 201:
        run = r.json()
        new_pid = run.get("project_id", "")
        expect("run returned project_id", bool(new_pid))
        expect("run task_count matches step count",
               run.get("task_count") == len(wf.get("step_template", [])))
        expect("run returned variables_applied", bool(run.get("variables_applied")))
        # The new project should have source_workflow_id set
        if new_pid:
            r2 = httpx.get(f"{BASE}/api/projects/{new_pid}", timeout=5)
            new_proj = r2.json()
            expect("new project has source_workflow_id",
                   new_proj.get("source_workflow_id") == wf_id)
            expect("new project state is ready", new_proj.get("state") == "ready")
            # Fetch the tasks for the new project
            r3 = httpx.get(
                f"{BASE}/api/tasks/?project_id={new_pid}",
                timeout=5, follow_redirects=True,
            )
            if r3.status_code == 200:
                tdata = r3.json()
                new_tasks = tdata if isinstance(tdata, list) else tdata.get("tasks", [])
                expect("new project has the right task count",
                       len(new_tasks) == len(wf.get("step_template", [])))
                # Verify substitution: at least one of the provided
                # values should appear in the task params somewhere
                if new_tasks:
                    all_params_str = json.dumps(
                        [t.get("params", {}) for t in new_tasks],
                        default=str,
                    )
                    found_any = False
                    for k, v in run_variables.items():
                        if str(v) in all_params_str:
                            found_any = True
                            break
                    expect("at least one substituted value found in task params",
                           found_any)
                # Verify depends_on chain: at least one task should
                # have a non-empty depends_on
                has_deps = any(t.get("depends_on") for t in new_tasks)
                expect("depends_on chain is preserved", has_deps)
        # Cleanup: delete the new project (archive it)
        r4 = httpx.delete(f"{BASE}/api/projects/{new_pid}", timeout=5)
        # 204 = archived, 200 = also OK; just check it didn't error
        expect("cleanup: delete new project", r4.status_code in (200, 204))
    print()

    # ---- 9b. Stage 1.5: skill field in step_template (BEFORE delete) ----
    print("[9b] Stage 1.5: PATCH workflow to add skill reference")
    # Add `skill: "bus"` to the first step. PATCH validates the new
    # package structure (including the new `skill` field). Then run
    # and verify the task params contain `_workflow_skill: "bus"`.
    step_template_patched = json.loads(json.dumps(wf["step_template"]))  # deep copy
    if step_template_patched:
        step_template_patched[0]["skill"] = "bus"
    r = httpx.patch(
        f"{BASE}/api/workflows/{wf_id}",
        json={"step_template": step_template_patched},
        timeout=10,
    )
    expect("PATCH with skill field returns 200", r.status_code == 200,
           f"status={r.status_code}, body={r.text[:300]}")
    if r.status_code == 200:
        r2 = httpx.get(f"{BASE}/api/workflows/{wf_id}", timeout=5)
        patched = r2.json()
        expect("patched step 0 has skill='bus'",
               patched["step_template"][0].get("skill") == "bus")
        # Run the patched workflow
        run_variables2: dict[str, Any] = {}
        for v in patched.get("variables", []):
            if v.get("required", False) or v.get("default") is not None:
                if v["type"] in ("string", "path", "choice"):
                    run_variables2[v["name"]] = f"e2e-skill-{v['name']}"
                elif v["type"] == "number":
                    run_variables2[v["name"]] = 42
        r3 = httpx.post(
            f"{BASE}/api/workflows/{wf_id}/run",
            json={"variables": run_variables2,
                  "project_name": f"e2e-skill-run-{WF_NAME}"},
            timeout=30,
        )
        expect("run after skill PATCH returns 201", r3.status_code == 201,
               f"status={r3.status_code}, body={r3.text[:300]}")
        if r3.status_code == 201:
            new_pid2 = r3.json().get("project_id", "")
            if new_pid2:
                r4 = httpx.get(
                    f"{BASE}/api/tasks/?project_id={new_pid2}",
                    timeout=5, follow_redirects=True,
                )
                if r4.status_code == 200:
                    tdata = r4.json()
                    new_tasks = tdata if isinstance(tdata, list) else tdata.get("tasks", [])
                    found_skill = False
                    for t in new_tasks:
                        params = t.get("params", {}) or {}
                        if params.get("_workflow_skill") == "bus":
                            found_skill = True
                            break
                    expect("at least one task has _workflow_skill='bus' in params",
                           found_skill)
            httpx.delete(f"{BASE}/api/projects/{new_pid2}", timeout=5)
    print()

    # ---- 10. DELETE ----
    print("[10] DELETE /api/workflows/{id}")
    r = httpx.delete(f"{BASE}/api/workflows/{wf_id}", timeout=5)
    expect("delete returns 200", r.status_code == 200)
    r = httpx.get(f"{BASE}/api/workflows/{wf_id}", timeout=5)
    expect("workflow is gone after delete", r.status_code == 404)
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
