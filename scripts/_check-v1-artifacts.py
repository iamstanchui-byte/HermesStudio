"""Check v1 task artifacts (gold data, report)."""
import json
import urllib.request

for tid in ["t-7e7f055e", "t-36508b93", "t-e9f9df5d", "t-9a2feeb2"]:
    r = urllib.request.urlopen(f"http://127.0.0.1:8765/api/tasks/{tid}", timeout=5)
    t = json.loads(r.read())
    print(f"\n--- {tid} {t['name']} ---")
    print(f"  output_path: {t.get('output_path')!r}")
    arts = t.get("artifacts") or []
    print(f"  artifacts: {arts}")
    res = t.get("result") or {}
    print(f"  result keys: {list(res.keys())}")
    if "summary" in res:
        print(f"  summary[:200]: {res['summary'][:200]!r}")
