# coding: utf-8
"""Workflow package API + LLM synthesis.

Stage 1 (2026-07-23): synthesize a workflow package from a completed
project. A workflow package is a reusable execution template with
{{var}} placeholders + variables list. Stage 2b will add
POST /api/workflows/{id}/run that substitutes variables and spawns a
fresh project.

Reuses the 4-layer separation framework from
api/schedules.py::_SKILL_SYNTHESIS_PROMPT (drop L2/L3, keep L0/L1)
but the output is JSON, not markdown.
"""
from __future__ import annotations

import json
import re
import secrets
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes_orch.core.audit import audit_log
from hermes_orch.utils import now_iso as _now_iso


router = APIRouter(prefix="/api/workflows", tags=["workflows"])


# Hard cap on the step_template / variables JSON to keep them sane.
_MAX_STEP_TEMPLATE_BYTES = 100_000
_MAX_VARIABLES_BYTES = 50_000
# Names of fields the LLM is allowed to put in a step. Stricter than the
# raw tasks table (which has many internal columns) so the synthesized
# template is portable + read-only-by-the-runner.
_STEP_FIELDS = (
    "name", "agent_role", "action", "depends_on",
    "params_template", "output_path", "skill",
    # Stage 1.5 (2026-07-23): `skill` is an OPTIONAL reference to
    # an existing skill. The wrapper reads it from task params as
    # `_workflow_skill`, looks up `<profile>/skills/<name>/SKILL.md`
    # on the agent host, and injects the body into the task prompt
    # as a [SKILL: <name>] block. This is how workflow stays
    # parameter-light: instead of inlining skill content (huge
    # prompt, stale data) or re-discovering the data source URL
    # on every run (token waste), just reference the skill by name.
    # Phase 0 of visual workflow builder (2026-07-24, updated 2026-07-25
    # for Phase 2): `feedback_to` is an OPTIONAL list of step names
    # (NOT restricted to earlier ones — it's a TRIGGER semantic). If any
    # of those steps fails, this step is re-dispatched (and so are all
    # its transitive dependents via cascading invalidation). Used to
    # implement the search→analyze→audit→re-run-search loop-back pattern.
    # The wire direction in the visual builder is source→target, and
    # target.feedback_to += [source] — counter-intuitive but consistent:
    # target listens for source's failure and gets re-run on it.
    # Default: null/omitted (no loop-back). Cap: project.max_iterations.
    "feedback_to",
)
# Fields whose VALUES may contain {{var}} placeholders. Excludes
# `skill` (a static identifier) and `depends_on` (a list of step
# names — no user variables). If a future field needs variable
# substitution, add it here.
_STEP_FIELDS_WITH_VARS = (
    "name", "agent_role", "action", "params_template", "output_path",
)
# Variable types we accept. LLM may pick any; we validate.
_VALID_VAR_TYPES = {"string", "number", "path", "choice", "boolean"}


# --- Pydantic request/response models ---

class PromoteToWorkflowBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = Field("", max_length=500)
    variable_hints: list[dict] = Field(default_factory=list)
    # variable_hints: optional operator-provided hints like
    # [{"name": "folder_id", "type": "string", "description": "..."}]
    # The LLM can use them as a starting point but is free to add more
    # {{var}}s based on what it sees in the project trace.


class WorkflowSummary(BaseModel):
    id: str
    name: str
    version: str
    description: str
    source_project_id: str | None
    step_count: int
    variable_count: int
    created_at: str
    updated_at: str


class WorkflowDetail(WorkflowSummary):
    step_template: list[dict]
    variables: list[dict]
    # Phase 2.5 (2026-07-26): visual editor card positions. Dict
    # of {step_name: {x: int, y: int}}. Visual-only — never read by
    # the runner. Empty dict on workflows that have never been opened
    # in the visual editor, or after the operator clicks "Reset layout".
    visual_layout: dict = {}


# --- LLM synthesis ---

