"""SOUL apply lifecycle for v3.9.0 dispatch path.

This module sits between routing (which picks an agent profile for a
plan step) and the supervisor (which actually sends the task to the
agent). Its job is to guarantee the **right** SOUL.md is on the
target profile's disk *before* the task runs.

Per `docs/soul-routing-design.md Â§"Lifecycle: SOUL apply before
dispatch"`, the original spec proposed:
  - per-profile `asyncio.Lock` to serialize applies
  - heartbeat poll for SOUL.md mtime confirmation

The Round 1 review (2026-08-01) realized the existing
`profile_configs` flow already serializes + confirms via its
atomic UPDATE pattern in `claim_pending_config`, so this module just
writes a `profile_configs` row and polls the row's `status` field.
No additional lock, no additional heartbeat check â€” the wrapper
poll loop + ack endpoint ARE the confirmation mechanism.

Flow:
  1. `resolve_role_to_profile` (orchestrator.routing) picks the profile
  2. `_ensure_soul_preset` auto-populates a project_soul_presets row
     if none exists for the step's role
  3. `_compose_soul_md` builds the standard 4-line header + content
  4. `_submit_soul_to_profile` writes a `profile_configs` row
     (idempotent: re-applying the same content reuses the existing
     row's id)
  5. `_wait_for_soul_applied` polls the row's status until the
     wrapper acks (`applied` / `failed`) or the 10s timeout
  6. `touch_soul_preset` records the apply timestamp + mtime
  7. A task row is created (pending) with `assigned_profile_id` set
     â€” the supervisor picks it up and dispatches as usual

This module is imported by `api.projects.dispatch_step` (Round 3
integration). The public surface is just `dispatch_step`; everything
else is module-private (`_`-prefixed) and tested in isolation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any, Mapping, Union

from hermes_orch.api.projects import get_soul_preset_by_role, touch_soul_preset
from hermes_orch.core.audit import audit_log, record_dispatch
from hermes_orch.db import Database
from hermes_orch.orchestrator.routing import resolve_role_to_profile
from hermes_orch.utils import now_iso as _now_iso
# v3.12.1 hardening: use utils.now_iso (free function) instead of
# importing `_now_iso` from supervisor. supervisor does a function-local
# `from ... import soul_dispatch` (see supervisor._dispatch_via_soul_dispatch),
# so any module-level import back to supervisor would create a circular
# dep. utils is leaf-level, safe to alias at module scope.
from hermes_orch.utils import now_iso as _now_inner


# A "step" can come in two shapes:
#   - PlanStep (Pydantic v2 model from api.plans) â€” used by the
#     chatbox / plan editor
#   - dict (the shape the routing engine accepts) â€” used by tests
#     and anythin else that wants to avoid the Pydantic dep
# We normalise to a dict at the boundary so the rest of the module
# is uniform. Routing expects a dict too, so this is a no-op cost.
StepLike = Union[Mapping[str, Any], Any]


class SoulApplyError(Exception):
    """Raised when the SOUL apply cycle fails or times out.

    Attributes:
        cfg_id: the `profile_configs.id` row that failed (so the
            operator can inspect it in the DB / dashboard).
        error_msg: the wrapper-supplied error (or the timeout reason
            if no wrapper error was recorded).
    """

    def __init__(self, message: str, *, cfg_id: str, error_msg: str):
        self.cfg_id = cfg_id
        self.error_msg = error_msg
        super().__init__(message)


# ===== Pure helpers (no DB, easy to unit-test) =====


def _sha256(content: str) -> str:
    """Return the hex SHA-256 of a UTF-8 encoded string.

    Matches the hash used by `api.agents.create_config` for the
    `profile_configs.desired_sha256` column. Centralised here so the
    dispatch path and the agents endpoint can never disagree on the
    hash function.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _compose_soul_md(role_name: str, project_id: str, content: str) -> str:
    """Render the standard SOUL.md with the 4-line header.

    The header is the first thing the LLM sees when reading SOUL.md,
    so it primes the role context before the prose body. The 4 lines
    are deliberately parseable (`# KEY: value`) so a future tool can
    extract them without parsing free-form markdown.

    Args:
        role_name: the role label (e.g. "cpi-analyst") â€” denormalized
            from the project_soul_presets row.
        project_id: which project this preset belongs to.
        content: the actual SOUL prose (already stripped of trailing
            whitespace by the caller).

    Returns:
        The full SOUL.md body, terminated with a single newline.
    """
    return (
        f"# ROLE: {role_name}\n"
        f"# PROJECT: {project_id}\n"
        f"# APPLIED_AT: {_now_iso()}\n"
        f"# ----\n\n"
        f"{content.strip()}\n"
    )


