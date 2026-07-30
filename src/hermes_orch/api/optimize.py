# coding: utf-8
"""Optimize-tasks endpoint — chatbox-driven code-gen flow.

The user-stated model (2026-07-26): "用orch server chatbox LLM 分析流程,
project 中哪些task 是可以用script 去做, 不用LLM 浪費token, 如果user confirm
可行, 就叫agent 寫code, 將這個code 也register 做object, 之後可以係task 上掛上".

This endpoint implements the FIRST half — analyze the project,
suggest which tasks are good candidates for deterministic script
replacement. The second half (user confirms -> spawn single task
"write_skill" -> agent writes the code -> register new Skill) is
triggered via POST /api/single-tasks with source={"kind": "code_gen",
"source_task_id": ..., "suggested_skill_name": ...}.

The LLM is called via the same LLMCaller used by the plan
contract, with a custom prompt that asks for a structured list
of suggestions. Output is validated by OptimizeSuggestions
Pydantic model.

The LLM call is non-trivial in cost (LLM tokens, latency), so
the endpoint is intentionally POST-only (no GET caching) and
the response includes a `cached: bool` field so the UI can
show "analysis from <timestamp>" if the user retries quickly.
"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes_orch.core.llm_caller import LLMCaller, LLMCallError

router = APIRouter()


# ===== Pydantic models =====


class OptimizeBody(BaseModel):
    """Body for POST /api/contracts/optimize-tasks.

    `project_id` is the project to analyze. We use the project's
    goal + existing task list + available skills (from the
    Object Layer) as input to the LLM.
    """
    project_id: str = Field(..., min_length=1)


class OptimizeSuggestion(BaseModel):
    """One suggestion: a task that could be a deterministic script."""
    task_id: str
    task_name: str
    rationale: str  # why this task is a good candidate
    suggested_skill_name: str  # kebab-case skill name to register under
    suggested_skill_description: str = ""  # one-sentence summary
    # How much the suggestion is worth doing (rough heuristic).
    # 0-1; the UI uses this to sort + show a confidence badge.
    confidence: float = Field(..., ge=0.0, le=1.0)


class OptimizeSuggestions(BaseModel):
    """The full structured output from the LLM."""
    suggestions: list[OptimizeSuggestion] = Field(default_factory=list)
    overall_notes: str = ""


class OptimizeOut(BaseModel):
    """Wire format for the endpoint response."""
    project_id: str
    suggestions: list[OptimizeSuggestion]
    overall_notes: str
    # Echoed metadata for the UI to render.
    task_count_analyzed: int
    suggested_count: int
    # When this analysis was produced (ISO timestamp). The UI can
    # show "analysis from 2026-07-27 12:34" and warn if stale.
    generated_at: str


SYSTEM_PROMPT = """\
You are a workflow optimization assistant. Given a project's task
list, identify which tasks are good candidates for being REPLACED
by a deterministic script (no LLM call needed). A task is a good
candidate when ALL of these hold:
  1. The task's action is procedural (parse, transform, lookup,
     format, extract) — not a judgment call
  2. The inputs and outputs are well-defined (the existing
     task params + result schema are clear)
  3. There's no LLM-specific reasoning required (no "summarize
     in plain English" or "decide which path to take")

Output a JSON object with:
  - "suggestions": list of {task_id, task_name, rationale,
    suggested_skill_name, suggested_skill_description, confidence}
  - "overall_notes": one-paragraph summary of the project's
    overall deterministic-suitability

If NO tasks are good candidates, return suggestions=[]. Don't
force suggestions — only flag tasks you're confident about.

For suggested_skill_name: kebab-case, e.g. "fetch-and-parse-csv",
"daily-report-formatter". The skill is a Python script that any
agent profile can invoke.

