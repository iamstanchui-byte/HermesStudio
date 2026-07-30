# coding: utf-8
"""Notifier — sends alerts to the operator (currently Telegram).

Used by supervisor to flag failures that need human attention:
- LLM planner error / invalid plan
- No agent available for required role
- Project stuck in planning for too long
- Repeated task failures (TODO)

Design:
- If disabled or mis-configured, log and no-op (never raise).
- Use httpx async client to POST to Telegram Bot API.
- Single message format: "<title>\\n<body>".
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("hermes_orch.notifier")


class Notifier:
    """Telegram notifier. No-op if disabled or unconfigured."""

    def __init__(self, cfg: dict[str, Any]):
        tg = cfg.get("telegram", {}) or {}
        self.enabled = bool(tg.get("enabled", False))
        self.bot_token = (tg.get("bot_token") or "").strip()
        self.chat_id = str(tg.get("chat_id") or "").strip()
        self.timeout = float(tg.get("timeout_seconds", 10))
        self._ready = (
            self.enabled
            and bool(self.bot_token)
            and bool(self.chat_id)
        )
        if self.enabled and not self._ready:
            log.warning(
                "telegram.enabled=true but bot_token/chat_id missing; notifier will be a no-op"
            )

    async def send(self, title: str, body: str = "", *, level: str = "info") -> None:
        """Send a message. level is 'info'|'warn'|'error' (affects emoji prefix)."""
        if not self._ready:
            # No-op log so the operator can see what *would* have been sent
            log.info(f"[notifier:no-op {level}] {title} - {body}")
            return
        prefix = {
            "info": "[i]",
            "warn": "[!]",
            "error": "[X]",
        }.get(level, "[?]")
        text = f"{prefix} {title}\n{body}".strip()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": text[:3900],  # Telegram message cap is 4096; leave headroom
                        "disable_web_page_preview": True,
                    },
                )
                if r.status_code >= 300:
                    log.warning(f"telegram send failed: {r.status_code} {r.text}")
        except Exception as e:
            log.warning(f"telegram send exception: {e}")
