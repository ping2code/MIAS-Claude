"""Long-history backtest of the swing entries: pullback-over (the tier-1 alert) against simpler entries, traded with
real option prices. `python -m mias pullback-backtest`.

Scores: the same swing and intraday composites as the live service, rebuilt every 30 minutes from 5-minute and
daily bars (the live service checks every 2.5 minutes, so "2 confirming checks" here means 2 half-hours). Options
flow history doesn't exist that far back, so the "options not against" filter is off.

Entry groups (bull and the bear mirror):
  pullback   the live rule: swing UP+, intraday dips to -15 or lower, recovers to +35 for 2 checks
  strong     swing and intraday both STRONG (>= 60) for 2 checks (the strong-alignment alert, stock side only)
  trend      control: every 5th trading day at 10:30 while swing is UP+ (just buy the trend)

Each entry buys, at the next hourly option bar after the signal:
  single  the ~0.60 delta call/put, 50-100 days out (the swing contract the bot shows)
  spread  that contract with the ~0.30 delta one sold against it, same expiry
Exits, as in [positions]: single +50% take-profit, spread 55% of max profit; stop -50% of the debit; time stop
after 10 trading days; also (variant "+trend exit") out at the first check where the swing trend turns against.
Fills: hourly bar opens (targets/stops at their level; both in one bar counts as the stop). No commissions.
Deltas come from Black-Scholes with 20-day realized volatility (no historical greeks); entry IV is backed out of
the option price, so trades can be split by IV vs realized volatility, and by whether earnings fell in the hold.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import math
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

from .emacross import _Cache, _five_minute
from .indicators import Bar
from .trend import label_for, resample, timeframe_trend, to_bars, weekly

log = logging.getLogger(__name__)

SAMPLE_TIMES = [time(h, m) for h in range(10, 16) for m in (0, 30)] + [time(16, 0)]
WINDOW_5M = 78 * 28          # ~40 calendar days of 5-minute bars, as the live service uses
HOLD_DAYS = 10
SINGLE_DELTA, SHORT_DELTA = 0.60, 0.30
DTE = (50, 100)


def _et(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz)


# ---- scores --------------------------------------------------------------------------------------

def score_series(five: list[Bar], daily: list[Bar], start: date, end: date, tz_name: str,
                 profiles: dict, strong: float, bias: float) -> list[tuple]:
    """(ms, price, swing, intraday, hv20) every 30 minutes from start to end. Runs in a worker process."""
    from .trend import realized_vol
    tz = ZoneInfo(tz_name)
    out = []
    day_of = [_et(b.t, tz).date() for b in five]
    dday = [_et(b.t, tz).date() for b in daily]
    i = 0
    n = len(five)
    days = sorted({d for d in day_of if start <= d <= end})
    for d in days:
        prior_daily = [b for b, dd in zip(daily, dday) if dd < d][-420:]
        if len(prior_daily) < 60:
            continue
        hv = realized_vol(prior_daily)
        while i < n and day_of[i] < d:
            i += 1
        first = i
        for st in SAMPLE_TIMES:
            cut = int(datetime.combine(d, st, tz).timestamp() * 1000)
            j = first
            while j < n and day_of[j] == d and five[j].t + 300_000 <= cut:
                j += 1
            if j == first:
                continue
            window = five[max(0, j - WINDOW_5M):j]
            session = five[first:j]
            comp = _scores(window, session, prior_daily, tz, profiles, strong, bias)
            out.append((cut, session[-1].c, round(comp.get("swing", 0.0), 1), round(comp.get("intraday", 0.0), 1), hv))
    return out


def _scores(window: list[Bar], session: list[Bar], prior_daily: list[Bar], tz: ZoneInfo, profiles: dict,
            strong: float, bias: float) -> dict[str, float]:
    """Composite score per profile from 5-minute bars up to now (window), today's bars (session) and the daily
    bars before today: the same timeframes and weights as the live service."""
    today_bar = Bar(session[0].t, session[0].o, max(b.h for b in session), min(b.l for b in session),
                    session[-1].c, sum(b.v for b in session))
    days_now = prior_daily + [today_bar]
    series = {"5m": lambda: window, "15m": lambda: resample(window, 15, tz), "1h": lambda: resample(window, 60, tz),
              "1d": lambda: days_now, "1w": lambda: weekly(days_now, tz)}
    tfs = {}
    needed = {tf for w in profiles.values() for tf in w}
    for tf, build in series.items():
        if tf in needed:
            res = timeframe_trend(tf, build(), strong=strong, bias=bias,
                                  vwap_bars=session if tf in ("5m", "15m", "1h") else None)
            if res:
                tfs[tf] = res.score
    comp = {}
    for name, w in profiles.items():
        used = {tf: x for tf, x in w.items() if tf in tfs}
        tot = sum(used.values())
        comp[name] = sum(tfs[tf] * x for tf, x in used.items()) / tot if tot else 0.0
    return comp


def scores_at(five: list[Bar], daily: list[Bar], t_ms: int, tz: ZoneInfo, profiles: dict, strong: float,
              bias: float) -> dict[str, float] | None:
    """Swing and intraday composites as they stood at t_ms (5-minute bars completed by then)."""
    d = _et(t_ms, tz).date()
    j = next((n for n in range(len(five) - 1, -1, -1) if five[n].t + 300_000 <= t_ms), None)
    if j is None:
        return None
    first = j
    while first > 0 and _et(five[first - 1].t, tz).date() == d:
        first -= 1
    if _et(five[j].t, tz).date() != d:
        return None
    prior = [b for b in daily if _et(b.t, tz).date() < d][-420:]
    if len(prior) < 60:
        return None
    return _scores(five[max(0, j + 1 - WINDOW_5M):j + 1], five[first:j + 1], prior, tz, profiles, strong, bias)


# ---- entries -------------------------------------------------------------------------------------

@dataclass
class Entry:
    symbol: str
    group: str
    side: str
    ms: int
    price: float
    hv: float | None
    swing: float
    exits: list[int] = field(default_factory=list)    # sample ms where the swing trend was against the trade


def entries(symbol: str, series: list[tuple], cfg, tz: ZoneInfo) -> list[Entry]:
    from . import signals
    out: list[Entry] = []
    state: dict = {}
    strong_cnt = {"bull": 0, "bear": 0}
    trend_days: dict[str, int] = {"bull": -99, "bear": -99}
    day_index, last_day = -1, None
    for ms, price, swing, intra, hv in series:
        d = _et(ms, tz).date()
        if d != last_day:
            day_index, last_day = day_index + 1, d
        r = NS(stock=NS(score=swing, composites={"intraday": (intra, "")}), bias=None, options=None)
        side = signals.pullback(r, state, cfg)
        if side and signals._cooled(state, f"pullback_{side}", ms / 1000, cfg.pullback_cooldown_minutes):
            out.append(Entry(symbol, "pullback", side, ms, price, hv, swing))
        for s, sg in (("bull", 1), ("bear", -1)):
            if sg * swing >= cfg.trend_strong and sg * intra >= cfg.trend_strong:
                strong_cnt[s] += 1
                if strong_cnt[s] == cfg.confirm_cycles:
                    out.append(Entry(symbol, "strong", s, ms, price, hv, swing))
            else:
                strong_cnt[s] = 0
            t = _et(ms, tz).time()
            if t == time(10, 30) and sg * swing >= cfg.change_enter and day_index - trend_days[s] >= 5:
                trend_days[s] = day_index
                out.append(Entry(symbol, "trend", s, ms, price, hv, swing))
    against = [(ms, swing) for ms, _, swing, _, _ in series]
    for e in out:
        sg = 1 if e.side == "bull" else -1
        e.exits = [ms for ms, sw in against if ms > e.ms and sg * sw <= 0][:1]
    return out


# ---- option pricing helpers ----------------------------------------------------------------------

def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs(s: float, k: float, t: float, v: float, call: bool) -> float:
    if t <= 0 or v <= 0:
        return max(0.0, (s - k) if call else (k - s))
    d1 = (math.log(s / k) + 0.5 * v * v * t) / (v * math.sqrt(t))
    d2 = d1 - v * math.sqrt(t)
    return s * _ncdf(d1) - k * _ncdf(d2) if call else k * _ncdf(-d2) - s * _ncdf(-d1)


def implied_vol(price: float, s: float, k: float, t: float, call: bool) -> float | None:
    lo, hi = 0.01, 5.0
    if price <= max(0.0, (s - k) if call else (k - s)) or price <= 0:
        return None
    for _ in range(60):
        m = (lo + hi) / 2
        if _bs(s, k, t, m, call) > price:
            hi = m
        else:
            lo = m
    return (lo + hi) / 2


def strike_for_delta(s: float, t: float, vol: float, delta: float, call: bool) -> float:
    """Strike whose Black-Scholes delta is about `delta` (zero rates)."""
    from statistics import NormalDist
    z = NormalDist().inv_cdf(delta if call else 1 - delta)
    return s * math.exp(-z * vol * math.sqrt(t) + 0.5 * vol * vol * t)


# ---- trades --------------------------------------------------------------------------------------

def _add_trading_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def manage(bars: list[Bar], entry_ms: int, debit: float, take: float, stop: float, last_ms: int,
           trend_exit_ms: int | None) -> tuple[float, str, int] | None:
    """Walk hourly bars of the position value. Returns (exit value, reason, ms) or None without data."""
    path = [b for b in bars if b.t >= entry_ms]
    if not path:
        return None
    for b in path:
        if trend_exit_ms and b.t >= trend_exit_ms:
            return b.o, "trend", b.t
        if b.t > last_ms:
            return b.o, "time", b.t
        if b.l <= stop:
            return min(b.o, stop), "stop", b.t
        if b.h >= take:
            return max(b.o, take) if b.o >= take else take, "target", b.t
    return path[-1].c, "end", path[-1].t


def _spread_bars(a: list[Bar], b: list[Bar]) -> list[Bar]:
    """Hour-by-hour value of long a / short b. The short (further out) leg trades less, so its last price is carried
    forward. Highs and lows of two legs don't happen together, so each bar spans only its open and close values."""
    bb = {x.t: x for x in b}
    out, last = [], None
    for x in a:
        y = bb.get(x.t)
        o = x.o - (y.o if y else last) if (y or last is not None) else None
        if y:
            last = y.c
        if o is None or last is None:
            continue
        c = x.c - last
        out.append(Bar(x.t, o, max(o, c), min(o, c), c, 0))
    return out


