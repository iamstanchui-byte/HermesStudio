"""Update wrapper-config.json with the user's real agent profiles.

win-local-1 has 2 profiles: win-agent01, win-agent02.
This is the wrapper daemon's manifest — it tells the daemon which profile
to use for which role.
"""
import json
from pathlib import Path

CFG_PATH = Path.home() / ".hermes-orchestrator" / "wrapper-config.json"

cfg = {
    "agent_id": "win-local-1",
    "orchestrator_url": "http://localhost:8765",
    "secret_file": str(Path.home() / ".hermes-orchestrator" / ".secret-win-local-1"),
    "profiles": {
        # Map each role to a hermes profile root.
        # These are the profiles the user added in the dashboard.
        "win-agent01": {
            "root": "C:/Users/stanley/AppData/Local/hermes/profiles/win-agent01"
        },
        "win-agent02": {
            "root": "C:/Users/stanley/AppData/Local/hermes/profiles/win-agent02"
        },
    },
}

CFG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
print(f"wrote {CFG_PATH}")
print()
print(json.dumps(cfg, indent=2))
