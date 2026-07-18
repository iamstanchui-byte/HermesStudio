"""Verify audit_log created_at has local time + offset (not SQLite UTC naive)."""
import asyncio
import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, "src")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from hermes_orch.db import Database
from hermes_orch.core.audit import audit_log, _now_iso


async def main():
    print(f"_now_iso = {_now_iso()!r}")
    # Check format: must contain "+" or "Z" (offset marker)
    assert "+" in _now_iso() or _now_iso().endswith("Z"), \
        f"_now_iso should have offset: {_now_iso()!r}"
    print(f"  OK: _now_iso has timezone offset")

    # Use a temp DB
    tmp = Path(tempfile.mkdtemp(prefix="audit-tz-"))
    db_path = tmp / "test.db"
    db = Database(str(db_path))
    await db.connect()
    try:
        await audit_log(
            db, "test.event", actor="tester", project_id="proj-test",
            payload={"k": "v"},
        )
        row = await db.fetchone(
            "SELECT created_at FROM audit_log WHERE event_type = 'test.event'"
        )
        created_at = row["created_at"]
        print(f"  inserted created_at = {created_at!r}")
        # Must have offset marker
        assert "+" in created_at or created_at.endswith("Z"), \
            f"audit_log.created_at should have offset: {created_at!r}"
        # Must NOT be in the SQLite DEFAULT CURRENT_TIMESTAMP format ('YYYY-MM-DD HH:MM:SS')
        assert "T" in created_at, \
            f"audit_log.created_at should be ISO-8601 with T separator: {created_at!r}"
        print(f"  OK: audit_log row has ISO-8601 + offset")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
    print("[ALL OK] audit_log TZ fix verified")
