"""Check skills on linux super agent."""
import json
import urllib.request

r = urllib.request.urlopen("http://127.0.0.1:8765/api/agents/linux-a-01/profiles/super/skills", timeout=10)
data = json.loads(r.read())
print(f"super agent skills: {len(data)}")
for s in data:
    print(f"  {s['name']:<30} status={s['status']:<10} size={s['size']}")
