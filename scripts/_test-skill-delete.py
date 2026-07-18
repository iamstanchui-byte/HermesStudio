"""Test skill apply+delete on a fresh skill name, with longer wait + verify file gone."""
import json
import os
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
SKILLS_DIR = os.path.expandvars(
    r"%LOCALAPPDATA%\hermes\profiles\win-agent01\skills"
)


def get(path: str):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=5).read())


def post(path: str, body: dict):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


def delete(path: str):
    return json.loads(
        urllib.request.urlopen(
            urllib.request.Request(BASE + path, method="DELETE"), timeout=5
        ).read()
    )


name = "smoke-test-1"
file_path = os.path.join(SKILLS_DIR, name + ".md")
print(f"=== creating skill '{name}' ===")
created = post(
    f"/api/agents/win-local-1/profiles/win-agent01/skills",
    {"name": name, "content": "# smoke test\n"},
)
print(f"  pending config id={created['id']} file={created['file_path']}")

print("=== waiting 8s for wrapper to apply ===")
time.sleep(8)
exists = os.path.exists(file_path)
size = os.path.getsize(file_path) if exists else None
print(f"  file exists={exists} size={size}")
print(f"  list: {get('/api/agents/win-local-1/profiles/win-agent01/skills')}")

print("=== deleting skill ===")
deleted = delete(f"/api/agents/win-local-1/profiles/win-agent01/skills/{name}")
print(f"  delete-config id={deleted['id']} status={deleted['status']}")

print("=== waiting 8s for wrapper to remove file ===")
time.sleep(8)
exists_after = os.path.exists(file_path)
print(f"  file exists after delete = {exists_after}")
print(f"  list: {get('/api/agents/win-local-1/profiles/win-agent01/skills')}")
