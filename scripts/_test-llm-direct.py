"""Direct test of LLM planner to see response usage."""
import asyncio
import json
from hermes_orch.core.planner import Planner

async def main():
    # Read config
    import yaml
    with open(r"C:\Users\stanley\.hermes-orchestrator\config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    print(f"config LLM section: {cfg.get('llm', {})}")
    p = Planner(cfg)
    print(f"planner mock: {p.mock}, model: {p.model}, max_tokens: see body in plan()")

    # Call the LLM directly via plan()
    try:
        plan = await p.plan(
            "Test goal: fetch XAUUSD correlation with CPI/PPI",
            available_roles=["super", "win-agent01"],
            role_skills={"super": ["trading-data"], "win-agent01": ["mt5"]},
        )
        print(f"  plan: {plan}")
    except Exception as e:
        print(f"  plan failed: {e}")

asyncio.run(main())
