"""Unit test for the workflow evidence builder — verify it includes
'Skills the source agent loaded' from tasks.result.skills_used.

This is the server-side (orchestrator) read path. The wrapper writes
skills_used to the result JSON, the orchestrator's _gather_workflow_evidence
reads it and adds a section to the LLM evidence.
"""
import sys
import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, r'C:\Project\minimax code\hermes-orchestrator\src')
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from hermes_orch.api.workflows import _gather_workflow_evidence


def make_db(tasks):
    """Build a mock db.fetchall that returns the given task list."""
    db = MagicMock()
    async def fetchall(sql, params):
        return tasks
    db.fetchall = fetchall
    return db


def main():
    proj = {
        'id': 'proj-test',
        'name': 'test project',
        'state': 'completed',
        'goal': 'Do weather stuff',
        'coordinator_role': None,
        'max_iterations': 0,
    }
    pdir = Path('/tmp/nonexistent')  # not used for skills extraction anymore

    print('[1] No tasks — no skills section')
    db = make_db([])
    result = asyncio.run(_gather_workflow_evidence(db, pdir, 'proj-test', proj))
    assert 'Skills the source agent loaded' not in result, 'should not include skills section'
    print('  PASS  no skills section when no tasks')

    print()
    print('[2] Tasks without skills_used — no skills section')
    db = make_db([
        {'name': 't1', 'agent_role': 'win', 'action': 'do_x', 'status': 'completed',
         'depends_on': '[]', 'output_path': 'x.md', 'params': '{}', 'result': '{"summary": "x"}'},
    ])
    result = asyncio.run(_gather_workflow_evidence(db, pdir, 'proj-test', proj))
    assert 'Skills the source agent loaded' not in result, 'should not include skills section'
    print('  PASS  no skills section when tasks have no skills_used')

    print()
    print('[3] Tasks with skills_used — section appears, names listed')
    db = make_db([
        {'name': 't1', 'agent_role': 'win-agent02', 'action': 'do_x', 'status': 'completed',
         'depends_on': '[]', 'output_path': 'x.md', 'params': '{}',
         'result': json.dumps({'summary': 'x', 'skills_used': ['hk-weather-forecast', 'gdrive-write']})},
    ])
    result = asyncio.run(_gather_workflow_evidence(db, pdir, 'proj-test', proj))
    assert 'Skills the source agent loaded' in result, 'should include skills section'
    assert '`hk-weather-forecast`' in result, 'should list hk-weather-forecast'
    assert '`gdrive-write`' in result, 'should list gdrive-write'
    print('  PASS  section includes both skill names')

    print()
    print('[4] Multiple tasks, dedupe skills across tasks')
    db = make_db([
        {'name': 't1', 'agent_role': 'win-agent02', 'action': 'do_x', 'status': 'completed',
         'depends_on': '[]', 'output_path': 'x.md', 'params': '{}',
         'result': json.dumps({'skills_used': ['hk-weather-forecast']})},
        {'name': 't2', 'agent_role': 'win-agent02', 'action': 'do_y', 'status': 'completed',
         'depends_on': '[]', 'output_path': 'y.md', 'params': '{}',
         'result': json.dumps({'skills_used': ['gdrive-write', 'hk-weather-forecast']})},  # duplicate
    ])
    result = asyncio.run(_gather_workflow_evidence(db, pdir, 'proj-test', proj))
    # Count occurrences of each skill in the skills section
    section_start = result.index('Skills the source agent loaded')
    section = result[section_start:]
    assert section.count('`hk-weather-forecast`') == 1, 'dedupe hk-weather-forecast'
    assert section.count('`gdrive-write`') == 1, 'dedupe gdrive-write'
    print('  PASS  dedupes skills across multiple tasks')

    print()
    print('[5] Failed tasks do not contribute skills (only completed)')
    db = make_db([
        {'name': 't1', 'agent_role': 'win', 'action': 'do_x', 'status': 'failed',
         'depends_on': '[]', 'output_path': 'x.md', 'params': '{}',
         'result': json.dumps({'skills_used': ['failed-skill-should-be-ignored']})},
        {'name': 't2', 'agent_role': 'win', 'action': 'do_y', 'status': 'completed',
         'depends_on': '[]', 'output_path': 'y.md', 'params': '{}',
         'result': json.dumps({'skills_used': ['good-skill']})},
    ])
    result = asyncio.run(_gather_workflow_evidence(db, pdir, 'proj-test', proj))
    assert 'failed-skill' not in result, 'failed task skills should not appear'
    assert '`good-skill`' in result, 'completed task skills should appear'
    print('  PASS  failed tasks do not contribute to skills_used')

    print()
    print('=== ALL PASS ===')


if __name__ == '__main__':
    main()
