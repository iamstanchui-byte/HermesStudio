# coding: utf-8
"""Dashboard pages (per REVIEW.md §7).

Pages:
- GET /                  -> redirect to /agents
- GET /agents            -> Agents page (with expandable profile sub-cards)
- GET /tasks             -> Tasks page (filterable)
- GET /projects          -> Projects list
- GET /projects/{id}     -> Project detail (plan + tasks)
- GET /schedules         -> Recurring schedules (CRUD + template dropdown)
- GET /history           -> History (audit log)

Live updates via 5s polling (vanilla JS setInterval + fetch).
"""
from __future__ import annotations

import json
from datetime import timedelta, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from hermes_orch.utils import now_iso as _now_iso, now_aware

router = APIRouter()

# Templates directory (relative to this file: src/hermes_orch/api/dashboard.py)
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
# auto_reload=True so template edits show up on the next request without
# a full server restart. Important during dev (the wrapper has a
# self-restart watchdog but the server is manually started, so this
# avoids a second manual step). Jinja2's FileSystemLoader checks the
# mtime of every template on every request, which is a few ms — fine
# for a local-LAN dashboard. Set to False in production to skip the
# stat calls.
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.auto_reload = True


def _format_tokens(n) -> str:
    """Compact human-friendly token count.

    Examples:
      0       -> "0"
      999     -> "999"
      1.2K    -> "1.2K"
      45.2K   -> "45.2K"
      12.45M  -> "12.45M"
      1.23B   -> "1.23B"

    Used by the new v3.0 dashboard pages (agents / token-usage)
    instead of duplicating this 4-line conditional in every template.
    """
    if n is None:
        return "0"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1_000:.1f}K"
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n / 1_000_000_000:.2f}B"


templates.env.filters["format_tokens"] = _format_tokens


def _llm_configured(cfg: dict[str, Any] | None) -> bool:
    if not cfg:
        return False
    llm = cfg.get("llm") or {}
    return bool((llm.get("api_key") or "").strip())


async def _base_context(request: Request, active_page: str) -> dict[str, Any]:
    """Common context for all dashboard templates (llm_configured + active_page).

    v3.4: also surfaces the current user (or None) so the topbar can
    show a user pill vs a "Sign in" link without each page having to
    re-fetch /api/auth/me. The middleware already validated the
    cookie before this renders, so `current_user` is always non-None
    on pages; the None branch exists for completeness (and for any
    page that the middleware later decides to allowlist).
    """
    # Imported lazily to avoid a circular import (auth.cookie depends
    # on core.audit, which is independent, but the page route handlers
    # only need it here, not at module import time).
    from hermes_orch.auth.cookie import current_user as _current_user

    # Cached on request.state so the per-page render doesn't re-query
    # if a page route also calls /api/auth/me.
    cached = getattr(request.state, "current_user_ctx", None)
    if cached is None and hasattr(request.state, "user_id"):
        cached = await _current_user(request, user_id=request.state.user_id)
        request.state.current_user_ctx = cached
    elif cached is None:
        cached = await _current_user(request)
        request.state.current_user_ctx = cached

    return {
        "active_page": active_page,
        "llm_configured": _llm_configured(getattr(request.app.state, "config", None)),
        "current_user_ctx": cached,
    }


def _project_storage_view(cfg: dict) -> dict:
    """Compact view of the project storage config for templates."""
    from pathlib import Path
    proj = cfg.get("projects") or {}
    root = (proj.get("storage_root") or "").strip()
    is_default = root in ("./projects", "projects", "")
    project_count = -1
    exists = False
    writable = False
    if root:
        p = Path(root)
        exists = p.exists() and p.is_dir()
        if exists:
            try:
                project_count = sum(
                    1 for x in p.iterdir() if x.is_dir() and not x.name.startswith(".")
                )
                # Test write access with a tiny file
                test_file = p / ".orch-write-test"
                try:
                    test_file.write_text("ok\n", encoding="utf-8")
                    test_file.unlink()
                    writable = True
                except Exception:
                    writable = False
            except Exception:
                pass
    return {
        "storage_root": root,
        "exists": exists,
        "writable": writable,
        "project_count": project_count,
        "is_default": is_default,
    }


# ===== Helpers =====


def _parse_json_fields(row: dict[str, Any], *fields: str) -> dict[str, Any]:
    """Parse JSON-encoded string fields into Python objects."""
    for col in fields:
        v = row.get(col)
        if isinstance(v, str):
            try:
                row[col] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                row[col] = {} if col != "depends_on" else []
    return row


def _compute_task_timing(task: dict[str, Any]) -> dict[str, Any]:
    """Derive start/end/duration for a task from the real DB columns.

    Reads `started_at` and `ended_at` directly — both set server-side
    on the actual lifecycle transitions:
    - started_at: set on /start (assigned → running)
    - ended_at:   set on /result, /cancel, /interrupt (any terminal)

    The previous implementation hacked duration from
    `updated_at - last_liveness_at`, which always gave 1-30s because
    both fields are written within 1-2s of task completion. For
    pre-migration tasks, started_at / ended_at may be NULL; we fall
    back to last_liveness_at / updated_at for those rows (best-effort
    until the one-shot backfill script is run).

    Returns:
        started_at: ISO timestamp or None
        completed_at: ISO timestamp (== ended_at, kept for template compat)
        duration_seconds: float or None
    """
    from datetime import datetime
    TERMINAL = {"completed", "failed", "cancelled", "interrupted", "skipped"}
    result: dict[str, Any] = {"started_at": None, "completed_at": None, "duration_seconds": None}

    def _parse(s: str | None):
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    # Prefer the real columns; fall back to last_liveness_at/updated_at
    # for tasks created before the schema migration.
    started = _parse(task.get("started_at")) or _parse(task.get("last_liveness_at"))
    ended = _parse(task.get("ended_at")) or (
        _parse(task.get("updated_at"))
        if task.get("status") in TERMINAL
        else None
    )

    if started:
        result["started_at"] = started.isoformat()
    if ended:
        result["completed_at"] = ended.isoformat()
    if started and ended:
        result["duration_seconds"] = max(0.0, (ended - started).total_seconds())
    elif started and task.get("status") == "running":
        # Still running — duration so far
        now = datetime.now(started.tzinfo) if started.tzinfo else datetime.now()
        result["duration_seconds"] = max(0, (now - started).total_seconds())

    return result


# Files the supervisor treats as ephemeral control signals (read +
# unlink). These get registered as artifacts in the DB by the wrapper's
# auto-upload path, but the file itself is gone from disk by the time
# the user looks at the page. The template uses this set to render
# a "(deleted)" badge instead of a broken link. Keep in sync with
# agent_cli.py:1420 (where the wrapper SKIPS auto-uploading these).
EPHEMERAL_CONTROL_FILES = frozenset({
    "decision.md",
    "decisions.md",
    "status.md",
    "plan.md",
})


def _annotate_artifact_exists(
    task: dict[str, Any], projects_root: "Path | None"
) -> None:
    """For each artifact in `task.result.artifacts`, set `exists: bool`
    and `is_ephemeral: bool` flags so the template can render links /
    "deleted" badges without re-doing the disk check. Mutates `task`
    in place. Safe to call with projects_root=None (skips disk check
    and falls back to False).
    """
    result = task.get("result") or {}
    arts = result.get("artifacts") or []
    if not arts:
        return
    for a in arts:
        name = a.get("name") or a.get("path") or ""
        is_ephemeral = name in EPHEMERAL_CONTROL_FILES
        a["is_ephemeral"] = is_ephemeral
        if projects_root is None:
            a["exists"] = False
            continue
        # Resolve the on-disk path. Server-side convention: project
        # files live at <projects_root>/<project_id>/<name>. The
        # wrapper uploads via PUT /api/projects/{pid}/files/<rel>
        # which writes to exactly that location.
        try:
            pdir = projects_root / task["project_id"]
            fpath = pdir / name
            a["exists"] = fpath.is_file()
        except (OSError, ValueError):
            a["exists"] = False


def _format_duration(seconds: float | None) -> str:
    """Format a duration in seconds as a human-readable string."""
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 1:
        return "<1s"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


async def _load_agents(db: Any) -> list[dict[str, Any]]:
    rows = await db.fetchall("SELECT * FROM agents ORDER BY created_at DESC")
    agents = []
    for row in rows:
        profiles = await db.fetchall(
            "SELECT * FROM agent_profiles WHERE agent_id = ? ORDER BY name",
            (row["id"],),
        )
        profile_list = [dict(p) for p in profiles]
        # Augment each profile with its current skills (latest version per name).
        # Also parse the `capabilities` JSON column to a dict — the DB stores
        # it as a string, but the template (and the Pydantic model in the API
        # path) expects a dict. Without this parse, the template chokes with
        # "str object has no attribute 'items'" when a profile has any
        # capabilities set. Phase 4 (smart dispatch).
        for p in profile_list:
            # v3.9.0: split the two `skills` concepts cleanly.
            # - `p["skills"]`       = file-based (list of dicts loaded from
            #                         <profile>/skills/*/SKILL.md). This is
            #                         the existing UI semantic — the agents
            #                         page's "Skills" tab renders this list.
            # - `p["capability_tags"]` = declared at registration (the
            #                         new `agent_profiles.skills` JSON
            #                         column parsed to a list of strings).
            #                         Surfaced in the Overview tab as a
            #                         "capabilities" badge.
            # The DB column is still named `skills` (no migration to
            # rename); we parse it once here into a list and stash it
            # under `capability_tags` so the rest of the template +
            # the JSON API response don't confuse the two.
            skills_raw = p.get("skills")
            capability_tags: list[str] = []
            if skills_raw:
                try:
                    parsed = json.loads(skills_raw) if isinstance(skills_raw, str) else skills_raw
                    if isinstance(parsed, list):
                        capability_tags = [str(s) for s in parsed if isinstance(s, (str, int, float))]
                except (json.JSONDecodeError, TypeError):
                    pass
            p["capability_tags"] = capability_tags
            # Now overwrite `p["skills"]` with the file-based list.
            # The DB column value (a JSON string) is gone from the
            # template context — the template only ever sees the
            # file-based list under `p["skills"]`.
            p["skills"] = await _load_profile_skills(db, p["id"])
            # LLM model fields (plain TEXT, no JSON parsing — just pass through).
            # If all three are NULL, the wrapper hasn't reported yet; the
            # template renders a grey fallback badge with a tooltip.
            p["llm_model_default"] = p.get("llm_model_default")
            p["llm_model_base_url"] = p.get("llm_model_base_url")
            p["llm_model_provider"] = p.get("llm_model_provider")
            # MCP server list — JSON array of {name, enabled}. Same defensive
            # parse as _row_to_profile (pattern #9 — both paths).
            mcp_raw = p.get("mcp_servers")
            mcps: list[dict] = []
            if mcp_raw:
                try:
                    parsed = json.loads(mcp_raw) if isinstance(mcp_raw, str) else mcp_raw
                    if isinstance(parsed, list):
                        for m in parsed:
                            if isinstance(m, dict) and "name" in m:
                                mcps.append({
                                    "name": str(m["name"]),
                                    "enabled": bool(m.get("enabled", True)),
                                })
                except (json.JSONDecodeError, TypeError):
                    pass
            p["mcp_servers"] = mcps
            # Storage references (user-stated 2026-07-22). Same defensive
            # parse as _row_to_profile. Pattern #9 reminder: both API
            # and HTML page paths must parse the same JSON column.
            # Includes `name` (optional alias, 2026-07-22) so the
            # agents.html template can show it as a short tag in front
            # of the kind chip.
            sref_raw = p.get("storage_refs")
            srefs: list[dict] = []
            if sref_raw:
                try:
                    parsed = json.loads(sref_raw) if isinstance(sref_raw, str) else sref_raw
                    if isinstance(parsed, list):
                        for s in parsed:
                            if isinstance(s, dict) and "kind" in s and "ref" in s:
                                srefs.append({
                                    "name": str(s.get("name", "")).strip() or None,
                                    "kind": str(s["kind"]),
                                    "ref": str(s["ref"]),
                                    "description": str(s.get("description", "")),
                                })
                except (json.JSONDecodeError, TypeError):
                    pass
            p["storage_refs"] = srefs
            caps_raw = p.get("capabilities")
            caps: dict[str, bool] = {}
            if caps_raw:
                try:
                    parsed = json.loads(caps_raw) if isinstance(caps_raw, str) else caps_raw
                    if isinstance(parsed, dict):
                        caps = {str(k): bool(v) for k, v in parsed.items()}
                except (json.JSONDecodeError, TypeError):
                    pass
            p["capabilities"] = caps
        agents.append(
            {
                "id": row["id"],
                "ip": row.get("ip"),
                "os_type": row.get("os_type"),
                "status": row["status"],
                "last_heartbeat_at": row.get("last_heartbeat_at"),
                "created_at": row.get("created_at"),
                # v3.6.0: per-agent concurrent task cap. Surfaced to
                # the dashboard so the operator can see at a glance
                # how parallel the wrapper will run.
                "max_concurrent_tasks": int(row.get("max_concurrent_tasks") or 1),
                "profiles": profile_list,
            }
        )
    return agents


