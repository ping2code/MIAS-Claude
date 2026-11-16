"""Telegram delivery for the Early Watch pilot (enabled 2026-11-11 by the user for live validation).

Only the initial message of a main-lane watch created live is sent (never invalidation / expiry / upgrade /
suppression messages, never opening-lane research candidates, never watches rebuilt by a restart backfill).
Plain text (no HTML parse mode), same bot and alert chats as MIAS. A separate worker sends from a queue, so a slow or
failed send can never block or change observer state; the outcome is written to the watch row. The request URL
carries the bot token and is never logged.

Telegram API safety (2026-11-12, after the MIAS 6-per-hour cap was removed; this paces delivery, it never drops a
message): sends are serialised (the v1 and v2 workers share one sender); at most one message per second to a
private chat and one per 3 s to a group chat (Telegram's published limits: ~1/s per chat, 20/min per group); a 429
response's retry_after is respected for that chat; at most 2 attempts per message (no retry loop); a required wait
longer than MAX_WAIT_S fails the message with the reason recorded instead of blocking the worker.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("mias.observer.telegram")


class SendError(Exception):
    pass


PRIVATE_GAP_S = 1.0
GROUP_GAP_S = 3.0
MAX_WAIT_S = 60.0


class WatchSender:
    def __init__(self, token: str, chats: list[str], *, transport: httpx.AsyncBaseTransport | None = None,
                 sleep=asyncio.sleep, clock=time.monotonic, max_wait_s: float = MAX_WAIT_S):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chats = [c for c in chats if c]
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), transport=transport)
        self._sleep, self._clock, self.max_wait_s = sleep, clock, max_wait_s
        self._lock = asyncio.Lock()
        self._next: dict[str, float] = {}           # chat -> earliest monotonic time of the next send
        self.rate_limited = 0                       # 429 responses seen (status / logs)

    async def close(self) -> None:
        await self._http.aclose()

    async def send(self, text: str) -> list[str]:
        """Send to every alert chat; returns the message ids. Raises SendError if any chat failed."""
        ids, errors = [], []
        entities = heading_entities(text)
        async with self._lock:
            for chat in self.chats:
                try:
                    ids.append(str(await self._one(chat, text, entities)))
                except SendError as exc:
                    errors.append(str(exc))
        if errors:
            raise SendError("; ".join(errors)[:300] + (f" (sent ids {','.join(ids)})" if ids else ""))
        return ids

    @staticmethod
    def _gap(chat: str) -> float:
        return GROUP_GAP_S if str(chat).startswith("-") else PRIVATE_GAP_S

    async def _one(self, chat: str, text: str, entities: list | None = None):
        err = "unknown"
        for attempt in range(2):
            wait = self._next.get(chat, 0.0) - self._clock()
            if wait > self.max_wait_s:
                raise SendError(f"rate limited: chat busy for {wait:.0f}s (retry_after > {self.max_wait_s:.0f}s), "
                                "not sent")
            if wait > 0:
                await self._sleep(wait)
            try:
                body = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
                if entities:
                    body["entities"] = entities
                resp = await self._http.post(self._url, json=body)
                if resp.status_code == 400 and entities:
                    # formatting is cosmetic: if Telegram rejects the bold span, send the same text plain
                    log.warning("telegram rejected the bold heading; sending the alert as plain text")
                    entities = None
                    body.pop("entities")
                    resp = await self._http.post(self._url, json=body)
            except httpx.HTTPError as exc:
                err = f"network error: {type(exc).__name__}"
                self._next[chat] = self._clock() + 1 + attempt
                continue
            if resp.status_code == 200:
                self._next[chat] = self._clock() + self._gap(chat)
                try:
                    return resp.json()["result"]["message_id"]
                except (ValueError, KeyError, TypeError):
                    return ""
            if resp.status_code == 429:
                try:
                    retry_after = float((resp.json().get("parameters") or {}).get("retry_after", 3))
                except (ValueError, TypeError, AttributeError):
                    retry_after = 3.0
                self.rate_limited += 1
                self._next[chat] = self._clock() + retry_after
                err = f"rate limited by Telegram (retry_after {retry_after:.0f}s, attempt {attempt + 1} of 2)"
                log.warning("telegram 429 for one alert chat: retry_after %.0fs (attempt %d of 2)", retry_after,
                            attempt + 1)
                continue
            try:
                desc = resp.json().get("description", "")
            except ValueError:
                desc = ""
            raise SendError(f"HTTP {resp.status_code}: {desc[:150]}")
        raise SendError(err)


HEADING_EMOJI = ("👀 ", "🧭 ")
DIRECTIONS = ("BULLISH", "BEARISH")


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def heading_entities(text: str) -> list[dict] | None:
    """Telegram bold entity for an Early Watch heading: the title, symbol and direction after the emoji, e.g.
    "V2 STRUCTURE WATCH · NVDA · BEARISH". Plain text (no parse mode), so nothing in the message needs escaping
    and the text itself is unchanged. Offsets are UTF-16 code units, as Telegram requires. None if the first
    line is not an Early Watch heading (e.g. the telegram-test message)."""
    first = text.split("\n", 1)[0]
    start = next((len(e) for e in HEADING_EMOJI if first.startswith(e)), None)
    if start is None or "WATCH" not in first:
        return None
    ends = [first.find(d, start) + len(d) for d in DIRECTIONS if first.find(d, start) >= 0]
    if not ends:
        return None
    end = min(ends)
    return [{"type": "bold", "offset": _u16(first[:start]), "length": _u16(first[start:end])}]
