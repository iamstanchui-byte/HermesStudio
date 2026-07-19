# User Recent (last 7 days)

> Auto-generated from L1 trace.jsonl (user-level).
> Last regenerated: now by RecentGenerator.

## Active projects
- `proj-e37455d2` Phase3 PlannerTest (started) — testing planner visibility of recent context; planner=llm-fallback, 3 tasks; super task t-f6f408b0 running on agent `linux-a-01`

## Recently completed
- (none in window)

## Patterns observed
- Heavy profile-skill churn on `win-agent02` for `skills/computer-use.md`: 6 `profile.skill_submitted` (self-taught) + 6 `profile.config_acked` events between 16:45–16:48, oscillating between two SHA256 hashes (`e59ce22e…` 11374B and `7513c401…` 11637B)
- Project lifecycle is fast and automated: operator-created → supervisor plan (~9s) → assign+start (~5s) → project.started (~7s after create)
- `llm-fallback` planner invoked (no real LLM planner used) for the Phase3 test project

## Recurring failures / friction
- Likely: skill self-taught wrapper on `win-agent02` is flapping between two versions of `computer-use.md` (3 round-trips each, ~36s apart) — possible reject→re-apply loop or non-idempotent upserts
- `llm-fallback` planner dependency — real planner path not exercised

## User preferences (inferred from activity)
- Mode: `auto` for orchestrator projects (no manual supervision requested)
- Tight time tolerance: experiment-style jobs with sub-minute plan→start cycles
- Interest in planner observability (Phase3 test explicitly checks planner's view of recent context)
- Operates profiles via self-taught skill submissions (file format, sha256-tracked); expects config_acked confirmations
- Default profile actively touched: `win-agent02` (skill `computer-use`); default agent observed: `linux-a-01`