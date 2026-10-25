"""Long-running service: market-hours scheduler, digests, self-monitoring, health endpoint."""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime, time as dtime, timedelta, timezone

from .config import Config
from .engine import Engine
from .massive import MassiveClient
from .notify import Dispatcher, Telegram
from .store import Store
from .web import WebServer

log = logging.getLogger(__name__)

WINDOW_START, WINDOW_END = dtime(9, 25), dtime(16, 15)
OPEN, CLOSE = dtime(9, 30), dtime(16, 0)
DEGRADED_AFTER = 3


class Service:
    def __init__(self, cfg: Config, *, transport=None):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.client = MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout,
                                    max_retries=cfg.max_retries, max_concurrency=cfg.max_concurrency,
                                    transport=transport)
        tg = None
        if cfg.telegram_enabled and cfg.telegram_token and cfg.telegram_alert_chats:
            tg = Telegram(cfg.telegram_token, cfg.telegram_alert_chats[0])
        elif cfg.telegram_enabled:
            log.warning("Telegram enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing: alerts are only logged")
        self.telegram = tg
        self.dispatcher = Dispatcher(self.store, tg, cfg.telegram_alert_chats)
        self.engine = Engine(cfg, self.client, self.store, self.dispatcher)
        self.heartbeat = time.time()
        self.market_state = "unknown"
        self.bad_cycles = 0
        self.degraded = False
        self._stop = asyncio.Event()
        self._screen_task: asyncio.Task | None = None
        self.screen_started = 0.0
        self.screen_error: str | None = None

    # ---- lifecycle --------------------------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass
        web = WebServer(self) if self.cfg.web_enabled else None
        if web:
            await web.start(self.cfg.web_host, self.cfg.web_port)
        bot_task = bot = None
        if self.telegram and self.cfg.telegram_commands:
            from .bot import CommandBot
            bot = CommandBot(self)
            bot_task = asyncio.create_task(bot.run())
        self.store.enqueue("system", f"✅ MIAS started · watching {len(self.cfg.symbols)} symbols · "
                                     f"every {self.cfg.interval_seconds}s in market hours"
                                     + (" · send /help for commands" if bot else ""))
        await self.dispatcher.flush()
        if self.cfg.screen_enabled and not self.store.get("screen:latest"):
            self.start_screen()
        try:
            while not self._stop.is_set():
                await self._tick()
                await self._sleep_until_next()
        finally:
            log.info("shutting down")
            if self._screen_task:
                self._screen_task.cancel()
                await asyncio.gather(self._screen_task, return_exceptions=True)
            if bot_task:
                bot_task.cancel()
                await asyncio.gather(bot_task, return_exceptions=True)
                await bot.close()
            if web:
                await web.stop()
            await self.client.close()
            if self.telegram:
                await self.telegram.close()
            self.store.close()

    async def _sleep_until_next(self) -> None:
        local = datetime.now(self.cfg.tz)
        interval = self.cfg.interval_seconds if _in_window(local) else 60
        delay = interval - (time.time() % interval) + 1
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    # ---- one tick ---------------------------------------------------------------------------

    async def _tick(self, now: datetime | None = None) -> None:
        self.heartbeat = time.time()
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(self.cfg.tz)
        today = local.date().isoformat()
        sent = self.store.get(f"digests:{today}", [])

        if not _in_window(local):
            self.market_state = "closed"
            await self._housekeeping(today)
            await self.dispatcher.flush()   # retry anything that failed earlier
            return

        is_open = await self._market_open(local)
        if is_open:
            self.market_state = "open"
            await self._run_cycle(now)
            self._send_reports(now)
            self._send_unusual(now)
            await self._send_picks(now)
            if self.cfg.screen_enabled and self._screen_due(now):
                self.start_screen()
            for t in self.cfg.digest_times:
                due = datetime.combine(local.date(), dtime.fromisoformat(t), self.cfg.tz)
                if t not in sent and due <= local < due + timedelta(minutes=30):
                    self.store.enqueue("digest", self.engine.digest_text(f"MIAS digest {t}", now))
                    sent.append(t)
        else:
            was_open = self.market_state == "open"
            self.market_state = "closed"
            cycled_today = self._cycled_on(local)
            if (was_open or local.time() >= CLOSE) and "close" not in sent and cycled_today:
                if was_open:
                    await self._run_cycle(now)     # final read after the bell
                self.store.enqueue("digest", self.engine.digest_text("Closing summary", now))
                sent.append("close")
        self.store.put(f"digests:{today}", sent)
        await self.dispatcher.flush()

    # ---- periodic per-symbol reports (e.g. NDX every 10 minutes) -------------------------------

    def _send_reports(self, now: datetime) -> None:
        if not self.cfg.reports or self.engine.last_cycle.get("at") != now.isoformat():
            return                                   # only report on data from this very cycle
        from .formatting import scheduled_report
        local = now.astimezone(self.cfg.tz)
        since_open = (local - datetime.combine(local.date(), OPEN, self.cfg.tz)).total_seconds()
        if since_open < 0:
            return
        for sym, minutes in self.cfg.reports.items():
            until = self.cfg.report_until.get(sym)
            if until and local.date() > until:
                continue                             # temporary report has expired
            r = next((x for x in self.engine.last_results if x.symbol == sym), None)
            if not r or not r.ok:
                continue
            tag = f"{local.date()}:{int(since_open // (minutes * 60))}"
            last = self.store.get(f"report:{sym}") or {}
            if last.get("tag") == tag:
                continue
            prev = last.get("price") if last.get("date") == str(local.date()) else None
            self.store.enqueue("report", scheduled_report(r, self.cfg, local, minutes, prev), sym)
            self.store.put(f"report:{sym}", {"tag": tag, "date": str(local.date()), "price": r.price})

    def _send_unusual(self, now: datetime) -> None:
        minutes = self.cfg.unusual_every_minutes
        if not minutes or self.engine.last_cycle.get("at") != now.isoformat():
            return
        from .formatting import unusual_report
        local = now.astimezone(self.cfg.tz)
        since_open = (local - datetime.combine(local.date(), OPEN, self.cfg.tz)).total_seconds()
        if since_open < 0:
            return
        tag = f"{local.date()}:{int(since_open // (minutes * 60))}"
        last = self.store.get("unusual_report") or {}
        if last.get("tag") == tag:
            return
        prev = last.get("volumes") if last.get("date") == str(local.date()) else None
        text, volumes = unusual_report(self.engine.last_results, local, prev, self.cfg.unusual_top)
        if text:
            self.store.enqueue("unusual", text)
        self.store.put("unusual_report", {"tag": tag, "date": str(local.date()), "volumes": volumes})

    # ---- daily spread picks ---------------------------------------------------------------------

    async def _send_picks(self, now: datetime) -> None:
        if not self.cfg.picks_enabled or self.engine.last_cycle.get("at") != now.isoformat():
            return
        local = now.astimezone(self.cfg.tz)
        due = datetime.combine(local.date(), dtime.fromisoformat(self.cfg.picks_time), self.cfg.tz)
        key = f"picks:{local.date()}"
        if not (due <= local < due + timedelta(minutes=60)) or self.store.get(key):
            return
        self.store.put(key, True)          # once a day, even if building fails
        try:
            text = await self.picks_text(now)
        except Exception:
            log.exception("daily picks failed")
            return
        self.store.enqueue("picks", text)

    async def picks_text(self, now: datetime) -> str:
        from .formatting import picks_message
        from .picks import daily_picks
        p = await daily_picks(self.client, self.engine.last_results, self.cfg, now)
        return picks_message(p, now.astimezone(self.cfg.tz))

    # ---- options screen (background; never blocks the alert cycle) ---------------------------

    def _screen_due(self, now: datetime) -> bool:
        last = self.store.get("screen:latest")
        if not last:
            return True
        age = now - datetime.fromisoformat(last["at"])
        return age >= timedelta(minutes=self.cfg.screen_interval_minutes)

    def start_screen(self, min_gap_s: float = 0) -> bool:
        """Start a screen unless one is running or one started less than min_gap_s ago."""
        if self._screen_task and not self._screen_task.done():
            return False
        if time.time() - self.screen_started < min_gap_s:
            return False
        self.screen_started = time.time()
        self._screen_task = asyncio.create_task(self._run_screen())
        return True

    @property
    def screen_running(self) -> bool:
        return bool(self._screen_task and not self._screen_task.done())

    async def _run_screen(self) -> None:
        from . import screen
        try:
            result = await screen.run(self.client, self.cfg.symbols, self.cfg.tz, datetime.now(self.cfg.tz))
        except Exception as exc:
            log.exception("options screen failed")
            self.screen_error = f"{type(exc).__name__}: {exc}"[:200]
            return
        if result is None:
            self.screen_error = "too little data returned; kept the previous result"
            log.warning("options screen: %s", self.screen_error)
            return
        self.store.put("screen:latest", result)
        self.screen_error = None
        log.info("options screen done: %d screened", result["screened"])

    async def _run_cycle(self, now: datetime) -> None:
        try:
            await asyncio.wait_for(self.engine.cycle(now), timeout=self.cfg.interval_seconds * 0.9)
            ok = self.engine.healthy_cycle()
        except Exception as exc:
            log.exception("cycle failed: %s", exc)
            ok = False
        self.bad_cycles = 0 if ok else self.bad_cycles + 1
        if self.bad_cycles >= DEGRADED_AFTER and not self.degraded:
            self.degraded = True
            failed = self.engine.last_cycle.get("failed", [])
            self.store.enqueue("system", f"⚠️ MIAS degraded: {self.bad_cycles} bad cycles in a row. "
                                         f"Failing: {', '.join(failed) or 'all requests'}")
        elif ok and self.degraded:
            self.degraded = False
            self.store.enqueue("system", "✅ MIAS recovered: data flowing normally again")

    def _cycled_on(self, local: datetime) -> bool:
        at = self.engine.last_cycle.get("at")
        return bool(at) and datetime.fromisoformat(at).astimezone(self.cfg.tz).date() == local.date()

    async def _market_open(self, local: datetime) -> bool:
        try:
            status = await self.client.market_status()
            return status.get("market") == "open"
        except Exception as exc:
            log.warning("market status unavailable (%s); using the clock", exc)
            return local.weekday() < 5 and OPEN <= local.time() < CLOSE

    async def _housekeeping(self, today: str) -> None:
        if self.store.get("pruned") != today:
            self.store.prune(self.cfg.retention_days)
            self.store.put("pruned", today)

    # ---- status -----------------------------------------------------------------------------

    def health(self) -> tuple[bool, dict]:
        age = time.time() - self.heartbeat
        alive = age < self.cfg.interval_seconds * 3 + 120
        return alive, {
            "alive": alive, "heartbeat_age_s": round(age, 1), "market": self.market_state,
            "degraded": self.degraded, "bad_cycles": self.bad_cycles,
            "last_cycle": self.engine.last_cycle, "telegram_error": self.dispatcher.last_error,
        }


def _in_window(local: datetime) -> bool:
    return local.weekday() < 5 and WINDOW_START <= local.time() <= WINDOW_END
