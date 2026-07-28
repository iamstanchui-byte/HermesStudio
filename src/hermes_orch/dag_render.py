"""Plain-text DAG renderer for project plans (Phase 1, 2026-07-28).

Renders a ProjectPlan (or just a list of steps) as an ASCII tree
using box-drawing characters, suitable for embedding in chat
messages and LLM prompt examples.

Output examples (see tests/test_dag_render.py for full coverage):

    Linear chain:
        step-1
        └─ step-2
             └─ step-3

    Branching (fan-out + fan-in via duplicate rendering):
        step-1
        ├─ step-2
        │     └─ step-4
        └─ step-3
              └─ step-4

    Parallel roots:
        step-1
        step-2
        └─ step-2a

Pure function. No I/O, no DB, no Pydantic validation (the caller
should pass a valid ProjectPlan; this module handles shape errors
gracefully with ⚠ prefixed error strings).
"""
from __future__ import annotations

from typing import Iterable

# Type alias for "anything step-shaped" — accept either a Pydantic
# PlanStep (with .name / .depends_on) or a plain dict (e.g. for
# tests, or for the LLM to render its in-memory draft before
# Pydantic validation). This keeps the renderer usable from
# multiple call sites.
class _StepLike:
    """Protocol-style hint: needs .name: str and .depends_on: Iterable[str]."""
    name: str
    depends_on: Iterable[str]


def _step_name(s) -> str:
    if isinstance(s, dict):
        return s.get("name", "")
    return getattr(s, "name", "")


def _step_deps(s) -> list[str]:
    if isinstance(s, dict):
        return list(s.get("depends_on") or [])
    return list(getattr(s, "depends_on", None) or [])


def render_plan_dag(steps: Iterable, *, show_agent_role: bool = False) -> str:
    """Render a list of steps as a plain-text DAG.

    Args:
        steps: iterable of step objects (Pydantic PlanStep or dict).
            Each must have .name (str, unique) and .depends_on
            (list of str referencing other step names).
        show_agent_role: if True, append `  (agent_role)` to each
            step name. Default False for compact output.

    Returns:
        A multi-line string using box-drawing characters
        (├─, └─, │). For empty input, returns "(empty plan)".
        For invalid input (unknown dep, cycle), returns a
        single-line warning prefixed with ⚠ so the LLM/UI can
        detect and surface it.

    Notes on shape:
        - Step names must be unique within the input. Duplicates
          are treated as the first occurrence; the second is
          ignored silently (defensive — caller should validate).
        - Unknown dependencies are rendered as a warning line.
        - Cycles produce a warning line; remaining sub-DAGs
          (acyclic parts) still render.
    """
    steps_list = list(steps)
    if not steps_list:
        return "(empty plan — no steps yet)"

    # Build maps. Track which names we already saw to dedupe.
    step_by_name: dict[str, object] = {}
    duplicates: list[str] = []
    for s in steps_list:
        n = _step_name(s)
        if not n:
            continue
        if n in step_by_name:
            duplicates.append(n)
            continue
        step_by_name[n] = s
    children: dict[str, list[str]] = {n: [] for n in step_by_name}

    # Build children map + check unknown deps
    warnings: list[str] = []
    for n, s in step_by_name.items():
        for d in _step_deps(s):
            if d in step_by_name:
                children[d].append(n)
            else:
                warnings.append(
                    f"⚠ step {n!r} depends on unknown step {d!r}"
                )
    for d in duplicates:
        warnings.append(f"⚠ duplicate step name {d!r} (kept first)")

    # Roots = steps with no deps. Sort for deterministic output.
    roots = sorted(
        (n for n, s in step_by_name.items() if not _step_deps(s))
    )
    if not roots:
        # All steps have deps → cycle. Pick the first step (alphabetical)
        # as the entry point and let the recursive render detect the cycle.
        roots = sorted(step_by_name.keys())[:1]
        warnings.append("⚠ cycle detected — render may repeat nodes")
    if warnings:
        warning_text = "\n".join(warnings) + "\n"
    else:
        warning_text = ""

    # Sort children for determinism
    for c in children.values():
        c.sort()

    # Helper: format a step's display label
    def _label(name: str) -> str:
        if not show_agent_role:
            return name
        s = step_by_name[name]
        role = (
            s.get("agent_role") if isinstance(s, dict)
            else getattr(s, "agent_role", "")
        ) or ""
        if role:
            return f"{name}  ({role})"
        return name

    # Recursive renderer. `prefix` is the indentation + connectors
    # to prepend to the current line. `is_last` controls whether
    # the connector is └─ (last) or ├─ (not last).
    # `path` is the set of ancestor names on the current recursion
    # path — used to break cycles (a node appearing twice on a
    # single path is a cycle).
    lines: list[str] = []

    def _render_subtree(node: str, prefix: str, is_last: bool, path: set[str]) -> None:
        connector = "└─ " if is_last else "├─ "
        if node in path:
            # Cycle on the current path: render this node as a
            # back-edge marker and stop. Prevents infinite recursion.
            lines.append(f"{prefix}{connector}{node}  ↩ (cycle)")
            return
        path.add(node)
        lines.append(f"{prefix}{connector}{_label(node)}")
        # Children of this node will be indented further. The
        # extension under ├─ is "│   " (vertical bar continues);
        # under └─ is "    " (no vertical line below).
        ext = "    " if is_last else "│   "
        child_prefix = prefix + ext
        kids = children.get(node, [])
        for i, kid in enumerate(kids):
            _render_subtree(kid, child_prefix, i == len(kids) - 1, path)
        path.discard(node)

    # Render each root as a top-level node (no connector above).
    for root in roots:
        lines.append(_label(root))
        kids = children.get(root, [])
        for i, kid in enumerate(kids):
            _render_subtree(kid, "", i == len(kids) - 1, {root})

    return warning_text + "\n".join(lines)
