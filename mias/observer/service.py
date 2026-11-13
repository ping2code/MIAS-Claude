"""The live shadow-mode observer process (``python -m mias.observer run``).

Tasks: the WebSocket stream (records every AM bar), a 1-second loop (day start with REST warm-up and backfill,
minute flush in (bar end, symbol order), confirmed-event polling, end-of-day finalize, the evening REST-vs-WebSocket
validation) and a status line every 5 minutes. Events go to the observer database only. No Telegram.

Minute flush: bars for minute E are held until every symbol has reported E or E + grace seconds have passed, then
processed in symbol order (SPY first). Before a symbol's bar is processed, a gap since its last processed bar is
filled from REST; if REST cannot be reached the bar is processed with gap=True (stale for 2 bars). After a
reconnect every symbol is backfilled from REST before newer stream bars are evaluated. Bars older than the last
processed bar of that symbol are recorded but never evaluated.

Telegram pilot ([observer] telegram = true): after a new main-lane watch is stored, its initial message (and nothing
else) is queued for a separate send worker if the watch is fresh (created within telegram_max_age_seconds, so a
restart backfill never re-sends or sends stale watches). The send outcome is written to the watch row; a failed or
slow send never blocks or changes observer state.

Early Watch v2 research ([observer] v2 = true): a separate V2Observer (own state instances, own database
data/observer/v2.db) receives the same bars right after v1 has finished with them; any v2 error is logged and never
reaches v1. v2 Telegram ([observer] v2_telegram) has its own policy (mias.observer.v2.V2Telegram) and queue.
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
# Upper bound for one queued message, including waiting behind other messages for the shared, paced sender (one
# group chat = 1 message per 3 s, so a burst of ~50 messages still fits). Not a delivery cap.
SEND_TIMEOUT_S = 180


class LiveRunner:
    def __init__(self, cfg, settings, client, db: ObserverDB, source, clock=time.time, sender=None, v2db=None):
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
        self.sender = sender
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.counts.update({"tg_queued": 0, "tg_sent": 0, "tg_failed": 0, "tg_skipped_stale": 0})
        from .v2 import V2Telegram
        self.v2db = v2db
        self.v2 = None
        self.v2tg = V2Telegram(max_age_s=settings.telegram_max_age_s)
        self.v2_outbox: asyncio.Queue = asyncio.Queue()
        self.counts.update({"v2_watches": 0, "v2_errors": 0, "v2_tg_sent": 0, "v2_tg_failed": 0, "v2_tg_merged": 0,
                            "v2_tg_skipped": 0})

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
        if self.v2db is not None:
            try:
                from .v2_replay import build_v2
                self.v2 = build_v2(syms, day, self.tz, hist, daily, confirms, self.source.options_context)
                for ev in confirms:
                    self.v2db.confirm(str(day), ev)
            except Exception:
                self.v2 = None
                log.exception("v2 start failed (v1 unaffected)")
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

    def _fresh(self, kind: str, sym: str, *args) -> list[dict]:
        """Measurement only (mias/observer/fresh.py); a failure here never reaches the observers."""
        from .fresh import V1Fresh, V2Fresh
        if kind == "v1" and not hasattr(self.ob, "lanes") or kind == "v2" and not hasattr(self.v2, "open"):
            return []                                       # not a real observer (test doubles)
        try:
            if kind == "v1":
                if getattr(self, "_v1fresh", None) is None or self._v1fresh.ob is not self.ob:
                    self._v1fresh = V1Fresh(self.ob)
                return self._v1fresh.after(sym, *args)
            if getattr(self, "_v2fresh", None) is None or self._v2fresh.v2 is not self.v2:
                self._v2fresh = V2Fresh(self.v2)
            return self._v2fresh.after(sym, *args)
        except Exception:
            self.counts["fresh_errors"] = self.counts.get("fresh_errors", 0) + 1
            log.exception("fresh-candidate instrumentation failed for %s (observers unaffected)", sym)
            return []

    def _process(self, sym: str, b: Bar, source: str, received_at: float | None, gap: bool = False) -> None:
        p_before = getattr(self.ob, "prev", {}).get(sym)
        events = self.ob.process(sym, b, gap, received_at)
        events = events + self._fresh("v1", sym, p_before, events)
        self.db.bar1(str(self.day), sym, b, source)
        new = self.db.apply(events, "live")
        self.counts["processed"] += 1
        self.counts["watches"] += len(new)
        for ev in events:
            if ev["kind"] == "watch" and ev["watch_id"] in new:
                self._maybe_queue(ev)
                log.info("SHADOW EARLY WATCH %s %s %s %s @ %.2f (%s)%s", ev["lane"], ev["sym"], ev["side"],
                         ev["structure_type"], ev["price"], ev["leg_id"],
                         " opening_shadow_only" if ev["opening_shadow_only"] else "")
            elif ev["kind"] == "end":
                log.info("SHADOW watch end %s %s (%s)", ev["watch_id"], ev["state"], ev["end_reason"])
        if self.v2 is not None:
            self._process_v2(sym, b, source, received_at, gap)

    def _process_v2(self, sym: str, b: Bar, source: str, received_at: float | None, gap: bool) -> None:
        """Research track; runs after v1 is done with the bar. Errors stay here."""
        try:
            events = self.v2.process(sym, b, gap, received_at)
            events = events + self._fresh("v2", sym, events)
            self.v2db.bar1(str(self.day), sym, b, source)
            new = self.v2db.apply_v2(events, "live", received_at)
            self.counts["v2_watches"] += len(new)
            for ev in events:
                if ev["kind"] == "v2_watch" and ev["watch_id"] in new:
                    log.info("V2 WATCH %s %s %s %s @ %.2f (%s, pre-EMA %s)", ev["family"], ev["lane"], ev["sym"],
                             ev["side"], ev["watch_price"], ev["leg_id"], ev["pre_ema"])
                    self._maybe_queue_v2(ev)
        except Exception:
            self.counts["v2_errors"] += 1
            log.exception("v2 processing failed for %s (v1 unaffected)", sym)

    # ---- Telegram pilot ---------------------------------------------------------------------------

    def _maybe_queue(self, ev: dict) -> None:
        if not (self.s.telegram and self.sender is not None) or ev["lane"] != "main" or ev["opening_shadow_only"]:
            return
        age = self.clock() - ev["created_at"] / 1000
        if age > self.s.telegram_max_age_s:
            self.counts["tg_skipped_stale"] += 1
            self.db.mark_telegram(ev["watch_id"], False, None, f"not sent: {age:.0f}s old when found", self.clock())
            return
        self.outbox.put_nowait(ev)
        self.counts["tg_queued"] += 1

    def _maybe_queue_v2(self, w: dict) -> None:
        if not (self.s.v2_telegram and self.sender is not None):
            return
        verdict, info = self.v2tg.decide(w, int(self.clock() * 1000))
        if verdict == "send":
            fams = sorted({k[0] for k in self.v2.open if k[1] == w["sym"] and k[2] == w["side"]} | {w["family"]})
            self.v2_outbox.put_nowait((w, fams, info))
        elif verdict == "merged":
            self.counts["v2_tg_merged"] += 1
            self.v2db.mark_v2_telegram(w["watch_id"], False, None, f"merged into {info}", info, self.clock())
        else:
            self.counts["v2_tg_skipped"] += 1
            self.v2db.mark_v2_telegram(w["watch_id"], False, None, f"not sent: {info}", None, self.clock())

    async def v2_send_worker(self, stop: asyncio.Event) -> None:
        from .v2 import v2_text
        while not stop.is_set():
            try:
                w, fams, group = await asyncio.wait_for(self.v2_outbox.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            try:
                ids = await asyncio.wait_for(self.sender.send(v2_text(w, fams, self.tz)), timeout=SEND_TIMEOUT_S)
                self.v2db.mark_v2_telegram(w["watch_id"], True, ids, None, group, self.clock())
                self.counts["v2_tg_sent"] += 1
            except Exception as exc:
                self.counts["v2_tg_failed"] += 1
                try:
                    self.v2db.mark_v2_telegram(w["watch_id"], False, None, f"{type(exc).__name__}: {exc}"[:300],
                                               group, self.clock())
                    self.feed("v2_telegram_failed", f"{w['watch_id']}: {type(exc).__name__}")
                except Exception:
                    log.exception("could not record the v2 telegram failure")

    async def send_worker(self, stop: asyncio.Event) -> None:
        from .message import watch_text
        while not stop.is_set():
            try:
                ev = await asyncio.wait_for(self.outbox.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            try:
                ids = await asyncio.wait_for(self.sender.send(watch_text(ev, self.tz)), timeout=SEND_TIMEOUT_S)
                self.db.mark_telegram(ev["watch_id"], True, ids, None, self.clock())
                self.counts["tg_sent"] += 1
                log.info("telegram sent %s (message %s)", ev["watch_id"], ",".join(ids))
            except Exception as exc:                      # never let delivery affect the observer
                self.counts["tg_failed"] += 1
                try:
                    self.db.mark_telegram(ev["watch_id"], False, None, f"{type(exc).__name__}: {exc}"[:300],
                                          self.clock())
                    self.feed("telegram_failed", f"{ev['watch_id']}: {type(exc).__name__}")
                except Exception:
                    log.exception("could not record the telegram failure")

    # ---- confirmed events / end of day ------------------------------------------------------------

    def poll_confirms(self) -> None:
        if self.ob is None:
            return
        for ev in self.source.confirms(self.day, self.s.symbols):
            if self.ob.add_confirm(ev):
                self.db.confirm(str(self.day), ev)
            if self.v2 is not None:
                try:
                    if self.v2.add_confirm(ev):
                        self.v2db.confirm(str(self.day), ev)
                except Exception:
                    log.exception("v2 confirm failed")

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
        try:
            from .report import build
            build(self.db, self.tz, f"Early Watch v1 — live session {self.day}",
                  Path("data/observer/daily") / str(self.day), day=str(self.day))
        except Exception:
            log.exception("end-of-day report failed")
        if self.v2 is not None:
            try:
                async with self.lock:
                    self.v2db.apply_v2(self.v2.finalize(), "live")
                    self.v2db.db.execute("UPDATE v2_watches SET state='EXPIRED', end_reason='session_end_orphan', "
                                         "end_at=? WHERE day=? AND state='WATCH'", (self.ob.t_close, str(self.day)))
                from .v2_report import build_v2_report
                build_v2_report(self.v2db, self.tz, Path("data/observer/daily") / str(self.day), day=str(self.day),
                                v1db=self.db, title=f"Early Watch v1 vs v2 — live session {self.day}")
            except Exception:
                log.exception("v2 end of day failed")
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
        try:
            import resource
            ru = resource.getrusage(resource.RUSAGE_SELF)
            st["rss_mb"] = round(ru.ru_maxrss / 1024, 1)
            st["cpu_s"] = round(ru.ru_utime + ru.ru_stime, 1)
        except Exception:
            pass
        if self.sender is not None:
            st["tg_rate_limited"] = getattr(self.sender, "rate_limited", 0)
            st["tg_backlog"] = self.outbox.qsize() + self.v2_outbox.qsize()
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
    sender = None
    v2db = None
    if settings.v2:
        from .v2_log import V2DB
        v2db = V2DB(settings.v2_db_path)
    if settings.telegram or (settings.v2 and settings.v2_telegram):
        if cfg.telegram_token and cfg.telegram_alert_chats:
            from .telegram import WatchSender
            sender = WatchSender(cfg.telegram_token, cfg.telegram_alert_chats)
        else:
            log.warning("observer telegram = true but no bot token / alert chat: watches are only logged")
    async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout, max_retries=cfg.max_retries,
                             max_concurrency=4) as client:
        runner = LiveRunner(cfg, settings, client, db, source, sender=sender, v2db=v2db)
        stream = StreamClient(settings.ws_url, cfg.api_key, settings.symbols, on_bar=runner.on_bar,
                              on_connect=runner.on_connect, on_feed=runner.feed, in_session=runner.in_session,
                              watchdog_s=settings.watchdog_s)
        runner.stream = stream
        runner.feed("start", f"{len(settings.symbols)} symbols, v1 telegram "
                             f"{'PILOT ON (initial main-lane watch messages only)' if sender and settings.telegram else 'off'}"
                             f", v2 research {'ON' if v2db else 'off'}, v2 telegram "
                             f"{'ON' if sender and v2db and settings.v2_telegram else 'off'}")
        tasks = [stream.run(stop, runner.connect_window), runner.loop(stop)]
        if sender:
            tasks.append(runner.send_worker(stop))
            if v2db is not None and settings.v2_telegram:
                tasks.append(runner.v2_send_worker(stop))
        try:
            await asyncio.gather(*tasks)
        finally:
            runner.feed("stop", "")
            if sender:
                await sender.close()
            db.close()
            if v2db is not None:
                v2db.close()
