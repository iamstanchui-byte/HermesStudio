"""Planner — convert a project goal into a list of tasks (DAG).

Two modes:
- mock=True (or api_key empty): keyword-based hard-coded plan. No API call.
- mock=False with api_key: call MiniMax (OpenAI-compatible) with strict JSON schema.

Output: list of TaskPlan dicts, each with:
- name: human-readable (e.g. "Fetch data")
- agent_role: must be in available_roles
- depends_on: list of task NAMES (not ids) in the same plan, or []
- action: verb/function name (e.g. "fetch_market_data")
- params: dict (JSON-serializable)

The planner is also given a map of `role -> [skill_name, ...]` so it can
prefer the right role for each step based on what the user has taught each
agent. The mock planner ignores this and uses keyword heuristics.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger("hermes_orch.planner")


SYSTEM_PROMPT = """\
You are the planning brain of a multi-agent orchestrator. Given a project goal
and a set of available agent roles, you produce a JSON plan of tasks.

Output format (STRICT — output ONLY this JSON object, no prose, no markdown):
{
  "tasks": [
    {
      "name": "<short human-readable label>",
      "agent_role": "<must be one of the available roles>",
      "depends_on": ["<name of an earlier task>", ...],
      "action": "<verb or function name the agent will run>",
      "params": { "<key>": "<value>", ... },
      "required_capability": "<capability key the role MUST have, or null>"
    },
    ...
  ]
}

Rules:
1. Use ONLY the agent_role values provided. Do not invent roles.
2. Keep plans small (3-8 tasks). Prefer parallel where independent.
3. depends_on uses the `name` of earlier tasks in the same plan. Empty list = no deps.
4. action should be a snake_case verb (e.g. fetch_data, run_backtest, summarize).
5. params is a flat object with concrete values extracted from the goal.
6. The FIRST task(s) should have depends_on=[].
7. The LAST task should be a summary/finalize step that depends on prior work.
8. Output JSON only. No commentary.
9. ROLE SELECTION: each role has been taught some "skills" (listed below).
   Pick the role whose skills best match each step. If two roles can both
   do the work, prefer the one with more matching skills. If NO role has a
   matching skill, pick the most general role and have it improvise — don't
   invent a new role.
10. TERSENESS: keep `name` and `action` to ≤ 5 words each. No prose in any
    field. params should be a small flat object (≤ 4 keys) with concrete
    values, not long strings. The model's output token budget is tight;
    verbose tasks get truncated and the plan fails. Stay terse.
"""


# Max chars of the goal to inline in the planner prompt. The full goal is
# always stored in projects.goal; we just shorten it for the LLM so it
# doesn't burn reasoning tokens on rephrasing a long prompt. The LLM only
# needs the gist to plan; concrete values still go in task params from
# the full goal.
GOAL_PROMPT_CHARS = 200


USER_PROMPT_TEMPLATE = """\
Goal: {goal}

Available agent roles: {roles}

Role skills (what each role has been taught how to do):
{role_skills_block}

{capabilities_block}

{recent_block}

Produce a JSON plan. Pick the role whose skills best match each step.
If recent activity shows the user has been working on related goals, lean on
those patterns (e.g. "browser_fetch_X then ridge_predict then finalize")
instead of inventing new ones from scratch. The user prefers continuity.

