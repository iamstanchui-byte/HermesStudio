"""Read telegram-direct skill content."""
import json
import urllib.request

r = urllib.request.urlopen("http://127.0.0.1:8765/api/agents/linux-a-01/profiles/super/skills/telegram-direct", timeout=10)
data = json.loads(r.read())
print(f"file_path: {data['file_path']}")
print(f"size: {data['size']}")
print(f"content:")
print(data['content'])