def _generic_role_template(role_name: str) -> str:
    """Return a sensible default SOUL for an unknown role.

    Used when neither the workflow step's `default_soul` nor the
    project preset has content. The wording is intentionally generic
    so it works for any role â€” the workflow author is expected to
    supply a real `default_soul` for production workflows.

    Args:
        role_name: the role label to embed in the prompt.

    Returns:
        A multi-line SOUL body (no header; the header is added by
        `_compose_soul_md`).
    """
    return (
        f"You are an AI assistant serving as `{role_name}` in this project.\n"
        f"Adapt your expertise to whatever the project requires of this role.\n"
        f"Be precise, cite your assumptions, and surface uncertainty explicitly."
    )


# ===== LLM-driven SOUL generation (v3.10.5) =====
#
# Why this exists: prior to v3.10.5, the planner LLM was asked to
# produce a `default_soul` field on every step. That had two problems:
#   1. The planner prompt is already long (8K+ chars of context) and
#      `default_soul` was just another field the LLM had to fill
#      correctly under a tight token budget. Real-world repro: a
#      4-step plan came back with empty `default_soul` on every step
#      because the LLM had burned its budget on the rest of the
#      schema. The seed helper then either skipped (old code) or
#      fell back to a generic template (v3.10.4 follow-up).
#   2. The planner LLM sees a compact, role-centric view of the
#      project. It doesn't know what other steps the same role will
#      execute, or the user-level project description, or the role's
#      storage aliases. A dedicated SOUL-generation call with all
#      that context produces much better personas.
#
# v3.10.5 split: a dedicated LLM call focuses on SOUL generation.
# Triggered at "Generate Task" time (alongside task creation) and at
# the manual "Generate SOUL" button (for the recovery use case where
# the user accidentally deleted a preset). Falls back to the generic
# template on any error â€” never blocks the user's flow.


SOUL_GEN_SYSTEM_PROMPT = """\
You are the SOUL persona writer for a multi-agent orchestrator. Given
a project's goal and the planned tasks, you produce a SOUL persona for
each unique agent role in the plan.

A SOUL is a 2-4 sentence persona text that will be prepended to an
agent's LLM call when it runs a task. The persona tells the agent:
  - WHO it is (role + domain expertise)
  - WHAT this specific project is about (so it doesn't drift)
  - HOW it should approach the work (tone, format, language)
  - WHERE outputs go (storage alias mentioned in params, if any)

Output format (STRICT JSON, no prose, no markdown, no <think>):
{
  "souls": {
    "<role_name>": "<2-4 sentence persona text>",
    ...
  }
}

Rules:
1. One entry per unique role in the plan. Same role in different
   steps gets ONE persona (not per-step) â€” the persona should be
   general enough to cover all the steps the role executes.
2. Each persona MUST be specific to this project â€” name the project's
   topic/focus when the description provides it. Generic text like
   "you are a helpful assistant" is not acceptable.
3. If a role's steps specify a language (e.g. `lang: zh-HK` in
   params), the persona should mention the language. Same for
   output_path â†’ mention the storage alias the agent will use.
4. Keep each persona 2-4 sentences (60-200 words). Concise wins.
5. Output JSON only. No commentary, no markdown fences, no <think>.
6. If the project description is too vague to write a specific
   persona, fall back to "You are a <role> agent for this project"
   + 1 sentence of context from the goal. Do NOT leave a role out.
"""