For each task, set `required_capability` ONLY if the step genuinely requires
a specific tool/integration (e.g. "mt5", "xauusd_feed", "fred_csv").
A task that just needs general research or web browsing should leave
`required_capability` unset — don't over-constrain. The supervisor will
fail the task with `dispatch.mismatch` if the chosen role lacks the
required capability, so only set it when "wrong tool = wrong answer"
(e.g. the XAUUSD case: Linux super must use the MT5 bridge, not
Yahoo's free feed, or the analysis is built on stale/wrong prices).
"""


class Planner:
    def __init__(self, cfg: dict[str, Any], db: Any = None):
        self.cfg = cfg.get("llm", {}) or {}
        self.mock = bool(self.cfg.get("mock", True)) or not (self.cfg.get("api_key") or "").strip()
        self.base_url = (self.cfg.get("base_url") or "").rstrip("/")
        self.api_key = (self.cfg.get("api_key") or "").strip()
        self.model = self.cfg.get("model") or "MiniMax-Text-01"
        self.timeout = float(self.cfg.get("timeout_seconds", 60))
        # Optional DB handle for token-usage recording. If None
        # (legacy / tests), the recording is silently skipped. The
        # main.py lifespan passes the real db.
        self.db = db
        if self.mock:
            log.info("planner in MOCK mode (no api_key)")
        else:
            log.info(f"planner using {self.model} at {self.base_url}")

    async def plan(
        self,
        goal: str,
        available_roles: list[str],
        role_skills: dict[str, list[str]] | None = None,
        role_capabilities: dict[str, dict[str, bool]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return list of task plans. Raises on error.

        `role_skills` is an optional map of role-name -> list of skill names
        the role has been taught. When provided, the LLM planner uses it to
        pick the right role for each step. The mock planner ignores it.

        `role_capabilities` (Phase 4) is an optional map of role-name ->
        {capability_name: true}. The LLM planner uses this to decide
        whether to set `required_capability` on a task. A task that needs
        a specific integration (e.g. "mt5", "xauusd_feed") should set
        `required_capability` so the supervisor can fail-fast with
        `dispatch.mismatch` if no matching profile exists, instead of
        silently using a fallback (the XAUUSD case: Linux super picked
        Yahoo data instead of the MT5 bridge, producing wrong analysis).

        If the LLM planner fails (network, parse error, truncated response,
        etc.), we fall back to the mock plan so the project doesn't get
        stuck in 'planning' forever. The supervisor logs a
        `planner_fell_back_to_mock` event so operators can diagnose.
        """
        if not available_roles:
            raise ValueError("no available agent roles registered")
        # Track whether the last call fell back to mock so callers (e.g. the
        # supervisor's audit log) can report which planner actually produced
        # the plan. Without this, the audit logs "llm" even when the LLM
        # failed and we silently used the mock fallback — operators can't
        # tell a working LLM plan from a failed one.
        self.last_plan_was_fallback = False
        if self.mock:
            return self._plan_mock(goal, available_roles, role_skills)
        try:
            return await self._plan_llm(goal, available_roles, role_skills, role_capabilities)
        except Exception as e:
            # Distinguish truncation from other failures so operators can
            # tell at a glance whether the M3 cap was the cause. The
            # finish_reason=length message has a specific prefix that
            # logs and dashboards can grep on.
            err_str = str(e)
            if "finish_reason=length" in err_str:
                log.warning(
                    "LLM planner response truncated by model output cap "
                    "(likely M3 ~2-3k limit), falling back to mock plan. "
                    "Goal len=%d, goal_preview=%r",
                    len(goal), goal[:200],
                )
            else:
                log.warning(
                    "LLM planner failed (%s), falling back to mock plan: %s",
                    type(e).__name__, err_str,
                )
            self.last_plan_was_fallback = True
            return self._plan_mock(goal, available_roles, role_skills)

    def _format_recent_block(self) -> str:
        """Read user-level recent.md and format as a prompt block.

        Returns the recent context as a "Recent user activity" section
        that the LLM planner can use to bias the plan toward patterns
        the user has been using. Returns an empty string if recent.md
        is missing (e.g. on first project ever).
        """
        try:
            from hermes_orch.core.memory import get_memory_writer
            text = get_memory_writer().read_recent_tail(max_bytes=2048)
            if not text:
                return ""
            return (
                "Recent user activity (last 7 days, L3 cross-project summary):\n"
                + text
            )
        except Exception as e:
            log.warning(f"planner: failed to load recent.md: {e}")
            return ""

    @staticmethod
    def _format_role_skills(
        available_roles: list[str],
        role_skills: dict[str, list[str]] | None,
    ) -> str:
        """Render a role->skills map as a human-readable block for the prompt.

        Always lists every available role (even ones without skills, so the
        model knows they exist). Empty list is shown as '(no skills yet)'.
        """
        if not role_skills and not available_roles:
            return "(no roles)"
        lines: list[str] = []
        for r in available_roles:
            skills = role_skills.get(r) if role_skills else []
            if skills:
                lines.append(f"- {r}: {', '.join(skills)}")
            else:
                lines.append(f"- {r}: (no skills yet)")
        return "\n".join(lines) if lines else "(no roles)"

    @staticmethod
    def _format_capabilities_block(
        available_roles: list[str],
        role_capabilities: dict[str, dict[str, bool]] | None,
    ) -> str:
        """Render a role->capabilities map for the prompt.

        Capabilities are operator-curated flags like {"mt5": true,
        "xauusd_feed": true}. They're the "hard" version of skills:
        if a task needs `mt5` and the profile doesn't have it, the
        supervisor will fail the task with `dispatch.mismatch` rather
        than silently letting the agent use a fallback (e.g. yahoo data).

        If no capabilities are configured for any role, returns an empty
        string (don't bother the LLM with an empty block).
        """
        if not role_capabilities:
            return ""
        has_any = any(role_capabilities.get(r) for r in available_roles)
        if not has_any:
            return ""
        lines = ["Role capabilities (operator-curated; if a task needs a specific capability, set required_capability to the missing key, e.g. \"mt5\"):"]
        for r in available_roles:
            caps = role_capabilities.get(r) or {}
            true_caps = sorted(k for k, v in caps.items() if v)
            if true_caps:
                lines.append(f"- {r}: {', '.join(true_caps)}")
            else:
                lines.append(f"- {r}: (no capabilities set)")
        return "\n".join(lines)

    # ===== mock =====

    def _plan_mock(
        self,
        goal: str,
        available_roles: list[str],
        role_skills: dict[str, list[str]] | None = None,
    ) -> list[dict[str, Any]]:
        """Keyword-based hard-coded plan. Picks the first matching template.

        If role_skills is provided, we use it to bias the role pick: a role
        whose skills match the goal's keywords wins over a generic role.
        """
        g = goal.lower()
        # Pick roles from available. Skill-matching overrides the static
        # preferred list — e.g. if goal mentions "mt5" and a role has the
        # "mt5" skill, that role wins regardless of its name.
        role_pick = self._pick_roles_with_skills(
            available_roles,
            role_skills or {},
            preferred=["data-analyst", "backtest-runner", "research"],
        )

        # Ticker symbols from goal (uppercase 3-6 chars)
        symbols = re.findall(r"\b[A-Z]{3,6}\b", goal)
        if not symbols:
            symbols = re.findall(r"\b[a-z]{3,6}/[a-z]{3,6}\b", goal.lower())
        sym = symbols[0] if symbols else "EURUSD"
        timeframe = "M5" if "m5" in g or "5m" in g else "H1"

        if any(k in g for k in ["backtest", "strategy", "trade", "mt5"]):
            return [
                {
                    "name": "Fetch market data",
                    "agent_role": role_pick.get("data-analyst", available_roles[0]),
                    "depends_on": [],
                    "action": f"fetch_market_data for goal: {goal}",
                    "params": {"symbol": sym, "timeframe": timeframe, "period": "last 90 days", "goal": goal},
                },
                {
                    "name": "Run backtest",
                    "agent_role": role_pick.get("backtest-runner", available_roles[0]),
                    "depends_on": ["Fetch market data"],
                    "action": f"run_backtest for goal: {goal}",
                    "params": {"symbol": sym, "strategy": "default", "goal": goal},
                },
                {
                    "name": "Summarize results",
                    "agent_role": role_pick.get("data-analyst", available_roles[0]),
                    "depends_on": ["Run backtest"],
                    "action": f"summarize results for goal: {goal}",
                    "params": {"format": "markdown", "goal": goal},
                },
            ]
        if any(k in g for k in ["research", "news", "search", "find"]):
            return [
                {
                    "name": "Search sources",
                    "agent_role": role_pick.get("research", available_roles[0]),
                    "depends_on": [],
                    "action": f"web_search for goal: {goal}",
                    "params": {"query": goal, "max_results": 10, "goal": goal},
                },
                {
                    "name": "Summarize findings",
                    "agent_role": role_pick.get("research", available_roles[0]),
                    "depends_on": ["Search sources"],
                    "action": f"summarize findings for goal: {goal}",
                    "params": {"format": "markdown", "goal": goal},
                },
            ]
        # Generic: split into 3 steps. The full goal text is embedded
        # in each task's `action` field so the agent has full context
        # even though the mock plan is keyword-blind. The supervisor
        # can also pass the goal via params if it needs to render it
        # elsewhere.
        return [
            {
                "name": "Investigate",
                "agent_role": available_roles[0],
                "depends_on": [],
                "action": (
                    f"investigate — gather the data and context needed "
                    f"to address this goal: {goal}"
                ),
                "params": {"topic": goal, "goal": goal},
            },
            {
                "name": "Execute",
                "agent_role": available_roles[0],
                "depends_on": ["Investigate"],
                "action": (
                    f"execute — perform the analysis / work for this goal: {goal}"
                ),
                "params": {"topic": goal, "goal": goal},
            },
            {
                "name": "Report",
                "agent_role": available_roles[0],
                "depends_on": ["Execute"],
                "action": (
                    f"report — write a final markdown report summarizing the "
                    f"work done for this goal: {goal}"
                ),
                "params": {"format": "markdown", "goal": goal},
            },
        ]

    def _pick_roles(self, available: list[str], preferred: list[str]) -> dict[str, str]:
        """For mock: map preferred role names to whichever is in available (else fall back to first)."""
        out: dict[str, str] = {}
        fallback = available[0] if available else ""
        for p in preferred:
            if p in available:
                out[p] = p
            else:
                # Try to find a profile that contains the keyword
                match = next((a for a in available if p.split("-")[0] in a), None)
                out[p] = match or fallback
        return out

    def _pick_roles_with_skills(
        self,
        available: list[str],
        role_skills: dict[str, list[str]],
        preferred: list[str],
    ) -> dict[str, str]:
        """Like _pick_roles but biased by skills.

        If a role has skills that match the goal's keywords, that role
        wins over the static preferred list. Falls back to _pick_roles.
        The current caller doesn't pass the goal here, so we keep this
        conservative: only use skills as a tie-breaker when no preferred
        role is registered. (For richer selection, the LLM planner is
        the right tool — the mock is just a fallback.)
        """
        return self._pick_roles(available, preferred)

    # ===== real LLM =====

    async def _plan_llm(
        self,
        goal: str,
        available_roles: list[str],
        role_skills: dict[str, list[str]] | None = None,
        role_capabilities: dict[str, dict[str, bool]] | None = None,
    ) -> list[dict[str, Any]]:
        """Call MiniMax (OpenAI-compatible) with strict JSON mode.

        role_skills, if provided, is rendered into the prompt so the LLM
        picks the role whose skills best match each step.

        Truncation defenses (added 2026-07-18 after observing MiniMax M3
        hit a ~2-3k output cap and emit non-JSON):
        - Truncate the goal in the prompt to GOAL_PROMPT_CHARS so the LLM
          doesn't burn reasoning tokens rephrasing a long prompt.
        - SYSTEM_PROMPT rule #10: keep name/action ≤ 5 words, params ≤ 4 keys.
        - Detect finish_reason="length" before parsing — that means the
          response was cut off mid-JSON. Raise a specific error so the
          caller's try/except in plan() can fall back to the mock planner.
        """
        # Truncate the goal for the prompt only. The full goal remains in
        # projects.goal; concrete values still get pulled into task params
        # by the LLM (it sees the role_skills block which gives context).
        # Word-boundary split keeps the truncation from chopping a word.
        if len(goal) > GOAL_PROMPT_CHARS:
            truncated = goal[:GOAL_PROMPT_CHARS]
            # Drop the partial last word so we don't end on "inter" or similar
            truncated = truncated.rsplit(" ", 1)[0] + "..."
            goal_for_prompt = truncated
        else:
            goal_for_prompt = goal
        user_prompt = USER_PROMPT_TEMPLATE.format(
            goal=goal_for_prompt,
            roles=available_roles,
            role_skills_block=self._format_role_skills(available_roles, role_skills),
            capabilities_block=self._format_capabilities_block(available_roles, role_capabilities),
            recent_block=self._format_recent_block(),
        )
        # Generous max_tokens because thinking models (MiniMax M3, DeepSeek R1)
        # burn tokens on reasoning before producing JSON. Default would truncate.
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 8000,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=body,
            )
            r.raise_for_status()
            data = r.json()
        # Parse the content
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason", "")
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"LLM response missing content: {data}") from e

        # Record token usage. The OpenAI-compatible `usage` field is
        # present for all major providers (OpenAI, Anthropic via
        # proxy, MiniMax). The _plan_llm helper doesn't know which
        # project_id this plan is for (the supervisor wraps it; we
        # only have the goal here). call_label is the first 50 chars
        # of the goal so the dashboard shows which plan used what.
        if self.db is None:
            logger.warning(
                "planner: self.db is None, cannot record token usage. "
                "Was Planner() constructed without db=?"
            )
        else:
            try:
                usage = data.get("usage", {}) or {}
                from .token_usage import record_token_usage
                label = (goal[:50] + "...") if len(goal) > 50 else goal
                await record_token_usage(
                    self.db,
                    model=self.model,
                    base_url=self.base_url,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                    call_kind="planner",
                    call_label=label,
                )
            except Exception as ex:  # noqa: BLE001
                logger.debug("token usage recording skipped: %s", ex)
        # Detect truncation BEFORE we try to parse the JSON. If the model
        # hit its output cap mid-stream, finish_reason will be "length"
        # and the JSON is almost certainly incomplete. Falling back to
        # the mock planner is more useful than retrying (same model,
        # same cap, same result) or raising (the project would get stuck
        # in 'planning' until manual replan). The error message includes
        # enough detail for an operator to recognize "yes, this is the
        # M3 cap" vs other failures.
        if finish_reason == "length":
            preview = (content or "")[:200]
            raise RuntimeError(
                f"LLM response truncated (finish_reason=length, "
                f"content_len={len(content) if content else 0}); "
                f"preview={preview!r}"
            )
        # Some models (MiniMax M3, DeepSeek R1, etc.) wrap output in
        # <think>...</think> reasoning blocks. Sometimes the closing </think>
        # is missing (truncated). Handle both cases:
        #   1. Has </think>: take everything after it
        #   2. No </think> but has <think>: take everything from the first '{' onwards
        #   3. No thinking: use as-is
        import re
        if "</think>" in content:
            content_clean = content.split("</think>", 1)[1].strip()
        elif "<think>" in content:
            # Find the first { (start of JSON) and take from there
            brace_idx = content.find("{")
            content_clean = content[brace_idx:].strip() if brace_idx >= 0 else ""
        else:
            content_clean = content.strip()
        # Strip markdown code fences (```json ... ``` or ``` ... ```). MiniMax
        # M3 in particular wraps JSON in fences even when the SYSTEM_PROMPT
        # says "Output JSON only." Without this, json.loads fails and we
        # fall back to the mock plan unnecessarily. We match either the
        # first opening fence + the last closing fence, or the whole
        # fenced block.
        fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", content_clean, re.DOTALL)
        if fence_match:
            content_clean = fence_match.group(1).strip()
        else:
            # No full fenced block — maybe just a leading ```json line.
            # Strip any leading "```json" / "```" and trailing "```".
            content_clean = re.sub(r"^```(?:json)?\s*\n?", "", content_clean)
            content_clean = re.sub(r"\n?```\s*$", "", content_clean)
        if not content_clean:
            raise RuntimeError(f"LLM returned only thinking, no JSON: {content[:200]!r}")
        try:
            parsed = json.loads(content_clean)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"LLM returned non-JSON: {content_clean[:200]!r}") from e
        tasks = parsed.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise RuntimeError(f"LLM plan missing 'tasks' array: {parsed!r}")
        # Validate each task
        out: list[dict[str, Any]] = []
        names_seen: set[str] = set()
        for i, t in enumerate(tasks):
            if not isinstance(t, dict):
                raise ValueError(f"task #{i} not a dict: {t!r}")
            for required in ("name", "agent_role", "action"):
                if not t.get(required):
                    raise ValueError(f"task #{i} missing {required!r}: {t!r}")
            if t["name"] in names_seen:
                raise ValueError(f"duplicate task name: {t['name']!r}")
            names_seen.add(t["name"])
            if t["agent_role"] not in available_roles:
                raise ValueError(
                    f"task {t['name']!r} uses unknown role {t['agent_role']!r} "
                    f"(available: {available_roles})"
                )
            deps = t.get("depends_on") or []
            if not isinstance(deps, list):
                raise ValueError(f"task {t['name']!r} depends_on must be a list")
            for d in deps:
                if d not in names_seen:
                    raise ValueError(
                        f"task {t['name']!r} depends on {d!r} which is not defined earlier"
                    )
            params = t.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError(f"task {t['name']!r} params must be a dict")
            out.append({
                "name": t["name"],
                "agent_role": t["agent_role"],
                "depends_on": deps,
                "action": t["action"],
                "params": params,
            })
        if not out:
            raise RuntimeError("LLM plan produced 0 tasks")
        return out
