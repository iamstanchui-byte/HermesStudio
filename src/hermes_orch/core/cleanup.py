# coding: utf-8
"""Cleanup job — hard-deletes projects that have been in `deleted` state
longer than `cleanup.retention_days`.

Hard-delete = DELETE FROM projects (CASCADE removes tasks, artifacts,
project_sessions, project_soul_presets) + shutil.rmtree the project
folder on disk. Audit_log rows are preserved (no FK), so we have a
permanent record of `project.hard_deleted` events.

Triggered:
- On server startup (fire-and-forget, run by main.py lifespan)
- Daily by the supervisor's tick (if `cleanup.daily_sweep: true`)
- Manually via POST /api/settings/cleanup/run

The job is idempotent: if no projects are eligible, it returns
{scanned: 0, deleted: 0, errors: 0} without writing any audit events
beyond `cleanup.completed`.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

from hermes_orch.core.audit import audit_log
from hermes_orch.utils import now_iso, now_aware

logger = logging.getLogger(__name__)


class CleanupJob:
    """Hard-deletes stale soft-deleted projects."""

    def __init__(self, db: Any, cfg: dict[str, Any]) -> None:
        self.db = db
        self.cfg = cfg
        # Track last run for supervisor's daily sweep (separate from
        # config so we don't need a config write to update it)
        self._last_run_at: str | None = None
        self._last_run_result: dict[str, Any] | None = None
        # Lock so manual + auto runs don't double-delete
        self._lock = asyncio.Lock()

    @property
    def cleanup_cfg(self) -> dict[str, Any]:
        return (self.cfg.get("cleanup") or {})

    @property
    def retention_days(self) -> int:
        """Return configured retention days, or 0 if disabled."""
        try:
            return int(self.cleanup_cfg.get("retention_days", 30))
        except (TypeError, ValueError):
            return 30

    @property
    def enabled(self) -> bool:
        return self.retention_days > 0

    @property
    def last_run_at(self) -> str | None:
        return self._last_run_at

    @property
    def last_run_result(self) -> dict[str, Any] | None:
        return self._last_run_result

    def update_config(self, new_cfg: dict[str, Any]) -> None:
        """Update the in-memory config (call after saving to disk)."""
        self.cfg = new_cfg
        # Force the cleanup_cfg property to re-read
        # (it does that already since it reads from self.cfg each call)

    # ===== Public =====

    async def preview(self, retention_days: int | None = None) -> dict[str, Any]:
        """Return a preview of what cleanup would do, without deleting anything.

        Useful for the settings page: shows the user how many projects
        are eligible right now.
        """
        days = retention_days if retention_days is not None else self.retention_days
        if days <= 0:
            return {
                "enabled": False,
                "retention_days": days,
                "eligible_count": 0,
                "cutoff": None,
                "oldest_eligible": None,
            }
        cutoff = (now_aware() - timedelta(days=days)).isoformat()
        rows = await self.db.fetchall(
            "SELECT id, name, created_at FROM projects "
            "WHERE state = 'deleted' AND created_at < ? "
            "ORDER BY created_at ASC",
            (cutoff,),
        )
        eligible = [dict(r) for r in rows]
        return {
            "enabled": True,
            "retention_days": days,
            "cutoff": cutoff,
            "eligible_count": len(eligible),
            "oldest_eligible": eligible[0]["created_at"] if eligible else None,
        }

    async def run(
        self,
        retention_days: int | None = None,
        trigger: str = "manual",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run the cleanup. Returns a result dict.

        Args:
            retention_days: override the configured value (for testing
                or ad-hoc runs). None = use config.
            trigger: 'manual' (UI button) or 'auto' (startup / daily sweep).
                Written to audit log so operators can tell the two apart.
            dry_run: if True, query eligible projects and return the
                list but do NOT delete anything.

        Returns:
            {
                "trigger": "manual"|"auto",
                "retention_days": int,
                "cutoff": ISO,
                "scanned": int,
                "deleted": int,
                "skipped": int,
                "errors": int,
                "eligible": [{"id":..., "name":..., "age_days":...}],
                "dry_run": bool,
                "started_at": ISO,
                "completed_at": ISO,
            }
        """
        days = retention_days if retention_days is not None else self.retention_days
        result: dict[str, Any] = {
            "trigger": trigger,
            "retention_days": days,
            "cutoff": None,
            "scanned": 0,
            "deleted": 0,
            "skipped": 0,
            "errors": 0,
            "eligible": [],
            "dry_run": dry_run,
            "started_at": now_iso(),
            "completed_at": None,
        }
        if days <= 0:
            result["completed_at"] = now_iso()
            return result

        # Serialize so a manual click and a daily sweep don't race
        async with self._lock:
            cutoff = (now_aware() - timedelta(days=days)).isoformat()
            result["cutoff"] = cutoff
            # Find eligible projects
            rows = await self.db.fetchall(
                "SELECT id, name, created_at FROM projects "
                "WHERE state = 'deleted' AND created_at < ? "
                "ORDER BY created_at ASC",
                (cutoff,),
            )
            eligible = [dict(r) for r in rows]
            result["scanned"] = len(eligible)
            # Augment with age in days
            from datetime import datetime
            now = now_aware()
            for e in eligible:
                try:
                    created = datetime.fromisoformat(
                        e["created_at"].replace("Z", "+00:00")
                    )
                    e["age_days"] = int((now - created).total_seconds() // 86400)
                except (ValueError, TypeError, AttributeError):
                    e["age_days"] = None
            result["eligible"] = eligible

            if dry_run:
                result["completed_at"] = now_iso()
                return result

            # Log start (only when actually deleting, to keep audit
            # log clean on no-op days)
            if eligible:
                await audit_log(
                    self.db,
                    "cleanup.started",
                    actor="system",
                    payload={
                        "trigger": trigger,
                        "retention_days": days,
                        "cutoff": cutoff,
                        "eligible_count": len(eligible),
                    },
                )

            projects_root = Path(
                (self.cfg.get("projects") or {}).get("storage_root", "./projects")
            )

            for proj in eligible:
                pid = proj["id"]
                pname = proj.get("name") or pid
                age = proj.get("age_days")
                try:
                    # Order matters here!
                    # 1) Delete the project row FIRST (CASCADE removes
                    #    tasks, artifacts, project_sessions,
                    #    project_soul_presets).
                    # 2) Write the audit log SECOND. If we did rmtree
                    #    first, the audit_log L1 mirror hook would
                    #    recreate the project folder by appending to
                    #    <projects_root>/<pid>/trace.jsonl, undoing
                    #    the cleanup. (Pattern catalog #12.)
                    # 3) rmtree LAST.
                    await self.db.execute(
                        "DELETE FROM projects WHERE id = ?", (pid,)
                    )
                    await audit_log(
                        self.db,
                        "project.hard_deleted",
                        actor="system",
                        project_id=pid,
                        payload={
                            "name": pname,
                            "age_days": age,
                            "trigger": trigger,
                            "retention_days": days,
                        },
                    )
                    # Now remove the project folder from disk
                    folder = projects_root / pid
                    folder_removed = False
                    if folder.exists():
                        try:
                            shutil.rmtree(folder)
                            folder_removed = True
                            logger.info("Cleanup: rmtree OK for %s", pid)
                        except Exception as ex:  # noqa: BLE001
                            # Folder removal failed but DB row is gone
                            # AND audit event is written (above). Log
                            # warning, still counts as deleted (user
                            # intent is honored; residue can be wiped
                            # manually).
                            logger.warning(
                                "Cleanup: removed DB row for %s but folder "
                                "rmtree failed: %s", pid, ex,
                            )
                    result["deleted"] += 1
                    if not folder_removed and folder.exists():
                        # Could not rmtree but folder still there.
                        # Record in result so the operator sees it.
                        result.setdefault("folder_residue", []).append(pid)
                except Exception as ex:  # noqa: BLE001
                    logger.exception("Cleanup: failed to delete %s: %s", pid, ex)
                    result["errors"] += 1

            # Final audit (only if we did something OR had errors)
            if result["deleted"] > 0 or result["errors"] > 0:
                await audit_log(
                    self.db,
                    "cleanup.completed",
                    actor="system",
                    payload={
                        "trigger": trigger,
                        "retention_days": days,
                        "scanned": result["scanned"],
                        "deleted": result["deleted"],
                        "errors": result["errors"],
                    },
                )

            result["completed_at"] = now_iso()
            self._last_run_at = result["completed_at"]
            self._last_run_result = result
            logger.info(
                "Cleanup done: trigger=%s days=%d scanned=%d deleted=%d errors=%d",
                trigger, days, result["scanned"], result["deleted"], result["errors"],
            )
            return result
