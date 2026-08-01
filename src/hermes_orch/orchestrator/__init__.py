# coding: utf-8
"""Orchestrator package — central SOUL routing and dispatch (v3.9.0).

This package owns the *control flow* for v3.9.0:

  - `routing.py`     picks a profile for a workflow step (hybrid strategy)
  - `soul_dispatch.py` (added by Round 2B) applies the SOUL cleanly +
                     confirms via heartbeat, then dispatches the task

Per the orch-as-coordinator principle: the orchestrator decides *which*
profile runs *which* step and *when* it may write to that profile's
SOUL.md. The agent (hermes wrapper) just executes the task.

The public API in this package is async + DB-bound: callers pass a
`Database` instance and a `step` (PlanStep-like), get a `Profile` row
dict back. No FastAPI Request, no global state, no I/O outside SQLite.
"""