def _format_plan_for_soul_gen(
    project_name: str,
    project_description: str,
    role_to_steps: dict[str, list[dict[str, Any]]],
) -> str:
    """Render the user-prompt input block for SOUL generation.

    Args:
        project_name: the project's display name (e.g. "analyst 6").
        project_description: the project's goal/description (can be empty).
        role_to_steps: {role_name: [step_dict, ...], ...}. Each step_dict
            has keys name, action, params, output_path, depends_on so
            the LLM can see what the role will do.

    Returns:
        A multi-line string suitable for the LLM's user message.
    """
    lines: list[str] = []
    lines.append(f"Project name: {project_name or '(unnamed)'}")
    desc = (project_description or "").strip()
    if desc:
        # Truncate to avoid burning reasoning tokens on a long goal.
        if len(desc) > 600:
            desc = desc[:600].rsplit(" ", 1)[0] + "..."
        lines.append(f"Project description: {desc}")
    else:
        lines.append("Project description: (none provided)")
    lines.append("")
    lines.append("Roles and the steps they will execute:")
    for role in sorted(role_to_steps):
        steps = role_to_steps[role]
        lines.append(f"\n## Role: {role} ({len(steps)} step(s))")
        for i, s in enumerate(steps, 1):
            lines.append(f"  Step {i}: name={s.get('name', '?')!r}")
            lines.append(f"    action: {s.get('action', '?')}")
            params = s.get("params") or {}
            if params:
                # Compact param rendering; full params can be huge
                param_str = ", ".join(f"{k}={v!r}" for k, v in params.items())
                if len(param_str) > 300:
                    param_str = param_str[:300] + "..."
                lines.append(f"    params: {param_str}")
            op = s.get("output_path") or ""
            if op:
                lines.append(f"    output_path: {op}")
    return "\n".join(lines)


