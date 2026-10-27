"""Telegram commands via long polling.

Served: messages from allow-listed users (TELEGRAM_ALLOWED_USERS) in any chat, and any member of an
allow-listed chat such as the private group (TELEGRAM_ALLOWED_CHATS). Everyone else is ignored, except
/whoami, which only tells people their own IDs so they can be added. Replies go to the chat that asked.
In groups the bot reacts to /commands only, so normal conversation never triggers it.

  /trend NVDA [MU ...]   fresh trend + options read + confidence (also: /t NVDA, or just send "NVDA")
  /market                SPY / QQQ / IWM, watchlist breadth and the sector board
  /sector [energy]       sector board, or one sector's members
  /buy, /sell, /positions   track your own option positions (private chat only); alerts come privately
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

from . import formatting, positions, sectors
from .trend import direction
from .verdict import verdict

log = logging.getLogger(__name__)

# The chat currently being answered; set per incoming message so concurrent replies never cross chats.
_reply_chat: contextvars.ContextVar[str | None] = contextvars.ContextVar("reply_chat", default=None)
GROUP_TYPES = ("group", "supergroup")
KNOWN = {"trend", "t", "market", "sector", "sectors", "picks", "list", "status", "help", "start", "whoami",
         "buy", "sell", "positions", "pos", "ema"}
PRIVATE_ONLY = {"buy", "sell", "positions", "pos"}
# The user and chat type of the message being answered (position commands are per user, private chats only).
_user: contextvars.ContextVar[str] = contextvars.ContextVar("user", default="")
_private: contextvars.ContextVar[bool] = contextvars.ContextVar("private", default=True)

TICKER = re.compile(r"^\$?([A-Z]{1,5}(?:\.[A-Z])?)$")
MAX_SYMBOLS = 5
COMMANDS = [
    ("trend", "Trend, options and confidence for symbols, e.g. /trend NVDA MU"),
    ("market", "Overall market: SPY, QQQ, IWM and breadth"),
    ("sector", "Sector board, or one sector: /sector energy"),
    ("picks", "Today's call and put spread picks"),
    ("ema", "EMA 9/21 day-trade watch and today's paper trades"),
    ("positions", "Your open positions (private chat)"),
    ("buy", "Track a position: /buy AMZN 11/20 260/280C 6.63 x1"),
    ("sell", "Close a tracked position: /sell AMZN 13.40"),
    ("list", "Watched symbols"),
    ("status", "Service health"),
    ("help", "How to use this bot"),
    ("whoami", "Your Telegram user and chat IDs"),
]
HELP = """<b>MIAS bot</b>
/trend NVDA: trend, options read and confidence (up to 5: /trend NVDA MU AMD)
Shortcut: just send <code>NVDA</code> or <code>nvda mu</code>
/market: SPY, QQQ, IWM, watchlist breadth and sectors
/sector: which sector is strongest (also in every digest); /sector energy for its members
/picks: today's call and put spread picks (also sent automatically at 10:00 ET)
/ema: 5-minute EMA 9/21 cross-and-retest watch (SPY, QQQ, NVDA, TSLA, META) and today's paper trades (entries also alert the group)
/list: watched symbols · /status: health · /whoami: your IDs