async def _load_profile_skills(db: Any, profile_id: str) -> list[dict[str, Any]]:
    """Return latest version of each skill for a profile.

    Sort order: flat skills first (alphabetical), then subfolder
    skills (alphabetical by category then name). The CASE in the
    ORDER BY groups flat (0) and subfolder (1) separately so
    subfolder skills don't appear at the top of the list just
    because "apple/..." sorts before "browser-automation"
    alphabetically. The file_path ASC tiebreak keeps each group
    in canonical name order within itself.

    The same sort is applied in agents.py:list_skills; keep them
    in sync so the API and the HTML page show the same order.
    User requested this on 2026-07-24 — the previous ORDER BY
    file_path ASC mixed flat and subfolder alphabetically which
    made the list hard to scan when there are 80+ skills.

    A skill is `profile_configs.file_path` of the form `skills/<name>/SKILL.md`
    (hermes 0.17+ folder layout; flat `skills/<name>.md` was dropped in
    commit 5e69bdb). We only keep the newest row per file_path (created_at
    DESC), and we treat empty applied content as a deletion — those entries
    are filtered out so the dashboard shows what's actually on the host.

    Performance: we don't SELECT desired_content (templates only show
    name/status/size in the list; content is loaded on demand by
    /api/agents/.../skills/<name>). With 86 skills × 10KB each, this
    saves ~1MB of HTML per page render. 2026-07-25 fix after the
    runaway-skill-upload loop made the page take 4.5s.
    """
    rows = await db.fetchall(
        "SELECT id, profile_id, file_path, status, error, created_at, applied_at, "
        "LENGTH(desired_content) AS size_bytes "
        "FROM profile_configs WHERE profile_id = ? "
        "AND file_path LIKE 'skills/%/SKILL.md' "
        "ORDER BY (CASE WHEN file_path LIKE 'skills/%/%/SKILL.md' THEN 1 ELSE 0 END), "
        "file_path ASC, created_at DESC",
        (profile_id,),
    )
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["file_path"] in seen:
            continue
        seen.add(r["file_path"])
        # Treat applied-with-empty-content as deleted; skip from list.
        # The empty-content check is cheap even without selecting the
        # full text: size_bytes=0 means empty (an applied row with
        # size 0 = a deletion marker).
        if r["status"] == "applied" and r["size_bytes"] == 0:
            continue
        # Path is skills/<name>/SKILL.md — strip both ends
        name = r["file_path"][len("skills/"):-len("/SKILL.md")]
        out.append({
            "name": name,
            "file_path": r["file_path"],
            "status": r["status"],
            "size": r["size_bytes"],
            "created_at": r.get("created_at"),
            "applied_at": r.get("applied_at"),
            "error": r.get("error"),
            "content": "",  # not loaded in list; fetch via /skills/{name} on demand
        })
    return out


async def _load_tasks(db: Any) -> list[dict[str, Any]]:
    rows = await db.fetchall("SELECT * FROM tasks ORDER BY created_at DESC")
    return [_parse_json_fields(dict(r), "depends_on", "params", "result") for r in rows]


# ===== Routes =====


@router.get("/", response_class=HTMLResponse)
async def root(request: Request) -> HTMLResponse:
    """Landing page.

    v1.0.1 (§3.2): if the current user's onboarding is incomplete
    (and not skipped), render the 4-step checklist
    (`templates/onboarding.html`) instead of redirecting to /agents.
    This is the new-user activation path: someone who just installed
    the server lands on a single page that walks them through the
    4 steps (password → LLM → agent → first task) rather than an
    empty /agents page with no idea what to do next.

    Unauthenticated users get redirected to /login (the existing
    auth middleware handles that — the request just continues here
    with current_user_ctx=None).
    """
    from hermes_orch.core.onboarding import (
        is_checklist_complete,
        should_show_checklist,
    )
    from hermes_orch.auth.cookie import current_user
    from fastapi.responses import RedirectResponse as _Redirect

    user = await current_user(request)
    if not user:
        return _Redirect(url="/login", status_code=302)

    # Read the user's onboarding state. Default to `{}` for a fresh
    # user who somehow has no state row (shouldn't happen — the
    # column has a NOT NULL DEFAULT '{}' constraint).
    state = await _get_user_onboarding_state(request, user["id"])
    if should_show_checklist(state):
        return templates.TemplateResponse(
            request=request,
            name="onboarding.html",
            context={
                **(await _base_context(request, "onboarding")),
                "onboarding_state": state,
                "is_complete": is_checklist_complete(state),
            },
        )
    return _Redirect(url="/agents", status_code=302)


async def _get_user_onboarding_state(request: Request, user_id: str) -> dict[str, Any]:
    """Read + parse the user's onboarding_state column.

    Helper used by `GET /` and the settings page banner. The parse is
    defensive: malformed JSON or a missing row both yield the empty
    state (which `should_show_checklist` treats as a fresh user — the
    correct response to "we don't know what this user's state is").
    """
    from hermes_orch.core.onboarding import empty_state, parse_state
    row = await request.app.state.db.fetchone(
        "SELECT onboarding_state FROM users WHERE id = ?", (user_id,)
    )
    if not row:
        return empty_state()
    return parse_state(row["onboarding_state"] or "{}")


async def _load_agents_overview(db: Any) -> dict[str, Any]:
    """Aggregate stats for the /agents overview dashboard.

    Computes:
      - total_agents, online (heartbeat within 90s)
      - idle / busy profile counts (only for online agents)
      - windows_online / linux_online
      - profiles_total
      - subagents_online (= online profile count)

    The 90s "online" cutoff matches the dashboard's dot-color logic
    (status=verified + last_heartbeat >= 90s ago -> green).

    We use TWO queries instead of one big LEFT JOIN aggregate, because
    the JOIN would inflate agent-level counts by the profile count
    (e.g. an agent with 2 profiles would be counted twice in
    `SUM(CASE WHEN a.status='verified' ...)`). Profile-level counts
    (idle/busy/subagents) DO want the join.

    Donut proportions (idle / busy / offline) are derived client-side
    in agents.html's renderDonut() — we send the raw counts only.
    """
    from datetime import timedelta
    now = now_aware()
    online_cutoff = (now - timedelta(seconds=90)).isoformat()
    # Agent-level: use the agents table directly (no join) to avoid
    # the per-profile-row inflation bug.
    agent_row = await db.fetchone(
        """
        SELECT
            COUNT(*) AS total_agents,
            SUM(CASE WHEN status='verified' AND last_heartbeat_at >= ? THEN 1 ELSE 0 END) AS online,
            SUM(CASE WHEN os_type='windows' AND status='verified' AND last_heartbeat_at >= ? THEN 1 ELSE 0 END) AS windows_online,
            SUM(CASE WHEN os_type='linux' AND status='verified' AND last_heartbeat_at >= ? THEN 1 ELSE 0 END) AS linux_online
        FROM agents
        """,
        (online_cutoff, online_cutoff, online_cutoff),
    )
    # Profile-level: only count profiles whose agent is currently
    # online. The join is correct here because we want per-profile
    # counts (an agent with 2 idle profiles should show idle=2).
    profile_row = await db.fetchone(
        """
        SELECT
            COUNT(*) AS profiles_total,
            SUM(CASE WHEN a.last_heartbeat_at >= ? AND p.status='idle' THEN 1 ELSE 0 END) AS idle,
            SUM(CASE WHEN a.last_heartbeat_at >= ? AND p.status='busy' THEN 1 ELSE 0 END) AS busy,
            SUM(CASE WHEN a.last_heartbeat_at >= ? THEN 1 ELSE 0 END) AS subagents_online
        FROM agent_profiles p
        JOIN agents a ON a.id = p.agent_id
        """,
        (online_cutoff, online_cutoff, online_cutoff),
    )
    return {
        "total_agents": int(agent_row["total_agents"] or 0) if agent_row else 0,
        "online": int(agent_row["online"] or 0) if agent_row else 0,
        "idle": int(profile_row["idle"] or 0) if profile_row else 0,
        "busy": int(profile_row["busy"] or 0) if profile_row else 0,
        "windows_online": int(agent_row["windows_online"] or 0) if agent_row else 0,
        "linux_online": int(agent_row["linux_online"] or 0) if agent_row else 0,
        "profiles_total": int(profile_row["profiles_total"] or 0) if profile_row else 0,
        "subagents_online": int(profile_row["subagents_online"] or 0) if profile_row else 0,
    }


