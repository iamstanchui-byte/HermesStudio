"""One-shot migration: copy existing wrapper secret files into the orchestrator DB.

Background: before v1.6, the orchestrator only stored SHA-256(secret)
in the `secret_hash` column. v1.6 introduces HMAC, which needs the
plaintext secret server-side to verify signatures. The wrapper
self-bootstraps by POSTing its secret to
`/api/agents/{id}/secret` on every start, but for EXISTING agents
that the operator hasn't restarted yet, this script is the
admin-side path to populate hmac_secret without touching the agent.

Usage (PowerShell):
    # For each known agent, read the .secret file and POST it:
    $secret = Get-Content ~\.hermes-orchestrator\.secret-win-local-1 -Raw
    curl -X POST http://127.0.0.1:8765/api/agents/win-local-1/secret `
         -H "Content-Type: application/json" `
         -d (@{secret = $secret.Trim()} | ConvertTo-Json)

This script does the same, iterating over all agents in
wrapper-config.json and pushing their secrets.

Idempotent: re-running with the same secret is a no-op (returns 200
"already_set"). A mismatched secret returns 409 (operator must
investigate before running again).
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


ORCHESTRATOR_URL = "http://127.0.0.1:8765"
WRAPPER_CONFIG = Path.home() / ".hermes-orchestrator" / "wrapper-config.json"


def push_secret(orchestrator_url: str, agent_id: str, secret: str) -> tuple[int, str]:
    url = f"{orchestrator_url}/api/agents/{agent_id}/secret"
    body = json.dumps({"secret": secret}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Agent-Id": agent_id,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def main() -> int:
    orchestrator_url = ORCHESTRATOR_URL
    if len(sys.argv) > 1:
        orchestrator_url = sys.argv[1].rstrip("/")
    if not WRAPPER_CONFIG.exists():
        print(f"No wrapper config at {WRAPPER_CONFIG}; nothing to do.")
        return 0
    cfg = json.loads(WRAPPER_CONFIG.read_text(encoding="utf-8"))
    agent_id = cfg.get("agent_id")
    secret_path = Path(cfg.get("secret_file", "")).expanduser()
    if not agent_id or not secret_path:
        print("wrapper-config.json missing agent_id or secret_file")
        return 1
    if not secret_path.exists():
        print(f"Secret file not found: {secret_path}")
        return 1
    secret = secret_path.read_text(encoding="utf-8").strip()
    if not secret:
        print(f"Secret file {secret_path} is empty")
        return 1
    print(f"Pushing secret for agent {agent_id} (len={len(secret)}) to {orchestrator_url}...")
    status, body = push_secret(orchestrator_url, agent_id, secret)
    print(f"  -> {status} {body[:200]}")
    if status == 409:
        print(
            "ERROR: conflict — orchestrator has a different hmac_secret for this "
            "agent. Re-register or manual DB update needed."
        )
        return 2
    if status not in (200, 201):
        print(f"ERROR: unexpected status {status}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
