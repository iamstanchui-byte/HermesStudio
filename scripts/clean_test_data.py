"""One-shot cleanup: remove test data accumulated by the test suite.

Cleans up:
  1. Agents: id LIKE 'test-%' / 'test-hmac-%' (HMAC test fixture)
  2. Tasks: assigned to those test agents
  3. Projects: name matches auto-generated test patterns
     (chat-apply-test-*, plan-test-*, from-llm-test-*, etc.)
  4. Audit log: rows for those test projects
  5. Project sessions / soul presets / workflows linked to test projects
  6. Orphaned tasks (assigned to non-existent agents)

Manual test projects (e.g. "Memory Test", "15MB cap test",
"TestLoop") are LEFT ALONE — the user might want to inspect them.

Idempotent. Run while the server is running (WAL mode).

Usage:
    .venv\Scripts\python.exe scripts/clean_test_data.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path("~/.hermes-orchestrator/hermes-orch.db").expanduser()
if len(sys.argv) > 1:
    DB = Path(sys.argv[1]).expanduser()


# Auto-generated test project name patterns (the ones with random
# hex suffixes that come from automated test runs). Manual tests
# (no hex suffix) are left for the user to inspect / delete by hand.
TEST_PROJECT_NAME_PATTERNS = [
    "chat-apply-test-%",
    "plan-test-%",
    "plan-agents-test-%",
    "from-llm-test-%",
    "loop-smoke",
    "hotfix-smoke",
    "output-test-%",
    "loop-test-%",
    "tool-test-%",
    "hmac-test-%",
    "format-test",
    "dedup-test",
    "dedup-e2e-test",
    "phase-c-test",
    "name-alias-test",
    "output-format-test",
    "smoke-%",
    "chat-test-%",
    "e2e-test-%",
]

# Test agent ID patterns
TEST_AGENT_PATTERNS = [
    "test-%",
    "test-hmac-%",
]


def main() -> int:
    if not DB.exists():
        print(f"DB not found: {DB}")
        return 1
    print(f"Cleaning test data in {DB}")
    print()

    conn = sqlite3.connect(str(DB))
    conn.execute("PRAGMA journal_mode=WAL")

    # 1. Find test agents
    where_a = " OR ".join(f"id LIKE ?" for _ in TEST_AGENT_PATTERNS)
    test_agents = [r[0] for r in conn.execute(
        f"SELECT id FROM agents WHERE {where_a}", TEST_AGENT_PATTERNS
    )]
    print(f"  {len(test_agents)} test agents")

    # 2. Find test projects
    where_p = " OR ".join(f"name LIKE ?" for _ in TEST_PROJECT_NAME_PATTERNS)
    test_projects = [r[0] for r in conn.execute(
        f"SELECT id FROM projects WHERE {where_p}", TEST_PROJECT_NAME_PATTERNS
    )]
    print(f"  {len(test_projects)} test projects")

    # 3. Delete tasks assigned to test agents
    if test_agents:
        placeholders = ",".join("?" * len(test_agents))
        n = conn.execute(
            f"DELETE FROM tasks WHERE assigned_agent_id IN ({placeholders})",
            test_agents,
        ).rowcount
        if n:
            print(f"  deleted {n} tasks (assigned to test agents)")

    # 4. Delete test projects (cascades to their tasks/audit/etc.)
    if test_projects:
        placeholders = ",".join("?" * len(test_projects))
        # Order: child tables first
        for table in (
            "tasks",
            "audit_log",
            "project_sessions",
            "project_soul_presets",
            "project_plans",
            "workflows",
            "workflow_runs",
            "schedules",
        ):
            try:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE project_id IN ({placeholders})",
                    test_projects,
                )
                if cur.rowcount:
                    print(f"  deleted {cur.rowcount} {table} rows")
            except sqlite3.OperationalError:
                pass  # table doesn't exist
        cur = conn.execute(
            f"DELETE FROM projects WHERE id IN ({placeholders})",
            test_projects,
        )
        print(f"  deleted {cur.rowcount} test projects")

    # 5. Delete test agents (cascades to agent_profiles)
    if test_agents:
        placeholders = ",".join("?" * len(test_agents))
        for table in ("agent_profiles", "profile_configs"):
            try:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE agent_id IN ({placeholders})",
                    test_agents,
                )
                if cur.rowcount:
                    print(f"  deleted {cur.rowcount} {table} rows")
            except sqlite3.OperationalError:
                pass
        cur = conn.execute(
            f"DELETE FROM agents WHERE id IN ({placeholders})",
            test_agents,
        )
        print(f"  deleted {cur.rowcount} test agents")

    # 6. Orphan tasks (assigned to non-existent agents)
    cur = conn.execute("""
        DELETE FROM tasks
        WHERE assigned_agent_id IS NOT NULL
          AND assigned_agent_id NOT IN (SELECT id FROM agents)
    """)
    if cur.rowcount:
        print(f"  deleted {cur.rowcount} orphaned tasks")

    conn.commit()

    # Summary
    print()
    print("Final state:")
    for row in conn.execute("SELECT id, status FROM agents"):
        print(f"  agent  {row[0]:30s} {row[1]}")
    n = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    print(f"  projects: {n}")
    n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    print(f"  tasks:    {n}")
    n = conn.execute("SELECT COUNT(*) FROM agents WHERE id LIKE 'test-%' OR id LIKE 'test-hmac-%'").fetchone()[0]
    print(f"  test agents remaining: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
