"""Replace datetime.now(timezone.utc).isoformat() with local-time version across files."""
from pathlib import Path

files = [
    r"C:\Project\minimax code\hermes-orchestrator\src\hermes_orch\api\agents.py",
    r"C:\Project\minimax code\hermes-orchestrator\src\hermes_orch\api\artifacts.py",
    r"C:\Project\minimax code\hermes-orchestrator\src\hermes_orch\api\tasks.py",
    r"C:\Project\minimax code\hermes-orchestrator\src\hermes_orch\core\supervisor.py",
]
for f in files:
    p = Path(f)
    content = p.read_text(encoding="utf-8")
    new_content = content.replace(
        "datetime.now(timezone.utc).isoformat()",
        "datetime.now().astimezone().isoformat()",
    )
    n = content.count("datetime.now(timezone.utc).isoformat()")
    if n > 0:
        p.write_text(new_content, encoding="utf-8")
        print(f"{f}: replaced {n}")
    else:
        print(f"{f}: 0 matches (skip)")

# verify
import subprocess
result = subprocess.run(
    ["python", "-c", "import hermes_orch.api.agents, hermes_orch.api.tasks, hermes_orch.api.artifacts, hermes_orch.core.supervisor; print('OK')"],
    cwd=r"C:\Project\minimax code\hermes-orchestrator",
    env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    capture_output=True,
    text=True,
)
print("import test:", result.stdout.strip(), result.stderr.strip()[:200] if result.stderr else "")
