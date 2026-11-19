"""The live V3 process (``python -m mias.early_watch_v3 run``, user unit ``mias-early-watch-v3``).

Isolation: V3 is its own process with its own state, config, storage, Telegram chat and send queue. It opens NO
market-data WebSocket (a second connection on the same key could disturb the observer's feed); it follows the
completed 1-minute bars the observer has already processed, from data/observer/observer.db ``bars1`` in processing
order (rowid), on a READ-ONLY connection (sqlite mode=ro). Warm-up uses the prior-session files the observer cached
(read-only), else REST into V3's own cache. Confirmed events and the options context are the same read-only MIAS
sources V2 uses. Nothing here writes a V1 / V2 table or file, and nothing V1 / V2 runs imports this package, so a V3
exception, restart or kill switch can never suppress, delay or change a V1 / V2 alert, state or record.

Within V3: a detector error skips that bar for V3 only; a presentation, storage or Telegram error is logged and
recorded, never reaches detection; Telegram sends run from a separate queue / worker with V3's own pacing.
Hot kill switch (early_watch_v3.toml, re-read every reload_seconds): enabled = false stops V3 entirely (idle),
telegram_enabled = false keeps detection + storage and sends nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sqlite3
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

from ..indicators import Bar
from . import config as v3config
from .config import ONE, V3Config
from .detector import V3Detector
from .policy import V3Telegram, v2_policy
from .present import build_view, render
from .store import V3DB

log = logging.getLogger("mias.early_watch_v3")
INDEX = ("SPY", "QQQ", "IWM")
MARKET = ("SPY", "QQQ")


# ---- warm-up data (read-only reuse of the observer's cached files; else REST into V3's own cache) ----------------

class ReadThroughCache:
    """emacross._Cache interface. Looks in V3's cache, then (read-only) in the observer's cache; a miss is fetched
    and written to V3's cache only."""

    def __init__(self, own: Path, fallback: Path | None = None):
        self.root, self.fallback = Path(own), Path(fallback) if fallback else None
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = {"own": 0, "observer": 0, "fetched": 0}

    async def get(self, name: str, fetch):
        fn = name.replace(":", "_").replace("/", "_") + ".json"
        if (self.root / fn).exists():
            self.hits["own"] += 1
            return json.loads((self.root / fn).read_text())
        if self.fallback is not None and (self.fallback / fn).exists():
            self.hits["observer"] += 1
            return json.loads((self.fallback / fn).read_text())
        data = await fetch()
        (self.root / fn).write_text(json.dumps(data))
        self.hits["fetched"] += 1
        return data


async def warm_data(client, v3: V3Config, syms, day: date, tz):
    """Same windows as the observer's start_day: 1-minute bars day-21 .. day-1, daily closes for 420 days."""
    from ..emacross import _five_minute
    from ..observer.replay import day_of, sessions_by_day
    cache = ReadThroughCache(v3.cache_dir, v3.source_cache_dir)
    hist, daily = {}, {}
    for sym in syms:
        bars = await _five_minute(client, cache, sym, day - timedelta(days=21), day - timedelta(days=1), tz, minutes=1)
        hist[sym] = sessions_by_day(bars, tz)
        end = day - timedelta(days=1)
        start = end - timedelta(days=420)
        raw = await cache.get(f"{sym}_1d_{start}_{end}", lambda s=sym, a=start, b=end: client.aggregates(s, 1, "day",
                                                                                                         a, b))
        daily[sym] = [(day_of(int(r["t"]), tz), float(r["c"])) for r in raw if "c" in r]
    return hist, daily, dict(cache.hits)


def build_detector(v3: V3Config, syms, day, tz, hist, daily, confirms, context=None) -> V3Detector:
    det = V3Detector(list(syms), day, tz, v3.rules, context=context)
    for sym in syms:
        prior = [(d, bs) for d, bs in (hist.get(sym) or {}).items() if d < day][-v3.rules.warm_sessions:]
        det.warm(sym, prior, [c for d, c in (daily.get(sym) or []) if d < day])
    for ev in confirms:
        det.add_confirm(ev)
    return det


