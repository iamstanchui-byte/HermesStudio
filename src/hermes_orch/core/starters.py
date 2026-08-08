# coding: utf-8
"""Bundled starter catalog (v1.0.1 new-user-activation §3.4).

The catalog is a set of read-only YAML files at
`src/hermes_orch/starters/*.yaml`. They are loaded once at server
startup and served via the API. Cloning a starter creates a
user-owned `workflow_packages` row (the catalog YAML is never
mutated; the user gets a snapshot — spec §3.4 versioning rule).

Starter shape (YAML):
  name: <kebab-case id>
  version: "0.x.y"
  display: { title, description, icon, category, mock_mode_supported,
             estimated_minutes }
  step_template: [ <step>, ... ]   # same shape as workflow_packages.step_template
  variables: [ <var>, ... ]         # same shape as workflow_packages.variables
  required_capability: <string|None>

The `display` block is the only catalog-specific extension. The
rest is identical to a workflow_packages row, which is what makes
the clone flow trivial: copy step_template + variables + display.description
into a new workflow_packages row.

The system-health starter (§3.5) has a special step action
`_server_healthcheck` that the supervisor handles in-process. See
`api/starters.py::clone_starter` for the magic-action handling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# The server-side magic action handled in-process (no agent dispatch,
# no LLM call). Used by the system-health starter per spec §3.5.
SERVER_HEALTHCHECK_ACTION = "_server_healthcheck"


@dataclass(frozen=True)
class StarterDisplay:
    """UI-facing metadata for a starter."""
    title: str
    description: str
    icon: str = "📦"
    category: str = "general"
    mock_mode_supported: bool = True
    estimated_minutes: int = 5


@dataclass(frozen=True)
class Starter:
    """A single starter catalog entry (loaded from YAML, never mutated)."""
    name: str
    version: str
    display: StarterDisplay
    step_template: list[dict[str, Any]] = field(default_factory=list)
    variables: list[dict[str, Any]] = field(default_factory=list)
    required_capability: str | None = None

    def to_summary_dict(self) -> dict[str, Any]:
        """Shape returned by GET /api/starters (list view)."""
        return {
            "name": self.name,
            "version": self.version,
            "title": self.display.title,
            "description": self.display.description,
            "icon": self.display.icon,
            "category": self.display.category,
            "mock_mode_supported": self.display.mock_mode_supported,
            "estimated_minutes": self.display.estimated_minutes,
        }

    def to_detail_dict(self) -> dict[str, Any]:
        """Shape returned by GET /api/starters/{name} (single, with template)."""
        d = self.to_summary_dict()
        d["step_template"] = self.step_template
        d["variables"] = self.variables
        d["required_capability"] = self.required_capability
        return d


def _catalog_dir() -> Path:
    """Path to the bundled starter catalog directory."""
    # This file lives at src/hermes_orch/core/starters.py, so the
    # catalog is at src/hermes_orch/starters/.
    return Path(__file__).resolve().parent.parent / "starters"


def _parse_starter(path: Path) -> Starter:
    """Parse a single starter YAML into a Starter dataclass.

    Defensive: missing optional fields get sensible defaults. A
    malformed YAML raises — that's a build-time bug and should
    fail fast at startup, not at request time.
    """
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    name = data.get("name") or path.stem
    version = str(data.get("version", "0.1.0"))
    display_data = data.get("display") or {}
    if not isinstance(display_data, dict):
        display_data = {}
    display = StarterDisplay(
        title=display_data.get("title") or name,
        description=display_data.get("description") or "",
        icon=display_data.get("icon") or "📦",
        category=display_data.get("category") or "general",
        mock_mode_supported=bool(display_data.get("mock_mode_supported", True)),
        estimated_minutes=int(display_data.get("estimated_minutes", 5)),
    )
    step_template = data.get("step_template") or []
    if not isinstance(step_template, list):
        step_template = []
    variables = data.get("variables") or []
    if not isinstance(variables, list):
        variables = []
    required_capability = data.get("required_capability")
    if required_capability is not None:
        required_capability = str(required_capability)
    return Starter(
        name=str(name),
        version=version,
        display=display,
        step_template=step_template,
        variables=variables,
        required_capability=required_capability,
    )


def load_catalog() -> dict[str, Starter]:
    """Load all starter YAMLs from the catalog directory.

    Called once at server startup. Returns a dict keyed by starter
    name. Missing catalog directory → empty dict (the server still
    boots, but the gallery shows "no starters available").
    """
    catalog: dict[str, Starter] = {}
    catalog_dir = _catalog_dir()
    if not catalog_dir.exists():
        return catalog
    for path in sorted(catalog_dir.glob("*.yaml")):
        try:
            starter = _parse_starter(path)
        except Exception as e:
            # Defensive: a single bad YAML shouldn't crash the
            # whole server. Log and skip.
            import logging
            logging.getLogger("hermes_orch.core.starters").error(
                "Failed to load starter %s: %s", path, e
            )
            continue
        catalog[starter.name] = starter
    return catalog