# Reuses the 4-layer separation framework from schedules.py, but the
# output schema is JSON, not markdown. Same anti-dump discipline.
_WORKFLOW_SYNTHESIS_PROMPT = """You are converting a project execution trace into a reusable workflow package.

A workflow package is a *long-lived reusable asset* — different from a one-off project. Someone (or some schedule) will run this workflow many times with different values. So the package must NOT bake in specific values from this project run; instead, it must PARAMETERIZE every value that would change on a re-run.

# Goal
Output a JSON object describing the workflow:
- step_template: ordered list of step objects
- variables: list of variable definitions (one per unique {{var}} in step_template)
- description: a 1-2 sentence summary of what this workflow does

# CRITICAL: output structure (often gets this wrong)
Your response MUST be a SINGLE JSON OBJECT starting with `{` and ending with `}`. The first line of your response MUST be the opening `{` of the WRAPPER, not the opening `{` of a step object. The wrapper has THREE keys at the TOP level: `description`, `step_template`, `variables` -- ALL THREE ARE REQUIRED. If you output the steps without the wrapper, the response is invalid. If you forget `description`, the response is invalid (we will fail validation and ask you to retry).

The `description` field MUST be a 1-2 sentence human-readable summary of what the workflow does (e.g. "Fetch HK weather forecast for a given date and write a Markdown report to Google Drive.").

# 4-Layer Separation (mandatory)
- L0 structure (step count, ordering, dependencies) → KEEP as step ordering + depends_on
- L1 actions (which tool / API / website / command) → KEEP in step.action
- L2 data (specific values: folder IDs, dates, coordinates, file names, raw text) → DROP. Replace with `{{var}}` placeholders.
- L3 decisions (task IDs, PASS/FAIL, coord scaffolding, internal engine names) → DROP entirely.

# When in doubt: would this value be the SAME on a re-run? If yes, hardcode it. If no (depends on user input, target, time, env), parameterize as `{{var}}`.

# Output schema (strict JSON, follow exactly)
```json
{{
  "description": "1-2 sentence summary, third person, no first-person pronouns",
  "step_template": [
    {{
      "name": "kebab-case step name (≤ 40 chars)",
      "agent_role": "win-agent01|super|super-b|... (which role runs this step; pick ONE)",
      "action": "verb_thing (snake_case action identifier; same shape as a tasks.action)",
      "depends_on": ["step_name_of_a_prior_step"],   // list of names; [] if no deps
      "params_template": {{
        "param1": "{{var1}}",
        "param2": "literal value if it's always the same, e.g. 30"
      }},
      "output_path": "relative path the step writes (e.g. list_files.results.json)",
      "skill": "kebab-case skill name (OPTIONAL — only when the source project used an existing skill; see Rule 11 below)",
      "feedback_to": ["step_name"]   // OPTIONAL — list of step names (NOT just earlier ones; trigger semantic) whose failure re-dispatches this step (Rule 13)
    }}
  ],
  "variables": [
    {{
      "name": "var1",                           // matches the {{var1}} in step_template
      "type": "string|number|path|choice|boolean",
      "description": "what this variable is, ≤ 80 chars",
      "required": true|false,
      "default": "optional default value if not required"
    }}
  ]
}}
```

# Rules (each will be validated)
1. Output MUST be valid JSON. No markdown wrapper, no preamble, no explanation. The response is parsed with json.loads.
2. step_template MUST be an ordered list (preserve execution order from the evidence).
3. depends_on MUST reference step names that appear EARLIER in step_template (no forward refs).
4. Every `{{var}}` in step_template MUST have a matching entry in variables.
5. Every entry in variables MUST appear in at least one step_template `{{var}}`.
6. step name MUST be kebab-case, ≤ 40 chars, unique within the template.
7. agent_role MUST be one of the role names the operator's agents actually have.
8. action MUST be a stable identifier (snake_case verb_thing). Drop internal engine names like `_iteration_review:1`, `coord_pickup`.
9. L2 data (specific values) MUST become `{{var}}`. CRITICAL: use EXACTLY two opening braces and two closing braces. NOT one brace. NOT three. The validator's regex is `\{\{name\}\}` — it will silently miss `{name}` or `{{{name}}}` and the workflow will be rejected.
   - "folder_id: 1NSWXynTF6HO..."  →  "folder_id": "{{gdrive_folder_id}}"
   - "date: 2026-07-22"            →  "date": "{{target_date}}"
   - "city: Taipei"                →  "city": "{{city}}"
   - "query: XAUUSD 1h data"       →  "query": "{{symbol_timeframe}}"
   - WRONG: `{gdrive_folder_id}` (single brace — will be ignored)
   - WRONG: `{{{gdrive_folder_id}}}` (triple brace — invalid)
   - RIGHT: `{{gdrive_folder_id}}` (double brace — matches regex)
   - CRITICAL: each `{{var}}` MUST be wrapped in double quotes as a JSON string value:
     - RIGHT: `"max_items": "{{max_items}}"`
     - WRONG: `"max_items": {{max_items}}` (unquoted — invalid JSON)
     - WRONG: `"max_items": "{{max_items}}}` (extra closing brace)
10. L3 scaffolding (PASS/FAIL, [cite:...], task IDs) MUST NOT appear in the output.
11. **OPTIONAL `skill` field** (Stage 1.5, 2026-07-23). Use this when
    the source project's tasks referenced a published skill (e.g.
    the agent had a `bus` skill that knows KMB bus route lookup
    URLs, or a `mt5-bridge` skill for MT5 trading data). Instead
    of inlining the skill content (huge, often stale), set:
      `"skill": "<skill-name>"`
    The wrapper resolves this at execute time: reads
    `<profile_root>/skills/<name>/SKILL.md` on the agent host and
    injects the body as a `[SKILL: <name>]` block in the task
    prompt. The agent then has the procedure (URLs, API patterns,
    completion criteria) without re-discovering it on every run.
    - OMIT `skill` when the step uses generic tools (web_search
      etc.) — let the agent figure it out from the goal.
    - DO NOT in skill L2 data (specific values) — skill content
      is L1 structural; specific dates / IDs / values belong in
      `params_template` as `{{var}}`.
    - Skill name MUST be kebab-case, ≤ 40 chars.
    - WRONG: `"skill": "_iteration_review:1"` (L3 scaffolding)
12. **PRESERVE ALL skills the source agent loaded** (Stage 1.5 multi-
    skill, 2026-07-23). The evidence section lists "Skills the source
    agent loaded" — these are real skills the agent on the source
    project actually used (parsed from its transcripts). Your
    synthesized step(s) MUST reference each one, either:
      - in the same step's `action` text (agent loads multiple
        skills at runtime via hermes skill mechanism), or
      - in a separate step's `skill` field (sequential)
    If you drop a skill the source used, the workflow loses that
    capability on every re-run. Real example that bit us: source
    project used `hk-weather-forecast` + `gdrive-write`; the LLM
    kept only `hk-weather-forecast` and the workflow never uploaded
    to GDrive. The user had to PATCH a 2nd step by hand.
13. **OPTIONAL `feedback_to` for the search→analyze→audit loop-back
    pattern** (Phase 0 of visual workflow builder, 2026-07-24,
    updated 2026-07-25 for Phase 2 — feedback_to can now reference
    ANY step in the workflow, not just earlier ones; it's a TRIGGER
    semantic).
    When a later step AUDITS the output of an earlier step (e.g.
    `audit-quality` checks `analyze-report`), and the audit can
    fail in a way that requires the earlier step to re-run with
    new inputs, set the earlier step's `feedback_to` to the audit
    step's name. The supervisor's loop-back logic will, on audit
    failure: (a) re-dispatch the earlier step, (b) **cascade** reset
    all its transitive dependents to `pending` (so analyze doesn't
    keep the stale report), (c) increment `current_iteration`, and
    (d) bail out at `max_iterations` (project-level cap).
    - `feedback_to` is a LIST of step names (NOT restricted to
      earlier ones — it can reference any step in the workflow;
      the validator just checks that the name exists).
      Self-reference is a silent no-op.
    - OMIT `feedback_to` when no loop-back is needed (the safe
      default). Forgetting it is OK; including it incorrectly
      (e.g. pointing to a step that doesn't actually audit this
      step) just means the loop never fires — safe failure.
    - Cap: `feedback_to` may reference MULTIPLE steps (the
      target step is re-dispatched if ANY of them fails).
    - Real example (search→analyze→audit→deliver):
      ```
      step_template: [
        {{ "name": "search",  "depends_on": [],  ... }},
        {{ "name": "analyze", "depends_on": ["search"], ... }},
        {{ "name": "audit",   "depends_on": ["analyze"], ... }},
        {{ "name": "deliver", "depends_on": ["audit"], ... }}
      ]
      ```
      WRONG (no feedback_to — audit fails, downstream still runs
      with stale data):
      ```
      // no feedback_to anywhere → audit fails → project fails
      // (downstream tasks get SKIPPED by _propagate_failures, but
      // the user has to manually re-run)
      ```
      WRONG (only audit gets feedback_to — re-runs audit, but
      analyze still uses old data — STALE OUTPUT BUG the user
      caught on 2026-07-24):
      ```
      {{ "name": "audit", "feedback_to": ["audit"], ... }}
      // self-reference — silent no-op
      ```
      RIGHT (search re-dispatched on audit fail; cascade resets
      analyze, audit, deliver to pending so they re-run with
      fresh search data). NOTE: search.feedback_to references
      audit, which is LATER in step_template — this is now valid
      because feedback_to is a trigger, not a dependency:
      ```
      {{ "name": "search",  "feedback_to": ["audit"], ... }},
      // feedback_to=["audit"] means:
      //   "if audit fails, re-run me (search)"
      // analyze/audit/deliver need NO feedback_to — the cascade
      // follows depends_on backwards from search automatically
      ```

# Operator hints (use as starting point for variable names + types)
{operator_hints}

# Evidence
{evidence}

# Output
Output ONLY the JSON object, starting with `{{`. No preamble, no explanation, no markdown wrapper.
"""


