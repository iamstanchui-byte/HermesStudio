"""3-tier project memory (Phase 1: L1 trace + L2 facts).

See docs/design/3-tier-memory.md for full design.

This module provides:
- L1: append-only JSONL event trace, mirrored from audit_log
- L2: curated facts.md, auto-appended by supervisor + human-editable
- Read APIs for wrapper prompt injection + dashboard

L3 (LLM-synthesized state) is Phase 2, not implemented here.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from hermes_orch.utils import now_iso as _now_iso

log = logging.getLogger("hermes_orch.core.memory")

# Size limits (bytes)
FACTS_MAX_BYTES = 8 * 1024          # 8KB cap per facts.md; auto-archive beyond
FACTS_INJECT_MAX_BYTES = 4 * 1024   # 4KB injected into task prompts

# Default memory root (orchestrator-level)
DEFAULT_MEMORY_ROOT = Path.home() / ".hermes-orchestrator" / "memory"

# Project file names
TRACE_FILENAME = "trace.jsonl"
FACTS_FILENAME = "facts.md"
ARCHIVE_FILENAME = "facts_archive.md"
STATE_FILENAME = "state.md"
STATE_ARCHIVE_DIRNAME = "state_archive"


def _project_dir(project_id: str, projects_root: Path) -> Path:
    """Path to the project's directory under projects storage root."""
    return projects_root / project_id


def _project_trace_path(project_id: str, projects_root: Path) -> Path:
    return _project_dir(project_id, projects_root) / TRACE_FILENAME


def _project_facts_path(project_id: str, projects_root: Path) -> Path:
    return _project_dir(project_id, projects_root) / FACTS_FILENAME


def _project_archive_path(project_id: str, projects_root: Path) -> Path:
    return _project_dir(project_id, projects_root) / ARCHIVE_FILENAME


def _project_state_path(project_id: str, projects_root: Path) -> Path:
    return _project_dir(project_id, projects_root) / STATE_FILENAME


def _project_state_archive_dir(project_id: str, projects_root: Path) -> Path:
    return _project_dir(project_id, projects_root) / STATE_ARCHIVE_DIRNAME


# Standard section headers in facts.md (canonical ordering)
FACTS_SECTIONS = [
    "## Goal",
    "## Plan History",
    "## Task Results",
    "## Key Findings",
    "## Files (artifacts)",
    "## Coord Verdicts",
    "## Human Notes",
]