def ro_connect(path: Path, timeout_s: float = 0.5) -> sqlite3.Connection:
    """READ-ONLY connection to a V1 / V2 / MIAS database (any write raises)."""
    c = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, check_same_thread=False, timeout=timeout_s)
    c.execute(f"PRAGMA busy_timeout={int(timeout_s * 1000)}")
    return c


class V3Runner:
    def __init__(self, cfg, v3: V3Config, store: V3DB, *, source=None, client=None, sender=None, clock=time.time,
                 options_factory=None, warm=None, v2_policy_factory=v2_policy, config_toml: str = "config.toml",
                 mode: str = "live"):
        self.cfg, self.v3, self.store, self.source, self.client, self.sender = cfg, v3, store, source, client, sender
        self.tz = cfg.tz
        self.clock = clock
        self.mode = mode
        self.syms = list(v3.symbols)
        self.det: V3Detector | None = None
        self.day: date | None = None
        self.last_rowid = 0
        self.finalized: set[str] = set()
        self.hot = {"enabled": v3.enabled, "telegram_enabled": v3.telegram_enabled, "send": dict(v3.tg.send)}
        self.policy = V3Telegram(v3.tg.combine_s, v3.tg.max_age_s, self.hot["send"])
        self.options_factory = options_factory
        self.warm = warm or (lambda day: warm_data(self.client, self.v3, self.syms, day, self.tz))
        self.octx = None
        self._v2pol_factory, self._config_toml = v2_policy_factory, config_toml
        self.v2pol, self.v2_on = self._new_v2_policy()
        self.outbox: asyncio.Queue = asyncio.Queue()
        self._bars: sqlite3.Connection | None = None
        self.last_reload = self.last_poll = self.last_status = 0.0
        self.counts = {"bars": 0, "watches": 0, "errors_detector": 0, "errors_present": 0, "errors_store": 0,
                       "errors_deliver": 0, "tg_queued": 0, "tg_sent": 0, "tg_failed": 0, "tg_merged": 0,
                       "tg_skipped": 0, "tg_muted": 0, "tg_disabled": 0}

    def _new_v2_policy(self):
        try:
            from ..observer import settings as obs_settings
            s = obs_settings.load(self._config_toml)
            pol, _ = self._v2pol_factory(self._config_toml)
            return pol, bool(s.v2 and s.v2_telegram)
        except Exception:
            log.exception("V2 policy mirror unavailable (v2_would_send left empty)")
            return None, False

    # ---- clocks / config ------------------------------------------------------------------------------

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc).astimezone(self.tz)

    def reload(self) -> None:
        if self.v3.path is None:
            return
        try:
            h = v3config.hot(self.v3.path)
        except Exception:
            log.exception("could not re-read %s (keeping the current switches)", self.v3.path)
            return
        if h != self.hot:
            self.store.feed("config", f"hot reload: {h}")
            log.info("V3 hot reload: %s", h)
        self.hot = h
        self.policy.send_flags = dict(h["send"])

    # ---- day ------------------------------------------------------------------------------------

    async def ensure_day(self) -> None:
        n = self.now()
        if n.weekday() >= 5 or self.day == n.date() or not (self.v3.day_start_from <= n.time() < self.v3.day_start_until):
            return
        await self.start_day(n.date())

    async def start_day(self, day: date, confirms: list | None = None) -> None:
        hist, daily, hits = await self.warm(day)
        if confirms is None:
            try:
                confirms = self.source.confirms(day, self.syms) if self.source else []
            except Exception:
                log.exception("confirmed events unavailable at day start")
                confirms = []
        if self.octx is not None:
            try:
                self.octx.close()
            except Exception:
                pass
        self.octx = None
        if self.options_factory is not None:
            try:
                self.octx = self.options_factory()
            except Exception:
                log.exception("options context unavailable (Watches show 'Options: unavailable')")
        self.det = build_detector(self.v3, self.syms, day, self.tz, hist, daily, confirms, self.octx)
        if self.octx is not None and hasattr(self.octx, "pivots"):
            self.octx.pivots = lambda s, det=self.det: det.states[s].structure.pivots
        for ev in confirms:
            self.store.confirm(str(day), ev)
        self.day = day
        self.last_rowid = 0
        self.policy = V3Telegram(self.v3.tg.combine_s, self.v3.tg.max_age_s, self.hot["send"])
        self.v2pol, self.v2_on = self._new_v2_policy()
        self.store.feed("day_start", f"{day}: warmed {len(self.syms)} symbols (cache {hits}), "
                                     f"{len(confirms)} confirmed events")
        log.info("V3 day start %s (cache %s)", day, hits)

    # ---- bars -----------------------------------------------------------------------------------

    def _bars_conn(self) -> sqlite3.Connection:
        if self._bars is None:
            self._bars = ro_connect(self.v3.bars_db)
        return self._bars

    def tail(self, limit: int = 5000) -> int:
        """Process the bars the observer has processed since the last call (its order). Returns the count."""
        if self.det is None:
            return 0
        conn = self._bars_conn()
        rows = conn.execute("SELECT rowid, sym, s, o, h, l, c, v, source FROM bars1 WHERE rowid > ? AND day = ? "
                            "ORDER BY rowid LIMIT ?", (self.last_rowid, str(self.day), limit)).fetchall()
        for rowid, sym, s, o, h, l, c, v, source in rows:
            self.last_rowid = rowid
            if sym not in self.det.states:
                continue
            received_at, gap = self._bar_meta(conn, sym, s, source)
            self.process(sym, Bar(int(s), float(o), float(h), float(l), float(c), float(v)), source, rowid,
                         received_at, gap)
        return len(rows)

    @staticmethod
    def _bar_meta(conn, sym: str, s: int, source: str):
        """The stream receive time of the bar, and whether the observer processed it with gap=True (REST gap fill
        failed: recorded as a 'gap_unfilled' feed event while it handled that bar)."""
        if source != "ws":
            return None, False
        try:
            r = conn.execute("SELECT received_at FROM ws_bars WHERE sym=? AND s=? AND status='ok' "
                             "ORDER BY received_at LIMIT 1", (sym, s)).fetchone()
            rec = r[0] if r else None
            gap = False
            if rec is not None:
                gap = conn.execute("SELECT 1 FROM feed_events WHERE kind='gap_unfilled' AND detail LIKE ? AND ts >= ? "
                                   "AND ts <= ? LIMIT 1", (f"{sym}:%", rec, rec + 120)).fetchone() is not None
            return rec, gap
        except sqlite3.Error:
            return None, False

    def process(self, sym: str, b: Bar, source: str, rowid=None, received_at=None, gap: bool = False) -> None:
        seen = self.clock()
        try:
            events = self.det.process(sym, b, gap, received_at)
        except Exception:
            self.counts["errors_detector"] += 1
            log.exception("V3 detector failed on %s %s (bar skipped for V3 only)", sym, b.t)
            return
        self.counts["bars"] += 1
        meta = {}
        for ev in events:
            if ev["kind"] == "v3_watch":
                meta[ev["watch_id"]] = self._present(ev, received_at, seen)
        try:
            self.store.bar(str(self.day), sym, b, source, rowid, received_at, seen, gap)
            new = self.store.apply(events, self.mode, meta)
        except Exception:
            self.counts["errors_store"] += 1
            log.exception("V3 storage failed on %s %s", sym, b.t)
            return
        for ev in events:
            if ev["kind"] == "v3_watch" and ev["watch_id"] in new:
                self.counts["watches"] += 1
                log.info("V3 WATCH %s %s %s %s @ %.2f", ev["family"], ev["lane"], sym, ev["side"], ev["watch_price"])
                self._deliver(ev, meta[ev["watch_id"]])
        self._warm_options(sym)

    def _present(self, ev: dict, received_at, seen) -> dict:
        m = {"bar_received_at": received_at, "bar_seen_at": seen, "created_at": self.clock()}
        try:
            fams = self.det.open_families(ev["sym"], ev["side"])
            prog = self.det.vwap_progress(ev["sym"], ev["side"])
            levels = {f: self.det.open[(f, ev["sym"], ev["side"])].get("level") for f in fams}
            view = build_view(ev, fams, prog, self.v3.pres, self.tz, self.det.t_close, self.v3.options_fresh_s,
                              levels)
            m["view"], m["text"] = view, render(view)
        except Exception as exc:
            self.counts["errors_present"] += 1
            m["render_error"] = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("V3 presentation failed for %s (Watch stored, not sent)", ev["watch_id"])
        return m

    def _warm_options(self, sym: str) -> None:
        if self.octx is None or not hasattr(self.octx, "warm"):
            return
        try:
            self.octx.warm([sym] + ([] if sym in INDEX else list(MARKET)), self.clock())
        except Exception:
            log.debug("options warm-up failed for %s", sym, exc_info=True)

    # ---- delivery -------------------------------------------------------------------------------

    def _deliver(self, w: dict, m: dict) -> None:
        try:
            now_ms = int(self.clock() * 1000)
            v2_verdict, would = None, False
            if self.v2pol is not None:
                try:
                    v2_verdict = self.v2pol.decide(dict(w, family=w["family"]), now_ms)[0]
                    would = self.v2_on and v2_verdict == "send"
                except Exception:
                    log.exception("V2 policy mirror failed for %s", w["watch_id"])
            verdict, info = self.policy.decide(w, now_ms)
            self.store.set_policy(w["watch_id"], verdict, v2_verdict, would)
            if verdict == "send":
                if not self.hot["telegram_enabled"]:
                    self.counts["tg_disabled"] += 1
                    self.store.mark_telegram(w["watch_id"], False, None, "not sent: telegram_enabled = false", info,
                                             self.clock())
                elif self.sender is None or not m.get("text"):
                    self.store.mark_telegram(w["watch_id"], False, None,
                                             "not sent: " + ("no V3 sender" if self.sender is None else
                                                             "render failed"), info, self.clock())
                else:
                    self.outbox.put_nowait((w["watch_id"], m["text"], info))
                    self.counts["tg_queued"] += 1
                    self.store.mark_queued(w["watch_id"], self.clock(), info)
            else:
                k = {"merged": "tg_merged", "muted": "tg_muted"}.get(verdict, "tg_skipped")
                self.counts[k] += 1
                err = f"merged into {info}" if verdict == "merged" else f"{'muted' if verdict == 'muted' else 'not sent'}: {info}"
                self.store.mark_telegram(w["watch_id"], False, None, err, info if verdict == "merged" else None,
                                         self.clock())
        except Exception:
            self.counts["errors_deliver"] += 1
            log.exception("V3 delivery decision failed for %s", w.get("watch_id"))

    async def send_worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                wid, text, group = await asyncio.wait_for(self.outbox.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            await self.send_one(wid, text, group)

    async def send_one(self, wid: str, text: str, group) -> None:
        if not (self.hot["enabled"] and self.hot["telegram_enabled"]):
            self.counts["tg_disabled"] += 1
            self.store.mark_telegram(wid, False, None, "not sent: switched off before send", group, self.clock())
            return
        try:
            ids = await asyncio.wait_for(self.sender.send(text), timeout=self.v3.tg.send_timeout_s)
            self.store.mark_telegram(wid, True, ids, None, group, self.clock(), text)
            self.counts["tg_sent"] += 1
        except Exception as exc:                      # delivery only: never reaches detection / storage of the Watch
            self.counts["tg_failed"] += 1
            try:
                self.store.mark_telegram(wid, False, None, f"{type(exc).__name__}: {exc}"[:300], group, self.clock())
                self.store.feed("telegram_failed", f"{wid}: {type(exc).__name__}")
            except Exception:
                log.exception("could not record the V3 telegram failure")

    # ---- confirms / end of day ------------------------------------------------------------------

    def poll_confirms(self) -> None:
        if self.det is None or self.source is None:
            return
        try:
            for ev in self.source.confirms(self.day, self.syms):
                if self.det.add_confirm(ev):
                    self.store.confirm(str(self.day), ev)
        except Exception:
            log.exception("V3 confirm poll failed")

    def end_of_day(self, force: bool = False) -> str | None:
        if self.det is None or str(self.day) in self.finalized:
            return None
        if not force and self.now().date() == self.day and self.now().time() < self.v3.end_of_day:
            return None
        self.tail()
        self.poll_confirms()
        day = str(self.day)
        try:
            self.store.apply(self.det.finalize(), self.mode)
            self.store.db.execute("UPDATE v3_watches SET state='EXPIRED', end_reason='session_end_orphan', end_at=? "
                                  "WHERE day=? AND state='WATCH'", (self.det.t_close, day))
        except Exception:
            log.exception("V3 finalize failed")
        self.finalized.add(day)
        try:
            from .report import build, fill_outcomes
            fill_outcomes(self.store, day, self.det.t_close)
            out = Path(self.store.path).parent / ("daily" if self.mode == "live" else "replay_daily") / day
            text = build(self.store, self.tz, day, self.v3.v2_db, out)
            self.store.feed("day_end", f"{day}: report {out / 'v3_report.md'}")
            return text
        except Exception:
            log.exception("V3 end-of-day report failed")
            return None

    def status(self) -> dict:
        return {"at": self.now().isoformat(timespec="seconds"), "day": str(self.day) if self.day else None,
                "last_rowid": self.last_rowid, "hot": self.hot, "backlog": self.outbox.qsize(),
                "tg_rate_limited": getattr(self.sender, "rate_limited", 0), **self.counts}

    async def step(self) -> None:
        if self.clock() - self.last_reload >= self.v3.reload_s:
            self.last_reload = self.clock()
            self.reload()
        if not self.hot["enabled"]:
            return                                      # kill switch: V3 entirely off (idle)
        await self.ensure_day()
        self.tail()
        if self.clock() - self.last_poll >= self.v3.confirm_poll_s:
            self.last_poll = self.clock()
            self.poll_confirms()
        self.end_of_day()
        if self.clock() - self.last_status >= 300:
            self.last_status = self.clock()
            self.store.put("status", self.status())

    async def loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.step()
            except Exception:
                log.exception("V3 loop step failed (V1 / V2 unaffected: separate process)")
                self._bars = None                       # reconnect the read-only bar source next time
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.v3.poll_s)
            except asyncio.TimeoutError:
                pass


