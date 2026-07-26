"""Seed an example skill sidecar (one-time setup, idempotent).

This script demonstrates the SKILL.schema.yaml sidecar format by
attaching a sidecar to an existing skill. The sidecar is the
canonical "this skill is deterministic" example that the
Planner LLM uses as a reference for what the schema looks like.

After running, you can verify it via:
  curl http://127.0.0.1:8765/api/objects/skills/<profile>/<name> | jq .schema

To remove the example: run scripts/_reset-test-state.py
"""
import hashlib
import sqlite3
import sys
import uuid
from pathlib import Path

# The smoke-test workflow uses these skills on profile
# 034e6614-f394-4950-83e5-357132b06d66 (win-agent01, which has
# 'Google Drive' + 'mt5' capabilities). Pick the smallest one
# to minimize the sidecar's footprint.
PROFILE_ID = "034e6614-f394-4950-83e5-357132b06d66"
SKILL_NAME = "apple/apple-notes"
FILE_PATH = f"skills/{SKILL_NAME}/SKILL.schema.yaml"

SIDE_CAR_YAML = """\
# SKILL.schema.yaml — declares this skill's contract for the orchestrator.
# Sidecar is OPTIONAL; without it, the skill is treated as
# llm_required=true (safe default). With it, the LLM planner can
# route tasks to this skill without re-reading the body.
input_schema:
  note_id: string           # Apple Notes record UUID
  format: string            # 'plain' | 'markdown' | 'html'
output_schema:
  content: string           # extracted text body
  modified_at: string       # ISO timestamp from the source
deterministic: true         # script-only, no LLM
llm_required: false         # matches deterministic=true
requires_capabilities:
  - Apple Notes             # capability this skill needs from the agent
"""

DB = Path.home() / ".hermes-orchestrator" / "hermes-orch.db"


def main() -> int:
    cfg_id = str(uuid.uuid4())
    sha = hashlib.sha256(SIDE_CAR_YAML.encode("utf-8")).hexdigest()
    with sqlite3.connect(str(DB)) as conn:
        existing = conn.execute(
            "SELECT id FROM profile_configs WHERE profile_id = ? AND file_path = ?",
            (PROFILE_ID, FILE_PATH),
        ).fetchone()
        if existing:
            print(f"sidecar already exists at {FILE_PATH} (id={existing[0]}); skipping")
            return 0
        conn.execute(
            "INSERT INTO profile_configs "
            "(id, profile_id, file_path, desired_sha256, desired_content, "
            " status, created_at, applied_at) "
            "VALUES (?, ?, ?, ?, ?, 'applied', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (cfg_id, PROFILE_ID, FILE_PATH, sha, SIDE_CAR_YAML),
        )
        conn.commit()
    print(f"inserted sidecar id={cfg_id}")
    print(f"verify: GET /api/objects/skills/{PROFILE_ID}/{SKILL_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