async def _gather_workflow_evidence(db, pdir: Path, project_id: str, proj: dict) -> str:
    """Build the evidence block for workflow LLM synthesis.

    Reuses the 4-layer pattern from schedules.py::_gather_skill_evidence
    but trims more aggressively (workflow = structural only, no body
    samples needed). We DO include the goal (so the LLM knows the use
    case) but DROP facts.md / decision.md / sample artifacts (those
    were L2/L3 trace artifacts that should not be re-runnable).
    """
    parts: list[str] = ["# Project evidence\n"]
    parts.append("## Project metadata\n")
    parts.append(f"- id: {project_id}\n")
    parts.append(f"- name: {proj.get('name') or project_id}\n")
    parts.append(f"- state: {proj.get('state', '?')}\n")
    parts.append(f"- goal: {(proj.get('goal') or '(none)')[:500]}\n")
    parts.append(f"- coordinator_role: {proj.get('coordinator_role') or '(none)'}\n")
    parts.append(f"- max_iterations: {proj.get('max_iterations') or 0}\n")

    # Tasks: include ALL terminal ones (completed + failed), with
    # structured fields the LLM needs. Drop the body of the result —
    # that's L2, would re-introduce stale specific values.
    tasks = await db.fetchall(
        "SELECT name, agent_role, action, status, depends_on, output_path, params, result "
        "FROM tasks WHERE project_id = ? "
        "ORDER BY created_at ASC",
        (project_id,),
    )

    completed = [t for t in tasks if (t.get("status") or "") == "completed"]
    failed = [t for t in tasks if (t.get("status") or "") in ("failed", "skipped", "cancelled", "interrupted")]

    # Skills the source agent loaded (parsed from completed task
    # results — the wrapper reported `skills_used` per task on
    # /result POST, parsed from the hermes transcript's
    # `📚 skill <name>` markers).
    # Without this, the LLM silently drops skill references from the
    # synthesized step (e.g. original task used hk-weather-forecast +
    # gdrive-write, synthesis kept only hk-weather-forecast).
    seen_skills: set[str] = set()
    skills_used: list[str] = []
    for t in completed:
        try:
            rraw = t.get("result") or ""
            r = json.loads(rraw) if isinstance(rraw, str) else rraw
        except Exception:
            r = {}
        for s in (r.get("skills_used") or []):
            if s and s not in seen_skills:
                seen_skills.add(s)
                skills_used.append(s)
    if skills_used:
        parts.append("\n## Skills the source agent loaded (Stage 1.5 ref: PRESERVE ALL of these in your synthesized step)\n")
        for s in skills_used:
            parts.append(f"- `{s}`\n")

    def _shape(t: dict) -> str:
        deps = t.get("depends_on") or []
        deps_str = f" (after: {', '.join(deps)})" if deps else ""
        params = t.get("params") or {}
        try:
            params_dict = json.loads(params) if isinstance(params, str) else params
        except Exception:
            params_dict = {}
        # Keep only stable-ish param fields; if a value looks like a
        # specific L2 fact, the LLM should replace with {{var}}.
        params_str = json.dumps(params_dict, ensure_ascii=False)[:300] if params_dict else "{}"
        op = t.get("output_path") or "(none)"
        return (
            f"- **{t.get('name') or 'task'}** [{t.get('agent_role') or '?'}]"
            f"{deps_str}: action=`{t.get('action') or '?'}`, "
            f"output=`{op}`, params={params_str}\n"
        )

    if completed:
        parts.append("\n## Completed steps (L0 structure + L1 actions, NO L2 values)\n")
        for t in completed:
            parts.append(_shape(t))
    if failed:
        parts.append("\n## Failed/skipped steps (drop these unless they reveal a dead-end the workflow should avoid)\n")
        for t in failed[:5]:  # cap to 5
            parts.append(_shape(t))

    return "".join(parts)


async def _call_llm_for_workflow_synthesis(
    evidence: str, llm_cfg: dict, operator_hints: list[dict] | None
) -> dict:
    """Call the LLM to synthesize a workflow package (JSON output).

    Same httpx + MiniMax pattern as _call_llm_for_skill_synthesis
    (schedules.py). Strips <think> traces and outer code-fence wrappers
    defensively. Output is parsed as JSON.
    """
    import httpx

    base_url = (llm_cfg.get("base_url") or "https://api.minimax.io/v1").rstrip("/")
    api_key = llm_cfg.get("api_key") or ""
    model = llm_cfg.get("model") or "MiniMax-M3"
    # Workflow synthesis uses longer evidence blocks than skill
    # synthesis. Bump default to 240s to survive MiniMax load spikes
    # (recent runs have hit 60-180s including <think> reasoning trace).
    timeout = float(llm_cfg.get("timeout_seconds") or 240)
    if not api_key:
        raise HTTPException(
            503,
            "LLM api_key not configured — set llm.api_key in config.yaml "
            "before promoting projects to workflows.",
        )

    hints_block = "None."
    if operator_hints:
        hints_block = json.dumps(operator_hints, ensure_ascii=False, indent=2)

    # Use plain str.replace, NOT str.format, because the prompt has
    # lots of literal `{{var}}` examples (which the LLM needs to see
    # verbatim) — str.format would interpret those as escape sequences
    # and produce `{var}` (single braces) in the rendered prompt,
    # which then makes the LLM use single braces too.
    prompt = (
        _WORKFLOW_SYNTHESIS_PROMPT
        .replace("{operator_hints}", hints_block)
        .replace("{evidence}", evidence)
    )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You output JSON only. Strict 4-layer separation. "
                    "Every value that would change on a re-run must be a "
                    "{{var}} placeholder, not a literal. "
                    "Do not emit thinking/reasoning trace."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 8000,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            f"{base_url}/chat/completions", json=payload, headers=headers
        )
    if r.status_code != 200:
        raise HTTPException(
            502, f"LLM returned HTTP {r.status_code}: {r.text[:300]}"
        )
    data = r.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise HTTPException(502, f"LLM response shape unexpected: {e}")
    print(
        f"[promote-to-workflow] LLM response: text_len={len(text) if isinstance(text, str) else 'N/A'}, "
        f"finish_reason={data['choices'][0].get('finish_reason')}, "
        f"first_100={(text[:100] if isinstance(text, str) else 'N/A')!r}",
        file=sys.stderr,
    )
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(502, f"LLM returned empty content (text type={type(text).__name__})")
    # Strip reasoning traces
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    text = text.strip()
    # Strip outer code fence (defensive)
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # Locate the first '{' and parse from there (the LLM may add
    # leading text despite the prompt)
    brace = text.find("{")
    if brace < 0:
        raise HTTPException(502, f"LLM response contained no JSON object: {text[:200]!r}")
    text = text[brace:]
    # Helper: try to wrap a "no outer wrapper" LLM response. Used
    # by both the success path (json.loads parsed the first bare step
    # silently) and the exception path (json.loads raised on extra
    # data). Returns the wrapped dict or None if not applicable.
    def _try_wrap_missing_wrapper(s: str):
        if "step_template" in s or "variables" not in s:
            return None
        try:
            var_pos = s.find('"variables"')
            if var_pos <= 0:
                return None
            pre = s[:var_pos].rstrip().rstrip(",").rstrip()
            # Strip a trailing stray `]` if the LLM accidentally
            # added one (it sometimes does, since it's confused about
            # where the step array should close). Without this, we'd
            # produce `...verify.results.json"}]]` (double `]`) which
            # is invalid JSON.
            while pre.endswith("]"):
                pre = pre[:-1].rstrip()
            first_obj_start = pre.find("{")
            if first_obj_start < 0:
                return None
            steps_array = "[" + pre[first_obj_start:] + "]"
            rest = s[var_pos:].rstrip().rstrip("}").rstrip(",").rstrip()
            wrapped = '{"step_template":' + steps_array + ", " + rest + "}"
            return json.loads(wrapped)
        except Exception:
            return None

    try:
        out = json.loads(text)
        # Post-parse sanity: if json.loads silently parsed the first
        # bare step object (so no exception), but the LLM response
        # also has "variables" (suggesting it forgot the wrapper),
        # try the wrap heuristic.
        if (isinstance(out, dict)
                and "step_template" not in out
                and "variables" not in out):
            wrapped = _try_wrap_missing_wrapper(text)
            if wrapped is not None:
                out = wrapped
        return out
    except json.JSONDecodeError as e:
        # Dump to a file for diagnosis
        try:
            dump_path = Path(r"C:\Users\stanley\AppData\Local\Temp\workflow_llm_dump.json")
            dump_path.write_text(text, encoding="utf-8")
        except Exception:
            pass
        # Heuristic fallback: the LLM may have output the step objects
        # + variables + closing } WITHOUT the outer {"step_template":
        # ... wrapper. Detect this by:
        #   1. response does NOT contain the literal "step_template"
        #   2. response contains "variables" (the LLM did produce that)
        # Then try to wrap: find the first `[` (the LLM would have
        # implied an array even if it wrote bare objects), wrap as
        # {"step_template": [<everything-before-first-]>], "variables": [...]}].
        if "step_template" not in text and "variables" in text:
            # Delegate to the helper (the inline version above is
            # identical in logic — kept for backward compat with
            # older debug dumps; the helper is the canonical one).
            wrapped = _try_wrap_missing_wrapper(text)
            if wrapped is not None:
                return wrapped
        raise HTTPException(502, f"LLM returned invalid JSON: {e}; dumped to {dump_path}; first 300 chars: {text[:300]!r}")
    return out