def make_sender(cfg, v3: V3Config):
    """V3's own sender instance (own pacing state = own rate-limit queue) to the V3 chat. Falls back to the V1 / V2
    alert chats only if no V3 chat is configured; even then the queue and pacing are V3's own."""
    from ..observer.telegram import WatchSender

    class V3Sender(WatchSender):
        def _gap(self, chat: str) -> float:
            return v3.tg.group_gap_s if str(chat).startswith("-") else v3.tg.private_gap_s

    chats = [v3.chat_id] if v3.chat_id else list(cfg.telegram_alert_chats)
    if not (cfg.telegram_token and chats):
        return None, chats
    return V3Sender(cfg.telegram_token, chats, max_wait_s=v3.tg.max_wait_s), chats


async def main(cfg, v3: V3Config) -> None:
    from ..massive import MassiveClient
    from ..observer.optctx import OptionsContext
    from ..observer.sources import MiasSource
    store = V3DB(v3.db_path)
    source = MiasSource(Path(cfg.db_path), cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    sender, chats = make_sender(cfg, v3)
    async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout, max_retries=cfg.max_retries,
                             max_concurrency=2) as client:
        runner = V3Runner(cfg, v3, store, source=source, client=client, sender=sender,
                          options_factory=lambda: OptionsContext(Path(cfg.db_path), cfg.tz, source="live snapshot"))
        store.feed("start", f"{len(v3.symbols)} symbols, enabled {v3.enabled}, telegram "
                            f"{'ON' if sender and v3.telegram_enabled else 'off'} "
                            f"({'V3 chat' if v3.chat_id else 'fallback: V1/V2 chats, own queue'}), families "
                            f"{', '.join(f for f, on in v3.tg.send.items() if on)}")
        try:
            await asyncio.gather(runner.loop(stop), runner.send_worker(stop))
        finally:
            store.feed("stop", "")
            if sender:
                await sender.close()
            store.close()
