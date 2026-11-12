"""The live shadow-mode observer process (``python -m mias.observer run``).

Tasks: the WebSocket stream (records every AM bar), a 1-second loop (day start with REST warm-up and backfill,
minute flush in (bar end, symbol order), confirmed-event polling, end-of-day finalize, the evening REST-vs-WebSocket
validation) and a status line every 5 minutes. Events go to the observer database only. No Telegram.

Minute flush: bars for minute E are held until every symbol has reported E or E + grace seconds have passed, then
processed in symbol order (SPY first). Before a symbol's bar is processed, a gap since its last processed bar is
filled from REST; if REST cannot be reached the bar is processed with gap=True (stale for 2 bars). After a
reconnect every symbol is backfilled from REST before newer stream bars are evaluated. Bars older than the last
processed bar of that symbol are recorded but never evaluated.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

from ..indicators import Bar
from .log import ObserverDB
from .replay import build_observer, daily_closes, day_of, minute_history, ordered, today_minutes
from .rules import ONE, RULES
from .state import at
from .stream import Recorder, StreamClient

log = logging.getLogger("mias.observer")


class LiveRunner:
    def __init__(self, cfg, settings, client, db: ObserverDB, source, clock=time.time):
        self.cfg, self.s, self.client, self.db, self.source = cfg, settings, client, db, source
        self.tz = cfg.tz
        self.clock = clock
        self.ob = None
        self.day: date | None = None
        self.recorder = Recorder()
        self.pending: dict[int, dict[str, tuple[Bar, float]]] = {}
        self.lock = asyncio.Lock()
        self.finalized: set[str] = set()
        self.validated: set[str] = set()
        self.counts = {"ws": 0, "ok": 0, "dup": 0, "revised": 0, "old": 0, "processed": 0, "gap_filled": 0,
                       "gap_unfilled": 0, "watches": 0}
        self.last_status = 0.0
        self.last_poll = 0.0
        self.stream: StreamClient | None = None

    # ---- clocks -----------------------------------------------------------------------------------

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc).astimezone(self.tz)

    def in_session(self) -> bool:
        n = self.now()
        return n.weekday() < 5 and RULES.session_open <= n.time() < RULES.session_close

    def connect_window(self) -> bool:
        n = self.now()
        return n.weekday() < 5 and self.s.connect_from <= n.time() < self.s.connect_until

    def feed(self, kind: str, detail: str = "") -> None:
        self.db.feed(kind, detail)
        log.info("feed %s %s", kind, detail)

    # ---- day ------------------------------------------------------------------------------------

    async def ensure_day(self) -> None:
        n = self.now()
        if n.weekday() >= 5 or self.day == n.date() or not (dtime(9, 20) <= n.time() < dtime(16, 30)):
            return
        await self.start_day(n.date())

    async def start_day(self, day: date) -> None:
        syms = self.s.symbols
        hist = await minute_history(self.client, self.s.cache_dir, syms, day - timedelta(days=21),
                                    day - timedelta(days=1), self.tz)
        daily = await daily_closes(self.client, self.s.cache_dir, syms, day - timedelta(days=1), self.tz)
        confirms = self.source.confirms(day, syms)
        self.ob = build_observer(syms, day, self.tz, hist, daily, confirms, self.source.options_context)
        self.day = day
        self.pending.clear()
        for ev in confirms:
            self.db.confirm(str(day), ev)
        self.feed("day_start", f"{day}: warmed {len(syms)} symbols, {len(confirms)} confirmed events so far")
        await self.backfill("start")

    async def backfill(self, reason: str) -> None:
        if self.ob is None:
            return
        async with self.lock:
            now_ms = int(self.clock() * 1000)
            new = {}
            for sym in self.s.symbols:
                try:
                    bars = await today_minutes(self.client, sym, self.day, self.tz, now_ms)
                except Exception as exc:
                    self.feed("backfill_failed", f"{sym}: {exc}")
                    continue
                last = self.ob.states[sym].last_e or 0
                new[sym] = [b for b in bars if b.t + ONE > last]
            n = 0
            for sym, b in ordered(self.s.symbols, new):
                self._process(sym, b, f"rest_{reason}", None)
                n += 1
            self.feed("backfill", f"{reason}: {n} bars")

    async def on_connect(self, conn_id: int) -> None:
        if self.ob is not None and self.in_session():
            await self.backfill("reconnect")

    # ---- stream bars ------------------------------------------------------------------------------

    async def on_bar(self, ev: dict, received_at: float, conn: int) -> None:
        status = self.recorder.classify(ev)
        self.db.ws_bar(ev, received_at, status, conn)
        self.counts["ws"] += 1
        self.counts[status] = self.counts.get(status, 0) + 1
        if status != "ok" or self.ob is None or ev.get("sym") not in self.ob.states:
            return
        try:
            b = Bar(int(ev["s"]), float(ev["o"]), float(ev["h"]), float(ev["l"]), float(ev["c"]), float(ev["v"]))
        except (KeyError, TypeError, ValueError):
            return
        t_open, t_close = self.ob.t_open, self.ob.t_close
        if day_of(b.t, self.tz) != self.day or not (t_open <= b.t < t_close):
            return                                            # extended hours: recorded only
        e = b.t + ONE
        self.pending.setdefault(e, {})[ev["sym"]] = (b, received_at)
        if len(self.pending[e]) == len(self.s.symbols):
            await self.flush(force=e)

    async def flush(self, force: int | None = None) -> None:
        if self.ob is None:
            return
        async with self.lock:
            now_ms = int(self.clock() * 1000)
            for e in sorted(self.pending):
                if not (e == force or now_ms >= e + self.s.minute_grace_s * 1000 or
                        (force is not None and e < force)):
                    break
                bars = self.pending.pop(e)
                for sym in self.s.symbols:
                    if sym in bars:
                        b, rec = bars[sym]
                        await self._with_gap_fill(sym, b, rec)

    async def _with_gap_fill(self, sym: str, b: Bar, received_at: float | None) -> None:
        st = self.ob.states[sym]
        last = st.last_e or self.ob.t_open
        if b.t + ONE <= (st.last_e or 0):
            return                                            # already processed (e.g. by a REST backfill)
        gap = False
        if b.t > last:
            try:
                rest = await today_minutes(self.client, sym, self.day, self.tz, b.t)
                fill = [x for x in rest if last <= x.t < b.t]
                for x in fill:
                    self._process(sym, x, "rest_gap", None)
                self.counts["gap_filled"] += 1
                self.feed("gap", f"{sym}: {(b.t - last) // ONE} min before {b.t}, REST filled {len(fill)}")
            except Exception as exc:
                gap = True
                self.counts["gap_unfilled"] += 1
                self.feed("gap_unfilled", f"{sym}: {exc}")
        self._process(sym, b, "ws", received_at, gap)

    def _process(self, sym: str, b: Bar, source: str, received_at: float | None, gap: bool = False) -> None:
        events = self.ob.process(sym, b, gap, received_at)
        self.db.bar1(str(self.day), sym, b, source)
        new = self.db.apply(events, "live")
        self.counts["processed"] += 1
        self.counts["watches"] += new
        for ev in events:
            if ev["kind"] == "watch" and new:
                log.info("SHADOW EARLY WATCH %s %s %s %s @ %.2f (%s)%s", ev["lane"], ev["sym"], ev["side"],
                         ev["structure_type"], ev["price"], ev["leg_id"],
                         " opening_shadow_only" if ev["opening_shadow_only"] else "")
            elif ev["kind"] == "end":
                log.info("SHADOW watch end %s %s (%s)", ev["watch_id"], ev["state"], ev["end_reason"])

    # ---- confirmed events / end of day ------------------------------------------------------------

    def poll_confirms(self) -> None:
        if self.ob is None:
            return
        for ev in self.source.confirms(self.day, self.s.symbols):
            if self.ob.add_confirm(ev):
                self.db.confirm(str(self.day), ev)

    async def end_of_day(self) -> None:
        if self.ob is None or str(self.day) in self.finalized:
            return
        if self.now().date() == self.day and self.now().time() < dtime(16, 3):
            return
        await self.flush(force=max(self.pending) if self.pending else None)
        self.poll_confirms()
        async with self.lock:
            events = self.ob.finalize()
            self.db.apply(events, "live")
            self.db.db.execute("UPDATE watches SET state='EXPIRED', end_reason='session_end_orphan', end_at=? "
                               "WHERE day=? AND state='WATCH'", (self.ob.t_close, str(self.day)))
        self.finalized.add(str(self.day))
        rows = self.db.watches("day = ?", (str(self.day),))
        self.feed("day_end", f"{self.day}: {len(rows)} shadow watches "
                             f"({sum(r['lane'] == 'main' for r in rows)} main, "
                             f"{sum(r['lane'] == 'opening' for r in rows)} opening-shadow)")

    async def validate_day(self) -> None:
        if self.day is None or str(self.day) in self.validated or self.now().time() < dtime(20, 10):
            return
        from .validate import validate
        self.validated.add(str(self.day))
        try:
            text = await validate(self.client, self.db, self.s.symbols, self.day, self.tz)
            out = Path("data/observer/validation") / f"{self.day}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)
            self.feed("validated", str(out))
        except Exception as exc:
            self.feed("validate_failed", str(exc))

    def status(self) -> dict:
        st = {"at": self.now().isoformat(timespec="seconds"), "day": str(self.day) if self.day else None,
              "connected": bool(self.stream and self.stream.connected),
              "last_msg_age_s": round(self.clock() - self.stream.last_msg, 1) if self.stream and self.stream.last_msg
              else None, "reconnects": self.stream.reconnects if self.stream else 0, **self.counts}
        if self.ob is not None:
            now_ms = int(self.clock() * 1000)
            st["lag_min"] = {s: None if x.last_e is None else round((now_ms - x.last_e) / ONE, 1)
                             for s, x in self.ob.states.items()}
            if self.in_session():
                st["stale"] = [s for s, lag in st["lag_min"].items() if lag is None or lag > 3]
        return st

    async def loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.ensure_day()
                await self.flush()
                if self.clock() - self.last_poll >= self.s.confirm_poll_s:
                    self.last_poll = self.clock()
                    self.poll_confirms()
                await self.end_of_day()
                await self.validate_day()
                if self.clock() - self.last_status >= 300:
                    self.last_status = self.clock()
                    st = self.status()
                    self.db.put("status", st)
                    if self.ob is not None:
                        self.db.put(f"state:{self.day}", self.ob.snapshot())
                    log.info("status %s", {k: v for k, v in st.items() if k != "lag_min"})
            except Exception:
                log.exception("observer loop step failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass


async def main(cfg, settings) -> None:
    from ..massive import MassiveClient
    from .sources import MiasSource
    db = ObserverDB(settings.db_path)
    source = MiasSource(Path(cfg.db_path), cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout, max_retries=cfg.max_retries,
                             max_concurrency=4) as client:
        runner = LiveRunner(cfg, settings, client, db, source)
        stream = StreamClient(settings.ws_url, cfg.api_key, settings.symbols, on_bar=runner.on_bar,
                              on_connect=runner.on_connect, on_feed=runner.feed, in_session=runner.in_session,
                              watchdog_s=settings.watchdog_s)
        runner.stream = stream
        runner.feed("start", f"shadow mode, {len(settings.symbols)} symbols, telegram disabled")
        try:
            await asyncio.gather(stream.run(stop, runner.connect_window), runner.loop(stop))
        finally:
            runner.feed("stop", "")
            db.close()
