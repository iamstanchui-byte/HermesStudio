docs: rewrite README to reflect Object Layer + Agent Layer positioning

The README was a one-liner ("# HermesStudio"). The product
positioning has shifted significantly since then (per the doc
the user shared with Perplexity, 2026-07-26) — from "agent
orchestrator with chat" to "Hybrid Agentic Workflow Runtime"
where LLM is design-time only and the runtime stays
deterministic.

New README structure:
  1. Product positioning (one-liner + 5-layer architecture)
  2. The 5 Object types (Skill / Tool / Resource / Policy /
     AgentProfile) with their schema sources
  3. The 5 Agent contracts (plan real, others stub)
  4. Object Layer API endpoint table
  5. Single-task-as-virtual-project explanation
  6. Recent commit history (chronological, last 10 commits)
  7. Roadmap (done / in progress / deferred)
  8. Dev conventions (push cadence, test stack, schema
     migration, Pydantic v2, deterministic-first)
  9. Quick start (install / configure / run / register agent)

Per the new "push per commit" workflow rule: pushing
immediately so Perplexity can see the latest state.
