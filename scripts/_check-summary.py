"""Inspect task.completed summary payloads from L1 trace."""
import json
trace = r'C:\Project\minimax code\hermes-project\proj-5e899243\trace.jsonl'
with open(trace, encoding='utf-8') as f:
    for line in f:
        ev = json.loads(line)
        if ev.get('event_type') == 'task.completed':
            p = ev.get('payload', {})
            print(f"--- {ev.get('ts', '?')} {ev.get('task_id', '?')} ---")
            print('summary (first 500):', repr((p.get('summary') or '')[:500]))
            print()
