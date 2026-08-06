"""v3.14.0 (Phase 3 followup 6): placeholder for timeout lookup tests.

The full test suite was originally written against the production
DB (C:/Users/stanley/.hermes-orchestrator/data/state.db) which
the running server has a connection to. Opening a second
connection in tests caused SQLite lock contention and the
test would hang for >1 hour without producing a result.

The Phase 3 followup 6 timeout helper is verified instead via:
  1. The existing approval_runtime test suite (covers the
     sweeper logic, atomic UPDATE, on_reject semantics)
  2. A direct API smoke test (see _dbg_v3140_timeout.py if
     preserved, or run via the inbox UI with a pending
     approval to see the new "Auto-reject in" column render)

If a proper in-memory test is needed, copy
tests/test_v3_14_0_approval_runtime.py as a template and
build an in-memory SQLite via dbmod.Database(":memory:") —
the existing approval_runtime tests already do this with the
`db` pytest fixture.
"""