async def _generate_souls_via_llm(
    plan: Any,
    project_name: str,
    project_description: str,
    llm_cfg: dict[str, Any],
) -> dict[str, str]:
    """Call the LLM to generate role-specific SOULs from a plan.

    v3.10.5 (2026-08-02): replaces the planner's `default_soul` field
    (which the planner LLM often produced empty) with a dedicated,
    focused LLM call that has the full project context.

    Args:
        plan: a ProjectPlan (Pydantic) â€” must have `.steps`.
        project_name: the project's display name.
        project_description: the project's goal/description (used to
            anchor the personas to the project topic).
        llm_cfg: the LLM config dict (the same `cfg["llm"]` block the
            planner uses). Must contain base_url, api_key, model.
            Optional: timeout_seconds (default 60), mock (if True, no
            call is made â€” empty dict is returned).

    Returns:
        A dict {role_name: persona_text}. The keys are the unique
        roles found in the plan; values are the LLM-generated 2-4
        sentence personas. The dict only contains roles the LLM
        actually returned â€” caller is responsible for filling any
        missing roles with the generic template.

    Raises:
        RuntimeError: on any LLM call / parse failure. Caller should
            fall back to the generic template for all roles.
    """
    import logging
    log = logging.getLogger(__name__)

    base_url = (llm_cfg.get("base_url") or "").rstrip("/")
    api_key = (llm_cfg.get("api_key") or "").strip()
    model = llm_cfg.get("model") or "MiniMax-Text-01"
    timeout = float(llm_cfg.get("timeout_seconds", 60))
    mock = bool(llm_cfg.get("mock", True)) or not api_key
    if mock:
        # Mock mode = no LLM call. Caller falls back to generic.
        raise RuntimeError("LLM in mock mode; cannot generate SOULs")
    if not base_url:
        raise RuntimeError("LLM base_url not configured")

    # Build role -> steps map. Same role across multiple steps gets
    # all of its steps listed so the LLM can write one persona
    # covering all of them.
    role_to_steps: dict[str, list[dict[str, Any]]] = {}
    for step in plan.steps:
        role = (step.agent_role or "").strip()
        if not role:
            continue
        role_to_steps.setdefault(role, []).append({
            "name": step.name,
            "action": step.action,
            "params": step.params_template or {},
            "output_path": step.output_path,
        })
    if not role_to_steps:
        return {}

    user_prompt = _format_plan_for_soul_gen(
        project_name=project_name,
        project_description=project_description,
        role_to_steps=role_to_steps,
    )

    # max_tokens: ~200 tokens per role (60-200 words of prose) + LLM
    # reasoning overhead. For a 5-role plan, 2000 tokens is plenty.
    # We use 3000 to leave headroom for the reasoning traces MiniMax
    # M3 emits before the JSON.
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SOUL_GEN_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.4,
        "max_tokens": 3000,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Late import so the helper stays testable without httpx in
    # non-LLM test paths.
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=body,
            )
        if r.status_code != 200:
            raise RuntimeError(
                f"LLM returned HTTP {r.status_code}: {r.text[:200]}"
            )
        data = r.json()
    except Exception as e:
        # Network / parse / HTTP error â€” log + raise for caller to
        # fall back. Don't expose raw httpx errors to the user.
        log.warning("SOUL gen LLM call failed: %s", e)
        raise RuntimeError(f"SOUL gen LLM call failed: {e}") from e

    # Extract content. Same truncation defenses as the planner.
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
        finish_reason = choice.get("finish_reason", "")
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"LLM response shape unexpected: {e}") from e

    if finish_reason == "length":
        # Truncated â€” likely won't parse. Bail and let caller fallback.
        raise RuntimeError(
            "LLM response truncated (finish_reason=length); "
            "max_tokens too small or output cap hit"
        )

    # Strip <think> traces (MiniMax M3 emits them before the answer).
    # If the response is ONLY <think>, content will be empty after
    # the strip and we'll fall back. Same defense as the chat path
    # (see api/projects.py:3643-3667 for the v3.10.4 lesson).
    import re as _re
    stripped = _re.sub(r"<think>.*?</think>", "", content, flags=_re.DOTALL).strip()
    if not stripped:
        raise RuntimeError(
            "LLM returned only internal reasoning (<think> only); "
            "no final SOUL JSON"
        )

    # Parse JSON.
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as e:
        log.warning(
            "SOUL gen LLM returned non-JSON: %s; first 200 chars: %r",
            e, stripped[:200],
        )
        raise RuntimeError(f"LLM returned non-JSON: {e}") from e

    souls_raw = parsed.get("souls")
    if not isinstance(souls_raw, dict):
        raise RuntimeError(
            f"LLM JSON missing 'souls' dict; got keys: {list(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__}"
        )

    # Validate each value is a non-empty string. Anything weird â†’ drop
    # the role (caller will fill with generic).
    out: dict[str, str] = {}
    for role, text in souls_raw.items():
        if not isinstance(role, str) or not role.strip():
            continue
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue
        # Cap at 1500 chars to avoid absurdly long personas.
        out[role.strip()] = text[:1500]

    if not out:
        raise RuntimeError("LLM returned 0 valid SOULs (all empty / non-string)")
    return out


# ===== Step normalisation =====


def _step_to_dict(step: StepLike) -> dict[str, Any]:
    """Coerce a PlanStep / dict to a uniform dict shape.

    Routing's contract is a plain dict (`agent_role`, `target_profiles`,
    `required_capabilities`). The chatbox hands us a Pydantic
    `PlanStep` whose field names line up but whose access is attribute
    style. We try attribute access first (PlanStep / dataclass),
    fall back to item access (dict), and as a last resort call
    `model_dump()` / `dict()` for any other model.

    Always returns a fresh dict so callers can mutate without
    surprising the input.
    """
    if isinstance(step, dict):
        return dict(step)
    if hasattr(step, "model_dump"):
        try:
            return dict(step.model_dump())
        except Exception:
            pass
    if hasattr(step, "dict") and callable(step.dict):
        try:
            return dict(step.dict())  # Pydantic v1
        except Exception:
            pass
    # Fallback: attribute access for each known key
    out: dict[str, Any] = {}
    for key in (
        "agent_role",
        "target_profiles",
        "required_capabilities",
        "required_capability",
        "default_soul",
        "depends_on",
        "feedback_to",
        "params_template",
        "output_path",
        "name",
        "action",
    ):
        if hasattr(step, key):
            out[key] = getattr(step, key)
    return out