async def option_trades(client, cache: _Cache, e: Entry, tz: ZoneInfo, earnings: set[date]) -> list[dict]:
    """Single and spread results for one entry (both with and without the trend exit)."""
    d = _et(e.ms, tz).date()
    call = e.side == "bull"
    kind = "call" if call else "put"
    key = f"{e.symbol}_{kind}_{d}"

    async def contracts():
        rows, _ = await client.paginate("/v3/reference/options/contracts", {
            "underlying_ticker": e.symbol, "contract_type": kind, "as_of": d.isoformat(),
            "expiration_date.gte": (d + timedelta(days=DTE[0])).isoformat(),
            "expiration_date.lte": (d + timedelta(days=DTE[1])).isoformat(),
            "strike_price.gte": f"{e.price * 0.6:.2f}", "strike_price.lte": f"{e.price * 1.6:.2f}", "limit": 1000}, 5)
        return [[r["ticker"], r["expiration_date"], r["strike_price"]] for r in rows]
    listed = await cache.get(f"ref_{key}", contracts)
    if not listed or not e.hv:
        return []
    mid_dte = sum(DTE) / 2
    expiry = min({x[1] for x in listed}, key=lambda x: abs((date.fromisoformat(x) - d).days - mid_dte))
    strikes = {x[2]: x[0] for x in listed if x[1] == expiry}
    t = (date.fromisoformat(expiry) - d).days / 365
    vol = e.hv / 100
    k1 = min(strikes, key=lambda k: abs(k - strike_for_delta(e.price, t, vol, SINGLE_DELTA, call)))
    k2 = min(strikes, key=lambda k: abs(k - strike_for_delta(e.price, t, vol, SHORT_DELTA, call)))
    end = d + timedelta(days=HOLD_DAYS * 2)

    async def hourly(tk):
        return await cache.get(f"h_{tk}_{d}", lambda: client.aggregates(tk, 1, "hour", d, end))
    a = to_bars(await hourly(strikes[k1]))
    out = []
    first = next((b for b in a if b.t >= e.ms), None)
    if not first or first.t - e.ms > 2 * 86400_000:
        return []
    entry = first.o
    iv = implied_vol(entry, e.price, k1, t, call)
    last_ms = int(datetime.combine(_add_trading_days(d, HOLD_DAYS), time(15, 0), tz).timestamp() * 1000)
    in_hold = any(d < x <= _add_trading_days(d, HOLD_DAYS) for x in earnings)
    base = {"symbol": e.symbol, "group": e.group, "side": e.side, "date": d.isoformat(),
            "time": _et(e.ms, tz).strftime("%H:%M"), "price": e.price, "swing": e.swing, "expiry": expiry,
            "iv": round(100 * iv, 1) if iv else None, "hv": e.hv,
            "iv_ratio": round(100 * iv / e.hv, 2) if iv and e.hv else None, "earnings_in_hold": in_hold}
    for exit_name, trend_ms in (("rules", None), ("rules+trend", e.exits[0] if e.exits else None)):
        res = manage(a, first.t, entry, entry * 1.5, entry * 0.5, last_ms, trend_ms)
        if res:
            out.append(dict(base, structure="single", exit_rule=exit_name, strikes=f"{k1:g}", debit=round(entry, 2),
                            exit=round(res[0], 2), reason=res[1], ret=round(100 * (res[0] / entry - 1), 1),
                            pnl=round(100 * (res[0] - entry), 0)))
    if k2 != k1:
        b = to_bars(await hourly(strikes[k2]))
        sp = _spread_bars(a, b)
        sfirst = next((x for x in sp if x.t >= e.ms), None)
        if sfirst and sfirst.o > 0:
            debit, width = sfirst.o, abs(k2 - k1)
            if debit < width:
                take = debit + 0.55 * (width - debit)
                for exit_name, trend_ms in (("rules", None), ("rules+trend", e.exits[0] if e.exits else None)):
                    res = manage(sp, sfirst.t, debit, take, debit * 0.5, last_ms, trend_ms)
                    if res:
                        out.append(dict(base, structure="spread", exit_rule=exit_name, strikes=f"{k1:g}/{k2:g}",
                                        debit=round(debit, 2), exit=round(res[0], 2), reason=res[1],
                                        ret=round(100 * (res[0] / debit - 1), 1), pnl=round(100 * (res[0] - debit), 0)))
    return out


