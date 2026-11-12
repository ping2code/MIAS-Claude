"""Telegram delivery for the Early Watch pilot (enabled 2026-11-11 by the user for live validation).

Only the initial message of a main-lane watch created live is sent (never invalidation / expiry / upgrade /
suppression messages, never opening-lane research candidates, never watches rebuilt by a restart backfill).
Plain text (no HTML parse mode), same bot and alert chats as MIAS. A separate worker sends from a queue, so a slow or
failed send can never block or change observer state; the outcome is written to the watch row. The request URL
carries the bot token and is never logged.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("mias.observer.telegram")


class SendError(Exception):
    pass


class WatchSender:
    def __init__(self, token: str, chats: list[str], *, transport: httpx.AsyncBaseTransport | None = None):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chats = [c for c in chats if c]
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), transport=transport)

    async def close(self) -> None:
        await self._http.aclose()

    async def send(self, text: str) -> list[str]:
        """Send to every alert chat; returns the message ids. Raises SendError if any chat failed."""
        ids, errors = [], []
        for chat in self.chats:
            try:
                ids.append(str(await self._one(chat, text)))
            except SendError as exc:
                errors.append(str(exc))
        if errors:
            raise SendError("; ".join(errors)[:300] + (f" (sent ids {','.join(ids)})" if ids else ""))
        return ids

    async def _one(self, chat: str, text: str):
        err = "unknown"
        for attempt in range(2):
            try:
                resp = await self._http.post(self._url, json={"chat_id": chat, "text": text,
                                                              "disable_web_page_preview": True})
            except httpx.HTTPError as exc:
                err = f"network error: {type(exc).__name__}"
                await asyncio.sleep(1 + attempt)
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()["result"]["message_id"]
                except (ValueError, KeyError, TypeError):
                    return ""
            if resp.status_code == 429:
                try:
                    wait = float(resp.json().get("parameters", {}).get("retry_after", 3))
                except ValueError:
                    wait = 3.0
                await asyncio.sleep(min(wait, 15))
                err = "rate limited"
                continue
            try:
                desc = resp.json().get("description", "")
            except ValueError:
                desc = ""
            raise SendError(f"HTTP {resp.status_code}: {desc[:150]}")
        raise SendError(err)