# --- Variable extraction + validation ---

_VAR_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _extract_variables_from_template(step_template: list[dict]) -> list[str]:
    """Scan step_template for {{var}} placeholders. Returns a sorted
    list of unique variable names found anywhere in the template
    (params_template values, output_path, etc.)."""
    found: set[str] = set()

    def _walk(value: Any) -> None:
        if isinstance(value, str):
            for m in _VAR_PLACEHOLDER_RE.finditer(value):
                found.add(m.group(1))
        elif isinstance(value, dict):
            for v in value.values():
                _walk(v)
        elif isinstance(value, list):
            for item in value:
                _walk(item)
        # numbers/booleans/None have no placeholders

    for step in step_template:
        for field in _STEP_FIELDS_WITH_VARS:
            if field in step:
                _walk(step[field])
    return sorted(found)


def _validate_workflow_package(pkg: dict) -> tuple[bool, str]:
    """Validate the LLM-produced workflow package.

    Returns (ok, error_message). On failure, the caller should refuse
    to write to DB and tell the user to re-run / hand-fix.

    Checks:
      1. Required keys present (description, step_template, variables).
      2. step_template is a non-empty list of dicts.
      3. Each step has the allowed fields only (no extra noise).
      4. Step names are unique + kebab-case + ≤ 40 chars.
      5. depends_on references earlier steps (no forward refs).
      6. variables is a list of {name, type, ...} dicts.
      7. Every {{var}} in step_template has a matching variables entry.
      8. Every variable is referenced in step_template (no orphans).
      9. variable type is in _VALID_VAR_TYPES.
     10. step_template JSON < 100KB, variables JSON < 50KB.
     11. No L3 scaffolding strings: [cite:, task.completed@, DECISION:,
         coord_pickup, iteration_completed@.
    """
    if not isinstance(pkg, dict):
        return False, f"top-level must be a dict, got {type(pkg).__name__}"
    for k in ("description", "step_template", "variables"):
        if k not in pkg:
            return False, f"missing required key: {k!r}"

    description = pkg["description"]
    if not isinstance(description, str) or len(description) > 1000:
        return False, f"description must be str ≤ 1000 chars (got {len(str(description))})"

    step_template = pkg["step_template"]
    if not isinstance(step_template, list) or not step_template:
        return False, f"step_template must be a non-empty list (got {type(step_template).__name__})"

    # Pre-pass: collect all step names so feedback_to validation can
    # reference the FULL workflow (not just earlier steps — feedback_to
    # is a TRIGGER semantic and order doesn't constrain it).
    all_step_names: set[str] = set()
    for i, step in enumerate(step_template):
        if not isinstance(step, dict):
            return False, f"step_template[{i}] must be a dict"
        name = step.get("name", "")
        if not isinstance(name, str) or not name:
            return False, f"step_template[{i}].name missing or empty"
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
            return False, f"step_template[{i}].name={name!r} not kebab-case"
        if len(name) > 40:
            return False, f"step_template[{i}].name={name!r} too long ({len(name)} > 40)"
        if name in all_step_names:
            return False, f"step_template[{i}].name={name!r} duplicate (already in {sorted(all_step_names)})"
        all_step_names.add(name)

    # Step-level checks
    seen_names: set[str] = set()
    for i, step in enumerate(step_template):
        if not isinstance(step, dict):
            return False, f"step_template[{i}] must be a dict"
        # Allowed fields only
        extra = set(step.keys()) - set(_STEP_FIELDS)
        if extra:
            return False, f"step_template[{i}] has extra fields: {sorted(extra)}; allowed: {_STEP_FIELDS}"
        # name
        name = step.get("name", "")
        if not isinstance(name, str) or not name:
            return False, f"step_template[{i}].name missing or empty"
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
            return False, f"step_template[{i}].name={name!r} not kebab-case"
        if len(name) > 40:
            return False, f"step_template[{i}].name={name!r} too long ({len(name)} > 40)"
        # Duplicate name check already done in pre-pass; keep seen_names
        # for the depends_on forward-ref check.
        seen_names.add(name)
        # action
        action = step.get("action", "")
        if not isinstance(action, str) or not action:
            return False, f"step_template[{i}].action missing or empty"
        if any(s in action for s in ("coord_pickup", "handoff:", "_iteration_review")):
            return False, f"step_template[{i}].action={action!r} contains L3 scaffolding"
        # depends_on
        deps = step.get("depends_on", [])
        if not isinstance(deps, list):
            return False, f"step_template[{i}].depends_on must be list"
        for d in deps:
            if d not in seen_names:
                return False, f"step_template[{i}].depends_on references {d!r} which is not an EARLIER step (or doesn't exist)"
        # skill (optional, Stage 1.5)
        skill = step.get("skill")
        if skill is not None:
            if not isinstance(skill, str) or not skill:
                return False, f"step_template[{i}].skill must be a non-empty string"
            if not re.match(r"^[a-z0-9][a-z0-9-]*$", skill):
                return False, f"step_template[{i}].skill={skill!r} not kebab-case"
            if len(skill) > 40:
                return False, f"step_template[{i}].skill={skill!r} too long ({len(skill)} > 40)"
            # Anti-L3: skill name shouldn't look like an internal action
            if any(s in skill for s in ("coord_pickup", "handoff:", "_iteration_review")):
                return False, f"step_template[{i}].skill={skill!r} contains L3 scaffolding"
        # feedback_to (optional, Phase 0 of visual builder, 2026-07-24,
        # updated 2026-07-25 for Phase 2 red dashed handle).
        # List of step names whose failure should re-dispatch THIS step.
        # null/omitted = no loop-back. Empty list = no loop-back.
        # Semantic: target.feedback_to = [source] means "if source fails,
        # re-run me (target)". It's a TRIGGER relationship — source is the
        # listener, target is the re-runner. Order does NOT matter; target
        # can be anywhere relative to source in step_template. The wire
        # goes source → target, and source is the one that gets re-run on
        # target's failure (intentionally counter-intuitive; see the
        # _STEP_FIELDS docstring + the LLM-synth prompt rules 13 for the
        # full example).
        # Each name must reference SOME step in the workflow (existence
        # check) but is NOT required to be earlier — only depends_on has
        # the forward-ref rule. Self-reference is a no-op (skip silently).
        fb = step.get("feedback_to")
        if fb is not None:
            if not isinstance(fb, list):
                return False, f"step_template[{i}].feedback_to must be a list (got {type(fb).__name__})"
            for fname in fb:
                if not isinstance(fname, str) or not fname:
                    return False, f"step_template[{i}].feedback_to contains non-string entry: {fname!r}"
                # Existence check against the FULL workflow (not just
                # earlier steps). feedback_to is a TRIGGER, not a
                # dependency — order doesn't constrain it.
                if fname not in all_step_names:
                    return False, f"step_template[{i}].feedback_to references {fname!r} which doesn't exist in this workflow"
                # Self-reference is silently dropped at runtime; we don't
                # reject it because the LLM sometimes produces it
                # defensively. It's just a no-op.

    # Variables
    variables = pkg["variables"]
    if not isinstance(variables, list):
        return False, f"variables must be a list (got {type(variables).__name__})"
    var_names_seen: set[str] = set()
    for i, v in enumerate(variables):
        if not isinstance(v, dict):
            return False, f"variables[{i}] must be a dict"
        vname = v.get("name", "")
        if not isinstance(vname, str) or not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", vname):
            return False, f"variables[{i}].name={vname!r} invalid (must be [a-zA-Z_][a-zA-Z0-9_]*)"
        if vname in var_names_seen:
            return False, f"variables[{i}].name={vname!r} duplicate"
        var_names_seen.add(vname)
        vtype = v.get("type", "string")
        if vtype not in _VALID_VAR_TYPES:
            return False, f"variables[{i}].type={vtype!r} invalid; valid: {sorted(_VALID_VAR_TYPES)}"

    # Cross-check: every {{var}} in step_template must have a variable entry
    found_vars = _extract_variables_from_template(step_template)
    missing = set(found_vars) - var_names_seen
    if missing:
        return False, f"step_template uses {{var}}s without variables entries: {sorted(missing)}"
    # And no orphan variables
    orphan = var_names_seen - set(found_vars)
    if orphan:
        return False, f"variables defined but not used in step_template: {sorted(orphan)}"

    # Size caps (JSON-encoded)
    st_bytes = len(json.dumps(step_template, ensure_ascii=False).encode("utf-8"))
    if st_bytes > _MAX_STEP_TEMPLATE_BYTES:
        return False, f"step_template too large: {st_bytes} > {_MAX_STEP_TEMPLATE_BYTES}"
    v_bytes = len(json.dumps(variables, ensure_ascii=False).encode("utf-8"))
    if v_bytes > _MAX_VARIABLES_BYTES:
        return False, f"variables too large: {v_bytes} > {_MAX_VARIABLES_BYTES}"

    # Anti-L3 dump: scan the entire JSON for known scaffolding strings
    full = json.dumps(pkg, ensure_ascii=False)
    bad = ("[cite:", "task.completed@", "DECISION: PASS", "DECISION: FAIL",
           "coord_pickup", "iteration_completed@")
    for s in bad:
        if s in full:
            return False, f"workflow package contains forbidden L3 scaffolding: {s!r}"

    # Defensive: if the LLM produced single-brace {var} placeholders
    # instead of the required {{var}}, auto-convert. This handles the
    # common LLM failure mode where the prompt says "double brace" but
    # the model still emits single braces. Only triggers if NO double
    # braces are present (so we don't accidentally touch an already-
    # correct workflow).
    if not _VAR_PLACEHOLDER_RE.search(full):
        single_re = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})")
        if single_re.search(full):
            def _conv(o):
                if isinstance(o, str):
                    return single_re.sub(r"{{\1}}", o)
                if isinstance(o, dict):
                    return {k: _conv(v) for k, v in o.items()}
                if isinstance(o, list):
                    return [_conv(x) for x in o]
                return o
            converted = _conv(pkg)
            # Recurse to re-validate the converted package
            return _validate_workflow_package(converted)

    return True, ""


