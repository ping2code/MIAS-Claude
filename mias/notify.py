"""Telegram delivery from the SQLite outbox. Alerts are stored first, then sent, so nothing is lost on errors."""
from __future__ import annotations

import asyncio
import logging

import httpx

from .store import Store

log = logging.getLogger(__name__)
MAX_LEN = 4000


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: str = "", *, transport: httpx.AsyncBaseTransport | None = None):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat = chat_id
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=10), transport=transport)

    async def close(self) -> None:
        await self._http.aclose()

    async def send(self, html: str, chat: str | None = None) -> None:
        for chunk in split_message(html):
            await self._send_one(chunk, chat or self._chat)

    async def _send_one(self, text: str, chat: str) -> None:
        for attempt in range(3):
            try:
                resp = await self._http.post(self._url, json={
                    "chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
            except httpx.HTTPError as exc:
                # Never include the URL: it carries the bot token.
                err = TelegramError(f"network error: {type(exc).__name__}")
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code == 200:
                return
            if resp.status_code == 429:
                try:
                    wait = float(resp.json().get("parameters", {}).get("retry_after", 5))
                except ValueError:
                    wait = 5.0
                await asyncio.sleep(min(wait, 60))
                err = TelegramError("rate limited")
                continue
            try:
                desc = resp.json().get("description", "")
            except ValueError:
                desc = ""
            if resp.status_code == 400 and "parse" in desc.lower():
                # Fall back to plain text rather than lose the alert.
                resp = await self._http.post(self._url, json={"chat_id": chat, "text": strip_html(text)})
                if resp.status_code == 200:
                    return
            raise TelegramError(f"HTTP {resp.status_code}: {desc[:200]}")
        raise err


def split_message(text: str) -> list[str]:
    if len(text) <= MAX_LEN:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > MAX_LEN:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:MAX_LEN])
            line = line[MAX_LEN:]
        if len(cur) + len(line) + 1 > MAX_LEN:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def strip_html(text: str) -> str:
    import re
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", text))


class Dispatcher:
    """Drains the outbox to every alert chat. With no Telegram configured, alerts are only logged.

    Each alert remembers which chats already received it, so a failure in one chat is retried without
    re-sending to the others."""

    def __init__(self, store: Store, telegram: Telegram | None, chats: list[str] | None = None):
        self.store = store
        self.telegram = telegram
        self.chats = [c for c in (chats or []) if c]
        self.last_error: str | None = None
        self._lock = asyncio.Lock()

    async def flush(self) -> int:
        async with self._lock:
            sent = 0
            for alert_id, rule, text, sent_to, only in self.store.pending():
                done = [c for c in (sent_to or "").split(",") if c]
                if not done:
                    log.info("ALERT [%s]\n%s", rule, strip_html(text))
                chats = [only] if only else self.chats
                if self.telegram is None or not chats:
                    self.store.mark_delivered(alert_id)
                    continue
                failed = False
                for chat in chats:
                    if chat in done:
                        continue
                    try:
                        await self.telegram.send(text, chat)
                    except Exception as exc:  # keep it in the outbox; only the missing chats are retried
                        self.last_error = str(exc)
                        self.store.mark_failed(alert_id, str(exc), ",".join(done))
                        log.error("telegram delivery to %s failed for alert %d: %s", chat, alert_id, exc)
                        failed = True
                        break
                    done.append(chat)
                if failed:
                    break
                self.store.mark_delivered(alert_id)
                self.last_error = None
                sent += 1
            return sent
