"""Backfill skills_used into tasks.result JSON for tasks that ran BEFORE
the wrapper was redeployed with the Stage 1.5 multi-skill fix.

Run from a dev/admin PowerShell (writes to the live DB):

    .venv\Scripts\python.exe scripts\\_backfill-skills-used.py [--dry-run]

Parses every hermes.<tid>.stdout.log in the agent-side cache, extracts
the `📚 skill <name> <duration>` markers, and writes the unique list
into tasks.result.skills_used. Backward compat: tasks without a
matching transcript are skipped.

Why a separate script:
  The Stage 1.5 multi-skill fix (commit 45348d3) requires the wrapper
  to POST skills_used in /result. Old tasks ran with the old wrapper
  and have skills_used=[]. Without backfill, promote-to-workflow on
  a project containing only old tasks sees an empty skills list and
  the LLM synthesizes a step with only the "most prominent" skill
  (typically the data-fetch one), silently dropping upload skills.
"""
import sys
import re
import json
import sqlite3
import argparse
import glob
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERMES_PROFILES = Path(r"C:\Users\stanley\AppData\Local\hermes\profiles")
DB_PATH = r"C:\Users\stanley\.hermes-orchestrator\hermes-orch.db"
PATTERN = re.compile(r"📚\s+skill\s+(\S+)\s+[\d.]+s")


def parse_transcript(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in PATTERN.finditer(text):
        name = m.group(1).strip()
        if name and name not in seen_set:
            seen_set.add(name)
            seen.append(name)
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help="Show what would change but don't write to DB")
    args = ap.parse_args()

    # Collect all transcripts: <profile>/.orch-cache/<pid>/hermes.<tid>.stdout.log
    if not HERMES_PROFILES.exists():
        print(f"hermes profiles dir not found: {HERMES_PROFILES}")
        sys.exit(1)
    transcript_map: dict[str, str] = {}  # task_id -> transcript path
    for stdout in HERMES_PROFILES.glob("*/.orch-cache/*/hermes.*.stdout.log"):
        # extract task_id from filename: hermes.<tid>.stdout.log
        m = re.match(r"hermes\.(t-[a-f0-9]+)\.stdout\.log", stdout.name)
        if not m:
            continue
        tid = m.group(1)
        transcript_map[tid] = str(stdout)
    print(f"Found {len(transcript_map)} hermes transcripts across all profiles")

    # Connect to DB
    conn = sqlite3.connect(DB_PATH)
    # Get all completed tasks
    rows = conn.execute(
        "SELECT id, result FROM tasks WHERE status = 'completed' AND result IS NOT NULL"
    ).fetchall()
    print(f"DB has {len(rows)} completed tasks with result")

    updated = 0
    skipped_no_transcript = 0
    skipped_already_set = 0
    for tid, result_str in rows:
        try:
            result = json.loads(result_str) if result_str else {}
        except Exception:
            continue
        existing = result.get('skills_used') or []
        if existing:
            skipped_already_set += 1
            continue
        if tid not in transcript_map:
            skipped_no_transcript += 1
            continue
        # Parse transcript
        skills = parse_transcript(Path(transcript_map[tid]))
        if not skills:
            skipped_no_transcript += 1
            continue
        # Apply
        result['skills_used'] = skills
        new_result = json.dumps(result, ensure_ascii=False)
        if args.dry_run:
            print(f"  [DRY] {tid}: would set skills_used={skills}")
        else:
            conn.execute(
                "UPDATE tasks SET result = ?, updated_at = ? WHERE id = ?",
                (new_result, "2026-07-23T22:11:00+08:00", tid),
            )
        updated += 1
    if not args.dry_run:
        conn.commit()
    conn.close()

    print()
    print(f"Updated:        {updated}")
    print(f"Already set:    {skipped_already_set}")
    print(f"No transcript:  {skipped_no_transcript}")
    if args.dry_run:
        print("(DRY RUN — no changes written)")


if __name__ == "__main__":
    main()