# --- DB row helpers ---

def _row_to_workflow_summary(row) -> dict:
    """Convert a DB row to the WorkflowSummary shape (with step_count + variable_count)."""
    try:
        st = json.loads(row["step_template"] or "[]")
    except Exception:
        st = []
    try:
        vs = json.loads(row["variables"] or "[]")
    except Exception:
        vs = []
    return {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "description": row["description"],
        "source_project_id": row["source_project_id"],
        "step_count": len(st) if isinstance(st, list) else 0,
        "variable_count": len(vs) if isinstance(vs, list) else 0,
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
    }


def _row_to_workflow_detail(row) -> dict:
    """Convert a DB row to the WorkflowDetail shape (with parsed JSON)."""
    summary = _row_to_workflow_summary(row)
    try:
        st = json.loads(row["step_template"] or "[]")
    except Exception:
        st = []
    try:
        vs = json.loads(row["variables"] or "[]")
    except Exception:
        vs = []
    # Phase 2.5 (2026-07-26): visual_layout is a {step_name: {x,y}} dict.
    # If the column doesn't exist yet (pre-migration DB), KeyError falls
    # back to {} — same default as the column DEFAULT. Older rows may
    # also have a NULL value if the migration ran with an older schema.
    try:
        vl_raw = row["visual_layout"]
    except (KeyError, IndexError):
        vl = {}
    else:
        try:
            vl = json.loads(vl_raw) if vl_raw else {}
        except Exception:
            vl = {}
    if not isinstance(vl, dict):
        vl = {}
    summary["step_template"] = st
    summary["variables"] = vs
    summary["visual_layout"] = vl
    return summary


# Phase 2.5 (2026-07-26): validate the visual_layout field. Separate
# from _validate_workflow_package so the LLM-synth validator stays
# clean (the LLM never produces visual_layout, only the visual editor
# sends it via PATCH). Schema: {step_name: {x: number, y: number}}.
# Orphan step names (referring to deleted/renamed steps) are allowed
# — they're harmless and the visual editor just ignores them on render.
def _validate_visual_layout(vl) -> tuple[bool, str | None]:
    if vl is None:
        return True, None
    if not isinstance(vl, dict):
        return False, "visual_layout must be a dict"
    for name, pos in vl.items():
        if not isinstance(name, str) or not name:
            return False, f"visual_layout key {name!r} must be a non-empty string"
        if not isinstance(pos, dict):
            return False, f"visual_layout[{name!r}] must be a dict"
        x = pos.get("x")
        y = pos.get("y")
        # bool is a subclass of int in Python — reject True/False explicitly
        if not isinstance(x, (int, float)) or isinstance(x, bool) \
                or not isinstance(y, (int, float)) or isinstance(y, bool):
            return False, f"visual_layout[{name!r}] must have numeric x and y"
    return True, None


# --- API endpoints ---