# ---- run -----------------------------------------------------------------------------------------

def _summary(rows: list[dict]) -> str:
    if not rows:
        return "| 0 | | | | |"
    rets = [r["ret"] for r in rows]
    win = 100 * sum(x > 0 for x in rets) / len(rets)
    gains = sum(r["pnl"] for r in rows if r["pnl"] > 0)
    losses = -sum(r["pnl"] for r in rows if r["pnl"] < 0)
    pf = f"{gains / losses:.2f}" if losses else "–"
    return f"| {len(rows)} | {win:.0f}% | {sum(rets) / len(rets):+.1f}% | {median(rets):+.1f}% | {pf} |"


def report(trades: list[dict], start: date, end: date, symbols: list[str]) -> str:
    lines = [f"# Swing entries backtest {start} to {end}", "",
             f"{len(symbols)} symbols. Returns are on the premium paid per trade (one contract or one spread).", ""]
    head = ["| Group | Trades | Win | Avg return | Median | Profit factor |", "|---|---|---|---|---|---|"]
    for structure in ("single", "spread"):
        for rule in ("rules", "rules+trend"):
            sub = [t for t in trades if t["structure"] == structure and t["exit_rule"] == rule]
            title = f"{'Single call/put (~0.60Δ)' if structure == 'single' else 'Debit spread (0.60Δ/0.30Δ)'}, " \
                    f"exits: {'TP/stop/time' if rule == 'rules' else 'TP/stop/time + trend turned against'}"
            lines += [f"## {title}", "", *head]
            for g in ("pullback", "strong", "trend"):
                for side in ("bull", "bear"):
                    rows = [t for t in sub if t["group"] == g and t["side"] == side]
                    lines.append(f"| {g} {'calls' if side == 'bull' else 'puts'} " + _summary(rows))
            lines.append("")
    lines += ["## Pullback entries split (single, TP/stop/time)", "", *head]
    pb = [t for t in trades if t["group"] == "pullback" and t["structure"] == "single" and t["exit_rule"] == "rules"]
    for name, f in (("IV cheap (< 0.9× realized)", lambda t: t["iv_ratio"] is not None and t["iv_ratio"] < 0.9),
                    ("IV fair", lambda t: t["iv_ratio"] is not None and 0.9 <= t["iv_ratio"] < 1.25),
                    ("IV rich (≥ 1.25× realized)", lambda t: t["iv_ratio"] is not None and t["iv_ratio"] >= 1.25),
                    ("earnings inside the hold", lambda t: t["earnings_in_hold"]),
                    ("no earnings in the hold", lambda t: not t["earnings_in_hold"])):
        lines.append(f"| {name} " + _summary([t for t in pb if f(t)]))
    lines += ["", "## Single vs spread when IV is rich (pullback, TP/stop/time)", "", *head]
    for structure in ("single", "spread"):
        rows = [t for t in trades if t["group"] == "pullback" and t["structure"] == structure and t["exit_rule"] == "rules"
                and t["iv_ratio"] is not None and t["iv_ratio"] >= 1.25]
        lines.append(f"| {structure} " + _summary(rows))
    lines += ["", "## By symbol (pullback, single, TP/stop/time)", "", *head]
    for s in symbols:
        rows = [t for t in pb if t["symbol"] == s]
        if rows:
            lines.append(f"| {s} " + _summary(rows))
    return "\n".join(lines)