def _step_default_soul(step_dict: dict[str, Any]) -> str:
    """Best-effort `default_soul` field on a step dict.

    Accepts either the top-level `default_soul` (v3.9.0 addition) or
    a `params_template["default_soul"]` fallback for forward-compat
    with the chatbox plan-editor's serialised form.

    Returns an empty string if no default is available â€” callers
    must fall back to `_generic_role_template`.
    """
    direct = step_dict.get("default_soul")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    params = step_dict.get("params_template") or {}
    if isinstance(params, dict):
        nested = params.get("default_soul")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


# ===== DB-backed helpers =====


async def _ensure_soul_preset(
    project_id: str,
    step_dict: dict[str, Any],
    profile: dict[str, Any],
    db: Database,
) -> dict[str, Any]:
    """Return the project's SOUL preset for the step's role, creating one if missing.

    The preset binds (project, profile) â€” workflow steps that name the
    same role but run on different agents in the same project will get
    one preset each. The `project_soul_presets` table has
    `UNIQUE (project_id, profile_id)`, so a re-insert for the same
    (project, profile) is impossible and we use the existing row.

    The content seed order (first non-empty wins):
      1. `step.default_soul` (the workflow author's persona)
      2. `_generic_role_template(role_name)` (last-resort fallback)

    Args:
        project_id: the project this step belongs to.
        step_dict: the step as a dict (normalised upstream).
        profile: the resolved `agent_profiles` row.
        db: the orchestrator's Database connection.

    Returns:
        The project_soul_presets row (dict), whether pre-existing or
        newly inserted.
    """
    role = step_dict.get("agent_role") or ""
    existing = await get_soul_preset_by_role(db, project_id, role)
    if existing:
        return existing

    # Auto-populate. Content priority: workflow default â†’ generic.
    default_soul = _step_default_soul(step_dict)
    content = default_soul or _generic_role_template(role)
    preset_id = str(uuid.uuid4())
    await db.insert(
        "project_soul_presets",
        {
            "id": preset_id,
            "project_id": project_id,
            "profile_id": profile["id"],
            "role_name": role,
            "content": content,
            # Persist the workflow default alongside so a future
            # `update_preset` (Phase 2+) can re-seed content from it.
            "default_soul": default_soul or None,
        },
    )
    row = await db.fetchone(
        "SELECT * FROM project_soul_presets WHERE id = ?", (preset_id,)
    )
    if not row:
        # Should be impossible â€” we just inserted. Treat as a soft
        # failure so the caller sees a meaningful error instead of
        # NoneType downstream.
        raise SoulApplyError(
            f"failed to read back auto-populated preset {preset_id}",
            cfg_id="",
            error_msg="insert succeeded but SELECT returned no row",
        )
    return row


async def _submit_soul_to_profile(
    profile_id: str,
    soul_md: str,
    db: Database,
) -> str:
    """Insert a profile_configs row for `soul.md` and return its id.

    Idempotent: if a row with the same `(profile_id, file_path,
    desired_sha256)` already exists in any status (pending, applying,
    applied, failed), we reuse it and skip the insert. This makes
    repeated dispatches with the same SOUL a no-op â€” the wrapper
    will re-claim the existing row, see the same content, and ack
    immediately. We do NOT force a re-apply; freshness is governed
    by `touch_soul_preset` and the project_soul_presets cache.

    A previously-failed row with the same content is reused too: the
    operator can either edit the preset to a different content (which
    will hash differently and trigger a fresh insert) or delete the
    failed row manually.

    Args:
        profile_id: the agent_profiles row to write SOUL.md on.
        soul_md: the full SOUL.md body (header + content).
        db: the orchestrator's Database connection.

    Returns:
        The `profile_configs.id` of the existing or newly inserted row.
    """
    file_path = "soul.md"
    sha = _sha256(soul_md)
    existing = await db.fetchone(
        "SELECT id FROM profile_configs "
        "WHERE profile_id = ? AND file_path = ? AND desired_sha256 = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (profile_id, file_path, sha),
    )
    if existing:
        return existing["id"]

    cfg_id = str(uuid.uuid4())
    await db.insert(
        "profile_configs",
        {
            "id": cfg_id,
            "profile_id": profile_id,
            "file_path": file_path,
            "desired_sha256": sha,
            "desired_content": soul_md,
            "status": "pending",
        },
    )
    return cfg_id