<b>Your positions</b> (private chat with the bot; alerts come to you only)
<code>/buy AMZN 11/20 260/280C 6.63 x1</code> debit spread, first strike = the one you bought
<code>/buy NVDA 12/18 190C 9.85 x2</code> single option · add <code>by 10/30</code> for your own exit date
<code>/sell AMZN 13.40</code> (or <code>/sell 3</code> by number; no price = current mid) · /positions
Alerts: take-profit (55% of max, +50% single), stop (-50%), near short strike, time stop (10 trading days),
7 days to expiry, swing trend turned against you
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
        _user.set(user)
        _private.set(chat_info.get("type", "private") == "private")
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
        elif cmd in ("sector", "sectors"):
            await self.reply(self.sector_text(args[0] if args else None))
        elif cmd == "ema":
            from . import emacross
            await self.reply(emacross.status_text(self.svc.store, self.svc.cfg, datetime.now(timezone.utc),
                                                  self.svc.engine.five))
        elif cmd in PRIVATE_ONLY and not _private.get():
            await self.reply("Positions are personal: send that to me in a private chat.")
        elif cmd == "buy":
            await self.reply(self.buy_text(args))
        elif cmd == "sell":
            await self.reply(await self.sell_text(args))
        elif cmd in ("positions", "pos"):
            await self.reply(await self.positions_text())
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
        board = sectors.board_lines(eng.last_results, cfg.sectors, cfg) if eng.last_results else []
        if board:
            lines += ["", "<b>Sectors</b>", *board]
        if self.svc.market_state != "open":
            lines.append("<i>market closed: last session data</i>")
        return "\n".join(lines)

    def _today(self):
        return datetime.now(timezone.utc).astimezone(self.svc.cfg.tz).date()

    def buy_text(self, args: list[str]) -> str:
        try:
            p = positions.open_position(self.svc.store, _user.get(), args, self._today())
        except positions.PositionError as exc:
            return f"⛔ {exc}"
        rules = self.svc.cfg.positions
        mp = positions.max_profit(p)
        target = p["entry"] + rules.take_profit * mp if mp else p["entry"] * (1 + rules.single_take_profit)
        stop = p["entry"] * (1 - rules.stop_loss)
        from .picks import add_trading_days
        from datetime import date
        by = date.fromisoformat(p["exit_by"]) if p["exit_by"] else \
            add_trading_days(date.fromisoformat(p["opened"]), rules.time_stop_days)
        return (f"✅ Tracking #{p['id']} <b>{escape(positions.name(p))}</b> @ {p['entry']:.2f}\n"
                f"Take profit ≈{target:.2f} · stop ≈{stop:.2f} · time stop {by:%a %b %d}"
                + (f" · max profit ${100 * mp * p['qty']:,.0f}" if mp else "")
                + "\nAlerts come to this chat. /positions to check, /sell when you close it.")

    async def sell_text(self, args: list[str]) -> str:
        if not args:
            return "Use: <code>/sell AMZN 13.40</code> or <code>/sell 3</code> (number from /positions)"
        found = positions.find_open(self.svc.store, _user.get(), args[0])
        if not found:
            return f"No open position {escape(args[0])}. /positions lists yours."
        if len(found) > 1:
            return "More than one: use the number.\n" + "\n".join(
                f"#{p['id']} {escape(positions.name(p))}" for p in found)
        p = found[0]
        if len(args) > 1:
            try:
                price = float(args[1].lstrip("$@"))
            except ValueError:
                return f"Can't read the price {escape(args[1])!r}"
        else:
            price, _ = await positions.quote(self.svc.client, p)
            if price is None:
                return "No live quote right now: add the price, e.g. <code>/sell AMZN 13.40</code>"
        closed = positions.close_position(self.svc.store, p, price, self._today())
        icon = "🟢" if closed["pnl"] >= 0 else "🔴"
        return (f"{icon} Closed #{p['id']} <b>{escape(positions.name(p))}</b> @ {price:.2f} "
                f"(entry {p['entry']:.2f}) · P&amp;L <b>{positions._money(closed['pnl'])}</b>")

    async def positions_text(self) -> str:
        mine = positions.mine(self.svc.store, _user.get())
        if not mine:
            return "No open positions. Add one: <code>/buy AMZN 11/20 260/280C 6.63 x1</code>"
        lines = ["📒 <b>Your positions</b>"]
        total = 0.0
        for p in mine:
            try:
                value, under = await positions.quote(self.svc.client, p)
            except Exception:
                value, under = None, None
            if value is not None:
                total += positions.pnl(p, value)
            lines.append(f"#{p['id']} {positions.status_line(p, value, under)}")
        lines.append(f"Open P&amp;L: <b>{positions._money(total)}</b>")
        return "\n".join(lines)

    def sector_text(self, name: str | None) -> str:
        cfg, results = self.svc.cfg, self.svc.engine.last_results
        now = datetime.now(timezone.utc).astimezone(cfg.tz)
        if not cfg.sectors:
            return "No sectors configured."
        if not name:
            return sectors.board(results or [], cfg.sectors, cfg, now)
        sector = sectors.find(cfg.sectors, name)
        if not sector:
            return "Sectors: " + ", ".join(s.key for s in cfg.sectors)
        return sectors.detail(results or [], sector, cfg, now)

    async def reply(self, html: str) -> None:
        if self.svc.telegram:
            await self.svc.telegram.send(html, _reply_chat.get())
