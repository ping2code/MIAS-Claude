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

WINDOW_START, WINDOW_END = dtime(9, 0), dtime(16, 15)   # 09:00-09:30: warm-up cycles, no alerts
OPEN, CLOSE = dtime(9, 30), dtime(16, 0)
TAPE_CONTRACTS = 30              # unusual contracts per report that get a trade-by-trade tape
FLOW_SCORE_PREMIUM = 1_000_000   # one-sided new flow at least this large is logged on the scorecard
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
        self._live_day = None                  # date of the last live cycle (closing summary)
        self._pre_cache: dict = {}             # daily bars for the pre-market messages, per day
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
        watch_task = asyncio.create_task(self._watch_paper_trades()) \
            if self.cfg.daytrade.enabled or self.cfg.closeplay_enabled else None
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
            if watch_task:
                watch_task.cancel()
                await asyncio.gather(watch_task, return_exceptions=True)
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

        await self._send_premarket(now)
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
            self._send_changes(now)
            await self._send_unusual(now)
            await self._send_picks(now)
            self._send_brief(now)
            if self.cfg.screen_enabled and self._screen_due(now):
                self.start_screen()
            for t in self.cfg.digest_times:
                due = datetime.combine(local.date(), dtime.fromisoformat(t), self.cfg.tz)
                if t not in sent and due <= local < due + timedelta(minutes=30):
                    self.store.enqueue("digest", self.engine.digest_text(f"MIAS digest {t}", now))
                    sent.append(t)
        elif local.time() < OPEN:
            # Warm-up before the bell: bar history cached, dashboard and /market on pre-market prices, data problems
            # caught early. Dry run: no alerts, no recorded checks (options don't trade yet).
            self.market_state = "pre-market"
            await self._run_cycle(now, dry_run=True)
        else:
            was_open = self.market_state == "open"
            self.market_state = "closed"
            cycled_today = self._cycled_on(local)
            if (was_open or local.time() >= CLOSE) and "close" not in sent and cycled_today:
                if was_open:
                    await self._run_cycle(now)     # final read after the bell
                self._send_changes(now, force=True)
                self.store.enqueue("digest", self.engine.digest_text("Closing summary", now))
                from . import health
                held = health.summary_line(health.day_counts(self.store, today))
                if held:
                    self.store.enqueue("system", held)
                if local.weekday() == 4:
                    from . import scorecard, strategies
                    self.store.enqueue("scorecard", scorecard.report(self.store, now, 7))
                    self.store.enqueue("strategies", strategies.report(self.store, self.cfg, now, 7))
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

    async def _send_unusual(self, now: datetime) -> None:
        minutes = self.cfg.unusual_every_minutes
        if not minutes or self.engine.last_cycle.get("at") != now.isoformat():
            return
        from . import confirm, flow, scorecard
        from .formatting import oi_lines, unusual_report
        local = now.astimezone(self.cfg.tz)
        today = str(local.date())
        open_at = datetime.combine(local.date(), OPEN, self.cfg.tz)
        since_open = (local - open_at).total_seconds()
        if since_open < 0:
            return
        tag = f"{local.date()}:{int(since_open // (minutes * 60))}"
        last = self.store.get("unusual_report") or {}
        if last.get("tag") == tag:
            return
        same_day = last.get("date") == today
        prev = last.get("volumes") if same_day else None
        since = datetime.fromtimestamp(last["ts"], self.cfg.tz) if same_day and last.get("ts") else open_at
        pending = self.store.get("blocks") or {}
        blocks = pending.get("items") if pending.get("date") == today else None

        # Trade-by-trade tapes for the contracts that traded unusually in this window, and for the blocks.
        rows = [(r.symbol, p) for r in self.engine.last_results if r.options for p in r.options.unusual[:3]]
        moved = [(s, p) for s, p in rows if p.volume - (prev or {}).get(p.ticker, 0.0) > 0]
        moved.sort(key=lambda sp: -(sp[1].volume - (prev or {}).get(sp[1].ticker, 0.0)) * (sp[1].price or 0))
        want = {p.ticker: p.kind for _, p in moved[:TAPE_CONTRACTS]}
        want.update({b["ticker"]: b["kind"] for b in blocks or []})
        tapes = {}
        if want:
            tapes = await flow.tapes(self.client, list(want.items()), int(since.timestamp() * 1e9),
                                     int(now.timestamp() * 1e9))

        # First report of the day: did yesterday's flagged volume show up as new open interest?
        day = self.store.get("unusual_day") or {}
        checks = []
        if day.get("date") and day["date"] != today:
            try:
                checks = await flow.oi_check(self.client, day)
            except Exception as exc:
                log.warning("open-interest check failed: %s", exc)
            for c in checks:
                r = next((x for x in self.engine.last_results if x.symbol == c["symbol"]), None)
                if c["verdict"] == "opened" and c.get("lean") in ("bull", "bear") and r and r.price:
                    scorecard.record(self.store, now.timestamp(), c["symbol"], "flow_opened", c["lean"], r.price)
            day = {}
        if not day:
            day = {"date": today, "contracts": {}}

        meta = {p.ticker: (s, p.expiry, p.kind, p.strike) for s, p in rows}
        meta.update({b["ticker"]: (b["symbol"], b["expiry"], b["kind"], b["strike"]) for b in blocks or []})
        structs = flow.structures(tapes, meta)
        in_struct = {tk for c in structs for tk in c["tickers"]}
        market = self.cfg.market_symbols
        reads = confirm.reads(self.engine, self.cfg, {s for s, _ in rows} | set(market), local.date())
        info: dict = {}
        text, volumes = unusual_report(self.engine.last_results, local, prev, self.cfg.unusual_top, blocks=blocks,
                                       tapes=tapes, since=since if same_day else None, oi_lines=oi_lines(checks),
                                       material=self.cfg.unusual_material, row_min=self.cfg.unusual_row_min,
                                       quiet=self.cfg.unusual_quiet, structures=structs, confirm=reads,
                                       market=market, info=info)
        log.info("unusual %s: %s · %d active, %d listed (%d >= material), %d minor, %d quiet, %d structures, "
                 "blocks %s, %d tapes", local.strftime("%H:%M"), "SENT" if info.get("material") else
                 ("quiet line" if text else "skipped"), info.get("active", 0), info.get("listed", 0),
                 info.get("big", 0), info.get("minor", 0), info.get("quiet", 0), info.get("structures", 0),
                 "yes" if info.get("blocks") else "no", len(tapes))
        for c in structs:
            log.info("unusual structure %s %s %s: %d contracts matched, premium %.0f", c["symbol"], c["expiry"],
                     "/".join(f"{k:g}{kind[0].upper()}" for kind, k in c["legs"]), c["matched"], c["premium"])
        if text:
            self.store.enqueue("unusual", text)
        price = {r.symbol: r.price for r in self.engine.last_results}
        mkt = confirm.market_lean(reads, market)
        for sym, p in rows:
            c = day["contracts"].setdefault(p.ticker, {"symbol": sym, "kind": p.kind, "strike": p.strike,
                                                       "expiry": p.expiry, "oi": p.oi, "bull": 0.0, "bear": 0.0,
                                                       "total": 0.0})
            c["volume"], c["premium"] = p.volume, p.premium
            t = tapes.get(p.ticker)
            if t is not None:
                if p.ticker not in in_struct:
                    c["bull"] += t.bull
                    c["bear"] += t.bear
                c["total"] += t.total
                c["lean"] = "bull" if c["bull"] >= 0.6 * c["total"] else "bear" if c["bear"] >= 0.6 * c["total"] \
                    else "mixed"
                if (t.lean in ("bull", "bear") and t.total >= FLOW_SCORE_PREMIUM and price.get(sym)
                        and p.ticker not in in_struct):
                    cost = t.total / t.contracts / 100 if t.contracts else None
                    scorecard.record(self.store, now.timestamp(), sym, "flow", t.lean, price[sym], p.ticker, cost)
                    ok, _ = confirm.verdict(t.lean, reads.get(sym), mkt)
                    if ok is not None:
                        scorecard.record(self.store, now.timestamp(), sym,
                                         "flow_confirmed" if ok else "flow_unconfirmed", t.lean, price[sym],
                                         p.ticker, cost)
        self.store.put("unusual_day", day)
        self.store.put("unusual_report", {"tag": tag, "date": today, "volumes": volumes, "ts": now.timestamp()})
        self.store.put("blocks", {"date": today, "items": []})

    # ---- tier 2: batched changes every N minutes ----------------------------------------------

    def _send_changes(self, now: datetime, force: bool = False) -> None:
        """Send the changes collected since the last batch, aligned to 09:30 (09:45, 10:00, ...)."""
        if self.cfg.alert_mode != "tiered":
            return
        local = now.astimezone(self.cfg.tz)
        since_open = (local - datetime.combine(local.date(), OPEN, self.cfg.tz)).total_seconds()
        if since_open < 0:
            return
        minutes = self.cfg.changes_every_minutes
        slot = int(since_open // (minutes * 60))
        last = self.store.get("changes_sent") or {}
        same_day = last.get("date") == str(local.date())
        if not force and (slot == 0 or (same_day and last.get("slot") == slot)):
            return                                   # first batch at 09:45; once per slot
        pending = self.store.get("changes") or {}
        events = pending.get("events", []) if pending.get("date") == str(local.date()) else []
        start = datetime.fromtimestamp(last["ts"], self.cfg.tz) if same_day and last.get("ts") else \
            datetime.combine(local.date(), OPEN, self.cfg.tz)
        ranks = self._sector_ranks()
        prev_ranks = last.get("ranks", {}) if same_day else {}
        from .formatting import changes_message
        text = changes_message(events, start, local, _sector_line(ranks, prev_ranks)) if events else None
        if text:
            self.store.enqueue("changes", text)
        self.store.put("changes", {"date": str(local.date()), "events": []})
        self.store.put("changes_sent", {"date": str(local.date()), "slot": slot, "ts": local.timestamp(),
                                        "ranks": ranks if text else prev_ranks})

    def _sector_ranks(self) -> dict[str, list]:
        from .sectors import read
        reads = [x for x in read(self.engine.last_results, self.cfg.sectors) if x.swing is not None]
        return {x.sector.name: [i, round(x.swing)] for i, x in enumerate(reads, 1)}

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

    def _send_brief(self, now: datetime) -> None:
        if not self.cfg.brief_enabled or self.engine.last_cycle.get("at") != now.isoformat():
            return
        local = now.astimezone(self.cfg.tz)
        due = datetime.combine(local.date(), dtime.fromisoformat(self.cfg.brief_time), self.cfg.tz)
        key = f"brief:{local.date()}"
        if not (due <= local < due + timedelta(minutes=30)) or self.store.get(key):
            return
        self.store.put(key, True)
        try:
            self.store.enqueue("brief", self.brief_text(now))
        except Exception:
            log.exception("morning brief failed")

    async def _send_premarket(self, now: datetime) -> None:
        """07:00 heads-up, 09:15 update ([premarket] times), each once in the 30 minutes after its time."""
        local = now.astimezone(self.cfg.tz)
        if not self.cfg.premarket_enabled or local.weekday() >= 5 or local.time() >= OPEN:
            return
        key = f"premarket:{local.date()}"
        st = self.store.get(key) or {"sent": []}
        for n, t in enumerate(self.cfg.premarket_times):
            due = datetime.combine(local.date(), dtime.fromisoformat(t), self.cfg.tz)
            if t in st["sent"] or not (due <= local < due + timedelta(minutes=30)):
                continue
            st["sent"].append(t)
            self.store.put(key, st)
            try:
                text, snap = await self.premarket_text(now, st.get("first") if n else None)
            except Exception:
                log.exception("pre-market %s failed", t)
                return
            if not snap.get("sectors") and not n:
                log.info("pre-market %s: no pre-market trades (holiday?), nothing sent", t)
                return
            if not n:
                st["first"] = snap
                self.store.put(key, st)
            self.store.enqueue("premarket", text)
            log.info("pre-market %s sent: leader %s, sectors %s", t, snap.get("leader"), snap.get("sectors"))

    async def premarket_text(self, now: datetime, earlier: dict | None = None) -> tuple[str, dict]:
        from . import premarket
        day = str(now.astimezone(self.cfg.tz).date())
        self._pre_cache = {k: v for k, v in self._pre_cache.items() if k.endswith(day)}
        return await premarket.build(self.client, self.store, self.cfg, now, self._pre_cache, earlier)

    def brief_text(self, now: datetime) -> str:
        from .brief import text
        return text(self.engine, self.cfg, now)

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

    async def _run_cycle(self, now: datetime, dry_run: bool = False) -> None:
        try:
            await asyncio.wait_for(self.engine.cycle(now, dry_run=dry_run), timeout=self.cfg.interval_seconds * 0.9)
            ok = self.engine.healthy_cycle()
            if not dry_run:
                self._live_day = now.astimezone(self.cfg.tz).date()
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
        """A live (market-hours) cycle ran on this day; warm-up cycles don't count."""
        return self._live_day == local.date()

    async def _watch_paper_trades(self) -> None:
        """Between cycles, re-price open EMA paper trades every [daytrade] manage_seconds (stops and trails would
        otherwise be checked only every cycle)."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.daytrade.manage_seconds)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.watch_once(datetime.now(timezone.utc))
            except Exception:
                log.exception("paper-trade watch failed")

    async def watch_once(self, now: datetime) -> int:
        from . import emacross
        from .trend import complete_bars, regular_session, to_bars
        if self.market_state != "open":
            return 0
        from . import closeplay
        sent = await closeplay.manage(self.client, self.store, self.cfg, now)     # the 15:59 exit
        if sent:
            await self.dispatcher.flush()
        day = str(now.astimezone(self.cfg.tz).date())
        open_syms = []
        for strategy in self.cfg.daytrade.strategies:
            st = self.store.get(emacross.STRATEGIES[strategy]["key"]) or {}
            if st.get("date") == day:
                open_syms += [s for s in st.get("open") or {} if s not in open_syms]
        if not open_syms:
            return sent
        today = now.astimezone(self.cfg.tz).date()
        five = {}
        for sym in open_syms:                      # fresh 5-minute closes for the invalidation check
            raw = await self.client.aggregates(sym, 5, "minute", today, today)
            five[sym] = complete_bars(regular_session(to_bars(raw), self.cfg.tz), 5, int(now.timestamp() * 1000))
        ema = await emacross.manage(self.client, self.store, self.cfg, now, five)
        if ema:
            await self.dispatcher.flush()
        return sent + ema

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


def _sector_line(ranks: dict[str, list], prev: dict[str, list]) -> str | None:
    """'Sectors: Energy #1 (+90) · Tech ▼ #3 (-18, was -10) · ...' with arrows for rank moves since last batch."""
    if not ranks:
        return None
    parts = []
    for name, (rank, score) in sorted(ranks.items(), key=lambda kv: kv[1][0]):
        old = prev.get(name)
        arrow = ""
        if old and old[0] != rank:
            arrow = "▲ " if rank < old[0] else "▼ "
        was = f", was {old[1]:+d}" if old and abs(old[1] - score) >= 5 else ""
        parts.append(f"{name} {arrow}#{rank} ({score:+d}{was})")
    return "Sectors: " + " · ".join(parts)


def _in_window(local: datetime) -> bool:
    return local.weekday() < 5 and WINDOW_START <= local.time() <= WINDOW_END