class MemoryWriter:
    """Writes L1 (trace) and L2 (facts) events to project memory.

    Thread-safe via internal lock. L1 is append-only; L2 is
    structured Markdown with each fact citing an L1 event_id.

    Usage:
        writer = MemoryWriter(projects_root=Path("/path/to/projects"))
        writer.append_event_L1(
            event_type="task.completed",
            actor="agent:linux-a-01",
            project_id="proj-abc",
            task_id="t-xyz",
            payload={"name": "fetch teams"},
        )
        writer.append_fact_L2(
            project_id="proj-abc",
            section="## Task Results",
            fact_text="[t-xyz] fetch teams -- Found Spain vs Argentina",
            cite_id="task.completed@2026-07-19T02:12",
        )
    """

    def __init__(self, projects_root: Path, memory_root: Path | None = None):
        self.projects_root = Path(projects_root)
        self.memory_root = Path(memory_root or DEFAULT_MEMORY_ROOT)
        self.memory_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ===== L1 (trace.jsonl) =====

    def append_event_L1(
        self,
        *,
        event_type: str,
        actor: str,
        project_id: str | None = None,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Mirror an audit event to JSONL trace (per-project + global).

        Both files are append-only. Best-effort: a write failure is
        logged but does not raise (audit_log is the source of truth;
        L1 is a derived view).
        """
        entry = {
            "ts": _now_iso(),
            "event_type": event_type,
            "actor": actor,
            "project_id": project_id,
            "task_id": task_id,
            "payload": payload or {},
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            # Per-project trace
            if project_id:
                try:
                    p = _project_trace_path(project_id, self.projects_root)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    with open(p, "a", encoding="utf-8") as f:
                        f.write(line)
                except Exception as e:
                    log.warning(f"L1 trace write failed (project={project_id}): {e}")
            # Global trace
            try:
                g = self.memory_root / TRACE_FILENAME
                with open(g, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as e:
                log.warning(f"L1 trace write failed (global): {e}")

    def read_trace(
        self,
        project_id: str,
        since: str | None = None,
        event_type: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Read trace.jsonl entries, optionally filtered."""
        p = _project_trace_path(project_id, self.projects_root)
        if not p.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if since and e.get("ts", "") < since:
                        continue
                    if event_type and e.get("event_type") != event_type:
                        continue
                    entries.append(e)
        except Exception as e:
            log.warning(f"L1 trace read failed: {e}")
        # Apply limit (most recent N)
        if len(entries) > limit:
            entries = entries[-limit:]
        return entries

    # ===== L2 (facts.md) =====

    def init_facts_file(self, project_id: str, project_name: str = "") -> None:
        """Bootstrap facts.md with header + Goal placeholder.

        Idempotent: if facts.md exists, do nothing.
        """
        p = _project_facts_path(project_id, self.projects_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            return
        title = project_name or project_id
        sections = "\n".join([f"{s}\n" for s in FACTS_SECTIONS])
        template = (
            f"# Project Facts: {title}\n\n"
            "> Auto-curated from L1 (trace.jsonl). Human-editable.\n"
            "> Each fact cites a L1 event_id for traceability.\n\n"
            f"{sections}\n"
        )
        try:
            p.write_text(template, encoding="utf-8")
        except Exception as e:
            log.warning(f"L2 facts init failed: {e}")

    def append_fact_L2(
        self,
        project_id: str,
        section: str,
        fact_text: str,
        cite_id: str,
    ) -> None:
        """Append a fact under a section in facts.md.

        Args:
            project_id: project
            section: one of FACTS_SECTIONS, e.g. "## Task Results"
            fact_text: e.g. "[t-abc] Find teams -- Spain vs Argentina"
            cite_id: L1 event_id, e.g. "task.completed@2026-07-19T02:12"

        The new line is inserted just after the section header (or at
        the end of the file if the section doesn't exist yet). When
        the file exceeds FACTS_MAX_BYTES, the oldest entries are
        moved to facts_archive.md.
        """
        p = _project_facts_path(project_id, self.projects_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        new_line = f"- [cite:{cite_id}] {fact_text}\n"

        with self._lock:
            try:
                content = p.read_text(encoding="utf-8") if p.exists() else ""
            except Exception as e:
                log.warning(f"L2 read failed: {e}")
                return
            content = self._insert_under_section(content, section, new_line)
            content = self._enforce_size_cap(project_id, content)
            try:
                p.write_text(content, encoding="utf-8")
            except Exception as e:
                log.warning(f"L2 write failed: {e}")

    def _insert_under_section(self, content: str, section: str, new_line: str) -> str:
        """Insert new_line under the given section header.

        - If section exists, append at the end of its body (just before
          the next ## or # header).
        - If section doesn't exist, append at the end with the header.
        """
        if f"\n{section}\n" in content:
            # Section exists, find its body and append at end
            head, tail = content.split(f"\n{section}\n", 1)
            # Find next section start
            next_idx = len(tail)
            for marker in ("\n## ", "\n# "):
                idx = tail.find(marker)
                if idx >= 0 and idx < next_idx:
                    next_idx = idx
            section_body = tail[:next_idx]
            rest = tail[next_idx:]
            return f"{head}\n{section}\n{section_body}{new_line}{rest}"
        else:
            # Section missing, append at end
            if not content.endswith("\n"):
                content += "\n"
            return f"{content}\n{section}\n{new_line}"

    def _enforce_size_cap(self, project_id: str, content: str) -> str:
        """If content exceeds FACTS_MAX_BYTES, archive older entries."""
        size = len(content.encode("utf-8"))
        if size <= FACTS_MAX_BYTES:
            return content
        # Find the first ## section after the header
        lines = content.split("\n")
        first_section_idx = None
        for i, line in enumerate(lines):
            if line.startswith("## ") and i > 0:
                first_section_idx = i
                break
        if first_section_idx is None:
            # No sections, just truncate
            return content.encode("utf-8")[:FACTS_MAX_BYTES].decode("utf-8", errors="replace")
        header = "\n".join(lines[:first_section_idx])
        body = "\n".join(lines[first_section_idx:])
        # Keep header + last 6KB of body
        header_bytes = header.encode("utf-8")
        keep_body_bytes = FACTS_MAX_BYTES - len(header_bytes)
        if keep_body_bytes < 1024:
            keep_body_bytes = 1024
        body_bytes = body.encode("utf-8")
        if len(body_bytes) > keep_body_bytes:
            truncated = body_bytes[-keep_body_bytes:]
            # Try to find next \n for cleaner cut
            nl = truncated.find(b"\n")
            if 0 < nl < 200:
                truncated = truncated[nl + 1:]
            body = truncated.decode("utf-8", errors="replace")
        # Append dropped content to archive
        try:
            archive = _project_archive_path(project_id, self.projects_root)
            archive.parent.mkdir(parents=True, exist_ok=True)
            with open(archive, "a", encoding="utf-8") as f:
                f.write(f"\n\n# Archive snapshot @ {_now_iso()}\n")
                # The dropped portion: full body - kept body
                full_body_bytes = body_bytes
                dropped_bytes = full_body_bytes[: len(full_body_bytes) - keep_body_bytes]
                f.write(dropped_bytes.decode("utf-8", errors="replace"))
        except Exception as e:
            log.warning(f"failed to write facts_archive: {e}")
        return f"{header}\n{body}"

    def read_facts_tail(
        self, project_id: str, max_bytes: int = FACTS_INJECT_MAX_BYTES
    ) -> str | None:
        """Read the tail of facts.md for prompt injection.

        Returns None if the file doesn't exist or can't be read.
        If the file is larger than max_bytes, returns the last
        max_bytes with a truncation marker.
        """
        p = _project_facts_path(project_id, self.projects_root)
        if not p.exists():
            return None
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            return None
        if len(text.encode("utf-8")) > max_bytes:
            tail = text.encode("utf-8")[-max_bytes:].decode("utf-8", errors="replace")
            return "[earlier entries truncated]\n" + tail
        return text

    def read_facts_full(self, project_id: str, section: str | None = None) -> str | None:
        """Read the full facts.md (no truncation), or a single section's body.

        If `section` is provided (e.g. "## Files (artifacts)"), returns
        just the body of that section, with the heading stripped and
        surrounding blank lines trimmed. Returns None if section missing.
        """
        p = _project_facts_path(project_id, self.projects_root)
        if not p.exists():
            return None
        try:
            full = p.read_text(encoding="utf-8")
        except Exception:
            return None
        if not section:
            return full
        if f"{section}\n" not in full and not full.startswith(section):
            return None
        # Split at the section heading
        after = full.split(section, 1)[1]
        # Strip the section's trailing block: up to next "## " heading
        import re as _re
        m = _re.search(r"^## ", after, flags=_re.MULTILINE)
        if m:
            after = after[: m.start()]
        return after.strip()

    # ===== L3 (state.md) — Phase 2 =====

    def read_state(self, project_id: str) -> str | None:
        """Read state.md. Returns None if missing/unreadable."""
        p = _project_state_path(project_id, self.projects_root)
        if not p.exists():
            return None
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return None

    def read_state_tail(
        self, project_id: str, max_bytes: int = 2048
    ) -> str | None:
        """Read state.md, truncating to max_bytes for prompt injection.

        Returns None if state.md doesn't exist. Truncation is byte-aware
        to avoid breaking multi-byte UTF-8 (e.g. CJK).
        """
        text = self.read_state(project_id)
        if text is None:
            return None
        encoded = text.encode("utf-8")
        if len(encoded) > max_bytes:
            head = encoded[:max_bytes].decode("utf-8", errors="replace")
            return head + "\n[…state truncated…]"
        return text

    def write_state(self, project_id: str, content: str) -> bool:
        """Write state.md, archiving the previous version first.

        Archives to `<project_dir>/state_archive/<timestamp>.md` so the
        diff between iterations is visible (each regen leaves a
        breadcrumb). Returns True on success.
        """
        p = _project_state_path(project_id, self.projects_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime as _dt
        try:
            with self._lock:
                if p.exists():
                    try:
                        archive_dir = p.parent / STATE_ARCHIVE_DIRNAME
                        archive_dir.mkdir(parents=True, exist_ok=True)
                        ts = _dt.now().strftime("%Y%m%dT%H%M%S")
                        old = p.read_text(encoding="utf-8")
                        (archive_dir / f"{ts}.md").write_text(
                            old, encoding="utf-8"
                        )
                    except Exception as e:
                        # Archive failure is non-fatal
                        log.warning(f"L3 state archive failed: {e}")
                p.write_text(content, encoding="utf-8")
            return True
        except Exception as e:
            log.warning(f"L3 state write failed (project={project_id}): {e}")
            return False


# ===== Singleton =====

_writer: MemoryWriter | None = None
_writer_lock = threading.Lock()


def get_memory_writer() -> MemoryWriter:
    """Get the process-wide MemoryWriter, lazily initialized from config."""
    global _writer
    with _writer_lock:
        if _writer is None:
            from hermes_orch.config import load_config
            cfg = load_config()
            projects_root_str = (cfg.get("projects") or {}).get(
                "storage_root", ""
            )
            if projects_root_str:
                projects_root = Path(projects_root_str)
            else:
                projects_root = Path.home() / "hermes-orchestrator" / "projects"
            _writer = MemoryWriter(projects_root=projects_root)
        return _writer


def reset_memory_writer() -> None:
    """For tests: drop the cached singleton."""
    global _writer
    with _writer_lock:
        _writer = None
