# coding: utf-8
"""Skill sidecar schema loader (Object Layer foundation, 2026-07-26).

Each skill (SKILL.md) can have an optional sidecar file
`SKILL.schema.yaml` describing its input/output contract, whether
it needs an LLM, and which capabilities it requires. Sidecars live
in the `profile_configs` table (file_path = `skills/<name>/SKILL.schema.yaml`)
alongside the SKILL.md row — same audit / apply / sha256 machinery,
just a different file_path. No new table, no new migration.

Why sidecar over inline YAML frontmatter:
- Frontmatter (--- yaml --- at the top of SKILL.md) is fragile — many
  LLM-edited skills strip or mangle it. A separate file is bulletproof.
- The wrapper reads skills verbatim and ships them to the agent as
  prompt context. Schema metadata is for the orchestrator, not the
  agent — it should never appear in the prompt.
- The orch can validate the sidecar (Pydantic) before the wrapper
  even sees the skill.

Schema fields (all optional — missing sidecar = default schema):

  input_schema:        dict[str, str]   # param_name -> type_name
  output_schema:       dict[str, str]   # field_name -> type_name
  deterministic:       bool             # script-only, no LLM
  llm_required:        bool             # needs LLM to make sense
  requires_capabilities: list[str]      # capability names this skill needs

The fallback schema (no sidecar) is:

  input_schema:        {}
  output_schema:       {}
  deterministic:       false
  llm_required:        true             # safe default
  requires_capabilities: []

`llm_required=true` is the safe default because we don't know whether
the skill is purely procedural or needs reasoning. Operators can opt
a skill into `deterministic: true` by adding a sidecar.

Usage:

  from hermes_orch.core.skill_loader import SkillLoader
  loader = SkillLoader(db)
  # Lazy-load on first call; no startup scan needed.
  for skill in loader.list_all():
      print(skill.profile_id, skill.name, skill.schema.llm_required)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from hermes_orch.db import Database


class SkillSchema(BaseModel):
    """Parsed sidecar schema for a skill.

    All fields default to safe values so a missing or partial
    sidecar still produces a usable schema (with `llm_required=True`
    as the conservative default — better to call an LLM than to
    silently fail to interpret a procedural skill).
    """
    input_schema: dict[str, str] = Field(default_factory=dict)
    output_schema: dict[str, str] = Field(default_factory=dict)
    deterministic: bool = False
    llm_required: bool = True
    requires_capabilities: list[str] = Field(default_factory=list)


@dataclass
class SkillRecord:
    """A skill as exposed by the Object Layer API.

    Combines the SKILL.md metadata (profile_id, name, file_path,
    size, sha256) with the parsed sidecar schema (or fallback).
    """
    profile_id: str
    name: str
    file_path: str           # always 'skills/<name>/SKILL.md'
    size: int                # byte length of desired_content
    sha256: str | None
    status: str              # applied | pending | applying | failed | deleted
    created_at: str | None
    applied_at: str | None
    schema: SkillSchema


class SkillLoader:
    """Reads skills + sidecars from profile_configs on demand.

    We query the DB on each call instead of caching at startup
    because:
    - profile_configs is small (typically < 100 rows per profile)
    - Skill content is versioned; caching would mean a stale view
      after the wrapper applies a new version
    - The API is called infrequently (UI list page, LLM planner
      pre-flight), not on the dispatch hot path

    For the dispatch hot path, the wrapper reads skills directly
    via the existing /api/agents/{id}/profiles/{name}/skills/{name}
    endpoint — it doesn't go through this loader.
    """

    # SQL fragment to pull SKILL.md + its sidecar (if any) per skill.
    # Uses a LEFT JOIN against the same table on file_path
    # 'skills/<name>/SKILL.schema.yaml', so skills without a sidecar
    # get desired_content=NULL and we fall back to the default schema.
    #
    # Note: SQLite uses LENGTH() (not LEN() like SQL Server). And
    # SUBSTR is 1-indexed — position 8 is the first char after the
    # 7-char 'skills/' prefix. We strip the trailing '/SKILL.md'
    # (9 chars including the leading slash) so the length to extract
    # is LENGTH(file_path) - 7 - 9. Bug fix 2026-07-26: the original
    # formula used 8 instead of 7, which dropped the last char of
    # every name (e.g. 'apple/apple-note' instead of 'apple/apple-notes').
    #
    # Split into _SQL_BASE (SELECT + FROM) and _SQL_TAIL (ORDER BY)
    # so list_all / get can interpose WHERE / LIMIT clauses correctly.
    _SQL_BASE = (
        "SELECT "
        "  s.profile_id, s.name, s.file_path, s.size, s.sha256, "
        "  s.status, s.created_at, s.applied_at, "
        "  c.desired_content AS sidecar_content "
        "FROM ("
        "  SELECT profile_id, "
        "         SUBSTR(file_path, 8, LENGTH(file_path) - 7 - LENGTH('/SKILL.md')) AS name, "
        "         file_path, LENGTH(desired_content) AS size, "
        "         desired_sha256 AS sha256, status, created_at, applied_at "
        "  FROM profile_configs "
        "  WHERE file_path LIKE 'skills/%/SKILL.md' "
        "    AND status != 'deleted' "
        ") s "
        "LEFT JOIN profile_configs c "
        "  ON c.profile_id = s.profile_id "
        "  AND c.file_path = 'skills/' || s.name || '/SKILL.schema.yaml' "
        "  AND c.status != 'deleted'"
    )
    _SQL_TAIL = " ORDER BY s.profile_id, s.name"

    def __init__(self, db: Database):
        self.db = db

    async def list_all(
        self,
        profile_id: str | None = None,
        deterministic_only: bool = False,
        requires_capability: str | None = None,
    ) -> list[SkillRecord]:
        """List skills (optionally filtered).

        `profile_id`: filter to one profile.
        `deterministic_only`: only return skills whose sidecar says
            `deterministic: true`. Useful for the LLM planner to find
            pure-script skills when looking for token-saving candidates.
        `requires_capability`: only return skills whose sidecar lists
            this capability in `requires_capabilities`. Used by the
            tool-suggestion hook to find skills that can use a given
            tool.
        """
        # Build SQL piecewise so adding a WHERE doesn't break the
        # ORDER BY clause (the base _SQL ends with ORDER BY).
        sql = self._SQL_BASE
        params: list = []
        conds: list[str] = []
        if profile_id:
            conds.append("s.profile_id = ?")
            params.append(profile_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += self._SQL_TAIL
        rows = await self.db.fetchall(sql, tuple(params))
        out: list[SkillRecord] = []
        for r in rows:
            rec = self._row_to_record(r)
            if deterministic_only and not rec.schema.deterministic:
                continue
            if requires_capability and requires_capability not in rec.schema.requires_capabilities:
                continue
            out.append(rec)
        return out

    async def get(self, profile_id: str, name: str) -> SkillRecord | None:
        """Get one skill by (profile_id, name). Returns None if not found."""
        # _SQL_BASE already has the JOIN; just add WHERE / LIMIT.
        # We can't reuse _SQL (which ends in ORDER BY) because
        # appending WHERE + LIMIT after ORDER BY is a syntax error.
        sql = (
            self._SQL_BASE
            + " WHERE s.profile_id = ? AND s.name = ?"
            + self._SQL_TAIL.replace("ORDER BY s.profile_id, s.name", "")
            + " LIMIT 1"
        )
        rows = await self.db.fetchall(sql, (profile_id, name))
        if not rows:
            return None
        return self._row_to_record(rows[0])

    def _row_to_record(self, r: dict) -> SkillRecord:
        sidecar = self._parse_sidecar(r.get("sidecar_content"))
        return SkillRecord(
            profile_id=r["profile_id"],
            name=r["name"],
            file_path=r["file_path"],
            size=r["size"],
            sha256=r["sha256"],
            status=r["status"],
            created_at=r["created_at"],
            applied_at=r["applied_at"],
            schema=sidecar,
        )

    @staticmethod
    def _parse_sidecar(content: str | None) -> SkillSchema:
        """Parse SKILL.schema.yaml text into a SkillSchema.

        Returns the fallback schema (all defaults, llm_required=True)
        if content is None/empty or YAML parsing fails. The fallback
        path is deliberately permissive — a malformed sidecar should
        not block the skill from being listed; the planner just won't
        know it's deterministic.
        """
        if not content or not content.strip():
            return SkillSchema()
        # Lazy import: PyYAML is heavy and may not be installed
        # everywhere. The Object Layer API needs it, so the import
        # is the right tradeoff; the agent dispatch path doesn't
        # touch this module.
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            return SkillSchema()
        try:
            data = yaml.safe_load(content) or {}
            if not isinstance(data, dict):
                return SkillSchema()
            return SkillSchema(**{
                k: v for k, v in data.items()
                if k in SkillSchema.model_fields
            })
        except Exception:
            return SkillSchema()