async def run(cfg, symbols: list[str], start: date, end: date, out_dir: Path) -> str:
    from .events import earnings_days
    from .massive import MassiveClient
    tz = cfg.tz
    cache = _Cache(out_dir / "cache")
    out_dir.mkdir(parents=True, exist_ok=True)
    async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout, max_retries=cfg.max_retries,
                             max_concurrency=cfg.max_concurrency) as client:
        data = {}

        async def load(sym):
            five = await _five_minute(client, cache, sym, start - timedelta(days=45), end, tz)
            raw = await cache.get(f"{sym}_1d_{start}_{end}", lambda: client.aggregates(
                sym, 1, "day", start - timedelta(days=700), end))
            data[sym] = (five, to_bars(raw), raw)
        await asyncio.gather(*(load(s) for s in symbols))
        log.info("bars loaded for %d symbols", len(data))

        loop = asyncio.get_running_loop()
        with ProcessPoolExecutor(max_workers=min(20, len(symbols))) as pool:
            futs = {s: loop.run_in_executor(pool, score_series, data[s][0], data[s][1], start, end, str(tz),
                                            cfg.trend_profiles, cfg.trend_strong, cfg.trend_bias) for s in symbols}
            series = {s: await f for s, f in futs.items()}
        log.info("scores computed")

        all_entries = [e for s in symbols for e in entries(s, series[s], cfg, tz)]
        earn = {s: set(earnings_days(data[s][2], tz)) for s in symbols}
        sem = asyncio.Semaphore(16)

        async def one(e):
            async with sem:
                try:
                    return await option_trades(client, cache, e, tz, earn[e.symbol])
                except Exception as exc:
                    log.warning("%s %s %s: %s", e.symbol, e.group, e.ms, exc)
                    return []
        trades = [t for ts in await asyncio.gather(*(one(e) for e in all_entries)) for t in ts]
    text = report(trades, start, end, symbols)
    (out_dir / "report.md").write_text(text)
    if trades:
        with (out_dir / "trades.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0]))
            w.writeheader()
            w.writerows(trades)
    return text
