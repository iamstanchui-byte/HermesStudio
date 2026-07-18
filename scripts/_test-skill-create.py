"""Quick smoke test for skill endpoints. Not part of the suite."""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8765"


def get(path: str) -> dict | list | None:
    r = urllib.request.urlopen(BASE + path, timeout=5)
    return json.loads(r.read())


def post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    r = urllib.request.urlopen(req, timeout=5)
    return json.loads(r.read())


def delete(path: str) -> dict:
    req = urllib.request.Request(BASE + path, method="DELETE")
    r = urllib.request.urlopen(req, timeout=5)
    return json.loads(r.read())


print("=== before ===")
print(json.dumps(get("/api/agents/win-local-1/profiles/win-agent01/skills"), indent=2))

print("\n=== create skill 'weather' ===")
created = post(
    "/api/agents/win-local-1/profiles/win-agent01/skills",
    {"name": "weather", "content": "# Weather API\n\nUse curl wttr.in\n"},
)
print(f"created config id={created['id']} status={created['status']} file={created['file_path']}")

print("\n=== get single skill ===")
got = get("/api/agents/win-local-1/profiles/win-agent01/skills/weather")
print(f"name={got['name']} status={got['status']} size={got['size']}")
print(f"content: {got['content']!r}")

print("\n=== list after create ===")
print(json.dumps(get("/api/agents/win-local-1/profiles/win-agent01/skills"), indent=2))

print("\n=== wait 6s for wrapper to apply, then list again ===")
import time
time.sleep(6)
print(json.dumps(get("/api/agents/win-local-1/profiles/win-agent01/skills"), indent=2))

print("\n=== delete skill ===")
deleted = delete("/api/agents/win-local-1/profiles/win-agent01/skills/weather")
print(f"created delete-config id={deleted['id']} status={deleted['status']}")

print("\n=== wait 6s for wrapper to remove file, then list again ===")
time.sleep(6)
print(json.dumps(get("/api/agents/win-local-1/profiles/win-agent01/skills"), indent=2))
