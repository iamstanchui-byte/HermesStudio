"""Repro the 500 error from the template."""
import asyncio
from jinja2 import Environment, FileSystemLoader, select_autoescape
from hermes_orch.api.dashboard import _load_agents
from hermes_orch.db import Database
env = Environment(loader=FileSystemLoader('src/hermes_orch/templates'), autoescape=select_autoescape(['html']))
tpl = env.get_template('agents.html')
async def main():
    db = Database(r'C:\Users\stanley\.hermes-orchestrator\hermes-orch.db')
    await db.connect()
    try:
        agents = await _load_agents(db)
        try:
            html = tpl.render(agents=agents, active_page='agents', now_iso='2026-07-19')
            print(f'OK, {len(html)} bytes')
        except Exception as e:
            import traceback; traceback.print_exc()
    finally:
        await db.close()
asyncio.run(main())
