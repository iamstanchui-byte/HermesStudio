"""Simulate Linux box scenario: wrapper-config.json with empty profiles, orchestrator has 'super' role.

Verifies sync-config auto-adds missing roles.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

CFG_PATH = Path.home() / ".hermes-orchestrator" / "wrapper-config.json"
SECRET_PATH = Path.home() / ".hermes-orchestrator" / ".secret-linux-a-01"

# Backup current config
backup = CFG_PATH.read_text(encoding="utf-8") if CFG_PATH.exists() else None

try:
    # Set up Linux-like config (linux-a-01, empty profiles)
    linux_cfg = {
        "agent_id": "linux-a-01",
        "orchestrator_url": "http://localhost:8765",
        "secret_file": str(SECRET_PATH),
        "profiles": {},
    }
    CFG_PATH.write_text(json.dumps(linux_cfg, indent=2) + "\n", encoding="utf-8")
    print(f"--- before sync ---")
    print(CFG_PATH.read_text(encoding="utf-8"))

    # Run sync-config via the wheel-installed CLI
    venv_python = Path("test-wheel-venv/Scripts/python.exe")
    if not venv_python.exists():
        print("venv not found; run from test dir first")
        sys.exit(1)
    result = subprocess.run(
        [str(venv_python), "-m", "hermes_orch.agent_cli", "sync-config"],
        capture_output=True,
        text=True,
    )
    print("--- sync-config output ---")
    print(result.stdout)
    if result.returncode != 0:
        print("STDERR:", result.stderr)

    print("--- after sync ---")
    print(CFG_PATH.read_text(encoding="utf-8"))

finally:
    # Restore
    if backup:
        CFG_PATH.write_text(backup, encoding="utf-8")
        print("--- restored original config ---")
