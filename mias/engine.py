"""One analysis cycle: fetch, analyze, persist, evaluate alerts. A failing symbol never stops the others."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from . import formatting, signals
from .config import Config
from .massive import MassiveClient
from .models import SymbolResult
from .notify import Dispatcher
from .options import Window, analyze, options_bias
from .store import Store
from .trend import direction, label_for, stock_trend

log = logging.getLogger(__name__)

# A volume baseline older than this is discarded rather than turned into one oversized flow window.
MAX_BASELINE_AGE_S = 15 * 60


class Engine:
    def __init__(self, cfg: Config, client: MassiveClient, store: Store, dispatcher: Dispatcher | None):
        self.cfg = cfg
        self.client = client
        self.store = store
        self.dispatcher = dispatcher
        self._hist5: dict[str, tuple[date, list[dict]]] = {}
        self._daily: dict[str, tuple[date, list[dict]]] = {}
        self.last_results: list[SymbolResult] = []
        self.last_market: tuple[str, float, float | None] | None = None
        self.last_cycle: dict = {}

    # ---- public -----------------------------------------------------------------------------

    async def cycle(self, now: datetime | None = None, *, dry_run: bool = False) -> list[SymbolResult]:
        now = now or datetime.now(timezone.utc)
        t0, req0 = time.monotonic(), self.client.requests
        today = now.astimezone(self.cfg.tz).date()

        try:
            snaps = await self.client.snapshots(self.cfg.symbols)
        except Exception as exc:
            log.error("snapshot request failed: %s", exc)
            snaps = {}

        results = await asyncio.gather(*(self._symbol(s, snaps.get(s, {}), now, today, dry_run)
                                         for s in self.cfg.symbols))
        results = list(results)

        if not dry_run:
            ts = now.timestamp()
            self.store.add_metrics(ts, today.isoformat(), {r.symbol: r.to_dict() for r in results if r.ok})
            self._evaluate(results, now)
            if self.dispatcher:
                await self.dispatcher.flush()

        mc = signals.market_composite(results, self.cfg.market_symbols)
        self.last_market = (label_for(mc[0], self.cfg.trend_strong, self.cfg.trend_bias), *mc) if mc else None
        self.last_results = results
        ok = sum(1 for r in results if r.ok and not r.errors)
        self.last_cycle = {
            "at": now.isoformat(), "duration_s": round(time.monotonic() - t0, 2),
            "requests": self.client.requests - req0, "symbols": len(results), "clean": ok,
            "failed": [r.symbol for r in results if not r.ok],
            "partial": [r.symbol for r in results if r.ok and r.errors],
        }
        log.info("cycle done: %s", self.last_cycle)
        return results

    async def analyze_one(self, sym: str, now: datetime | None = None) -> SymbolResult:
        """Fresh read of one symbol for an on-demand request. Reads alert state, never writes it."""
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(self.cfg.tz).date()
        try:
            snaps = await self.client.snapshots([sym])
        except Exception as exc:
            log.warning("snapshot for %s failed: %s", sym, exc)
            snaps = {}
        if snaps.get(sym, {}).get("error") or not snaps.get(sym, {}).get("session"):
            snaps.pop(sym, None)
        if sym not in snaps and sym not in self.cfg.symbols:
            return SymbolResult(sym, errors=["unknown symbol or no market data"])
        return await self._symbol(sym, snaps.get(sym, {}), now, today, dry_run=True)

    def healthy_cycle(self) -> bool:
        c = self.last_cycle
        return bool(c) and len(c.get("failed", [])) <= c.get("symbols", 0) // 2

    # ---- per symbol -------------------------------------------------------------------------

    async def _symbol(self, sym: str, snap: dict, now: datetime, today: date, dry_run: bool) -> SymbolResult:
        inst = self.cfg.instruments.get(sym)
        if inst:
            return await self._instrument(sym, inst, now, today, dry_run)
        r = SymbolResult(sym, errors=[])
        session = snap.get("session") or {}
        r.stock_price = _num(session.get("price")) or _num(session.get("close"))
        r.prev_close = _num(session.get("previous_close"))

        try:
            trend = await self._stock(sym, now, today)
            r.stock = trend if trend.timeframes else None
            if r.stock is None:
                r.errors.append("stock: not enough price history")
            if r.stock_price is None and r.stock and r.stock.last_close:
                r.stock_price = r.stock.last_close
        except Exception as exc:
            r.errors.append(f"stock: {exc}")
            log.warning("%s stock trend failed: %s", sym, exc)

        if r.stock_price:
            if self.cfg.stock_delay_minutes == 0:
                r.live_price = r.stock_price   # real-time stock feed
            try:
                await self._options(r, now, today, dry_run)
            except Exception as exc:
                r.errors.append(f"options: {exc}")
                log.warning("%s options failed: %s", sym, exc)
        else:
            r.errors.append("no reference price")
        return r

    async def _instrument(self, sym: str, inst, now: datetime, today: date, dry_run: bool) -> SymbolResult:
        """Index-style symbol: trend from a proxy's bars, price and options read from its own option chain."""
        r = SymbolResult(sym, errors=[], source_note=inst.note or None)
        try:
            trend = await self._stock(inst.trend_symbol, now, today)
            r.stock = trend if trend.timeframes else None
        except Exception as exc:
            r.errors.append(f"trend ({inst.trend_symbol}): {exc}")
            log.warning("%s trend via %s failed: %s", sym, inst.trend_symbol, exc)
        if inst.close_symbol:
            try:
                r.prev_close = await self._prev_close(inst.close_symbol, today)
            except Exception as exc:
                log.warning("%s previous close failed: %s", sym, exc)
        last = self.store.get(f"spot:{sym}")
        last_day = datetime.fromtimestamp(last["ts"], self.cfg.tz).date() if last else None
        # Without index bars (e.g. SPX) the previous close is the last price seen on an earlier day.
        prev_seen = (last.get("prev") if last_day == today else last["price"]) if last else None
        r.prev_close = r.prev_close or prev_seen
        ref = last["price"] if last and now.timestamp() - last.get("ts", 0) < 5 * 86400 else r.prev_close
        if not ref:
            try:
                ref = await self.client.underlying_value(inst.options_symbol)
            except Exception as exc:
                log.warning("%s underlying value failed: %s", sym, exc)
        if not ref:
            r.errors.append("no reference price")
            return r
        r.stock_price = ref          # only a reference for the chain window; the live price comes from the chain
        try:
            await self._options(r, now, today, dry_run, chain_symbol=inst.options_symbol,
                                strike_pct=inst.chain_strike_pct)
        except Exception as exc:
            r.errors.append(f"options: {exc}")
            log.warning("%s options failed: %s", sym, exc)
        if r.options and r.options.implied_spot:
            r.live_price = r.stock_price = r.options.implied_spot
            if not dry_run:
                self.store.put(f"spot:{sym}", {"ts": now.timestamp(), "price": r.live_price, "prev": prev_seen})
        else:
            r.live_price = None
            r.errors.append("no live price from option quotes")
        return r

    async def _prev_close(self, symbol: str, today: date) -> float | None:
        cached = self._daily.get(f"close:{symbol}")
        if cached and cached[0] == today:
            return cached[1]
        bars = await self.client.aggregates(symbol, 1, "day", today - timedelta(days=10), today - timedelta(days=1))
        # Index feeds can carry a weekend bar; the previous close is the last weekday session before today.
        closes = [b["c"] for b in bars
                  if (d := datetime.fromtimestamp(b["t"] / 1000, self.cfg.tz).date()) < today and d.weekday() < 5]
        value = closes[-1] if closes else None
        self._daily[f"close:{symbol}"] = (today, value)
        return value

    async def _stock(self, sym: str, now: datetime, today: date):
        cfg = self.cfg
        hist = self._hist5.get(sym)
        if not hist or hist[0] != today:
            raw = await self.client.aggregates(sym, 5, "minute", today - timedelta(days=40), today - timedelta(days=1))
            hist = (today, raw)
            self._hist5[sym] = hist
        daily = self._daily.get(sym)
        if not daily or daily[0] != today:
            raw = await self.client.aggregates(sym, 1, "day", today - timedelta(days=420), today - timedelta(days=1))
            daily = (today, raw)
            self._daily[sym] = daily
        today5 = await self.client.aggregates(sym, 5, "minute", today, today)

        available_until = int((now.timestamp() - cfg.stock_delay_minutes * 60) * 1000)
        five = hist[1] + today5
        days = list(daily[1])
        today_bar = _session_bar(today5, cfg.tz, available_until)
        if today_bar:
            days.append(today_bar)
        return stock_trend(five, days, tz=cfg.tz, available_until_ms=available_until,
                           profiles=cfg.trend_profiles, primary=cfg.trend_primary,
                           strong=cfg.trend_strong, bias=cfg.trend_bias)

    async def _options(self, r: SymbolResult, now: datetime, today: date, dry_run: bool, *,
                       chain_symbol: str | None = None, strike_pct: float | None = None) -> None:
        cfg, sym = self.cfg, r.symbol
        chain, truncated = await self.client.option_chain(
            chain_symbol or sym, spot=r.stock_price, today=today, max_dte=cfg.chain_max_dte,
            strike_pct=strike_pct or cfg.chain_strike_pct, max_pages=cfg.max_chain_pages)
        r.chain_truncated = truncated
        if truncated:
            r.errors.append("options chain truncated")
        if not chain:
            raise RuntimeError("empty options chain")

        ts = now.timestamp()
        base = self.store.get(f"vol:{sym}")
        prev = None
        if base and base.get("date") == today.isoformat() and ts - base.get("ts", 0) <= MAX_BASELINE_AGE_S:
            prev = base["v"]

        snap, volumes = analyze(
            chain, ref_spot=r.stock_price, prev_volumes=prev, today=today, tz=cfg.tz,
            now_ns=int(ts * 1e9), block_premium=cfg.block_premium,
            unusual_min_premium=cfg.unusual_min_premium, unusual_vol_oi_ratio=cfg.unusual_vol_oi_ratio,
            alert_0dte=cfg.alert_0dte, thin_volume=cfg.thin_options_volume,
            idea_templates=cfg.idea_templates)
        r.options = snap
        if cfg.stock_delay_minutes:
            r.live_price = snap.implied_spot     # delayed stock feed: estimate the live price from option quotes

        flow = self.store.get(f"flow:{sym}")
        if not flow or flow.get("date") != today.isoformat():
            flow = {"date": today.isoformat(), "since": ts, "session": asdict(Window(ts=ts)), "recent": [],
                    "rr_open": None}
        session_w = Window(**flow["session"])
        if snap.window:
            session_w.add(snap.window)
            flow["recent"] = (flow["recent"] + [asdict(snap.window)])[-cfg.recent_windows:]
        flow["session"] = asdict(session_w)
        if flow.get("rr_open") is None and snap.rr25 is not None:
            flow["rr_open"] = snap.rr25

        recent = None
        if flow["recent"]:
            recent = Window(ts=ts)
            for w in flow["recent"]:
                recent.add(Window(**w))
        r.bias = options_bias(snap, session_w if flow["recent"] else None, recent, flow["rr_open"],
                              strong=cfg.options_strong, bias=cfg.options_bias)

        if not dry_run:
            self.store.put(f"vol:{sym}", {"date": today.isoformat(), "ts": ts, "v": volumes})
            self.store.put(f"flow:{sym}", flow)

    # ---- alerts -----------------------------------------------------------------------------

    def _evaluate(self, results: list[SymbolResult], now: datetime) -> None:
        cfg, ts = self.cfg, now.timestamp()
        local = now.astimezone(cfg.tz)
        for r in results:
            if not r.ok:
                continue
            key = f"state:{r.symbol}"
            state = self.store.get(key, {})
            iv_hist = []
            for hts, d in self.store.history(r.symbol, ts - cfg.iv_spike_window_minutes * 60):
                iv = (d.get("options") or {}).get("atm_iv_front")
                if iv is not None and hts < ts:
                    iv_hist.append((hts, iv))
            try:
                if cfg.alert_mode == "strong_alignment":
                    reasons = signals.evaluate_strong(r, state, cfg, ts)
                else:
                    reasons = signals.evaluate(r, state, cfg, ts, iv_hist)
            except Exception:
                log.exception("%s: alert evaluation failed", r.symbol)
                continue
            self.store.put(key, state)
            if reasons:
                text = formatting.symbol_alert(r, [(x.direction, x.text) for x in reasons], local, cfg)
                self.store.enqueue(",".join(sorted({x.rule for x in reasons})), text, r.symbol)

        mc = signals.market_composite(results, cfg.market_symbols)
        if mc and cfg.alert_mode == "all":
            stock, opts = mc
            new = label_for(stock, cfg.trend_strong, cfg.trend_bias)
            mstate = self.store.get("state:__market__", {})
            change = signals._confirm(mstate.setdefault("trend", {}), direction(new), cfg.confirm_cycles)
            if change and signals._cooled(mstate, "market", ts, cfg.cooldown_minutes):
                old_label = {"bull": "up", "bear": "down", "neutral": "neutral"}[change[0]]
                self.store.enqueue("market", formatting.market_alert(
                    old_label, new, stock, opts, [s for s in cfg.market_symbols if s in cfg.symbols], local))
            self.store.put("state:__market__", mstate)

    def digest_text(self, title: str, now: datetime) -> str:
        return formatting.digest(self.last_results, self.last_market, title, now.astimezone(self.cfg.tz),
                                 self.cfg.stock_delay_minutes)


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _session_bar(raw5: list[dict], tz, available_until_ms: int) -> dict | None:
    """Today's partial daily bar built from completed regular-session 5-minute bars."""
    from .trend import complete_bars, regular_session, to_bars
    bars = complete_bars(regular_session(to_bars(raw5), tz), 5, available_until_ms)
    if not bars:
        return None
    return {"t": bars[0].t, "o": bars[0].o, "h": max(b.h for b in bars), "l": min(b.l for b in bars),
            "c": bars[-1].c, "v": sum(b.v for b in bars)}
