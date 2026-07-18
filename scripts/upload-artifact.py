"""upload-artifact.py - upload a file to the artifact API.

PowerShell 5.x doesn't support -Form on Invoke-RestMethod, so we use httpx via Python.
Prints the artifact ID on stdout (single line) so PowerShell can capture it.

Usage:
    python upload-artifact.py --file <path> --task-id <t-xxx> --project-id <proj-xxx>
    # Optional:
    --url http://localhost:8765/api/artifacts/
"""
import argparse
import sys
from pathlib import Path

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8765/api/artifacts/")
    parser.add_argument("--file", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--project-id", required=True)
    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"ERROR: file not found: {file_path}", file=sys.stderr)
        return 1

    with open(file_path, "rb") as f:
        files = {"file": (file_path.name, f, "application/octet-stream")}
        data = {"task_id": args.task_id, "project_id": args.project_id}
        r = httpx.post(args.url, files=files, data=data)

    if r.status_code == 201:
        body = r.json()
        # Print artifact ID on stdout for easy capture
        print(body["id"])
        return 0
    else:
        print(f"ERROR: status={r.status_code} body={r.text}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