@router.post("/from-project/{project_id}")
async def promote_project_to_workflow(
    project_id: str, body: PromoteToWorkflowBody, request: Request
) -> dict:
    """Synthesize a workflow package from a completed project.

    Pipeline: gather evidence → call LLM (4-layer framework) →
    validate → write to DB.

    Idempotency: if a workflow with `body.name` already exists, return
    409. Operator can DELETE the existing one first, or PATCH to update.
    """
    from hermes_orch.api.projects import _project_dir

    db = request.app.state.db
    cfg = request.app.state.config

    proj = await db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not proj:
        raise HTTPException(404, f"project {project_id} not found")

    # Refuse to synthesize from a project still in flight — workflow
    # templates should be from settled projects.
    if (proj.get("state") or "") not in ("completed", "failed", "cancelled", "interrupted"):
        raise HTTPException(
            400,
            f"project {project_id} is in state={proj.get('state')!r}; "
            "only terminal projects can be promoted to workflows.",
        )

    # Name uniqueness
    existing = await db.fetchone(
        "SELECT id FROM workflow_packages WHERE name = ?", (body.name,)
    )
    if existing:
        raise HTTPException(
            409,
            f"workflow package name={body.name!r} already exists "
            f"(id={existing['id']}); pick a different name or PATCH the existing one.",
        )

    pdir = _project_dir(request, project_id)
    evidence = await _gather_workflow_evidence(db, pdir, project_id, proj)
    llm_cfg = cfg.get("llm", {})

    try:
        pkg = await _call_llm_for_workflow_synthesis(
            evidence, llm_cfg, body.variable_hints
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"workflow LLM synthesis failed: {type(e).__name__}: {e}")

    # Operator description overrides LLM description if provided.
    if body.description:
        pkg["description"] = body.description

    # Defensive: the LLM sometimes forgets the top-level `description`
    # wrapper key (the prompt says it must be there, but M3 occasionally
    # drops it). Synthesize a sensible default rather than failing.
    # Operator override above takes precedence; this only kicks in when
    # pkg has no description AND body.description is empty.
    if not pkg.get("description"):
        # Build from project goal + first step action
        try:
            n_steps = len(pkg.get("step_template", []))
            first_action = (pkg["step_template"][0].get("action", "")
                            if pkg.get("step_template") else "")
            project_name = (proj.get("name") or proj.get("id") or project_id)
            if first_action:
                pkg["description"] = (
                    f"{first_action} workflow (synthesized from project "
                    f"{project_name}, {n_steps} step{'s' if n_steps != 1 else ''})"
                )
            else:
                pkg["description"] = (
                    f"Workflow synthesized from project {project_name} "
                    f"({n_steps} step{'s' if n_steps != 1 else ''})"
                )
        except Exception:
            pkg["description"] = (
                f"Workflow synthesized from project {project_id}"
            )

    ok, err = _validate_workflow_package(pkg)
    if not ok:
        # Include the LLM output in the error response so the operator
        # can see what came back and either re-run or hand-craft via PATCH.
        # Truncate to keep the error response small.
        llm_dump = json.dumps(pkg, ensure_ascii=False)[:1500]
        raise HTTPException(
            422,
            f"LLM-produced workflow failed validation: {err}. "
            f"Try again or hand-craft the workflow via PATCH. "
            f"LLM output: {llm_dump}",
        )

    # Write to DB
    wid = f"wf-{secrets.token_hex(6)}"
    now = _now_iso()
    try:
        await db.execute(
            "INSERT INTO workflow_packages "
            "(id, name, version, description, step_template, variables, "
            " source_project_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                wid, body.name, pkg.get("version", "0.1.0"),
                pkg["description"],
                json.dumps(pkg["step_template"], ensure_ascii=False),
                json.dumps(pkg["variables"], ensure_ascii=False),
                project_id, now, now,
            ),
        )
    except Exception as e:
        raise HTTPException(500, f"DB insert failed: {e}")

    await audit_log(
        db, "workflow.created", actor="operator", project_id=project_id,
        payload={"workflow_id": wid, "name": body.name,
                 "step_count": len(pkg["step_template"]),
                 "variable_count": len(pkg["variables"]),
                 "source": "promote-from-project"},
    )

    row = await db.fetchone(
        "SELECT * FROM workflow_packages WHERE id = ?", (wid,)
    )
    return _row_to_workflow_detail(row)


@router.get("/")
async def list_workflows(request: Request) -> list[dict]:
    """List all workflow packages (summary view)."""
    db = request.app.state.db
    rows = await db.fetchall(
        "SELECT * FROM workflow_packages ORDER BY updated_at DESC"
    )
    return [_row_to_workflow_summary(r) for r in rows]


@router.get("/{workflow_id}")
async def get_workflow(workflow_id: str, request: Request) -> dict:
    """Get a single workflow package (full detail)."""
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT * FROM workflow_packages WHERE id = ?", (workflow_id,)
    )
    if not row:
        # Try by name (more user-friendly)
        row = await db.fetchone(
            "SELECT * FROM workflow_packages WHERE name = ?", (workflow_id,)
        )
    if not row:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    return _row_to_workflow_detail(row)


class WorkflowPatchBody(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str | None = Field(None, max_length=500)
    step_template: list[dict] | None = None
    variables: list[dict] | None = None
    version: str | None = None
    # Phase 2.5 (2026-07-26): optional — only sent by the visual editor.
    # Visual-only, validated by _validate_visual_layout (separate from
    # _validate_workflow_package so the LLM-synth validator stays clean).
    visual_layout: dict | None = None


@router.patch("/{workflow_id}")
async def update_workflow(
    workflow_id: str, body: WorkflowPatchBody, request: Request
) -> dict:
    """Update an existing workflow package.

    Operator use case: LLM got it 90% right but the description is
    wrong, or one step name needs renaming, or a variable is missing.
    Re-validates the package on save.
    """
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT * FROM workflow_packages WHERE id = ?", (workflow_id,)
    )
    if not row:
        raise HTTPException(404, f"workflow {workflow_id} not found")

    # Compose new values
    new_name = body.name if body.name is not None else row["name"]
    new_desc = body.description if body.description is not None else row["description"]
    new_version = body.version if body.version is not None else row["version"]
    if body.step_template is not None:
        new_step = body.step_template
    else:
        new_step = json.loads(row["step_template"] or "[]")
    if body.variables is not None:
        new_vars = body.variables
    else:
        new_vars = json.loads(row["variables"] or "[]")
    # Phase 2.5 (2026-07-26): visual_layout. None means "don't touch"
    # (so non-visual PATCHes don't accidentally wipe positions). Validated
    # separately from the workflow package — the LLM-synth validator
    # doesn't see it, only the visual editor sends it.
    if body.visual_layout is not None:
        ok_vl, err_vl = _validate_visual_layout(body.visual_layout)
        if not ok_vl:
            raise HTTPException(422, f"visual_layout invalid: {err_vl}")
        new_visual_layout = body.visual_layout
    else:
        # Keep existing value from the row
        try:
            existing_vl_raw = row["visual_layout"]
        except (KeyError, IndexError):
            existing_vl_raw = "{}"
        try:
            new_visual_layout = json.loads(existing_vl_raw) if existing_vl_raw else {}
        except Exception:
            new_visual_layout = {}
        if not isinstance(new_visual_layout, dict):
            new_visual_layout = {}

    # Name uniqueness if changing
    if body.name is not None and body.name != row["name"]:
        existing = await db.fetchone(
            "SELECT id FROM workflow_packages WHERE name = ? AND id != ?",
            (body.name, workflow_id),
        )
        if existing:
            raise HTTPException(
                409, f"name={body.name!r} already used by workflow {existing['id']}"
            )

    # Validate
    fake_pkg = {
        "description": new_desc,
        "step_template": new_step,
        "variables": new_vars,
    }
    ok, err = _validate_workflow_package(fake_pkg)
    if not ok:
        raise HTTPException(422, f"validation failed: {err}")

    now = _now_iso()
    await db.execute(
        "UPDATE workflow_packages SET name=?, description=?, version=?, "
        "step_template=?, variables=?, visual_layout=?, updated_at=? WHERE id=?",
        (
            new_name, new_desc, new_version,
            json.dumps(new_step, ensure_ascii=False),
            json.dumps(new_vars, ensure_ascii=False),
            json.dumps(new_visual_layout, ensure_ascii=False),
            now, workflow_id,
        ),
    )
    await audit_log(
        db, "workflow.updated", actor="operator",
        payload={"workflow_id": workflow_id, "name": new_name,
                 "step_count": len(new_step), "variable_count": len(new_vars)},
    )
    row = await db.fetchone(
        "SELECT * FROM workflow_packages WHERE id = ?", (workflow_id,)
    )
    return _row_to_workflow_detail(row)


