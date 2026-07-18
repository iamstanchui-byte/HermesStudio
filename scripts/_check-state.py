"""Check current state of LLM/TG config and win-local-1 profiles."""
import asyncio
import httpx


async def main():
    async with httpx.AsyncClient() as client:
        llm = (await client.get("http://localhost:8765/api/settings/llm")).json()
        tg = (await client.get("http://localhost:8765/api/settings/telegram")).json()
        agents = (await client.get("http://localhost:8765/api/agents/")).json()
        win = next((a for a in agents["agents"] if a["id"] == "win-local-1"), None)

        print("--- LLM (in-memory) ---")
        print(f"  api_key_set: {llm['api_key_set']}  last4: {llm['api_key_last4']}")
        print(f"  base_url: {llm['base_url']}")
        print(f"  model: {llm['model']}")
        print(f"  mock: {llm['mock']}")
        print()
        print("--- Telegram (in-memory) ---")
        print(f"  enabled: {tg['enabled']}  ready: {tg['ready']}")
        print(f"  token last4: {tg['bot_token_last4']}")
        print(f"  chat_id: {tg['chat_id']}")
        print()
        print("--- win-local-1 ---")
        if win:
            print(f"  id: {win['id']}  status: {win['status']}  ip: {win['ip']}")
            print(f"  profiles ({len(win['profiles'])}):")
            for p in win["profiles"]:
                print(f"    - {p['name']:20s}  status={p['status']}")
        else:
            print("  NOT FOUND")


asyncio.run(main())