Rules:
  - JSON only, no prose
  - confidence is 0-1 (0.7+ = clear win, 0.4-0.7 = judgement call,
    <0.4 = don't include)
  - task_id MUST match one of the provided task ids exactly
"""


# ===== Endpoint =====


@router.post("", response_model=OptimizeOut)
async def optimize_tasks(body: OptimizeBody, request: Request) -> OptimizeOut:
    """Analyze a project's task list and suggest deterministic replacements.

    Pulls the project's tasks + goal + available skills (Object
    Layer), then calls the LLM via LLMCaller with the SYSTEM_PROMPT
    above. The output is validated against OptimizeSuggestions;
    any LLM parse error is surfaced as 502 with the raw text.
    """
    db = request.app.state.db
    cfg = request.app.state.config
    caller = LLMCaller(cfg, db=db)

    # 1. Load project
    proj = await db.fetchone(
        "SELECT id, name, goal, state FROM projects WHERE id = ?",
        (body.project_id,),
    )
    if not proj:
        raise HTTPException(404, f"Project not found: {body.project_id}")

    # 2. Load project tasks (active only — archived=0)
    task_rows = await db.fetchall(
        "SELECT id, name, action, agent_role, params, depends_on "
        "FROM tasks WHERE project_id = ? AND archived = 0 "
        "ORDER BY created_at ASC",
        (body.project_id,),
    )
    # Strip down the tasks for the LLM prompt (params can be huge)
    tasks_for_llm = []
    for r in task_rows:
        params = r.get("params") or "{}"
        if isinstance(params, str):
            try:
                params = json.loads(params) if params.strip() else {}
            except (json.JSONDecodeError, TypeError):
                params = {"_raw": params[:200]}
        deps = r.get("depends_on") or "[]"
        if isinstance(deps, str):
            try:
                deps = json.loads(deps) if deps.strip() else []
            except (json.JSONDecodeError, TypeError):
                deps = []
        tasks_for_llm.append({
            "id": r["id"],
            "name": r.get("name") or "",
            "action": r.get("action") or "",
            "agent_role": r.get("agent_role") or "",
            "params": params,
            "depends_on": deps,
        })

    if not tasks_for_llm:
        return OptimizeOut(
            project_id=body.project_id,
            suggestions=[],
            overall_notes="Project has no tasks yet. Add tasks first, then re-run.",
            task_count_analyzed=0,
            suggested_count=0,
            generated_at=_now_iso(),
        )

    # 3. Load available skills (Object Layer)
    # We do this via the API endpoint logic inline so the LLM
    # sees the same data the user sees in the registry.
    from hermes_orch.core.skill_loader import SkillLoader
    loader = SkillLoader(db)
    skills = await loader.list_all()
    skills_for_llm = [
        {
            "name": s.name,
            "profile_id": s.profile_id,
            "schema_deterministic": s.schema.deterministic,
            "schema_requires_capabilities": s.schema.requires_capabilities,
        }
        for s in skills
    ]

    # 4. Compose user prompt
    user_prompt = (
        f"Project: {proj['name']} (id={proj['id']}, state={proj['state']})\n"
        f"Goal: {proj.get('goal') or '(none)'}\n\n"
        f"Tasks ({len(tasks_for_llm)} total):\n"
        f"{json.dumps(tasks_for_llm, ensure_ascii=False, indent=2)}\n\n"
        f"Available skills ({len(skills_for_llm)} total):\n"
        f"{json.dumps(skills_for_llm, ensure_ascii=False, indent=2)}\n\n"
        "Identify the tasks that are good candidates for "
        "deterministic script replacement. Output the JSON object "
        "matching the OptimizeSuggestions schema."
    )

    # 5. Call LLM
    if caller.is_mock:
        # Mock: return one suggestion for the first task so the
        # UI has something to render. Real mode would fail because
        # mock is "no api_key".
        return OptimizeOut(
            project_id=body.project_id,
            suggestions=[
                OptimizeSuggestion(
                    task_id=tasks_for_llm[0]["id"],
                    task_name=tasks_for_llm[0]["name"],
                    rationale=(
                        "(mock) This task looks procedural — try "
                        "running it with a deterministic script."
                    ),
                    suggested_skill_name="mock-suggested-skill",
                    suggested_skill_description="Mock suggestion",
                    confidence=0.7,
                ),
            ],
            overall_notes="(mock mode) Real LLM not configured.",
            task_count_analyzed=len(tasks_for_llm),
            suggested_count=1,
            generated_at=_now_iso(),
        )
    try:
        data = await caller.call_json(
            user_prompt, system=SYSTEM_PROMPT,
            call_label="optimize.tasks",
            call_kind="agent_task",
            project_id=body.project_id,
        )
    except LLMCallError as e:
        raise HTTPException(502, f"LLM optimize-tasks call failed: {e}")
    try:
        parsed = OptimizeSuggestions.model_validate(data)
    except Exception as e:
        raise HTTPException(
            502, f"LLM output didn't match OptimizeSuggestions: {e}; "
            f"raw={json.dumps(data, ensure_ascii=False)[:500]!r}"
        )
    return OptimizeOut(
        project_id=body.project_id,
        suggestions=parsed.suggestions,
        overall_notes=parsed.overall_notes,
        task_count_analyzed=len(tasks_for_llm),
        suggested_count=len(parsed.suggestions),
        generated_at=_now_iso(),
    )


def _now_iso() -> str:
    """Local ISO timestamp (matches hermes_orch.utils.now_iso)."""
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")