@router.delete("/{workflow_id}")
async def delete_workflow(workflow_id: str, request: Request) -> dict:
    """Delete a workflow package. Source projects are NOT touched
    (FK is ON DELETE SET NULL on source_project_id, but the inverse
    doesn't apply — deleting a workflow doesn't affect the source)."""
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT * FROM workflow_packages WHERE id = ?", (workflow_id,)
    )
    if not row:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    await db.execute(
        "DELETE FROM workflow_packages WHERE id = ?", (workflow_id,)
    )
    await audit_log(
        db, "workflow.deleted", actor="operator",
        payload={"workflow_id": workflow_id, "name": row["name"]},
    )
    return {"deleted": True, "id": workflow_id, "name": row["name"]}


# ===== Stage 2b: Run a workflow with variables =====

class WorkflowRunBody(BaseModel):
    variables: dict[str, Any] = Field(default_factory=dict)
    project_name: str | None = None  # override project name (default: workflow name)
    # 2026-07-25: gap fix. Without this, the spawned project always
    # gets max_iterations=0 (the hardcoded safe default), and the
    # supervisor's _maybe_loop_back returns False fast — meaning any
    # step.feedback_to in the workflow silently does nothing. Operator
    # can override here (3 is a sensible default; the supervisor caps
    # at 3 by convention so the project can't loop forever).
    max_iterations: int = 3


def _substitute_variables(value: Any, vars_provided: dict[str, Any]) -> Any:
    """Recursively substitute `{{var}}` placeholders in a value.

    Walks dicts, lists, and strings. Every `{{name}}` in a string
    becomes `vars_provided[name]` (stringified). Unknown placeholders
    are left as-is (the caller has already validated they're declared
    in the workflow's variables list, so this should not happen —
    but defense-in-depth).

    Same regex as `_extract_variables_from_template` for consistency.
    """
    _sub_re = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

    def _sub_str(s: str) -> str:
        return _sub_re.sub(
            lambda m: str(vars_provided.get(m.group(1), m.group(0))),
            s,
        )

    if isinstance(value, str):
        return _sub_str(value)
    if isinstance(value, dict):
        return {k: _substitute_variables(v, vars_provided) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_variables(x, vars_provided) for x in value]
    return value


def _validate_run_variables(
    declared: list[dict], provided: dict[str, Any]
) -> tuple[bool, str, dict[str, Any]]:
    """Validate that all required declared variables are provided.

    Returns (ok, error_message, typecast_values).
    - `declared` is the workflow's variables list (each has {name, type,
      required, default?}).
    - `provided` is what the operator sent in the run body.
    - `typecast_values` is `provided` with type conversions applied
      (e.g. "true" -> True for boolean, "5" -> 5 for number).

    2026-07-25: also reject unknown provided variables (typo trap).
    Previously the comment said 'allow extra provided vars ... but only
    declared ones are used' — this caused a silent-no-op bug where a
    user typo like `gdrive_folder_idd` instead of `gdrive_folder_id`
    was silently dropped, the real var got its default, and the task
    failed with empty params. Now we fail loud.

    Type coercion rules:
      - string: as-is
      - number: int(value) if it's a string that parses, else float
      - path: as-is (just validates non-empty)
      - choice: must be in (allowed?) — but we don't track allowed
        choices in the schema yet, so we accept any string
      - boolean: True/False/yes/no/1/0 strings → bool
    """
    out: dict[str, Any] = {}
    for v in declared:
        vname = v.get("name", "")
        vtype = v.get("type", "string")
        vrequired = bool(v.get("required", False))
        vdefault = v.get("default")
        # Resolve: provided > default
        if vname in provided:
            val = provided[vname]
        elif vdefault is not None:
            val = vdefault
        else:
            if vrequired:
                return False, f"required variable {vname!r} not provided", {}
            # optional + no default → skip (won't be substituted)
            continue
        # Coerce by type
        try:
            if vtype == "string" or vtype == "path" or vtype == "choice":
                out[vname] = str(val) if val is not None else ""
                if vtype == "path" and not out[vname].strip():
                    return False, f"variable {vname!r} (type=path) is empty", {}
            elif vtype == "number":
                if isinstance(val, bool):
                    # bool is subclass of int in Python; reject
                    return False, f"variable {vname!r} (type=number) got a boolean", {}
                if isinstance(val, (int, float)):
                    out[vname] = val
                else:
                    # try int first, fall back to float
                    try:
                        out[vname] = int(str(val))
                    except (ValueError, TypeError):
                        out[vname] = float(str(val))
            elif vtype == "boolean":
                if isinstance(val, bool):
                    out[vname] = val
                elif isinstance(val, (int, float)):
                    out[vname] = bool(val)
                else:
                    s = str(val).strip().lower()
                    if s in ("true", "yes", "1", "on"):
                        out[vname] = True
                    elif s in ("false", "no", "0", "off", ""):
                        out[vname] = False
                    else:
                        return False, f"variable {vname!r} (type=boolean) got {val!r}, cannot coerce", {}
            else:
                # Unknown type — pass through as string
                out[vname] = str(val)
        except Exception as e:
            return False, f"variable {vname!r} (type={vtype}) coercion failed: {e}", {}
    # Reject unknown provided variables (typo trap — see docstring).
    declared_names = {v.get("name") for v in declared}
    unknown = set(provided.keys()) - declared_names
    if unknown:
        return False, f"unknown variable(s) provided: {sorted(unknown)}; declared: {sorted(declared_names)}", {}
    return True, "", out