async def _wait_for_soul_applied(
    cfg_id: str,
    db: Database,
    timeout_s: float = 30.0,
    poll_interval_s: float = 0.2,
) -> bool:
    """Poll the profile_configs row until the wrapper acks.

    The wrapper's daemon loop calls
    `POST /api/agents/{id}/profiles/{name}/configs/pending` (atomic
    claim â†’ status='applying'), writes the file, then
    `POST /configs/{id}/ack` (status='applied' or 'failed'). We poll
    the row every 200ms â€” fast enough to feel snappy on a healthy
    host (typical 1-3s), slow enough to not hammer SQLite.

    We return `bool` rather than raising because the dispatch step
    needs to read the row's `error` field to build a useful
    `SoulApplyError` message. The caller is responsible for raising.

    Args:
        cfg_id: the profile_configs row to watch.
        db: the orchestrator's Database connection.
        timeout_s: max seconds to wait before giving up. Default 30s
            (was 10s in v3.10.0; bumped 2026-08-02 after observing the
            real-world ack latency of 8-12s on win-local-1 â€” the 10s
            ceiling left no headroom for slow host I/O or the
            skills-sync thread blocking the ack POST). The supervisor
            reaper still reaps anything stuck in 'applying' after 60s
            (3x safety margin), so a true wrapper hang is still
            caught and the profile is freed.
        poll_interval_s: seconds between polls. Default 200ms.

    Returns:
        True if the row reached `status='applied'`. False if the row
        reached `status='failed'` or the timeout elapsed (caller
        should fetch the row's `error` field for the message).
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        row = await db.fetchone(
            "SELECT status, error FROM profile_configs WHERE id = ?",
            (cfg_id,),
        )
        if row is None:
            # Row disappeared (manual cleanup between insert and
            # poll). Treat as a soft failure.
            return False
        status = row.get("status")
        if status == "applied":
            return True
        if status == "failed":
            return False
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            return False
        await asyncio.sleep(min(poll_interval_s, max(0.0, deadline - now)))


async def _create_dispatched_task(
    project_id: str,
    step_dict: dict[str, Any],
    profile: dict[str, Any],
    db: Database,
) -> dict[str, Any]:
    """Create the task row for the dispatched step.

    Mirrors the schema used by `api.tasks.create_task` (status:
    'pending', assigned_profile_id set, depends_on from step). The
    supervisor's existing loop picks up pending tasks, so no extra
    integration is needed.

    The `required_capability` is propagated from the step so the
    supervisor's capability-mismatch check fires the same way it
    does for tasks created via the HTTP endpoint. Routing uses the
    plural `required_capabilities` (list) for its match algorithm;
    the task row keeps the legacy singular `required_capability`
    column for backward compat with the supervisor.

    Args:
        project_id: the project the task belongs to.
        step_dict: the normalised step dict.
        profile: the resolved profile row (used for assigned_profile_id).
        db: the orchestrator's Database connection.

    Returns:
        The newly inserted tasks row (dict).
    """
    step_name = step_dict.get("name") or ""
    task_id = str(uuid.uuid4())
    # v3.12.1: archive older same-name live tasks (skip 'running'
    # to avoid disrupting in-flight wrappers). The supervisor's
    # v3.12.1 dedupe (NOT EXISTS subquery in _find_ready_tasks) is
    # a safety net; this archive is the primary fix so the DB
    # stays clean and operators see the dedupe state directly.
    # Repro on proj-29b2990d (2026-08-03): t-8c7634e3 (old
    # check-total from apply-workflow) + 0407f925-... (new
    # check-total from SOUL dispatch) both got dispatched, ~2x
    # LLM cost per loopback iteration.
    #
    # v3.12.1 hardening: wrap archive + insert in a single
    # `db.transaction()` (BEGIN IMMEDIATE … COMMIT) so two
    # concurrent dispatchers for the same `(project_id, name)` pair
    # can't both pass the "no live duplicate" check before either
    # commits. Without the transaction, the archive and the insert
    # are separate statements; with SQLite's default DEFERRED
    # transactions, the read + write don't acquire the write lock
    # upfront, and the second dispatcher can race through the same
    # `fetchall` window. BEGIN IMMEDIATE acquires the write lock at
    # block entry, so the second dispatcher blocks on `BEGIN` until
    # the first commits (or rolls back). Net effect: even if the
    # archive path had a bug, the second dispatcher can't sneak a
    # duplicate row in.
    async with db.transaction():
        if step_name:
            archived = await db.fetchall(
                "SELECT id, status FROM tasks "
                "WHERE project_id = ? AND name = ? AND archived = 0 "
                "AND status IN ('pending', 'dispatched', 'assigned', 'failed', 'skipped')",
                (project_id, step_name),
            )
            if archived:
                ids = [r["id"] for r in archived]
                placeholders = ",".join("?" for _ in ids)
                await db.execute(
                    "UPDATE tasks SET archived = 1, updated_at = ? "
                    f"WHERE id IN ({placeholders})",
                    [_now_inner(), *ids],
                )
                for old in archived:
                    await audit_log(
                        db, "task.archived_on_soul_dispatch",
                        actor="supervisor",
                        project_id=project_id,
                        task_id=old["id"],
                        payload={
                            "name": step_name,
                            "old_status": old["status"],
                            "reason": "soul_dispatch_replaced",
                        },
                    )

        # Pydantic v2 keeps `depends_on` / `feedback_to` as list[str];
        # JSON-encode for the TEXT column to keep parity with
        # `create_task`. `_jsonify` in db.insert handles dicts/lists,
        # but we pre-encode here so the round-trip is exact.
        depends_json = json.dumps(list(step_dict.get("depends_on") or []))
        feedback_json = json.dumps(list(step_dict.get("feedback_to") or []))
        # Map plural `required_capabilities` â†’ singular
        # `required_capability`. Use the first entry (matching how the
        # chatbox plan editor currently emits single-capability steps).
        req_caps = step_dict.get("required_capabilities") or []
        if isinstance(req_caps, list) and req_caps:
            required_capability = req_caps[0]
        else:
            # Fall back to the legacy singular field if present.
            required_capability = step_dict.get("required_capability") or None
        await db.insert(
            "tasks",
            {
                "id": task_id,
                "project_id": project_id,
                "name": step_dict.get("name") or "",
                "agent_role": step_dict.get("agent_role") or "",
                # v3.9.0: also set `assigned_agent_id` so the supervisor's
                # per-agent cap check (`COUNT(*) WHERE assigned_agent_id = ?
                # AND status IN ('assigned','running')`) sees this new task
                # on the next tick. Without this, the cap count is off by
                # one for Round-3 dispatched tasks and the agent can be
                # over-committed. The `agent_id` column on the profile row
                # is the parent agent (set by the routing engine's JOIN
                # in `_list_online_profiles` / `_get_profile_row`).
                "assigned_profile_id": profile["id"],
                "assigned_agent_id": profile.get("agent_id"),
                "depends_on": depends_json,
                "feedback_to": feedback_json,
                "status": "pending",
                "action": step_dict.get("action") or "do_step",
                "required_capability": required_capability,
                "output_path": step_dict.get("output_path") or None,
                # params_template is the v3.5.x way to carry per-step
                # variables; serialise as JSON so the supervisor sees the
                # same shape it would from a POST /tasks call.
                "params": json.dumps(step_dict.get("params_template") or {}),
            },
        )
        # v3.12.1 follow-up #5: record the dispatch event inside the
        # same transaction as the task insert, so the row in
        # task_dispatch and the row in tasks either both commit or
        # both roll back. Cheap insert (one row, no joins); doesn't
        # widen the transaction's critical section meaningfully.
        await record_dispatch(
            db,
            project_id=project_id,
            task_id=task_id,
            dispatch_path="soul_dispatch",
            actor="supervisor",
        )

    # Read-back happens OUTSIDE the transaction — no longer need
    # the write lock to fetch a single row, and we want other
    # dispatchers to be able to proceed as soon as we COMMIT.
    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not row:
        raise SoulApplyError(
            f"task {task_id} missing after insert",
            cfg_id="",
            error_msg="insert succeeded but SELECT returned no row",
        )
    return row


# ===== Public entry point =====


async def dispatch_step(
    project_id: str,
    step: StepLike,
    db: Database,
) -> dict[str, Any]:
    """Resolve profile, apply SOUL, create the task.

    The single entry point used by the dispatch path (Round 3 will
    wire this into `api.projects.dispatch_step` and the chatbox
    "Run plan" flow). The end-to-end steps are:

      1. `resolve_role_to_profile` picks the best `agent_profiles`
         row for the step's role + project.
      2. `_ensure_soul_preset` returns the matching
         `project_soul_presets` row, auto-populating it on first
         dispatch for a (project, role) pair.
      3. `_compose_soul_md` renders the standard header + the
         preset's content.
      4. `_submit_soul_to_profile` writes a `profile_configs` row
         for `soul.md` (idempotent on identical content).
      5. `_wait_for_soul_applied` polls the row's status until the
         wrapper acks (10s timeout).
      6. `touch_soul_preset` records the apply timestamp.
      7. `_create_dispatched_task` inserts a pending task row
         assigned to the resolved profile; the supervisor picks
         it up from there.

    Args:
        project_id: the project this step belongs to.
        step: a PlanStep (Pydantic) or a dict with the same fields.
        db: the orchestrator's Database connection.

    Returns:
        The newly created `tasks` row (dict).

    Raises:
        SoulApplyError: if the SOUL apply times out, the wrapper
            reports failure, or the project preset cannot be
            materialised.
    """
    step_dict = _step_to_dict(step)
    role = step_dict.get("agent_role") or ""

    # 1. Routing â€” db is the last arg in the routing contract
    profile = await resolve_role_to_profile(project_id, step_dict, db)

    # 2. Preset (auto-populates on first dispatch)
    preset = await _ensure_soul_preset(project_id, step_dict, profile, db)

    # 3. Compose SOUL â€” content priority: preset.content â†’ preset
    #    .default_soul â†’ step.default_soul â†’ generic role template.
    raw_content = (
        (preset.get("content") or "").strip()
        or (preset.get("default_soul") or "").strip()
        or _step_default_soul(step_dict)
        or _generic_role_template(role)
    )
    soul_md = _compose_soul_md(
        role_name=preset["role_name"],
        project_id=project_id,
        content=raw_content,
    )

    # 4. Submit to profile_configs (idempotent on same content)
    cfg_id = await _submit_soul_to_profile(profile["id"], soul_md, db)

    # 5. Wait for wrapper to claim + ack
    applied = await _wait_for_soul_applied(cfg_id, db, timeout_s=30.0)
    if not applied:
        # Fetch the wrapper's error message (or fall back to
        # timeout). Use a fresh fetch rather than caching the row
        # in the poll loop so the error string is the latest.
        row = await db.fetchone(
            "SELECT status, error FROM profile_configs WHERE id = ?",
            (cfg_id,),
        )
        status = (row or {}).get("status") or "unknown"
        err = (row or {}).get("error") or (
            f"SOUL apply timed out (status={status})"
        )
        raise SoulApplyError(
            f"SOUL apply failed for profile {profile['id']} "
            f"(role={role}, cfg_id={cfg_id}): {err}",
            cfg_id=cfg_id,
            error_msg=err,
        )

    # 6. Record the apply. The mtime is the timestamp we composed
    #    into the SOUL.md header â€” good enough as a coarse "this is
    #    what we wrote, when" marker. The wrapper doesn't currently
    #    report an authoritative SOUL.md mtime on the heartbeat, so
    #    using our own header timestamp is the only consistent value
    #    available. (Future v3.10+: switch to wrapper-reported mtime
    #    once that field is added to the heartbeat payload.)
    await touch_soul_preset(db, preset["id"], applied_mtime=_now_iso())

    # 7. Create the task â€” supervisor picks it up from here
    return await _create_dispatched_task(project_id, step_dict, profile, db)

