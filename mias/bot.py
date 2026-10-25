"""Telegram commands via long polling.

Served: messages from allow-listed users (TELEGRAM_ALLOWED_USERS) in any chat, and any member of an
allow-listed chat such as the private group (TELEGRAM_ALLOWED_CHATS). Everyone else is ignored, except
/whoami, which only tells people their own IDs so they can be added. Replies go to the chat that asked.
In groups the bot reacts to /commands only, so normal conversation never triggers it.

  /trend NVDA [MU ...]   fresh trend + options read + confidence (also: /t NVDA, or just send "NVDA")
  /market                SPY / QQQ / IWM and watchlist breadth
  /list                  watchlist
  /status                service health
  /help
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import re
from datetime import datetime, timezone
from html import escape

import httpx

from . import formatting
from .trend import direction
from .verdict import verdict

log = logging.getLogger(__name__)

# The chat currently being answered; set per incoming message so concurrent replies never cross chats.
_reply_chat: contextvars.ContextVar[str | None] = contextvars.ContextVar("reply_chat", default=None)
GROUP_TYPES = ("group", "supergroup")
KNOWN = {"trend", "t", "market", "picks", "list", "status", "help", "start", "whoami"}

TICKER = re.compile(r"^\$?([A-Z]{1,5}(?:\.[A-Z])?)$")
MAX_SYMBOLS = 5
COMMANDS = [
    ("trend", "Trend, options and confidence for symbols, e.g. /trend NVDA MU"),
    ("market", "Overall market: SPY, QQQ, IWM and breadth"),
    ("picks", "Today's call and put spread picks"),
    ("list", "Watched symbols"),
    ("status", "Service health"),
    ("help", "How to use this bot"),
    ("whoami", "Your Telegram user and chat IDs"),
]
HELP = """<b>MIAS bot</b>
/trend NVDA: trend, options read and confidence (up to 5: /trend NVDA MU AMD)
Shortcut: just send <code>NVDA</code> or <code>nvda mu</code>
/market: SPY, QQQ, IWM and watchlist breadth
/picks: today's call and put spread picks (also sent automatically at 10:00 ET)
/list: watched symbols · /status: health · /whoami: your IDs
In the group, use commands: <code>/trend NVDA</code> or <code>/t nvda mu</code>