async def _load_token_usage_overview(db: Any) -> dict[str, Any]:
    """Aggregate LLM token usage for the agents page token section.

    Returns:
      - totals: {4h, 24h, 7d} -> {total, prompt, completion, calls}
      - by_model: [{model, total_7d, calls_7d}, ...]  (top 10)
      - by_agent: [{agent_id, profile, total_7d, calls_7d}, ...]  (top 10)
      - by_project: [{project_id, name, total_7d, calls_7d}, ...]  (top 5)
      - top_tasks: [{task_id, project_id, role, model, total_7d, calls_7d}, ...]
        (top 5, sorted by 7d total)
      - sparkline: [{date, total}, ...]  (7 daily buckets, oldest first)

    All numbers are integers; missing data is represented as 0 or [].
    The template handles the empty case with "no data yet" copy.
    """
    now = now_aware()
    cutoffs = {
        "4h": (now - timedelta(hours=4)).isoformat(),
        "24h": (now - timedelta(hours=24)).isoformat(),
        "7d": (now - timedelta(days=7)).isoformat(),
    }
    out: dict[str, Any] = {
        "totals": {},
        "by_model": [],
        "by_agent": [],
        "by_project": [],
        "by_provider": [],
        "top_tasks": [],
        "sparkline": [],
        # v3.0: per-day breakdown with prompt/completion split, used by
        # the standalone Token Usage page for the stacked bar chart.
        # 7 entries, oldest first, e.g. [{date: "Jul 24", prompt: 1.2M,
        # completion: 0.5M, total: 1.7M, calls: 312}, ...]
        "daily_breakdown": [],
    }
    # Totals for each window.
    #
    # v3.12.8 (Phase 5) — cache-aware totals. The `total` field is what
    # the LLM provider reports as usage.total_tokens (= prompt + output,
    # EXCLUDING cache_read_input_tokens). The new `true_total` field
    # is the "tokens the model actually saw" = prompt + cache_read +
    # completion. Without this fix, the dashboard can show a single
    # task with `cache_read` > `total` (looks like a bug) and the
    # input/output percentages as % of API total (misleading — the
    # real input billed is prompt + cache_read, and the "true total"
    # the operator cares about includes cache).
    #
    # Both fields are kept so existing consumers (e.g. the project
    # page header that shows "X tokens" as a quick KPI) keep
    # working; the new template uses `true_total` for the headline
    # number. External JSON consumers can still read `total` (= API
    # total) and add `cache_read` to get the true number.
    for window, cutoff in cutoffs.items():
        row = await db.fetchone(
            "SELECT COALESCE(SUM(total_tokens),0) as total, "
            "COALESCE(SUM(prompt_tokens),0) as prompt, "
            "COALESCE(SUM(completion_tokens),0) as completion, "
            "COALESCE(SUM(cache_read_tokens),0) as cache_read, "  # v3.1.2
            "COUNT(*) as calls FROM token_usage WHERE created_at >= ?",
            (cutoff,),
        )
        prompt_v = int(row["prompt"]) if row else 0
        completion_v = int(row["completion"]) if row else 0
        cache_read_v = int(row["cache_read"]) if row else 0
        out["totals"][window] = {
            # Provider-reported total (= input + output, EXCLUDES cache).
            "total": int(row["total"]) if row else 0,
            "prompt": prompt_v,
            "completion": completion_v,
            "cache_read": cache_read_v,  # v3.1.2
            # v3.12.8: true total = prompt + cache_read + completion.
            # This is the "tokens the model actually saw" — the right
            # number to display in the headline KPI. Cache hit tokens
            # are billed at ~10% of fresh input but they ARE tokens
            # the provider processed, so they belong in the total.
            "true_total": prompt_v + cache_read_v + completion_v,
            "calls": int(row["calls"]) if row else 0,
        }
    cutoff_7d = cutoffs["7d"]
    # by_model (7d). v3.12.8: also fetch prompt / cache_read so we
    # can compute true_total per model (sorting by true_total, not
    # API total, so the ranking is "tokens the model actually
    # served" rather than "tokens the model charged for at full
    # rate").
    out["by_model"] = [
        {
            "model": r["model"],
            "total": int(r["total"] or 0),       # API total
            "prompt": int(r["prompt"] or 0),
            "cache_read": int(r["cache_read"] or 0),
            "completion": int(r["completion"] or 0),
            "true_total": (int(r["prompt"] or 0)
                           + int(r["cache_read"] or 0)
                           + int(r["completion"] or 0)),
            "calls": int(r["calls"] or 0),
        }
        for r in await db.fetchall(
            "SELECT model, "
            "SUM(total_tokens) as total, "
            "SUM(prompt_tokens) as prompt, "
            "SUM(cache_read_tokens) as cache_read, "
            "SUM(completion_tokens) as completion, "
            "COUNT(*) as calls "
            "FROM token_usage WHERE created_at >= ? GROUP BY model "
            "ORDER BY (prompt + cache_read + completion) DESC LIMIT 10",
            (cutoff_7d,),
        )
    ]
    # by_agent (7d) — group by agent_id, take top 10.
    # v3.12.8: same pattern — true_total + sort by true_total.
    out["by_agent"] = [
        {
            "agent_id": r["agent_id"] or "(orchestrator)",
            "total": int(r["total"] or 0),
            "prompt": int(r["prompt"] or 0),
            "cache_read": int(r["cache_read"] or 0),
            "completion": int(r["completion"] or 0),
            "true_total": (int(r["prompt"] or 0)
                           + int(r["cache_read"] or 0)
                           + int(r["completion"] or 0)),
            "calls": int(r["calls"] or 0),
        }
        for r in await db.fetchall(
            "SELECT agent_id, "
            "SUM(total_tokens) as total, "
            "SUM(prompt_tokens) as prompt, "
            "SUM(cache_read_tokens) as cache_read, "
            "SUM(completion_tokens) as completion, "
            "COUNT(*) as calls "
            "FROM token_usage WHERE created_at >= ? "
            "GROUP BY agent_id ORDER BY (prompt + cache_read + completion) DESC LIMIT 10",
            (cutoff_7d,),
        )
    ]
    # by_project (7d) — top 5
    by_proj_rows = await db.fetchall(
        "SELECT tu.project_id, COALESCE(p.name, '?') as name, "
        "SUM(tu.total_tokens) as total, "
        "SUM(tu.prompt_tokens) as prompt, "
        "SUM(tu.cache_read_tokens) as cache_read, "
        "SUM(tu.completion_tokens) as completion, "
        "COUNT(*) as calls "
        "FROM token_usage tu LEFT JOIN projects p ON p.id = tu.project_id "
        "WHERE tu.created_at >= ? AND tu.project_id IS NOT NULL "
        "GROUP BY tu.project_id ORDER BY (prompt + cache_read + completion) DESC LIMIT 5",
        (cutoff_7d,),
    )
    out["by_project"] = [
        {
            "project_id": r["project_id"],
            "name": r["name"],
            "total": int(r["total"] or 0),
            "prompt": int(r["prompt"] or 0),
            "cache_read": int(r["cache_read"] or 0),
            "completion": int(r["completion"] or 0),
            "true_total": (int(r["prompt"] or 0)
                           + int(r["cache_read"] or 0)
                           + int(r["completion"] or 0)),
            "calls": int(r["calls"] or 0),
        }
        for r in by_proj_rows
    ]
    # v3.0: by_provider (7d) — group by base_url. The base_url column
    # holds the LLM API endpoint (e.g. https://api.openai.com/v1, or a
    # local vLLM URL). We strip the scheme and trailing slash, then
    # keep the path so distinct endpoints on the same host
    # (e.g. api.example.com/anthropic vs api.example.com/v1, a
    # common pattern when the same proxy serves both Anthropic- and
    # OpenAI-compatible APIs) show as separate rows. We strip "www."
    # only — leaving the rest of the path intact so the admin can tell
    # "api.minimax.io/anthropic" apart from "api.minimax.io/v1" at
    # a glance. A NULL base_url becomes "(unknown)" so we still count
    # it rather than dropping on the floor.
    def _provider_label(base_url: str | None) -> str:
        if not base_url:
            return "(unknown)"
        s = str(base_url).strip()
        # Strip scheme (http://, https://, etc.)
        for scheme in ("https://", "http://", "wss://", "ws://"):
            if s.lower().startswith(scheme):
                s = s[len(scheme):]
                break
        # Strip trailing slash
        s = s.rstrip("/")
        # Strip "www." prefix only
        if s.lower().startswith("www."):
            s = s[4:]
        return s or "(unknown)"

    provider_rows = await db.fetchall(
        "SELECT base_url, "
        "SUM(total_tokens) as total, "
        "SUM(prompt_tokens) as prompt, "
        "SUM(cache_read_tokens) as cache_read, "
        "SUM(completion_tokens) as completion, "
        "COUNT(*) as calls "
        "FROM token_usage WHERE created_at >= ? "
        "GROUP BY base_url "
        "ORDER BY (prompt + cache_read + completion) DESC LIMIT 10",
        (cutoff_7d,),
    )
    # by_provider (7d). v3.12.8: also pull prompt / cache_read so we
    # can compute true_total per provider (sorting by true_total).
    out["by_provider"] = [
        {
            "provider": _provider_label(r["base_url"]),
            "total": int(r["total"] or 0),
            "prompt": int(r["prompt"] or 0),
            "cache_read": int(r["cache_read"] or 0),
            "completion": int(r["completion"] or 0),
            "true_total": (int(r["prompt"] or 0)
                           + int(r["cache_read"] or 0)
                           + int(r["completion"] or 0)),
            "calls": int(r["calls"] or 0),
        }
        for r in provider_rows
    ]
    # v3.0: per-day breakdown (7d) with prompt/completion split. We
    # bucket by created_at date and use the local-time string format
    # already in the DB. The LEFT of the date string ("YYYY-MM-DD")
    # gives us a sortable, comparable bucket key. 7 entries, oldest
    # first, with zero-filled gaps so the chart x-axis stays even.
    # Use datetime.timedelta / datetime.date (not `from ... import
    # timedelta`) to avoid the UnboundLocalError that hits when a
    # function-local `from X import Y` shadows an outer Y binding.
    import datetime as _dt
    today = now_aware().date()
    days = [today - _dt.timedelta(days=i) for i in range(6, -1, -1)]  # 7 days, oldest first
    day_rows = await db.fetchall(
        "SELECT SUBSTR(created_at, 1, 10) as day, "
        "COALESCE(SUM(prompt_tokens),0) as prompt, "
        "COALESCE(SUM(completion_tokens),0) as completion, "
        "COALESCE(SUM(total_tokens),0) as total, "
        "COALESCE(SUM(cache_read_tokens),0) as cache_read, "  # v3.1.2
        "COUNT(*) as calls "
        "FROM token_usage WHERE created_at >= ? "
        "GROUP BY day ORDER BY day ASC",
        (cutoff_7d,),
    )
    by_day = {r["day"]: r for r in day_rows}
    out["daily_breakdown"] = [
        {
            "date": d.isoformat(),
            "label": d.strftime("%b %d"),
            "prompt": int((by_day.get(d.isoformat()) or {}).get("prompt") or 0),
            "completion": int((by_day.get(d.isoformat()) or {}).get("completion") or 0),
            "cache_read": int((by_day.get(d.isoformat()) or {}).get("cache_read") or 0),  # v3.1.2
            "total": int((by_day.get(d.isoformat()) or {}).get("total") or 0),
            # v3.12.8: true_total per day. The stacked bar chart
            # already shows cache / input / output as 3 separate
            # bars (so the bar widths were already correct), but
            # the headline tooltip used `d.total` (= API total,
            # excludes cache) and made cache appear "extra". Now
            # true_total = prompt + cache_read + completion = sum
            # of all 3 bars in the chart, matching what the
            # operator sees.
            "true_total": (
                int((by_day.get(d.isoformat()) or {}).get("prompt") or 0)
                + int((by_day.get(d.isoformat()) or {}).get("cache_read") or 0)
                + int((by_day.get(d.isoformat()) or {}).get("completion") or 0)
            ),
            "calls": int((by_day.get(d.isoformat()) or {}).get("calls") or 0),
        }
        for d in days
    ]
    # top_tasks (7d) — top 5. v3.12.8: also pull prompt/cache_read/
    # completion so true_total can be computed; sort by true_total
    # so the ranking reflects "tokens the model actually served"
    # rather than the API-reported total (which excluded cache).
    out["top_tasks"] = [
        {
            "task_id": r["task_id"],
            "project_id": r["project_id"],
            "role": r["role"],
            "model": r["model"],
            "total": int(r["total"] or 0),
            "prompt": int(r["prompt"] or 0),
            "cache_read": int(r["cache_read"] or 0),
            "completion": int(r["completion"] or 0),
            "true_total": (int(r["prompt"] or 0)
                           + int(r["cache_read"] or 0)
                           + int(r["completion"] or 0)),
            "calls": int(r["calls"] or 0),
        }
        for r in await db.fetchall(
            "SELECT task_id, project_id, role, model, "
            "SUM(total_tokens) as total, "
            "SUM(prompt_tokens) as prompt, "
            "SUM(cache_read_tokens) as cache_read, "
            "SUM(completion_tokens) as completion, "
            "COUNT(*) as calls "
            "FROM token_usage WHERE created_at >= ? AND task_id IS NOT NULL "
            "GROUP BY task_id "
            "ORDER BY (prompt + cache_read + completion) DESC LIMIT 5",
            (cutoff_7d,),
        )
    ]
    # 7-day sparkline (oldest first). v3.12.8: also pull prompt +
    # cache_read + completion so the sparkline shows true_total
    # rather than the API-reported total (which excluded cache).
    for i in range(7):
        day_start = (now - timedelta(days=6 - i)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end = day_start + timedelta(days=1)
        r = await db.fetchone(
            "SELECT COALESCE(SUM(total_tokens),0) as total, "
            "COALESCE(SUM(prompt_tokens),0) as prompt, "
            "COALESCE(SUM(cache_read_tokens),0) as cache_read, "
            "COALESCE(SUM(completion_tokens),0) as completion "
            "FROM token_usage "
            "WHERE created_at >= ? AND created_at < ?",
            (day_start.isoformat(), day_end.isoformat()),
        )
        prompt_v = int(r["prompt"]) if r else 0
        cache_v = int(r["cache_read"]) if r else 0
        comp_v = int(r["completion"]) if r else 0
        out["sparkline"].append({
            "date": day_start.strftime("%m-%d"),
            "total": int(r["total"]) if r else 0,
            "true_total": prompt_v + cache_v + comp_v,
        })
    return out


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request) -> HTMLResponse:
    """Agents page with expandable profile sub-cards."""
    db = request.app.state.db
    agents = await _load_agents(db)
    overview = await _load_agents_overview(db)
    token_usage = await _load_token_usage_overview(db)
    return templates.TemplateResponse(
        request=request,
        name="agents.html",
        context={
            **(await _base_context(request, "agents")),
            "agents": agents,
            "overview": overview,
            "token_usage": token_usage,
        },
    )