@router.post("/{workflow_id}/run", status_code=201)
async def run_workflow(
    workflow_id: str, body: WorkflowRunBody, request: Request
) -> dict:
    """Stage 2b: Run a workflow package with concrete variable values.

    Pipeline:
      1. Load workflow (by id or name)
      2. Validate provided variables against declared ones
         (required check, type coercion)
      3. Substitute `{{var}}` placeholders in step_template
      4. Create a fresh project (manual mode, tasks added directly)
      5. Insert one task per substituted step, with proper depends_on
      6. Audit: `workflow.run` event linking new project to workflow

    The new project has `source_workflow_id` (NEW column) set so
    the projects-list page can show a "🔁 from workflow X" badge
    like the existing schedule badge.
    """
    db = request.app.state.db
    from hermes_orch.api.projects import (
        _project_id, _projects_root, _serialize_plan_md,
    )
    from hermes_orch.core.memory import get_memory_writer

    # 1. Load workflow
    row = await db.fetchone(
        "SELECT * FROM workflow_packages WHERE id = ?", (workflow_id,)
    )
    if not row:
        row = await db.fetchone(
            "SELECT * FROM workflow_packages WHERE name = ?", (workflow_id,)
        )
    if not row:
        raise HTTPException(404, f"workflow {workflow_id} not found")

    try:
        step_template = json.loads(row["step_template"] or "[]")
    except Exception:
        step_template = []
    try:
        variables_declared = json.loads(row["variables"] or "[]")
    except Exception:
        variables_declared = []

    # 2. Validate variables
    ok, err, vars_typed = _validate_run_variables(
        variables_declared, body.variables
    )
    if not ok:
        raise HTTPException(400, f"variable validation failed: {err}")

    # 3. Substitute
    substituted = _substitute_variables(step_template, vars_typed)

    # 4. Create the new project (manual mode: tasks will be added below)
    new_pid = _project_id()
    now = _now_iso()
    # Auto-name format: include variables so each run is identifiable
    # by what it was given. Prefer the variables over a timestamp
    # (the timestamp is in the row's created_at column on the right
    # of the dashboard, so duplicating it in the name just makes
    # the row longer / pushes the right column off-screen). The
    # operator can still pass project_name in the request body to
    # fully override.
    if body.project_name:
        project_name = body.project_name
    elif vars_typed:
        # Show only the first 2 variables in the name to keep it
        # compact; the rest are in the goal field below. e.g.
        # "monthlyclaimtraffic-v1 (report_month=May2026)".
        var_items = list(vars_typed.items())
        shown = ", ".join(f"{k}={v!r}" for k, v in var_items[:2])
        if len(var_items) > 2:
            shown += f", +{len(var_items) - 2} more"
        project_name = f"{row['name']} ({shown})"
    else:
        # No variables — just the workflow name (timestamp lives
        # in created_at on the right of the row).
        project_name = f"{row['name']} @ {now[:10]}"
    # Build a goal from the workflow description + substituted values
    goal = row.get("description") or f"Run of workflow {row['name']}"
    if vars_typed:
        goal += "\n\nVariables:\n" + "\n".join(
            f"- {k} = {v!r}" for k, v in vars_typed.items()
        )
    try:
        await db.insert(
            "projects",
            {
                "id": new_pid,
                "name": project_name,
                "goal": goal,
                "state": "ready",  # manual mode = no planner; tasks are pre-loaded
                "coordinator_role": "",  # not iterative
                "accept_criteria": "",
                "deliverable_path": "",
                "max_iterations": body.max_iterations,  # 2026-07-25: was hardcoded 0, see WorkflowRunBody
                "current_iteration": 0,
                "last_iteration_summary": "",
                "source_workflow_id": row["id"],  # Stage 2b: link back
            },
        )
    except Exception as e:
        raise HTTPException(500, f"failed to create project: {e}")

    # Initialize project folder + plan.md (matches create_project() in
    # projects.py so the supervisor + memory hooks work normally)
    pdir = _projects_root(request) / new_pid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "agents").mkdir(exist_ok=True)
    plan_fm = {
        "project_id": new_pid, "state": "ready", "created_at": now,
        "tasks": [], "from_workflow": row["name"],
        "workflow_id": row["id"],
    }
    plan_body = (
        f"\n# Project: {project_name}\n\n"
        f"## Goal\n\n{goal}\n\n"
        f"## From workflow\n\n`{row['name']}` (id `{row['id']}`)\n"
    )
    (pdir / "plan.md").write_text(
        _serialize_plan_md(plan_fm, plan_body), encoding="utf-8"
    )
    status_fm = {"state": "ready", "last_updated": now}
    (pdir / "status.md").write_text(
        _serialize_plan_md(status_fm, "\n# Status\n\nWorkflow run started.\n"),
        encoding="utf-8",
    )
    (pdir / "decisions.md").write_text(
        _serialize_plan_md({"decisions": []}, "\n# Decisions\n\n"),
        encoding="utf-8",
    )

    # Bootstrap facts.md (L2) — same as create_project() does
    try:
        memory = get_memory_writer()
        memory.init_facts_file(new_pid, project_name=project_name)
        memory.append_fact_L2(
            project_id=new_pid, section="## Goal", fact_text=goal,
            cite_id="workflow.run",
        )
    except Exception:
        pass

    # 5. Insert tasks. First pass: insert all tasks and build a
    # step_name → task_id map for depends_on resolution.
    name_to_tid: dict[str, str] = {}
    task_rows: list[dict] = []
    for i, step in enumerate(substituted):
        sname = step.get("name") or f"step-{i+1}"
        tid = "t-" + secrets.token_hex(4)
        name_to_tid[sname] = tid
        # Resolve depends_on (which was step names) to task IDs
        dep_step_names = step.get("depends_on") or []
        dep_tids = [name_to_tid[d] for d in dep_step_names if d in name_to_tid]
        # Only first step can have no deps; if a later step references
        # an unknown step name, it would be lost (we set deps to [] and
        # log it).
        if dep_step_names and not dep_tids:
            await audit_log(
                db, "task.depends_on_unresolved",
                actor="workflow-runner", project_id=new_pid, task_id=tid,
                payload={"step_name": sname,
                         "unresolved_deps": dep_step_names},
            )
        # Reorder: tasks must be inserted in order they appear in
        # step_template so depends_on (which references earlier tasks)
        # can resolve. We've already done that.
        params = step.get("params_template") or {}
        # Stage 1.5 (2026-07-23): if the step has a `skill` reference,
        # pass the skill NAME in the task's params as `_workflow_skill`.
        # The wrapper reads this at execute time and injects the skill
        # body as a [SKILL: <name>] block in the task prompt. We pass
        # the NAME (not the body) so we don't bloat the DB with skill
        # content that may be hundreds of KB.
        skill_name = step.get("skill")
        if skill_name:
            # Copy params to avoid mutating the substituted template
            params = dict(params)
            params["_workflow_skill"] = skill_name
        # Phase 0 of visual workflow builder (2026-07-24): copy
        # step.feedback_to (a list of earlier step names) into the
        # task row. Supervisor reads this on every tick; if any named
        # step fails, this task is cascade-reset to pending. Default
        # '[]' = no loop-back (safe; matches pre-Phase-0 behavior).
        # Self-references (step name appears in its own feedback_to)
        # are silently dropped — a step can't react to its own failure.
        raw_fb = step.get("feedback_to") or []
        if isinstance(raw_fb, list):
            feedback_to = [f for f in raw_fb if f != sname]
        else:
            feedback_to = []
        task_rows.append({
            "id": tid,
            "project_id": new_pid,
            "name": sname,
            "agent_role": step.get("agent_role") or "",
            "depends_on": dep_tids,
            "on_parent_failure": "skip",
            "status": "pending",
            "priority": "normal",
            "action": step.get("action") or "do_task",
            "params": params,
            "max_retries": 2,
            "timeout_seconds": 1800,
            "output_path": step.get("output_path") or "",
            "required_capability": None,
            "feedback_to": json.dumps(feedback_to),
        })
    # Insert all tasks
    for t in task_rows:
        try:
            await db.insert("tasks", t)
        except Exception as e:
            raise HTTPException(
                500, f"failed to insert task {t['name']!r}: {e}"
            )
    # Audit each task.created (one event per task)
    for t in task_rows:
        await audit_log(
            db, "task.created",
            actor="workflow-runner", project_id=new_pid, task_id=t["id"],
            payload={"agent_role": t["agent_role"],
                     "action": t["action"],
                     "name": t["name"],
                     "source": "workflow-run"},
        )

    # 6. Audit the workflow.run event (top-level)
    await audit_log(
        db, "workflow.run", actor="operator", project_id=new_pid,
        payload={
            "workflow_id": row["id"],
            "workflow_name": row["name"],
            "project_id": new_pid,
            "variables_provided": list(vars_typed.keys()),
            "task_count": len(task_rows),
        },
    )

    return {
        "project_id": new_pid,
        "workflow_id": row["id"],
        "workflow_name": row["name"],
        "task_count": len(task_rows),
        "tasks": [{"id": t["id"], "name": t["name"]} for t in task_rows],
        "variables_applied": vars_typed,
        "state": "ready",
    }
