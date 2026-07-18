"""Restore TG to user's settings via API."""
import asyncio
import httpx

USER_TG = {
    "enabled": True,
    "bot_token": "8838785769:AAHMjJahTqZBUo6_zD2uXPRSSxhbjJgatOQ",
    "chat_id": "840869344",
}


async def main():
    async with httpx.AsyncClient() as client:
        # POST to save
        r = await client.post(
            "http://localhost:8765/api/settings/telegram",
            json=USER_TG,
        )
        print(f"POST: {r.status_code}")
        # GET to verify
        r2 = await client.get("http://localhost:8765/api/settings/telegram")
        data = r2.json()
        print(f"GET: enabled={data['enabled']}  ready={data['ready']}  chat_id={data['chat_id']}  token last4={data['bot_token_last4']}")


asyncio.run(main())