@router.get("/token-usage", response_class=HTMLResponse)
async def token_usage_page(request: Request) -> HTMLResponse:
    """Standalone Token Usage analytics page (v3.0).

    Reachable directly via /token-usage URL. Surfaced in the
    Settings expandable nav in base.html. Uses the same
    _load_token_usage_overview() helper the agents page uses
    (now extended with by_provider + daily_breakdown for the
    stacked bar chart).
    """
    db = request.app.state.db
    token_usage = await _load_token_usage_overview(db)
    return templates.TemplateResponse(
        request=request,
        name="token_usage.html",
        context={
            **(await _base_context(request, "token_usage")),
            "token_usage": token_usage,
        },
    )


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(
    request: Request,
    status: str | None = None,
    days: int | None = 7,
    limit: int = 50,
    offset: int = 0,
    include_archived: bool = False,
    search: str = "",
    kind: str = "all",  # all | project | single
) -> HTMLResponse:
    """Tasks page (filterable by status, date range, limit, kind, search).

    Unified view per the 2026-07-27 merge decision: project tasks
    and single tasks live on the same page. The `kind` filter
    defaults to "all"; "single" shows only single tasks (the
    `is_single_task=1` flag), "project" hides them.

    By default hides tasks from archived/deleted projects. Pass
    include_archived=true (via ?include_archived=1 in URL) to see them.
    """
    db = request.app.state.db
    where = []
    params: list[Any] = []
    if status:
        where.append("t.status = ?")
        params.append(status)
    if kind == "project":
        where.append("t.is_single_task = 0")
    elif kind == "single":
        where.append("t.is_single_task = 1")
    if not include_archived:
        # JOIN projects and hide tasks whose project is archived/deleted
        where.append("p.state NOT IN ('archived', 'deleted')")
    if days:
        # Compute the cutoff in local time with offset, matching the
        # format that db.insert / audit_log now writes. SQLite's
        # datetime('now', '-N days') would return UTC naive, but our
        # stored timestamps are local +offset — string comparison across
        # mixed formats is unreliable (date strings compare OK, but
        # the time portion has T-separator vs space-separator differences
        # plus a +08:00 suffix). Computing the cutoff in Python and
        # passing it as a parameter sidesteps the issue: both sides of
        # the comparison are now in the same ISO-8601-with-offset format.
        from datetime import timedelta
        cutoff = (now_aware() - timedelta(days=days)).isoformat()
        where.append("t.created_at >= ?")
        params.append(cutoff)
    if search:
        # Search across task id, name, action, agent_role, and project
        # name. SQLite LIKE is case-insensitive for ASCII by default;
        # the LOWER() wrapper makes it case-insensitive for everything
        # (PRAGMA case_sensitive_like = 0 is also set elsewhere, but
        # LOWER is portable). We split on whitespace and AND each
        # token so "write output" matches both "write_output" and
        # "output.json". Cheap because the table is small.
        tokens = [t for t in search.split() if t]
        for tok in tokens:
            where.append(
                "(LOWER(t.id) LIKE ? OR LOWER(IFNULL(t.name, '')) LIKE ? "
                "OR LOWER(IFNULL(t.action, '')) LIKE ? "
                "OR LOWER(IFNULL(t.agent_role, '')) LIKE ? "
                "OR LOWER(IFNULL(p.name, '')) LIKE ?)"
            )
            like = f"%{tok.lower()}%"
            params.extend([like, like, like, like, like])
    # Always JOIN projects — column refs use t.alias and the JOIN
    # is cheap (indexed FK). The `p.state NOT IN (...)` filter below
    # is only added when include_archived=False, so the same SQL
    # works for both cases.
    join_sql = " JOIN projects p ON t.project_id = p.id"
    where_sql = " WHERE " + " AND ".join(where) if where else ""

    # Total count for pagination
    total_row = await db.fetchone(
        f"SELECT COUNT(*) as c FROM tasks t{join_sql}{where_sql}", tuple(params)
    )
    total = total_row["c"] if total_row else 0

    # Page rows
    sql = f"SELECT t.* FROM tasks t{join_sql}{where_sql} ORDER BY t.created_at DESC LIMIT ? OFFSET ?"
    page_params = tuple(params) + (limit, offset)
    raw_rows = await db.fetchall(sql, page_params)
    tasks = []
    for r in raw_rows:
        for col in ("depends_on", "params", "result"):
            v = r.get(col)
            if isinstance(v, str):
                try:
                    r[col] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    r[col] = {} if col != "depends_on" else []
        tasks.append(r)
    # Pull all projects with their state. The dropdown in
    # tasks.html filters to active states itself (it can also
    # show the project id next to the name to disambiguate
    # duplicates). project_map keeps the full list so archived
    # tasks still show a sensible name (e.g. "vp-smoke-addfe8"
    # not the raw id).
    projects = await db.fetchall(
        "SELECT id, name, state FROM projects ORDER BY created_at DESC"
    )
    project_map = {p["id"]: (p["name"] or p["id"]) for p in projects}
    # Attach project name + timing to each task for the template
    for t in tasks:
        t["project_name"] = project_map.get(t["project_id"], t["project_id"])
        t["timing"] = _compute_task_timing(t)
        # Per the 2026-07-27 merge: annotate single tasks so the
        # template can render a "Single" badge and link to the
        # single-task detail URL (the project link would 404
        # because the virtual project has no /projects/{id} page).
        t["is_single_task"] = bool(t.get("is_single_task"))
    # Per the 2026-07-27 commit 3: profile column shows the human
    # form "<agent_id> / <profile_name>" instead of just the raw
    # agent_id. tasks.assigned_profile_id is the agent_profiles.id
    # (UUID), not the profile name — so key the lookup map by id.
    # Falls back to agent_role when no profile is assigned.
    profile_rows = await db.fetchall(
        "SELECT id, name, agent_id FROM agent_profiles"
    )
    profile_label = {
        p["id"]: f"{p['agent_id']} / {p['name']}" for p in profile_rows
    }
    for t in tasks:
        prof = t.get("assigned_profile_id") or ""
        if prof and prof in profile_label:
            t["profile_label"] = profile_label[prof]
        elif t.get("assigned_agent_id"):
            t["profile_label"] = t["assigned_agent_id"]
        else:
            t["profile_label"] = "—"
    # v3.12.8 (Phase 5): per-task token aggregation for the tasks page
    # task cards. Same shape as the project-page task_token_usage dict.
    # We pull ALL task IDs in the current page (the page is already
    # paginated), so this is a single SQL with IN (...).
    if tasks:
        task_ids = [t["id"] for t in tasks]
        # Build placeholders for the IN clause
        placeholders = ",".join("?" for _ in task_ids)
        per_task_rows = await db.fetchall(
            f"SELECT t.id AS task_id, "
            f"COUNT(tu.id) AS n_calls, "
            f"COALESCE(SUM(tu.prompt_tokens), 0) AS in_new, "
            f"COALESCE(SUM(tu.cache_read_tokens), 0) AS cache_hit, "
            f"COALESCE(SUM(tu.completion_tokens), 0) AS out, "
            f"COALESCE(SUM(tu.total_tokens), 0) AS api_total "
            f"FROM tasks t LEFT JOIN token_usage tu ON tu.task_id = t.id "
            f"WHERE t.id IN ({placeholders}) "
            f"GROUP BY t.id",
            tuple(task_ids),
        )
        tasks_token_usage: dict[str, dict] = {}
        for r in per_task_rows:
            d = dict(r)
            in_new = int(d.get("in_new") or 0)
            cache_hit = int(d.get("cache_hit") or 0)
            out = int(d.get("out") or 0)
            d["in_new"] = in_new
            d["cache_hit"] = cache_hit
            d["out"] = out
            d["total_processed"] = in_new + cache_hit + out
            d["cache_pct"] = (
                round(cache_hit / (in_new + cache_hit), 4)
                if (in_new + cache_hit) > 0
                else 0.0
            )
            tasks_token_usage[d["task_id"]] = d
    else:
        tasks_token_usage = {}
    # Per the 2026-07-27 commit 3: action preset chips. Top 10
    # distinct actions, ordered by frequency. Helps the user not
    # have to remember / type the exact action name for common
    # ones. Excludes 'do_task' (the single-task default) and empty
    # actions — we want concrete action types, not "no action".
    action_rows = await db.fetchall(
        "SELECT action, COUNT(*) AS c FROM tasks "
        "WHERE action IS NOT NULL AND action != '' "
        "AND action != 'do_task' "
        "GROUP BY action ORDER BY c DESC LIMIT 10"
    )
    action_presets = [{"name": r["action"], "count": int(r["c"] or 0)}
                      for r in action_rows]
    # Annotate each artifact with on-disk existence so the template
    # can show "deleted" badges for ephemeral control files
    # (decision.md, status.md, plan.md, etc.) that the supervisor
    # read + unlinked. Cheap stat() per artifact; not a hot path.
    from pathlib import Path
    cfg = request.app.state.config
    projects_root_t = Path(cfg["projects"]["storage_root"]).resolve()
    for t in tasks:
        _annotate_artifact_exists(t, projects_root_t)
    # Build the ordered list for the create-form dropdown. Reuse
    # the same query result from earlier (profile_rows above) by
    # sorting it here.
    all_profiles = sorted(
        [{"agent_id": p["agent_id"], "name": p["name"]}
         for p in profile_rows],
        key=lambda p: (p["agent_id"], p["name"]),
    )
    return templates.TemplateResponse(
        request=request,
        name="tasks.html",
        context={
            **(await _base_context(request, "tasks")),
            "tasks": tasks,
            "projects": projects,
            "project_map": project_map,
            "all_profiles": all_profiles,
            "action_presets": action_presets,
            "filter_status": status,
            "filter_days": days,
            "filter_limit": limit,
            "filter_offset": offset,
            "filter_include_archived": include_archived,
            "filter_search": search,
            "filter_kind": kind,
            "total_count": total,
            # v3.12.8 (Phase 5): per-task token breakdown keyed by
            # task_id; same shape as the project-page task_token_usage.
            "tasks_token_usage": tasks_token_usage,
        },
    )