<b>Reading the answer</b>
Swing = 1w/1d/1h trend (holding weeks). Intraday = 5m–1h.
Scores run −100…+100: ±25 trend, ±60 strong.
✅ HIGH: every swing timeframe and options agree
🟡 MEDIUM: trend agrees, options neutral/thin, or most timeframes agree
⚪ LOW: no clear trend or timeframes disagree
⚠️ CONFLICT: trend and options money point opposite ways"""


def parse(text: str) -> tuple[str, list[str]]:
    text = (text or "").strip()
    if not text:
        return "", []
    parts = text.split()
    if parts[0].startswith("/"):
        cmd = parts[0][1:].split("@")[0].lower()
        return cmd, [p.upper() for p in parts[1:]]
    upper = [p.upper().rstrip("?,") for p in parts]
    if len(upper) <= MAX_SYMBOLS and all(TICKER.match(p) for p in upper):
        return "trend", upper
    return "unknown", []


class CommandBot:
    def __init__(self, service):
        self.svc = service
        cfg = service.cfg
        self._base = f"https://api.telegram.org/bot{cfg.telegram_token}/"
        self._users = set(cfg.telegram_allowed_users)
        self._chats = set(cfg.telegram_allowed_chats)
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(70, connect=10))
        self._sem = asyncio.Semaphore(3)
        self._tasks: set[asyncio.Task] = set()

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await self._http.aclose()

    async def run(self) -> None:
        await self._set_commands()
        offset = self.svc.store.get("telegram_offset")
        while True:
            try:
                resp = await self._http.post(self._base + "getUpdates", json={
                    "timeout": 50, "offset": offset, "allowed_updates": ["message"]})
            except httpx.HTTPError as exc:
                log.warning("telegram poll failed: %s", type(exc).__name__)
                await asyncio.sleep(5)
                continue
            if resp.status_code == 409:
                log.error("telegram: another client is reading this bot's updates; retrying in 60s")
                await asyncio.sleep(60)
                continue
            if resp.status_code != 200:
                log.warning("telegram poll HTTP %d", resp.status_code)
                await asyncio.sleep(10)
                continue
            for upd in resp.json().get("result", []):
                offset = upd["update_id"] + 1
                self.svc.store.put("telegram_offset", offset)
                msg = upd.get("message")
                if msg:
                    task = asyncio.create_task(self._handle(msg))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)

    async def _set_commands(self) -> None:
        try:
            await self._http.post(self._base + "setMyCommands", json={
                "commands": [{"command": c, "description": d} for c, d in COMMANDS]})
        except httpx.HTTPError:
            pass

    def authorized(self, user: str, chat: str) -> bool:
        return user in self._users or chat in self._chats

    async def _handle(self, msg: dict) -> None:
        chat_info = msg.get("chat") or {}
        chat = str(chat_info.get("id", ""))
        user = str((msg.get("from") or {}).get("id", ""))
        text = msg.get("text") or ""
        in_group = chat_info.get("type") in GROUP_TYPES
        if in_group and not text.startswith("/"):
            return                                   # ordinary group conversation
        cmd, args = parse(text)
        _reply_chat.set(chat)
        if cmd == "whoami":
            ok = self.authorized(user, chat)
            await self.reply(f"Your user ID: <code>{escape(user)}</code>\nThis chat ID: <code>{escape(chat)}</code>\n"
                             f"Access: {'✅ allowed' if ok else '⛔ not allowed yet'}")
            return
        if not self.authorized(user, chat):
            log.warning("ignoring message from unauthorized user %s in chat %s", user, chat)
            return
        if in_group and cmd not in KNOWN:
            return                                   # someone else's command, or a typo: stay quiet
        try:
            await self.dispatch(cmd, args)
        except Exception:
            log.exception("command %s failed", cmd)
            await self.reply("Sorry, that failed. Try again in a minute.")

    async def dispatch(self, cmd: str, args: list[str]) -> None:
        if cmd in ("trend", "t"):
            syms = [m.group(1) for m in (TICKER.match(a) for a in args) if m]
            if not syms:
                await self.reply("Send a symbol, e.g. <code>/trend NVDA</code> or just <code>NVDA</code>")
                return
            if len(syms) > MAX_SYMBOLS:
                await self.reply(f"Up to {MAX_SYMBOLS} symbols at a time; checking the first {MAX_SYMBOLS}.")
            await asyncio.gather(*(self._trend(s) for s in dict.fromkeys(syms[:MAX_SYMBOLS])))
        elif cmd == "market":
            await self.reply(await self.market_text())
        elif cmd == "picks":
            if not self.svc.engine.last_results:
                await self.reply("No market data yet: picks need a completed check (market hours).")
                return
            await self.reply(await self.svc.picks_text(datetime.now(timezone.utc)))
        elif cmd == "list":
            await self.reply("Watching: " + ", ".join(self.svc.cfg.symbols))
        elif cmd == "status":
            _, info = self.svc.health()
            c = info.get("last_cycle") or {}
            await self.reply(
                f"Market {info['market']} · {'DEGRADED' if info['degraded'] else 'healthy'}\n"
                f"Last cycle: {escape(str(c.get('at', 'none yet')))[:19]} UTC, "
                f"{c.get('clean', 0)}/{c.get('symbols', 0)} clean in {c.get('duration_s', '–')}s")
        elif cmd in ("help", "start"):
            await self.reply(HELP)
        else:
            await self.reply("I didn't get that. Send a ticker like <code>NVDA</code>, or /help")

    async def _trend(self, sym: str) -> None:
        async with self._sem:
            r = await self.svc.engine.analyze_one(sym)
        now = datetime.now(timezone.utc).astimezone(self.svc.cfg.tz)
        await self.reply(formatting.trend_reply(r, self.svc.cfg, now, sym in self.svc.cfg.symbols,
                                                self.svc.market_state == "open"))

    async def market_text(self) -> str:
        cfg, eng = self.svc.cfg, self.svc.engine
        idx = ["SPY", "QQQ", "IWM"]
        async with self._sem:
            rs = await asyncio.gather(*(eng.analyze_one(s) for s in idx))
        now = datetime.now(timezone.utc).astimezone(cfg.tz)
        primary_tfs = list(cfg.trend_profiles[cfg.trend_primary])
        lines = [f"🌐 <b>MARKET</b>  {now:%a %H:%M} ET"]
        scores = []
        for r in rs:
            if not r.stock:
                lines.append(f"{r.symbol}: unavailable")
                continue
            scores.append(r.stock.score)
            v = verdict(r, primary_tfs)
            comp = " · ".join(f"{n} {formatting.label(l)} ({s:+.0f})" for n, (s, l) in r.stock.composites.items())
            opts = f" · options {formatting.label(r.bias.label)}" if r.bias and r.bias.components else ""
            lines.append(f"{formatting.ICON[direction(r.stock.label)]} <b>{r.symbol}</b> {formatting.num(r.price)} "
                         f"({formatting.num(r.day_change_pct, '.2f', True)}%)\n   {comp}{opts} · {v.icon} {v.level}")
        if scores:
            avg = sum(scores) / len(scores)
            lbl = formatting.label_for_score(avg, cfg)
            lines.append(f"\nOverall {cfg.trend_primary}: <b>{lbl}</b> ({avg:+.0f})")
        if eng.last_results:
            up = sum(1 for r in eng.last_results if r.stock and direction(r.stock.label) == "bull")
            down = sum(1 for r in eng.last_results if r.stock and direction(r.stock.label) == "bear")
            lines.append(f"Watchlist ({cfg.trend_primary}): {up} up · {down} down · "
                         f"{len(eng.last_results) - up - down} sideways")
        if self.svc.market_state != "open":
            lines.append("<i>market closed: last session data</i>")
        return "\n".join(lines)

    async def reply(self, html: str) -> None:
        if self.svc.telegram:
            await self.svc.telegram.send(html, _reply_chat.get())
