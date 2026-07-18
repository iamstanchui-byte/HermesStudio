"""Shared utilities for hermes-orchestrator.

Kept deliberately small. Right now just the timestamp helpers, which
used to be duplicated across 7 files (db.py, audit.py, supervisor.py,
plus 4 API modules). Single source of truth so a future change to the
format (e.g. always include microseconds, always use UTC) only needs
to happen in one place.
"""
from __future__ import annotations

from datetime import datetime


def now_iso() -> str:
    """Local-time ISO-8601 with timezone offset.

    Example: '2026-07-18T19:30:00.123456+08:00'. Used by db.insert for
    auto-fill, by the audit_log helper, by API endpoints, and by the
    supervisor for state-transition timestamps.

    Named `now_iso` (not `_now_iso`) because it's the public API of
    this module — callers can import it as a normal name. The previous
    `_now_iso` (underscore-prefixed) was a private convention within
    each module; consolidating into a single public helper removes
    the need for that private prefix.
    """
    return datetime.now().astimezone().isoformat()


def now_aware() -> datetime:
    """Return the current local time as a timezone-aware datetime.

    Used by the history/tasks filters that need to compute a cutoff
    (now - timedelta(days=N)) and pass it as a SQL parameter. Returns
    a datetime (not a string) so the caller can do arithmetic; the
    caller is expected to .isoformat() it before passing to SQLite so
    the format matches what db.insert wrote.
    """
    return datetime.now().astimezone()