def _get_dup_names_for_project(request: Request, project_id: str) -> list[str]:
    """v3.12.1 follow-up #3: per-project duplicate-running names
    pulled from the supervisor's in-memory cache. Returns an
    empty list when the supervisor isn't attached (e.g. in unit
    tests) or when the invariant holds. Safe to call from any
    page handler.
    """
    sup = getattr(request.app.state, "supervisor", None)
    if sup is None:
        return []
    try:
        return sup.get_duplicate_running_for_project(project_id)
    except Exception:
        return []


def _get_dup_running_map(request: Request) -> dict[str, list[str]]:
    """v3.12.1 follow-up #3: full duplicate-running map. Used by
    the projects list page to render a ⚠️ next to each affected
    project. Returns {} when the supervisor isn't attached.
    """
    sup = getattr(request.app.state, "supervisor", None)
    if sup is None:
        return {}
    try:
        return sup.get_duplicate_running_projects()
    except Exception:
        return {}


@router.get("/projects", response_class=HTMLResponse)
async def projects_list_page(
    request: Request,
    show_archived: bool = False,
    show_deleted: bool = False,
    state: str | None = None,
    days: str | None = None,  # string to allow empty string; parse below
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse:
    """Projects list. Filterable by state, date range, paginated.

    Default: hide archived AND soft-deleted. Both have toggleable
    filters in the page UI. Hidden states are still in the DB
    (soft delete) — a future settings-page cleanup job hard-deletes
    after 30d.
    """
    # Parse days manually (string param accepts "" from empty <select>)
    days_int: int | None = None
    if days and days.strip():
        try:
            days_int = int(days)
        except ValueError:
            days_int = None  # silently ignore non-numeric

    db = request.app.state.db
    where = []
    params: list[Any] = []
    if not show_archived:
        where.append("state != 'archived'")
    if not show_deleted:
        where.append("state != 'deleted'")
    if state:
        where.append("state = ?")
        params.append(state)
    if days_int:
        from datetime import timedelta
        cutoff = (now_aware() - timedelta(days=days_int)).isoformat()
        where.append("created_at >= ?")
        params.append(cutoff)
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    # Total count for pagination
    total_row = await db.fetchone(
        f"SELECT COUNT(*) as c FROM projects{where_sql}", tuple(params)
    )
    total = total_row["c"] if total_row else 0
    # Cap limit
    if limit < 1:
        limit = 50
    if limit > 200:
        limit = 200
    if offset < 0:
        offset = 0
    # Page rows
    page_params = tuple(params) + (limit, offset)
    projects = await db.fetchall(
        f"SELECT * FROM projects{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        page_params,
    )
    # Per-project token totals — single grouped query (subquery) so we
    # don't issue N+1. Used to show "X tokens" badge on each row in the
    # list. Empty dict means no rows (e.g. brand-new project).
    #
    # v3.12.8 (Phase 5): cache-aware. We pull prompt / cache_read /
    # completion in the same query and compute `true_total` in Python
    # (= prompt + cache + completion = "tokens the model actually
    # saw"). The legacy `total` field (= API total, EXCLUDES cache)
    # is kept so the template can show it as a sub-line for reference.
    # Without this fix, the projects list "X tokens" badge was
    # showing API total, which is misleading now that the global
    # token_usage page + the project detail page use true_total —
    # the same project would show different numbers on different
    # pages.
    if projects:
        proj_ids = [p["id"] for p in projects]
        placeholders = ",".join("?" for _ in proj_ids)
        token_rows = await db.fetchall(
            f"SELECT project_id, "
            f"COALESCE(SUM(total_tokens), 0) AS total, "
            f"COALESCE(SUM(prompt_tokens), 0) AS prompt, "
            f"COALESCE(SUM(cache_read_tokens), 0) AS cache_read, "
            f"COALESCE(SUM(completion_tokens), 0) AS completion "
            f"FROM token_usage WHERE project_id IN ({placeholders}) "
            f"GROUP BY project_id",
            tuple(proj_ids),
        )
        token_map = {}
        for r in token_rows:
            api_total = int(r["total"] or 0)
            prompt = int(r["prompt"] or 0)
            cache_read = int(r["cache_read"] or 0)
            completion = int(r["completion"] or 0)
            token_map[r["project_id"]] = {
                # true_total is the headline number now
                # (= prompt + cache + completion).
                "true_total": prompt + cache_read + completion,
                # API total kept as sub-line / tooltip reference
                # (= prompt + completion, EXCLUDES cache).
                "api_total": api_total,
                "prompt": prompt,
                "cache_read": cache_read,
                "completion": completion,
            }
    else:
        token_map = {}
    # Profiles for the "Coordinator role" dropdown in the create form
    profile_rows = await db.fetchall(
        "SELECT agent_id, name FROM agent_profiles ORDER BY agent_id, name"
    )
    all_profiles = [
        {"agent_id": r["agent_id"], "name": r["name"]} for r in profile_rows
    ]
    # Schedule info for projects that came from a recurring schedule
    # (#22). One query, returns {project_id: schedule_name} for projects
    # with a non-empty source_schedule_id. The dashboard shows a small
    # "🔁 <schedule_name>" badge next to those rows. Missing projects
    # (e.g. schedule deleted) just don't show the badge.
    schedule_rows = await db.fetchall(
        "SELECT p.id AS project_id, s.name AS schedule_name "
        "FROM projects p "
        "JOIN project_schedules s ON s.id = p.source_schedule_id "
        "WHERE p.source_schedule_id IS NOT NULL AND p.source_schedule_id != ''"
    )
    schedule_map = {r["project_id"]: r["schedule_name"] for r in schedule_rows}
    # Workflow-run markers (Stage 2b, 2026-07-23): show "🔁 from
    # workflow <name>" badge for projects created via
    # POST /api/workflows/{id}/run. Missing workflows (deleted) just
    # don't show the badge.
    workflow_rows = await db.fetchall(
        "SELECT p.id AS project_id, w.name AS workflow_name "
        "FROM projects p "
        "JOIN workflow_packages w ON w.id = p.source_workflow_id "
        "WHERE p.source_workflow_id IS NOT NULL AND p.source_workflow_id != ''"
    )
    workflow_map = {r["project_id"]: r["workflow_name"] for r in workflow_rows}
    # Also fetch template markers — show "📋 template" badge for projects
    # that are marked as reusable templates.
    template_ids = {
        r["id"] for r in await db.fetchall(
            "SELECT id FROM projects WHERE is_template = 1"
        )
    }
    return templates.TemplateResponse(
        request=request,
        name="projects_list.html",
        context={
            **(await _base_context(request, "projects")),
            "projects": projects,
            "token_map": token_map,
            "schedule_map": schedule_map,
            # v3.12.1 follow-up #3: per-project duplicate-running
            # map. {project_id: [name, ...]}. Empty when invariant
            # holds. Template renders a ⚠️ next to the project name
            # if its id is in this map.
            "duplicate_running_projects": _get_dup_running_map(request),
            "workflow_map": workflow_map,
            "template_ids": template_ids,
            "show_archived": show_archived,
            "show_deleted": show_deleted,
            "filter_state": state,
            "filter_days": days,
            "filter_limit": limit,
            "filter_offset": offset,
            "total_count": total,
            "all_profiles": all_profiles,
        },
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_page(
    project_id: str,
    request: Request,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse:
    """Single project view: plan + tasks (paginated)."""
    db = request.app.state.db
    project = await db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")

    # Cap limit to prevent abuse
    if limit < 1:
        limit = 50
    if limit > 500:
        limit = 500
    if offset < 0:
        offset = 0

    # Total count for pagination (matches the archived=0 filter below)
    total_count_row = await db.fetchone(
        "SELECT COUNT(*) as n FROM tasks WHERE project_id = ? AND archived = 0",
        (project_id,),
    )
    total_count = total_count_row["n"] if total_count_row else 0

    # Archived task history (created by prior Clone chain actions).
    # We always query the count so the toggle can render in the page
    # header; we only load the full rows when ?show_archived=1 is on
    # (to keep the default view fast). Older versions of the same chain
    # are kept (archived=1) so the operator can compare before/after.
    from pathlib import Path
    show_archived = request.query_params.get("show_archived") == "1"
    archived_count_row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? AND archived = 1",
        (project_id,),
    )
    archived_count = archived_count_row["n"] if archived_count_row else 0
    archived_rows: list[dict] = []
    if show_archived and archived_count > 0:
        # NOTE: tasks table has no `archived_at` column (the audit log
        # records task.archived events with the timestamp). We use
        # `updated_at` here as a proxy for "when this task was archived"
        # — that's when the Clone chain set archived=1 on it.
        # We also pull `result` + `error` so the operator can compare
        # before/after: what the OLD execution produced vs the new one.
        # User feedback (2026-07-26): "可以像之前一樣可以看到output 嗎?"
        # — without result, the history is just metadata, not useful.
        # We also pull `project_id` so _annotate_artifact_exists can
        # resolve file paths to <projects_root>/<project_id>/<name>.
        archived_rows = await db.fetchall(
            "SELECT id, project_id, name, agent_role, action, status, "
            "result, error, updated_at, created_at "
            "FROM tasks WHERE project_id = ? AND archived = 1 "
            "ORDER BY updated_at DESC, created_at DESC",
            (project_id,),
        )
        # result is stored as a JSON-encoded TEXT column; parse so the
        # template can use `t.result.summary` / `.session_id` / `.artifacts`
        for t in archived_rows:
            _parse_json_fields(t, "result")
        # Annotate each artifact with on-disk existence so the template
        # can render links vs "deleted" badges. The same helper the
        # live task list uses (see _annotate_artifact_exists for why
        # this matters — without it, links to ephemeral control files
        # like decision.md / status.md / plan.md 404 because the
        # supervisor read + unlinked them after each iter loop).
        from pathlib import Path
        cfg = request.app.state.config
        projects_root_p = Path(cfg["projects"]["storage_root"]).resolve()
        for t in archived_rows:
            _annotate_artifact_exists(t, projects_root_p)

    # Paginated task rows (project-scoped SQL, not "load all then filter").
    # Filter archived=0 by default (the active plan). The "show archived"
    # toggle is TODO; for now operators can see archived history via the
    # audit_log table or the /api/tasks endpoint with the include_archived
    # flag.
    task_rows = await db.fetchall(
        "SELECT * FROM tasks WHERE project_id = ? AND archived = 0 "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (project_id, limit, offset),
    )
    project_tasks = [
        _parse_json_fields(dict(r), "depends_on", "params", "result")
        for r in task_rows
    ]
    # Compute execution timing for each task (started_at, completed_at, duration)
    for t in project_tasks:
        t["timing"] = _compute_task_timing(t)
    # Annotate artifacts with on-disk existence (see _annotate_artifact_exists
    # for why this matters — without it, links to ephemeral control files
    # like decision.md / status.md / plan.md 404 because the supervisor
    # read + unlinked them after each iter loop)
    from pathlib import Path
    cfg = request.app.state.config
    projects_root_p = Path(cfg["projects"]["storage_root"]).resolve()
    for t in project_tasks:
        _annotate_artifact_exists(t, projects_root_p)

    # Live loop status for the Task Progress Monitor (T4, 2026-07-29).
    # Compute on the server at page-render time so the initial paint
    # already shows accurate slow/stuck/ok badges. The frontend
    # (static/task_progress.js) then re-polls every 5s and overrides
    # the badge text via the data-loop-status attribute.
    from hermes_orch.core.loop_status import compute_loop_status
    for t in project_tasks:
        ls = compute_loop_status(t, db.db_path)
        t["loop"] = {
            "status": ls.status,
            "reason": ls.reason,
            "duration_s": ls.duration_s,
            "last_event_age_s": ls.last_event_age_s,
        }

    # All profiles for role dropdown
    profile_rows = await db.fetchall(
        "SELECT ap.id, ap.name, a.id AS agent_id, a.ip, a.os_type, a.secret_hash "
        "FROM agent_profiles ap JOIN agents a ON a.id = ap.agent_id "
        "ORDER BY a.id, ap.name"
    )
    all_profiles = [
        {"id": r["id"], "name": r["name"], "agent_id": r["agent_id"]}
        for r in profile_rows
    ]
    # Group by host for the promote-to-skill distribute modal. Each
    # group is one agent with its profiles + a `verified` flag so the
    # UI can badge unverified agents (no auth secret). `ip` is
    # included as a secondary identifier when the agent_id is opaque
    # (rare in practice but useful for remote hosts).
    groups: dict[str, dict] = {}
    for r in profile_rows:
        aid = r["agent_id"]
        if aid not in groups:
            groups[aid] = {
                "agent_id": aid,
                "ip": r["ip"],
                "os_type": r["os_type"],
                "verified": bool(r["secret_hash"]),
                "profiles": [],
            }
        groups[aid]["profiles"].append({"id": r["id"], "name": r["name"]})
    all_profiles_grouped = list(groups.values())
    # SOUL presets for this project (so the page can show them inline + let
    # the user edit / apply each one)
    preset_rows = await db.fetchall(
        "SELECT sp.id, sp.project_id, sp.profile_id, sp.role_name, sp.content, "
        "sp.created_at, sp.updated_at, ap.agent_id, ap.name AS profile_name "
        "FROM project_soul_presets sp "
        "JOIN agent_profiles ap ON ap.id = sp.profile_id "
        "WHERE sp.project_id = ? "
        "ORDER BY ap.agent_id, ap.name",
        (project_id,),
    )
    soul_presets = [dict(r) for r in preset_rows]
    # v3.9.0 (Phase 3): per-preset version count + current (head)
    # version number, so the project page can show "v3 of 5" next
    # to each preset without a second roundtrip from the JS. The
    # head is always MAX(version_number) — the SQL runs in one
    # shot per project, indexed by (preset_id, version_number DESC).
    if soul_presets:
        version_rows = await db.fetchall(
            "SELECT preset_id, MAX(version_number) AS head, "
            "       COUNT(*) AS total "
            "FROM project_soul_preset_versions "
            "WHERE preset_id IN ("
            "  SELECT id FROM project_soul_presets WHERE project_id = ?"
            ") "
            "GROUP BY preset_id",
            (project_id,),
        )
        version_map = {
            vr["preset_id"]: {
                "current_version": int(vr["head"]),
                "version_count": int(vr["total"]),
            }
            for vr in version_rows
        }
        for sp in soul_presets:
            v = version_map.get(sp["id"], {})
            sp["current_version"] = v.get("current_version", 1)
            sp["version_count"] = v.get("version_count", 1)
    else:
        for sp in soul_presets:
            sp["current_version"] = 1
            sp["version_count"] = 1
    # Per-project token totals (so the user can see "this project used X
    # tokens" without going to the agents page). Broken down by call_kind
    # so the breakdown is visible (planner + synthesis + agent_task).
    token_rows = await db.fetchall(
        "SELECT call_kind, "
        "COALESCE(SUM(prompt_tokens), 0) AS prompt, "
        "COALESCE(SUM(completion_tokens), 0) AS completion, "
        "COALESCE(SUM(total_tokens), 0) AS total, "
        "COUNT(*) AS calls "
        "FROM token_usage WHERE project_id = ? "
        "GROUP BY call_kind ORDER BY call_kind",
        (project_id,),
    )
    token_breakdown = [dict(r) for r in token_rows]
    token_total = sum(r["total"] for r in token_breakdown)
    # v3.12.8 (Phase 5): also compute the cache-aware "true total"
    # for the project header KPI. Same logic as the global token
    # usage page — the headline should include cache hits so the
    # operator doesn't see a number that looks smaller than the
    # sum of (in + cache + out) shown in the per-task rows
    # below. The legacy `token_total` (= API total) is kept for
    # backward compat (it's used in the tooltip / break-down
    # title that already shows call_kind percentages).
    token_true_total = sum(
        (r.get("prompt") or 0) + (r.get("completion") or 0) + (r.get("cache_read") or 0)
        for r in token_breakdown
    )
    # v3.12.8 (Phase 5): per-task token aggregation for the project page
    # task list. Returns in (new prompt), cache_hit, out, n_calls per task.
    # `total_processed` is computed in Python (= in + cache_hit + out) so it
    # always covers the full "tokens the model actually saw" — note this is
    # different from the `total_tokens` field above, which is what the LLM
    # provider reports as `usage.total_tokens` (= input + output, NOT
    # including cache_read_input_tokens). See v3.12.8 design note.
    task_token_rows = await db.fetchall(
        "SELECT t.id AS task_id, t.name AS task_name, "
        "COUNT(tu.id) AS n_calls, "
        "COALESCE(SUM(tu.prompt_tokens), 0) AS in_new, "
        "COALESCE(SUM(tu.cache_read_tokens), 0) AS cache_hit, "
        "COALESCE(SUM(tu.completion_tokens), 0) AS out, "
        "COALESCE(SUM(tu.total_tokens), 0) AS api_total "
        "FROM tasks t "
        "LEFT JOIN token_usage tu ON tu.task_id = t.id "
        "WHERE t.project_id = ? "
        "GROUP BY t.id "
        "ORDER BY t.created_at ASC",
        (project_id,),
    )
    task_token_usage: dict[str, dict] = {}
    for r in task_token_rows:
        d = dict(r)
        in_new = int(d.get("in_new") or 0)
        cache_hit = int(d.get("cache_hit") or 0)
        out = int(d.get("out") or 0)
        # `total_processed` = sum of all token categories the LLM saw.
        # This is the right "total" for operator display — the column
        # `api_total` in the DB is what the LLM provider reports
        # (= in_new + out, EXCLUDES cache_hit) so it's misleading.
        d["in_new"] = in_new
        d["cache_hit"] = cache_hit
        d["out"] = out
        d["total_processed"] = in_new + cache_hit + out
        # `cache_pct` of input billed = how much of the input was served
        # from prompt cache (vs new tokens). 0..1. Used for the green bar.
        d["cache_pct"] = (
            round(cache_hit / (in_new + cache_hit), 4)
            if (in_new + cache_hit) > 0
            else 0.0
        )
        task_token_usage[d["task_id"]] = d
    # Schedule info (#22): if this project is marked as a template or
    # was created by a recurring schedule, pull the schedule row so the
    # page can show "this project is a template for <schedule>" or
    # "this run was triggered by <schedule>". The badge is on the
    # projects list; the detail page shows the schedule details inline
    # so the user can see cron + next fire + jump to the schedule page.
    template_schedule = None
    if project.get("is_template"):
        template_schedule = await db.fetchone(
            "SELECT * FROM project_schedules WHERE template_project_id = ?",
            (project_id,),
        )
    source_schedule = None
    if project.get("source_schedule_id"):
        source_schedule = await db.fetchone(
            "SELECT * FROM project_schedules WHERE id = ?",
            (project["source_schedule_id"],),
        )
    # Phase 2 Q6 — Iterations tab (2026-07-25). Read-only timeline of
    # iteration events. We pull 3 event types:
    #   - project.iteration_completed  (iterative projects w/ coordinator)
    #   - loopback.fired                (visual builder feedback_to)
    #   - loopback.cap_reached          (loop-back hit max_iterations)
    # These are 2 different concepts (coordinator-driven vs cascade-reset)
    # but the user sees them as "iteration history" — a unified timeline
    # of "the supervisor decided to retry something, and here's why".
    iter_event_rows = await db.fetchall(
        "SELECT id, event_type, actor, task_id, payload, created_at "
        "FROM audit_log "
        "WHERE project_id = ? "
        "AND event_type IN ('project.iteration_completed', "
        "                    'loopback.fired', "
        "                    'loopback.cap_reached') "
        "ORDER BY id ASC LIMIT 200",
        (project_id,),
    )
    iteration_events = []
    for r in iter_event_rows:
        d = dict(r)
        # Parse JSON payload for display
        try:
            d["payload_parsed"] = json.loads(d.get("payload") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["payload_parsed"] = {}
        iteration_events.append(d)
    # state_archive snapshots — visual timeline of every state.md regen
    # (Phase 2 of 3-tier memory: L3 state.md is regenerated after each
    # iteration_completed; each regen leaves a breadcrumb in state_archive/).
    decision_archives = []
    try:
        archive_dir = projects_root_p / project_id / "state_archive"
        if archive_dir.exists():
            for p in sorted(archive_dir.iterdir()):
                if p.is_file() and p.suffix == ".md":
                    stat = p.stat()
                    decision_archives.append({
                        "filename": p.name,
                        "size": stat.st_size,
                        "mtime_iso": datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    })
    except Exception:
        pass  # best-effort; missing dir = no archive yet
    # v3.9.0 (Phase 2 UX): read the user's UI-prefs cookie so the
    # template's `session.get('show_soul_editor')` can decide whether
    # to render the SOUL presets section. The cookie is per-user
    # (not per-project) so the opt-in is sticky across project
    # switches. Imported lazily to avoid a circular import with
    # ui_prefs (which itself imports from auth/cookie).
    from hermes_orch.api.ui_prefs import get_session_ui_prefs
    ui_prefs = get_session_ui_prefs(request)

    return templates.TemplateResponse(
        request=request,
        name="project.html",
        context={
            **(await _base_context(request, "projects")),
            "project": project,
            "tasks": project_tasks,
            "total_count": total_count,
            "filter_limit": limit,
            "filter_offset": offset,
            "all_profiles": all_profiles,
            "all_profiles_grouped": all_profiles_grouped,
            "soul_presets": soul_presets,
            "token_breakdown": token_breakdown,
            "token_total": token_total,
            # v3.12.8: cache-aware true total (= in + cache + out)
            # for the project header KPI. See design note above.
            "token_true_total": token_true_total,
            # v3.12.8 (Phase 5): per-task token breakdown. Keyed by
            # task_id; empty dict if no tasks have token data. Each
            # entry has in_new / cache_hit / out / total_processed /
            # n_calls / cache_pct. The template only shows the row
            # when n_calls > 0.
            "task_token_usage": task_token_usage,
            "template_schedule": dict(template_schedule) if template_schedule else None,
            "source_schedule": dict(source_schedule) if source_schedule else None,
            "iteration_events": iteration_events,
            "decision_archives": decision_archives,
            "iter_event_count": len(iteration_events),
            "archived_tasks": archived_rows,
            "archived_count": archived_count,
            "show_archived": show_archived,
            # v3.9.0 Phase 2 UX: per-user UI flags. The template
            # calls `session.get('show_soul_editor')` — we synthesize
            # a `session` dict here rather than wiring a full Starlette
            # SessionMiddleware (overkill for one boolean).
            "session": ui_prefs,
            # v3.12.1 follow-up #3: per-project duplicate-running
            # warning (list of step names that have >1 'running'
            # rows). Pulled from the supervisor's in-memory cache;
            # the supervisor refreshes it every tick (~every 5s).
            # Template renders a banner at the top of the project
            # page when this list is non-empty.
            "duplicate_running_names": _get_dup_names_for_project(request, project_id),
        },
    )


@router.get("/projects/{project_id}/visual", response_class=HTMLResponse)
async def project_visual_page(
    project_id: str,
    request: Request,
) -> HTMLResponse:
    """Phase 4 Stage 1 (2026-07-25): visual project page.

    Read-only card view of every task in the project, with status
    colors, token counts, artifact counts, and depends_on shown as
    a small list. Reuses project_page's queries; just renders them
    in a card layout instead of a table.

    No interactivity in Stage 1 — Stage 2 adds side panel, Stage 3
    adds drag-to-edit depends_on, Stage 4 adds live polling on top
    of the current 30s refresh.
    """
    db = request.app.state.db
    project = await db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project not found: {project_id}")

    # All tasks (no pagination — visual page shows all).
    # Filter archived=0 so the canvas only shows the active plan.
    # Archived tasks (from prior clone-and-cascade calls) are hidden
    # but their results are still in the DB + audit log.
    task_rows = await db.fetchall(
        "SELECT * FROM tasks WHERE project_id = ? AND archived = 0 "
        "ORDER BY created_at ASC",
        (project_id,),
    )
    # Archived tasks (the old tasks from prior clone-and-cascade
    # calls; rendered grayed-out with an "ARCHIVED" badge so the
    # operator can compare before/after). The default view hides
    # them. We always query the count so the toggle can render in
    # the page header; we only load the full rows when the toggle
    # is on (to keep the default view fast).
    from pathlib import Path
    show_archived = request.query_params.get("show_archived") == "1"
    archived_count_row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? AND archived = 1",
        (project_id,),
    )
    archived_count = archived_count_row["n"] if archived_count_row else 0
    archived_rows: list[dict] = []
    if show_archived and archived_count > 0:
        archived_rows = await db.fetchall(
            "SELECT * FROM tasks WHERE project_id = ? AND archived = 1 "
            "ORDER BY created_at ASC",
            (project_id,),
        )
        # Annotate artifacts with on-disk existence (needs projects_root_p)
        cfg = request.app.state.config
        projects_root_p = Path(cfg["projects"]["storage_root"]).resolve()
        for r in archived_rows:
            r["timing"] = _compute_task_timing(r)
            _annotate_artifact_exists(r, projects_root_p)
    tasks = [
        _parse_json_fields(dict(r), "depends_on", "params", "result")
        for r in task_rows
    ]
    for t in tasks:
        t["timing"] = _compute_task_timing(t)
    # Annotate artifacts with on-disk existence
    from pathlib import Path
    cfg = request.app.state.config
    projects_root_p = Path(cfg["projects"]["storage_root"]).resolve()
    for t in tasks:
        _annotate_artifact_exists(t, projects_root_p)

    # Per-task token counts (group by task_id; project_id also filtered
    # to be safe with the index). Sum of total_tokens for each task.
    token_rows = await db.fetchall(
        "SELECT task_id, COALESCE(SUM(total_tokens), 0) AS total, "
        "COALESCE(SUM(prompt_tokens), 0) AS prompt, "
        "COALESCE(SUM(completion_tokens), 0) AS completion "
        "FROM token_usage WHERE project_id = ? AND task_id IS NOT NULL "
        "GROUP BY task_id",
        (project_id,),
    )
    token_by_task = {r["task_id"]: dict(r) for r in token_rows}

    # Per-task artifact counts
    art_rows = await db.fetchall(
        "SELECT task_id, COUNT(*) AS n, "
        "COALESCE(SUM(size_bytes), 0) AS total_bytes "
        "FROM artifacts WHERE project_id = ? GROUP BY task_id",
        (project_id,),
    )
    art_by_task = {r["task_id"]: dict(r) for r in art_rows}

    # Status counts for the filter bar (active tasks only;
    # archived tasks are shown separately when ?show_archived=1)
    status_counts: dict[str, int] = {}
    for t in tasks:
        s = t.get("status") or "unknown"
        status_counts[s] = status_counts.get(s, 0) + 1

    # Phase 4 Stage 3.5 (2026-07-26): pass available agent profiles
    # so the edit form can render the agent_role <select>. Same
    # source-of-truth as the chat /chat apply endpoint uses.
    profile_rows = await db.fetchall(
        "SELECT name, agent_id FROM agent_profiles ORDER BY agent_id, name"
    )
    all_profiles = [
        {"name": p["name"], "agent_id": p["agent_id"]} for p in profile_rows
    ]
    profile_names = [p["name"] for p in all_profiles]

    return templates.TemplateResponse(
        request=request,
        name="visual_project.html",
        context={
            **(await _base_context(request, "projects")),
            "project": project,
            "tasks": tasks,
            "archived_tasks": archived_rows,
            "archived_count": archived_count,
            "show_archived": show_archived,
            "token_by_task": token_by_task,
            "art_by_task": art_by_task,
            "status_counts": status_counts,
            "profile_names": profile_names,
            "all_profiles": all_profiles,
            # UI cleanup 2026-07-27: the workflow_actions partial
            # (Promote / Apply workflow modals) reads this for the
            # blast-radius confirm() dialog. Active (non-archived)
            # task count is the right number — that's what the
            # apply-workflow will add to.
            "total_count": len(tasks),
        },
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Settings page (LLM + Telegram + Project Storage + Cleanup + HTTPS)."""
    from hermes_orch.api.settings import _https_view
    from hermes_orch.auth.cookie import current_user
    from hermes_orch.config import LLM_PROVIDERS
    cfg = request.app.state.config or {}
    llm = cfg.get("llm") or {}
    tg = cfg.get("telegram") or {}
    cleanup = cfg.get("cleanup") or {}
    api_key = (llm.get("api_key") or "").strip()
    try:
        cleanup_rd = int(cleanup.get("retention_days", 30))
    except (TypeError, ValueError):
        cleanup_rd = 30
    # Read last run info from the live job (more accurate than the
    # in-memory config, which is only written on manual save).
    cleanup_last_run_at = None
    cleanup_last_result = None
    job = getattr(request.app.state, "cleanup", None)
    if job is not None:
        cleanup_last_run_at = job.last_run_at
        cleanup_last_result = job.last_run_result
    # v3.12.0: HTTPS view (drives the toggle / cert path / expiry badge)
    https = _https_view(cfg)
    # v1.0.1 (§3.2 / §6.5): Onboarding state for the settings page banner.
    # Read directly from the user's row — `parse_state` handles the
    # default `{}` + malformed JSON cases.
    onboarding_state: dict[str, Any] = {}
    ctx_user = await current_user(request)
    if ctx_user:
        from hermes_orch.core.onboarding import parse_state
        row = await request.app.state.db.fetchone(
            "SELECT onboarding_state FROM users WHERE id = ?", (ctx_user["id"],)
        )
        if row:
            onboarding_state = parse_state(row["onboarding_state"] or "{}")
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            **(await _base_context(request, "settings")),
            "providers": LLM_PROVIDERS,
            "current_provider": llm.get("provider"),
            "current_base_url": llm.get("base_url"),
            "current_model": llm.get("model"),
            "api_key_last4": api_key[-4:] if len(api_key) >= 4 else None,
            "tg_token_last4": ((tg.get("bot_token") or "").strip())[-4:] or None,
            "tg_token_set": bool((tg.get("bot_token") or "").strip()),
            "tg_chat_id": (tg.get("chat_id") or "").strip() or None,
            "tg_enabled": bool(tg.get("enabled", False)),
            "project_storage": _project_storage_view(cfg),
            "cleanup_retention_days": cleanup_rd,
            "cleanup_daily_sweep": bool(cleanup.get("daily_sweep", True)),
            "cleanup_last_run_at": cleanup_last_run_at,
            "cleanup_last_result": cleanup_last_result,
            "https_enabled": https.enabled,
            "https_ready": https.ready,
            "https_cert_path": https.ssl_cert_path,
            "https_key_path": https.ssl_key_path,
            "https_expires_in": https.cert_expires_in_days,
            "https_cert_cn": https.cert_subject_cn,
            "https_sans": https.cert_sans,
            # v1.0.1: onboarding state for the settings page banner
            # (§6.5). Empty dict if user is unauthenticated; the
            # template renders "false" for all signals in that case.
            "onboarding_state": onboarding_state,
        },
    )


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request) -> HTMLResponse:
    """Admin-only Users management page (v3.5.0).

    Lists all dashboard users, with per-row actions:
    - Reset password (admin can set any user's password)
    - Disable / Enable (admins can't disable themselves — handled in
      the UI by greying out the button, and the server enforces it
      too with a 400)

    Non-admin users get a 403. The sidebar Admin section is also
    server-gated (only rendered when current_user.role == 'admin'),
    so most users won't even see the link.
    """
    ctx = await _base_context(request, "admin_users")
    user = ctx.get("current_user_ctx")
    if not user or user.get("role") != "admin":
        # 403 keeps the URL working for link-sharing but blocks
        # unauthorized access. JSON API endpoints return 403 too.
        from fastapi.responses import HTMLResponse as _HTML  # local
        return _HTML(
            "<h1 style='font-family:sans-serif;padding:2rem;'>403 — Admin role required</h1>"
            "<p style='font-family:sans-serif;padding:0 2rem;color:#666;'>"
            "This page is for admin users only. Ask an admin to grant you the admin role, "
            "or use the CLI: <code>hermes-orch user add --admin --username &lt;you&gt; --password &lt;pw&gt;</code>"
            "</p>",
            status_code=403,
        )
    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context=ctx,
    )


@router.get("/admin/soul-templates", response_class=HTMLResponse)
async def admin_soul_templates_page(request: Request) -> HTMLResponse:
    """Admin-only SOUL template library page (v3.9.0 Phase 3).

    Lists all published SOUL templates, grouped by category, with
    per-row edit / delete actions. Includes a "New template" form
    at the top.

    The JSON CRUD is at /api/soul-templates/* (api/soul_templates.py);
    this page is a thin render layer that hydrates from that API.
    Non-admin users get 403.
    """
    from fastapi.responses import HTMLResponse as _HTML  # local
    ctx = await _base_context(request, "admin_soul_templates")
    user = ctx.get("current_user_ctx")
    if not user or user.get("role") != "admin":
        return _HTML(
            "<h1 style='font-family:sans-serif;padding:2rem;'>403 — Admin role required</h1>"
            "<p style='font-family:sans-serif;padding:0 2rem;color:#666;'>"
            "This page is for admin users only. Ask an admin to grant you the admin role, "
            "or use the CLI: <code>hermes-orch user add --admin --username &lt;you&gt; --password &lt;pw&gt;</code>"
            "</p>",
            status_code=403,
        )
    return templates.TemplateResponse(
        request=request,
        name="admin_soul_templates.html",
        context=ctx,
    )


@router.get("/schedules", response_class=HTMLResponse)
async def schedules_page(
    request: Request,
    template: str | None = None,  # ?template=<id> pre-selects the template
) -> HTMLResponse:
    """Recurring project schedules (#22).

    Two sections:
    1. Active schedules table — one row per schedule, with
       last-fire / next-fire / last-run state / mode. Click row
       to expand inline edit form (toggle enabled, change cron,
       delete).
    2. "New schedule" form — pick a template from the dropdown,
       choose mode (clone vs append), enter cron expression
       (with a friendly "common patterns" helper that translates
       "every weekday 9am" → "0 9 * * 1-5").

    The page polls the same /api/schedules/ endpoint the form
    uses, so a 10s reload shows schedule state changes without
    needing explicit refresh. `?template=<id>` pre-selects the
    template dropdown (used by the "+ Schedule this" button on
    a project page).
    """
    db = request.app.state.db
    # List schedules (full join with last-run state) — same SQL
    # the API uses, so the table stays in sync.
    schedule_rows = await db.fetchall(
        "SELECT s.*, p.name AS template_name, "
        "(SELECT id FROM projects WHERE source_schedule_id = s.id "
        " ORDER BY created_at DESC LIMIT 1) AS last_run_project_id, "
        "(SELECT state FROM projects WHERE source_schedule_id = s.id "
        " ORDER BY created_at DESC LIMIT 1) AS last_run_state, "
        "(SELECT created_at FROM projects WHERE source_schedule_id = s.id "
        " ORDER BY created_at DESC LIMIT 1) AS last_run_at "
        "FROM project_schedules s "
        "LEFT JOIN projects p ON p.id = s.template_project_id "
        "ORDER BY s.enabled DESC, s.next_fire_at ASC, s.created_at DESC"
    )
    # Templates dropdown (is_template=1)
    template_rows = await db.fetchall(
        "SELECT id, name, goal, state, template_description, updated_at, created_at "
        "FROM projects WHERE is_template = 1 "
        "ORDER BY updated_at DESC, created_at DESC"
    )
    return templates.TemplateResponse(
        request=request,
        name="schedules.html",
        context={
            **(await _base_context(request, "schedules")),
            "schedules": [dict(r) for r in schedule_rows],
            "templates": [dict(r) for r in template_rows],
            "preselect_template_id": template or "",
        },
    )


@router.get("/workflows", response_class=HTMLResponse)
async def workflows_page(request: Request) -> HTMLResponse:
    """Workflow package library (Stage 1, 2026-07-23).

    A workflow package is a reusable, parameterized execution template
    synthesized from a completed project. Different from `is_template`
    (which is a 1:1 cron clone) and from skills (which are single-task
    knowledge). Workflows carry {{var}} placeholders for any value that
    would change on a re-run, so they can be re-run with different
    inputs.

    Stage 2b will add a "Run with variables" action that substitutes
    the {{var}}s and spawns a fresh project.
    """
    import json as _json
    db = request.app.state.db
    rows = await db.fetchall(
        "SELECT * FROM workflow_packages ORDER BY updated_at DESC"
    )
    workflows = []
    for r in rows:
        d = dict(r)
        try:
            st = _json.loads(d.get("step_template") or "[]")
            d["step_count"] = len(st) if isinstance(st, list) else 0
        except Exception:
            d["step_count"] = 0
        try:
            vs = _json.loads(d.get("variables") or "[]")
            d["variable_count"] = len(vs) if isinstance(vs, list) else 0
        except Exception:
            d["variable_count"] = 0
        workflows.append(d)
    return templates.TemplateResponse(
        request=request,
        name="workflows.html",
        context={
            **(await _base_context(request, "workflows")),
            "workflows": workflows,
        },
    )


@router.get("/workflows/{workflow_id}", response_class=HTMLResponse)
async def workflow_detail_page(workflow_id: str, request: Request) -> HTMLResponse:
    """Workflow detail page. Shows step_template + variables as JSON.
    Stage 2b adds a "Run with variables" form here."""
    import json as _json
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT * FROM workflow_packages WHERE id = ?", (workflow_id,)
    )
    if not row:
        # Try by name
        row = await db.fetchone(
            "SELECT * FROM workflow_packages WHERE name = ?", (workflow_id,)
        )
    if not row:
        return HTMLResponse(
            f"<h1>Workflow not found</h1><p>{workflow_id}</p>",
            status_code=404,
        )
    d = dict(row)
    try:
        d["step_template_pretty"] = _json.dumps(
            _json.loads(d.get("step_template") or "[]"),
            indent=2, ensure_ascii=False,
        )
    except Exception:
        d["step_template_pretty"] = "[]"
    try:
        d["variables_pretty"] = _json.dumps(
            _json.loads(d.get("variables") or "[]"),
            indent=2, ensure_ascii=False,
        )
    except Exception:
        d["variables_pretty"] = "[]"
    # Parsed variables list (for the Run-with-variables form)
    try:
        d["variables"] = _json.loads(d.get("variables") or "[]")
    except Exception:
        d["variables"] = []
    # Source project name (for "view source" link)
    if d.get("source_project_id"):
        src = await db.fetchone(
            "SELECT name FROM projects WHERE id = ?", (d["source_project_id"],)
        )
        d["source_project_name"] = (src or {}).get("name") or ""
    else:
        d["source_project_name"] = ""
    return templates.TemplateResponse(
        request=request,
        name="workflow_detail.html",
        context={
            **(await _base_context(request, "workflows")),
            "workflow": d,
        },
    )


@router.get("/inbox", response_class=HTMLResponse)
async def inbox_page(request: Request) -> HTMLResponse:
    """v3.14.0 (Phase 3): cross-workflow pending approvals inbox.

    Renders a list of all pending human_approval requests across
    workflows. Clicking a row opens a detail modal with the full
    payload + Approve / Reject buttons. The page polls the API
    every 30s to refresh the list (consistent with the sidebar
    badge polling).

    Approve and Reject actions POST to the existing
    /api/workflows/{id}/steps/{step}/(approve|reject) endpoints
    (introduced in Phase 2). On success the row is removed from
    the list and the next poll confirms the state.
    """
    return templates.TemplateResponse(
        request=request,
        name="inbox.html",
        context={
            **(await _base_context(request, "inbox")),
        },
    )


@router.get("/workflows/{workflow_id}/visual", response_class=HTMLResponse)
async def workflow_visual_page(workflow_id: str, request: Request) -> HTMLResponse:
    """Visual workflow builder (Phase 1 of visual-builder rollout, 2026-07-24).

    Renders the workflow as a drawflow canvas of cards + edges. The user
    can drag-reorder, drag-wire (depends_on), edit a step's metadata in
    a side panel, add a step from a 4-template palette, and Save
    (PUT /api/workflows/{id}) to persist.

    The text edit form on `workflow_detail.html` is hidden by default
    per Q4 (c) of the visual-builder design review — accessible via
    a small "Edit as JSON" link in the corner of this page.
    """
    import json as _json
    db = request.app.state.db
    row = await db.fetchone(
        "SELECT * FROM workflow_packages WHERE id = ?", (workflow_id,)
    )
    if not row:
        # Try by name (consistent with the text detail page above)
        row = await db.fetchone(
            "SELECT * FROM workflow_packages WHERE name = ?", (workflow_id,)
        )
    if not row:
        return HTMLResponse(
            f"<h1>Workflow not found</h1><p>{workflow_id}</p>",
            status_code=404,
        )
    d = dict(row)
    # Parse step_template + variables into Python objects so the
    # template's `| tojson` filter encodes them as JSON arrays (not
    # JSON-encoded strings). The JS does a single JSON.parse.
    try:
        d["step_template"] = _json.loads(d.get("step_template") or "[]")
    except Exception:
        d["step_template"] = []
    try:
        d["variables"] = _json.loads(d.get("variables") or "[]")
    except Exception:
        d["variables"] = []
    # Pretty-printed for the "Edit as JSON" toggle (Phase 1.5).
    d["step_template_pretty"] = _json.dumps(
        d["step_template"], indent=2, ensure_ascii=False
    )
    d["variables_pretty"] = _json.dumps(
        d["variables"], indent=2, ensure_ascii=False
    )
    # Phase 2.5 (2026-07-26): visual_layout is the {step_name: {x,y}}
    # dict used by the visual editor to remember card positions across
    # page reloads. Default to {} if column missing (pre-migration DBs).
    try:
        d["visual_layout"] = _json.loads(d.get("visual_layout") or "{}")
    except Exception:
        d["visual_layout"] = {}
    if not isinstance(d["visual_layout"], dict):
        d["visual_layout"] = {}
    d["step_count"] = len(d["step_template"]) if isinstance(d["step_template"], list) else 0
    # v3.14.x: chatbox removed. Previously we cleared
    # source_project_id in the rendered dict when the source
    # project didn't exist (orphan workflow). The chatbox is gone
    # now, so this guard is no longer needed — the workflow's
    # source_project_id is preserved as-is for audit and future
    # re-attach (it's in the data-source-project-id attribute on
    # the canvas wrap, but no UI element reads it on this page).
    return templates.TemplateResponse(
        request=request,
        name="visual_workflow.html",
        context={
            **(await _base_context(request, "workflows")),
            "workflow": d,
        },
    )


@router.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request,
    event_type: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    days: int | None = 7,
) -> HTMLResponse:
    """History / audit log (filterable)."""
    db = request.app.state.db
    where = []
    params: list[Any] = []
    if event_type:
        where.append("event_type = ?")
        params.append(event_type)
    if project_id:
        where.append("project_id = ?")
        params.append(project_id)
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    if days:
        # Same local-time cutoff as the tasks page above — keeps the
        # comparison format-consistent regardless of which timestamp
        # format the rows were originally written in.
        from datetime import timedelta
        cutoff = (now_aware() - timedelta(days=days)).isoformat()
        where.append("created_at >= ?")
        params.append(cutoff)
    sql = "SELECT * FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT 200"
    rows = await db.fetchall(sql, tuple(params))
    # Pretty-print payload JSON
    import json
    for e in rows:
        p = e.get("payload")
        if p:
            try:
                e["payload_pretty"] = json.dumps(json.loads(p), indent=2)
            except (json.JSONDecodeError, TypeError):
                e["payload_pretty"] = p
    # Filter dropdowns
    event_types = await db.fetchall(
        "SELECT DISTINCT event_type FROM audit_log ORDER BY event_type"
    )
    projects = await db.fetchall(
        "SELECT id, name FROM projects ORDER BY created_at DESC LIMIT 50"
    )
    agents = await db.fetchall(
        "SELECT id FROM agents ORDER BY id"
    )
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            **(await _base_context(request, "history")),
            "events": rows,
            "event_types": [r["event_type"] for r in event_types],
            "projects": projects,
            "agents": [r["id"] for r in agents],
            "filter_event_type": event_type,
            "filter_project_id": project_id,
            "filter_agent_id": agent_id,
            "filter_days": days,
            "active_page": "history",
        },
    )
