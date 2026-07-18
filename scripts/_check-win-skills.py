"""Check skill counts per profile."""
import json
import urllib.request

for profile in ("win-agent01", "win-agent02"):
    r = urllib.request.urlopen(f"http://127.0.0.1:8765/api/agents/win-local-1/profiles/{profile}/skills", timeout=5)
    skills = json.loads(r.read())
    print(f"{profile}: {len(skills)} skills")
    for s in skills:
        print(f"  {s['name']:<30} {s['size']} bytes")
    print()
