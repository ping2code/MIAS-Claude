"""5-minute EMA 9/21 cross-and-retest day trade (the Oct 2026 SPY handout): detection, trade management,
a backtest on real option prices, and live paper-tracked alerts. Untested as an edge: the backtest and the
live paper trades are how it gets tested.

Setup, all times ET:
  1. EMA 9 crosses EMA 21 on a completed 5-minute candle.
  2. Within retest_bars candles price pulls back into the EMA zone (bull: low <= EMA 9) and holds (closes stay
     on the right side of EMA 21). A close through EMA 21 or an opposite cross cancels the setup; no pullback
     in time means no trade (no chase).
  3. A confirmation candle (bull: green, closes above EMA 9 and the prior close) is the entry, at its close.
     Invalidation: the retest's extreme (bull: its lowest low).
Management: ATM option, 0DTE (1DTE from one_dte_after), 5 contracts. Stop: -20% premium, or a 5-minute close
through the invalidation. Sell 3 at +30%, runners to breakeven; at +75% protect +50%, at +100% protect +75%.
Close everything by close_time; no new entries after 2 losing trades in a row. Bear setups are the mirror (puts).
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from .indicators import Bar, ema, session_vwap
from .trend import regular_session, to_bars

log = logging.getLogger(__name__)

FIVE = 300_000


@dataclass(frozen=True)
class DayTradeRules:
    enabled: bool = True
    symbols: tuple[str, ...] = ("SPY",)
    fast: int = 9
    slow: int = 21
    retest_bars: int = 6            # the pullback must come within this many candles of the cross
    confirm_bars: int = 3           # ... and the confirmation within this many candles of the pullback
    start: str = "09:45"            # first entry (the 15-minute opening range is known by then)
    last_entry: str = "15:00"
    close: str = "15:10"            # everything is closed by then
    one_dte_after: str = "13:00"    # new entries use the next expiry from this time
    contracts: int = 5
    partial: int = 3
    stop: float = 0.20
    first_target: float = 0.30
    trail: tuple[tuple[float, float], ...] = ((0.75, 0.50), (1.00, 0.75))
    max_losses_in_row: int = 2
    manage_seconds: int = 30        # open paper trades are re-priced this often (the main cycle is 150 s)
    swing_filter: bool = False      # only take entries in the direction of the swing trend (see the backtest split)


def _hm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


def _et(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz)


def _at(day: date, hm: str, tz: ZoneInfo) -> int:
    return int(datetime.combine(day, _hm(hm), tz).timestamp() * 1000)


# ---- detection -----------------------------------------------------------------------------------

@dataclass
class Setup:
    symbol: str
    side: str                        # bull / bear
    cross_t: int                     # start of the crossing candle, epoch ms
    status: str = "pending"          # pending / entry / no_retest / failed / outside_hours
    entry_t: int | None = None       # when the confirmation candle closed
    price: float | None = None       # underlying at entry
    invalidation: float | None = None
    vwap: str | None = None          # with / against
    orb: str | None = None           # with / against / inside
    sma200: str | None = None        # with / against
    move: float | None = None        # best underlying move in the setup's favor before the next opposite cross
    regime: str | None = None        # trend / chop: the day's character at entry (see regime())
    retest_t: int | None = None      # start of the candle that pulled back into the EMAs

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.side}:{self.cross_t}"

    @property
    def sign(self) -> int:
        return 1 if self.side == "bull" else -1


def regime(bars: list[Bar], i: int, first: int) -> str:
    """'trend' when the day is moving (ADX(14) >= 25 on these candles and price at least 40% of an average day's
    range away from the open), else 'chop'."""
    from .indicators import adx
    prev = bars[max(0, first - 800):first]
    ranges: dict[int, list[float]] = {}
    for b in prev:
        r = ranges.setdefault(b.t // 86_400_000, [b.h, b.l])
        r[0], r[1] = max(r[0], b.h), min(r[1], b.l)
    avg = sum(h - l for h, l in ranges.values()) / len(ranges) if ranges else 0.0
    a = adx(bars[max(0, i - 80):i + 1])
    moved = abs(bars[i].c - bars[first].o)
    return "trend" if a is not None and a >= 25 and avg and moved >= 0.4 * avg else "chop"


def fill_entry(su: Setup, bars: list[Bar], i: int, first: int, extreme: float, tz: ZoneInfo, rules: DayTradeRules,
               span: int = FIVE, closes: list[float] | None = None) -> None:
    """Mark a setup entered at the close of candle i (unless outside entry hours), with its context."""
    b, sg = bars[i], su.sign
    day = _et(b.t, tz).date()
    su.entry_t = b.t + span
    if not _at(day, rules.start, tz) <= su.entry_t <= _at(day, rules.last_entry, tz):
        su.status = "outside_hours"
        return
    su.status, su.price, su.invalidation = "entry", b.c, extreme
    vw = session_vwap(bars[first:i + 1])
    su.vwap = "with" if vw is not None and sg * (b.c - vw) > 0 else "against"
    orb = [x for x in bars[first:i + 1] if x.t < bars[first].t + 15 * 60_000]
    if orb and b.t >= bars[first].t + 15 * 60_000:
        hi, lo = max(x.h for x in orb), min(x.l for x in orb)
        su.orb = "with" if sg * (b.c - (hi if sg > 0 else lo)) > 0 else \
            "against" if sg * (b.c - (lo if sg > 0 else hi)) < 0 else "inside"
    if i >= 199:
        closes = closes or [x.c for x in bars]
        su.sma200 = "with" if sg * (b.c - sum(closes[i - 199:i + 1]) / 200) > 0 else "against"
    su.regime = regime(bars, i, first)


def scan(symbol: str, bars: list[Bar], day: date, tz: ZoneInfo, rules: DayTradeRules, *,
         retest: bool = True, span: int = FIVE) -> list[Setup]:
    """Every cross on `day` and what became of it. `bars` are completed regular-session candles (5-minute unless
    span says otherwise), earlier days included so the EMAs are warmed up. retest=False is the control group:
    enter on the cross."""
    closes = [b.c for b in bars]
    fast, slow = ema(closes, rules.fast), ema(closes, rules.slow)
    idx = [i for i, b in enumerate(bars) if _et(b.t, tz).date() == day]
    if not idx or idx[0] == 0:
        return []
    first = idx[0]
    out: list[Setup] = []
    crosses: list[tuple[int, Setup]] = []
    active: Setup | None = None
    touched: float | None = None
    touched_i = cross_i = 0

    def enter(su: Setup, i: int, extreme: float) -> None:
        fill_entry(su, bars, i, first, extreme, tz, rules, span, closes)

    for i in idx:
        b = bars[i]
        now_up, was_up = fast[i] > slow[i], fast[i - 1] > slow[i - 1]
        side = "bull" if now_up and not was_up else "bear" if was_up and fast[i] < slow[i] else None
        if side:
            if active and active.status == "pending":
                active.status = "failed"
            active, touched, cross_i = Setup(symbol, side, b.t), None, i
            out.append(active)
            crosses.append((i, active))
            if not retest:
                enter(active, i, b.l if side == "bull" else b.h)
            continue
        if not active or active.status != "pending":
            continue
        sg = active.sign
        if sg * (fast[i] - slow[i]) <= 0 or sg * (b.c - slow[i]) < 0:
            active.status = "failed"                      # closed through EMA 21: the cross didn't hold
        elif touched is None:
            if sg * (b.l if sg > 0 else b.h) <= sg * fast[i]:
                touched, touched_i = (b.l if sg > 0 else b.h), i
            elif i - cross_i >= rules.retest_bars:
                active.status = "no_retest"
        else:
            touched = min(touched, b.l) if sg > 0 else max(touched, b.h)
            if sg * (b.c - b.o) > 0 and sg * (b.c - fast[i]) > 0 and sg * (b.c - bars[i - 1].c) > 0:
                active.retest_t = bars[touched_i].t
                enter(active, i, touched)
            elif i - touched_i >= rules.confirm_bars:
                active.status = "no_retest"
    if active and active.status == "pending" and idx[-1] - (touched_i if touched is not None else cross_i) \
            >= (rules.confirm_bars if touched is not None else rules.retest_bars):
        active.status = "no_retest"

    for n, (i, su) in enumerate(crosses):
        end = crosses[n + 1][0] if n + 1 < len(crosses) else idx[-1] + 1
        ref = bars[i].c
        best = [su.sign * ((x.h if su.sign > 0 else x.l) - ref) for x in bars[i + 1:end]]
        su.move = round(max(best), 2) if best else None
    return out


# ---- trade management ----------------------------------------------------------------------------

@dataclass
class Trade:
    symbol: str
    side: str
    contract: str
    strike: float
    expiry: str
    opened_t: int
    entry: float
    invalidation: float
    qty: int
    open_qty: int
    stop: float
    peak: float
    realized: float = 0.0
    partial_done: bool = False
    closed_t: int | None = None
    exit_reason: str | None = None
    low: float | None = None
    under: float | None = None
    slip: float = 0.0                # backtests: share of the price lost on every fill (bid/ask and impact)
    # Live paper trades fill at the real quote: bought at the ask, sold at the bid (never above the trigger price).
    # The midpoint result is kept alongside for comparison.
    entry_mid: float | None = None
    mid_realized: float = 0.0
    bid: float | None = None         # current bid, set before each price update
    ask: float | None = None
    peak_t: int | None = None        # when the best (MFE) and worst (MAE) option prices were seen
    low_t: int | None = None
    under_hi: float | None = None    # the stock's range while the trade was open
    under_lo: float | None = None
    info: dict = field(default_factory=dict)   # entry record: reason, timing, stop distances, spread, trend
    exit_spread_pct: float | None = None

    @classmethod
    def open(cls, su: Setup, contract: str, strike: float, expiry: str, t: int, premium: float,
             rules: DayTradeRules, slip: float = 0.0) -> "Trade":
        premium = round(premium * (1 + slip), 4)
        return cls(su.symbol, su.side, contract, strike, expiry, t, premium, su.invalidation, rules.contracts,
                   rules.contracts, round(premium * (1 - rules.stop), 2), premium, low=premium, under=su.price,
                   slip=slip)

    @property
    def closed(self) -> bool:
        return self.open_qty == 0

    @property
    def pnl(self) -> float:
        return round(self.realized, 2)

    @property
    def pnl_pct(self) -> float:
        return round(100 * self.realized / (self.entry * self.qty * 100), 1)

    @property
    def pnl_mid(self) -> float:
        return round(self.mid_realized, 2)

    @property
    def mfe_pct(self) -> float:
        return round(100 * (self.peak / self.entry - 1), 1)

    @property
    def mae_pct(self) -> float:
        return round(100 * ((self.low or self.entry) / self.entry - 1), 1)

    def _sell(self, qty: int, price: float) -> None:
        fill = min(price, self.bid) if self.bid else price * (1 - self.slip)
        self.realized += (fill - self.entry) * qty * 100
        self.mid_realized += (price - (self.entry_mid or self.entry)) * qty * 100
        self.open_qty -= qty

    def mark(self, t: int, under: float | None) -> None:
        """Track the stock's range while open (MAE/MFE in stock terms)."""
        if under:
            self.under_hi = max(self.under_hi or under, under)
            self.under_lo = min(self.under_lo or under, under)

    def exit(self, t: int, price: float, reason: str) -> list[tuple[str, str]]:
        if self.closed:
            return []
        n = self.open_qty
        self._sell(n, price)
        self.closed_t, self.exit_reason = t, reason
        return [(reason, f"sold {n} at {price:.2f}")]

    def on_price(self, t: int, o: float, h: float, l: float, c: float, rules: DayTradeRules) -> list[tuple[str, str]]:
        """Feed one price bar (a minute bar, or a single live quote as o=h=l=c). The stop is checked first."""
        if self.closed:
            return []
        if self.low is None or l < self.low:
            self.low, self.low_t = l, t
        if h > self.peak:
            self.peak_t = t
        if l <= self.stop:
            reason = "stop" if not self.partial_done else "breakeven" if self.stop <= self.entry else "trailed"
            return self.exit(t, min(o, self.stop), reason)
        events = []
        target = round(self.entry * (1 + rules.first_target), 2)
        if not self.partial_done and h >= target:
            n = min(rules.partial, self.open_qty)
            self._sell(n, target)
            self.partial_done, self.stop = True, self.entry
            events.append(("partial", f"sold {n} at {target:.2f} (+{100 * rules.first_target:.0f}%), "
                                      f"runner stop to breakeven {self.entry:.2f}"))
        self.peak = max(self.peak, h)
        for reach, lock in rules.trail:
            if self.partial_done and self.peak >= self.entry * (1 + reach):
                level = round(self.entry * (1 + lock), 2)
                if level > self.stop:
                    self.stop = level
                    events.append(("trail", f"+{100 * reach:.0f}% reached ({self.peak:.2f}): runner stop to "
                                            f"{level:.2f} (+{100 * lock:.0f}%)"))
        if self.open_qty == 0:
            self.closed_t, self.exit_reason = t, "target"
        return events


def invalidated(tr: Trade, five_close: float) -> bool:
    return (five_close < tr.invalidation) if tr.side == "bull" else (five_close > tr.invalidation)


# ---- contract choice -----------------------------------------------------------------------------

def pick_expiry(expiries: list[str], day: date, entry_t: int, tz: ZoneInfo, rules: DayTradeRules) -> str | None:
    """0DTE when listed, the next expiry from one_dte_after (or when there is no 0DTE)."""
    late = entry_t >= _at(day, rules.one_dte_after, tz)
    ok = sorted(e for e in expiries if (e > day.isoformat() if late else e >= day.isoformat()))
    return ok[0] if ok else None


def option_ticker(symbol: str, expiry: str, side: str, strike: float) -> str:
    return f"O:{symbol}{date.fromisoformat(expiry):%y%m%d}{'C' if side == 'bull' else 'P'}{round(strike * 1000):08d}"


# ---- backtest ------------------------------------------------------------------------------------

class _Cache:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    async def get(self, name: str, fetch):
        path = self.root / (name.replace(":", "_").replace("/", "_") + ".json")
        if path.exists():
            return json.loads(path.read_text())
        data = await fetch()
        path.write_text(json.dumps(data))
        return data


async def _five_minute(client, cache: _Cache, symbol: str, start: date, end: date, tz: ZoneInfo,
                       minutes: int = 5) -> list[Bar]:
    """Regular-session N-minute bars (5 by default), fetched a month at a time (the API caps a request at ~50k
    minutes)."""
    raw: list[dict] = []
    d = start
    while d <= end:
        chunk_end = min(end, d + timedelta(days=27))
        live = chunk_end >= date.today()
        fetch = lambda a=d, b=chunk_end: client.aggregates(symbol, minutes, "minute", a, b)
        raw += await (fetch() if live else cache.get(f"{symbol}_{minutes}m_{d}_{chunk_end}", fetch))
        d = chunk_end + timedelta(days=1)
    return regular_session(to_bars(raw), tz)


async def _expiries(client, cache: _Cache, symbol: str, day: date, spot: float) -> dict[str, list[float]]:
    async def fetch():
        rows, _ = await client.paginate("/v3/reference/options/contracts", {
            "underlying_ticker": symbol, "contract_type": "call", "as_of": day.isoformat(),
            "expiration_date.gte": day.isoformat(), "expiration_date.lte": (day + timedelta(days=7)).isoformat(),
            "strike_price.gte": f"{spot * 0.95:.2f}", "strike_price.lte": f"{spot * 1.05:.2f}", "limit": 1000}, 5)
        out: dict[str, list[float]] = {}
        for r in rows:
            out.setdefault(r["expiration_date"], []).append(float(r["strike_price"]))
        return out
    return await cache.get(f"{symbol}_contracts_{day}", fetch)


def _summary_rows(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0}
    pnl = [t["pnl"] for t in trades]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"trades": n, "win_rate": round(100 * len(wins) / n), "total": round(sum(pnl)),
            "avg": round(sum(pnl) / n), "avg_pct": round(sum(t["pnl_pct"] for t in trades) / n, 1),
            "avg_win": round(sum(wins) / len(wins)) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses)) if losses else 0,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "max_drawdown": round(dd)}


async def backtest(client, symbol: str, start: date, end: date, tz: ZoneInfo, rules: DayTradeRules,
                   cache_dir: Path, *, retest: bool = True, scan_fn=None, minutes: int = 5,
                   slip: float = 0.0, trend: tuple | None = None) -> tuple[list[dict], list[Setup]]:
    """Simulate every setup from start to end with real option minute bars. Fills: the option's first minute
    bar after the signal (its open); stops at the stop price (or the open if it gapped through); targets at the
    target price; every fill gives up `slip` of the price. A minute bar touching both stop and target counts
    as the stop (conservative). scan_fn defaults to the EMA cross-and-retest (mias/daysetups.py has others).
    trend: (profiles, strong, bias) to record the swing and intraday scores at each entry (slower)."""
    cache = _Cache(cache_dir)
    span = minutes * 60_000
    bars = await _five_minute(client, cache, symbol, start - timedelta(days=10), end, tz, minutes)
    scorer = None
    if trend:
        from .swingtest import scores_at
        five5 = await _five_minute(client, cache, symbol, start - timedelta(days=45), end, tz, 5)   # 40-day window
        daily = to_bars(await cache.get(f"{symbol}_1d_{start}_{end}", lambda: client.aggregates(
            symbol, 1, "day", start - timedelta(days=700), end)))
        scorer = lambda t_ms: scores_at(five5, daily, t_ms, tz, *trend)
    by_end = {b.t + span: b for b in bars}
    days = sorted({_et(b.t, tz).date() for b in bars if start <= _et(b.t, tz).date() <= end})
    trades: list[dict] = []
    setups: list[Setup] = []
    for day in days:
        cut = next((n for n, b in enumerate(bars) if _et(b.t, tz).date() > day), len(bars))
        day_setups = (scan_fn or scan)(symbol, bars[:cut], day, tz, rules, retest=retest, span=span)
        for su in day_setups:
            if su.status == "pending":
                su.status = "day_end"                     # crossed too late in the day to finish
        setups += day_setups
        busy_until, losses = 0, 0
        close_t = _at(day, rules.close, tz)
        for su in day_setups:
            if su.status != "entry":
                continue
            if losses >= rules.max_losses_in_row:
                su.status = "daily_limit"
                continue
            if su.entry_t < busy_until:
                su.status = "in_trade"
                continue
            chains = await _expiries(client, cache, symbol, day, su.price)
            expiry = pick_expiry(list(chains), day, su.entry_t, tz, rules)
            if not expiry:
                su.status = "no_contract"
                continue
            strike = min(chains[expiry], key=lambda k: (abs(k - su.price), k))
            ticker = option_ticker(symbol, expiry, su.side, strike)
            raw = await cache.get(f"{ticker}_{day}", lambda: client.aggregates(ticker, 1, "minute", day, day))
            mins = [b for b in to_bars(raw) if su.entry_t <= b.t < close_t]
            if not mins:
                su.status = "no_quotes"
                continue
            tr = Trade.open(su, ticker, strike, expiry, mins[0].t, mins[0].o, rules, slip)
            for m in mins:
                five = by_end.get(m.t)
                if five and five.t + span > su.entry_t and invalidated(tr, five.c):
                    tr.exit(m.t, m.o, "invalidated")
                    break
                tr.on_price(m.t, m.o, m.h, m.l, m.c, rules)
                if tr.closed:
                    break
            if not tr.closed:
                tr.exit(mins[-1].t, mins[-1].c, "time")
            busy_until = tr.closed_t or close_t
            losses = losses + 1 if tr.pnl < 0 else 0
            sc = scorer(su.entry_t) if scorer else None
            sw = sc.get("swing") if sc else None
            trades.append({
                "swing": round(sw, 1) if sw is not None else None,
                "intraday": round(sc.get("intraday", 0.0), 1) if sc else None,
                "with_swing": None if sw is None else "with" if su.sign * sw >= 25 else
                "against" if su.sign * sw <= -25 else "neutral",
                "mfe_pct": tr.mfe_pct, "mae_pct": tr.mae_pct,
                "date": day.isoformat(), "side": su.side, "entry_time": _et(su.entry_t, tz).strftime("%H:%M"),
                "underlying": su.price, "invalidation": su.invalidation, "contract": ticker, "expiry": expiry,
                "entry": tr.entry, "exit_reason": tr.exit_reason, "exit_time": _et(tr.closed_t, tz).strftime("%H:%M"),
                "pnl": tr.pnl, "pnl_pct": tr.pnl_pct, "peak_pct": round(100 * (tr.peak / tr.entry - 1)),
                "low_pct": round(100 * (tr.low / tr.entry - 1)), "vwap": su.vwap, "orb": su.orb, "sma200": su.sma200,
                "move": su.move, "regime": su.regime})
    return trades, setups


def report(symbol: str, start: date, end: date, trades: list[dict], setups: list[Setup],
           control: list[dict] | None, rules: DayTradeRules) -> str:
    lines = [f"# {symbol} 5-minute EMA {rules.fast}/{rules.slow} cross + retest: backtest {start} to {end}", ""]
    statuses: dict[str, int] = {}
    for su in setups:
        statuses[su.status] = statuses.get(su.status, 0) + 1
    days = len({_et(s.cross_t, ZoneInfo("America/New_York")).date() for s in setups}) or 1
    lines.append(f"Crosses: {len(setups)} ({len(setups) / days:.1f} a day). Outcome: " +
                 ", ".join(f"{k} {v}" for k, v in sorted(statuses.items(), key=lambda x: -x[1])))
    moved = [s.move for s in setups if s.move is not None]
    if moved:
        lines.append(f"'$2 move' check: {sum(m >= 2 for m in moved)} of {len(moved)} crosses "
                     f"({100 * sum(m >= 2 for m in moved) / len(moved):.0f}%) moved $2+ their way before the next "
                     f"opposite cross; median best move ${sorted(moved)[len(moved) // 2]:.2f}.")
    lines += ["", "## Result (5 contracts per trade)", ""]

    def row(name, ts):
        s = _summary_rows(ts)
        if not s["trades"]:
            return f"| {name} | 0 | | | | | | |"
        pf = s["profit_factor"] if s["profit_factor"] is not None else "–"
        return (f"| {name} | {s['trades']} | {s['win_rate']}% | ${s['total']:,} | ${s['avg']:,} ({s['avg_pct']:+}%) | "
                f"${s['avg_win']:,} / ${s['avg_loss']:,} | {pf} | ${s['max_drawdown']:,} |")
    lines += ["| Group | Trades | Win | Total P&L | Avg trade | Avg win / loss | Profit factor | Max drawdown |",
              "|---|---|---|---|---|---|---|---|", row("All retest entries", trades)]
    for side in ("bull", "bear"):
        lines.append(row(f"  {'calls (bull)' if side == 'bull' else 'puts (bear)'}",
                         [t for t in trades if t["side"] == side]))
    for key, label in (("vwap", "VWAP"), ("orb", "opening range")):
        for v in ("with", "against", "inside"):
            sub = [t for t in trades if t[key] == v]
            if sub:
                lines.append(row(f"  {label} {v}", sub))
    lines.append(row("  VWAP and range both with", [t for t in trades if t["vwap"] == "with" and t["orb"] == "with"]))
    lines.append(row("  before 1 PM (0DTE)", [t for t in trades if t["entry_time"] < rules.one_dte_after]))
    lines.append(row("  from 1 PM (1DTE)", [t for t in trades if t["entry_time"] >= rules.one_dte_after]))
    if control is not None:
        lines.append(row("Control: enter on the cross", control))
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    lines += ["", "Exits: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])), "",
              "Fills are the option's real minute bars (first minute open after the signal; stop/target at their "
              "price), no commissions or slippage, so real results will be somewhat worse.", ""]
    lines += ["## Trades", "", "| Date | Side | In | SPY | Contract | Entry | Exit | P&L | Peak | VWAP | Range |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in trades:
        lines.append(f"| {t['date']} | {t['side']} | {t['entry_time']} | {t['underlying']:.2f} | "
                     f"{t['contract'][2:]} | {t['entry']:.2f} | {t['exit_reason']} {t['exit_time']} | "
                     f"${t['pnl']:,.0f} ({t['pnl_pct']:+}%) | {t['peak_pct']:+}% | {t['vwap']} | {t['orb'] or '–'} |")
    return "\n".join(lines)


def write_csv(path: Path, trades: list[dict]) -> None:
    if not trades:
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0]))
        w.writeheader()
        w.writerows(trades)


# ---- live paper trading --------------------------------------------------------------------------

KEY = "ema_day"


def _state(store, today: date) -> dict:
    st = store.get(KEY) or {}
    if st.get("date") != today.isoformat():
        st = {"date": today.isoformat(), "seen": [], "open": {}, "closed": [], "losses": {}, "summary": False}
    return st


def _money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def entry_text(su: Setup, tr: Trade, rules: DayTradeRules, tz: ZoneInfo, losses: int) -> str:
    sg, word = su.sign, ("BULLISH" if su.side == "bull" else "BEARISH")
    kind = "C" if su.side == "bull" else "P"
    exp = date.fromisoformat(tr.expiry)
    dte = (exp - _et(su.entry_t, tz).date()).days
    target = tr.entry * (1 + rules.first_target)
    ctx = [f"VWAP {su.vwap}", f"opening range {su.orb or 'n/a'}"] + ([f"200 SMA {su.sma200}"] if su.sma200 else [])
    ok = lambda v: "✅" if v == "with" else "⚠️" if v == "against" else "·"
    info = tr.info or {}
    fill = f"at {tr.entry:.2f}"
    if tr.ask and tr.entry_mid:
        fill = f"at {tr.entry:.2f} ask (mid {tr.entry_mid:.2f}, spread {info.get('entry_spread_pct', 0):.1f}%)"
    stops = f" · chart stop {info['chart_stop_pct']:.2f}% away" if info.get("chart_stop_pct") is not None else ""
    if info.get("premium_stop_pct") is not None:
        stops += f", premium stop ≈ {info['premium_stop_pct']:.2f}% stock move"
    trend = []
    if info.get("swing") is not None:
        w = info.get("with_swing")
        trend.append(f"Trend: swing {info['swing']:+d}" + (f" · intraday {info['intraday']:+d}" if info.get("intraday")
                                                            is not None else "")
                     + (" ✅ with it" if w == "with" else " ⚠️ AGAINST the swing trend" if w == "against" else ""))
    return "\n".join([
        f"{'🟢' if sg > 0 else '🔴'} <b>{su.symbol} EMA {rules.fast}/{rules.slow} {word} CROSS + RETEST</b> · day trade (paper)",
        f"Entry {_et(su.entry_t, tz):%H:%M}: {su.symbol} {su.price:.2f} · ATM <b>{tr.strike:g}{kind} {exp:%m/%d}</b> "
        f"({dte}DTE) {fill}",
        f"Stop {tr.stop:.2f} (-{100 * rules.stop:.0f}%) or a 5m close {'below' if sg > 0 else 'above'} "
        f"{su.invalidation:.2f}{stops}",
        *trend,
        f"Target: sell {rules.partial} of {rules.contracts} at {target:.2f} (+{100 * rules.first_target:.0f}%), "
        f"runners to breakeven; trail +75%→+50%, +100%→+75%",
        "Context: " + " · ".join(f"{c} {ok(c.split()[-1])}" for c in ctx),
        f"<i>Close all by {rules.close} ET · losses in a row today: {losses}/{rules.max_losses_in_row} · "
        f"PAPER ONLY: this setup lost money in the 6-month backtest; tracked live to see how it does</i>"])


def event_text(tr: Trade, kind: str, detail: str, value: float, under: float | None) -> str:
    icon = {"partial": "💰", "trail": "📈", "stop": "🛑", "invalidated": "🛑", "breakeven": "⚖️", "trailed": "🎯",
            "target": "🎯", "time": "⏰"}.get(kind, "•")
    head = {"partial": "FIRST TARGET", "trail": "TRAIL RAISED", "stop": "STOPPED OUT", "invalidated": "INVALIDATED",
            "breakeven": "RUNNERS OUT AT BREAKEVEN", "trailed": "TRAILED OUT", "target": "ALL SOLD",
            "time": "CLOSED: END OF DAY"}.get(kind, kind.upper())
    k = "C" if tr.side == "bull" else "P"
    tail = (f" · total {_money(tr.pnl)} ({tr.pnl_pct:+}%) at bid/ask, {_money(tr.pnl_mid)} at mid · "
            f"best {tr.mfe_pct:+.0f}%, worst {tr.mae_pct:+.0f}%") if tr.closed else ""
    return (f"{icon} <b>{tr.symbol} {tr.strike:g}{k} EMA trade: {head}</b>\n{detail} · option {value:.2f}"
            + (f" · {tr.symbol} {under:.2f}" if under else "") + tail)


def summary_text(st: dict, rules: DayTradeRules, today: date) -> str | None:
    closed = st.get("closed") or []
    seen = len(st.get("seen") or [])
    if not closed and not seen:
        return None
    total = sum(t["pnl"] for t in closed)
    total_mid = sum(t.get("pnl_mid", t["pnl"]) for t in closed)
    lines = [f"📒 <b>EMA {rules.fast}/{rules.slow} day trades (paper)</b> {today:%a %b %d}: "
             f"{len(closed)} trade{'s' if len(closed) != 1 else ''} · {_money(total)} at bid/ask "
             f"({_money(total_mid)} at mid)"]
    for t in closed:
        ex = (f" · best {t['mfe_pct']:+.0f}% {t.get('mfe_at') or ''}, worst {t['mae_pct']:+.0f}% {t.get('mae_at') or ''}"
              if "mfe_pct" in t else "")
        tw = {"with": " · with swing", "against": " · ⚠️ against swing"}.get(t.get("with_swing"), "")
        lines.append(f"{'🟢' if t['side'] == 'bull' else '🔴'} {t['symbol']} {t['in']} {t['contract']} "
                     f"{t['entry']:.2f} → {t['exit_reason']} {t['out']} · {_money(t['pnl'])} ({t['pnl_pct']:+}%)"
                     f"{ex}{tw}")
    skipped = st.get("skipped") or {}
    if skipped:
        lines.append("Crosses without a trade: " + ", ".join(f"{k} {v}" for k, v in skipped.items()))
    return "\n".join(lines)


async def _chain(client, symbol: str, spot: float, today: date) -> list[dict]:
    rows, _ = await client.option_chain(symbol, spot=spot, today=today, max_dte=8, strike_pct=0.02, max_pages=5)
    return rows


MAX_SPREAD_PCT = 5.0       # prefer contracts whose bid/ask spread is within this share of the mid


def _spread_pct(c: dict) -> float:
    q = c.get("last_quote") or {}
    m = _mid(c)
    return 100 * (q["ask"] - q["bid"]) / m if m and q.get("bid") and q.get("ask") else 999.0


def _mid(c: dict) -> float | None:
    q = c.get("last_quote") or {}
    if q.get("bid") and q.get("ask"):
        return round((q["bid"] + q["ask"]) / 2, 2)
    return q.get("midpoint")


LOCK = asyncio.Lock()      # the 150 s cycle and the 30 s watcher both update the day's state
LOG_KEY = "ema_log"        # every closed paper trade, for study (python -m mias ema-log)


def _quote(snap: dict) -> tuple[float | None, float | None, float | None]:
    q = snap.get("last_quote") or {}
    bid, ask = q.get("bid"), q.get("ask")
    return bid, ask, _mid(snap)


def _close_record(tr: Trade, tz: ZoneInfo, out_t: int) -> dict:
    k = "C" if tr.side == "bull" else "P"
    rec = {"symbol": tr.symbol, "side": tr.side, "contract": f"{tr.strike:g}{k}", "ticker": tr.contract,
           "in": _et(tr.opened_t, tz).strftime("%H:%M"), "out": _et(out_t, tz).strftime("%H:%M"),
           "entry": tr.entry, "entry_mid": tr.entry_mid, "exit_reason": tr.exit_reason, "pnl": tr.pnl,
           "pnl_pct": tr.pnl_pct, "pnl_mid": tr.pnl_mid, "mfe_pct": tr.mfe_pct, "mae_pct": tr.mae_pct,
           "mfe_at": _et(tr.peak_t, tz).strftime("%H:%M") if tr.peak_t else None,
           "mae_at": _et(tr.low_t, tz).strftime("%H:%M") if tr.low_t else None,
           "exit_spread_pct": tr.exit_spread_pct, "minutes": round((out_t - tr.opened_t) / 60000, 1)}
    if tr.under and tr.under_hi and tr.under_lo:
        sg = 1 if tr.side == "bull" else -1
        best, worst = (tr.under_hi, tr.under_lo) if sg > 0 else (tr.under_lo, tr.under_hi)
        rec["under_mfe_pct"] = round(100 * sg * (best / tr.under - 1), 2)
        rec["under_mae_pct"] = round(100 * sg * (worst / tr.under - 1), 2)
    rec.update(tr.info)
    return rec


async def manage(client, store, cfg, now: datetime, five: dict[str, list[Bar]]) -> int:
    """Re-price open paper trades: stops, targets, trails, invalidation, the end-of-day exit. Returns alerts queued."""
    async with LOCK:
        st = _state(store, now.astimezone(cfg.tz).date())
        sent = _manage(await _snapshots(client, st), store, cfg, now, five, st)
        store.put(KEY, st)
        return sent


async def _snapshots(client, st: dict) -> dict:
    tickers = [d["contract"] for d in st["open"].values()]
    if not tickers:
        return {}
    try:
        return await client.snapshots(tickers)
    except Exception as exc:
        log.warning("ema quotes failed: %s", exc)
        return {}


def _manage(snaps: dict, store, cfg, now: datetime, five: dict[str, list[Bar]], st: dict) -> int:
    rules: DayTradeRules = cfg.daytrade
    tz = cfg.tz
    now_ms = int(now.timestamp() * 1000)
    close_ms = _at(now.astimezone(tz).date(), rules.close, tz)
    sent = 0
    for sym, d in list(st["open"].items()):
        tr = Trade(**d)
        snap = (snaps or {}).get(tr.contract) or {}
        bid, ask, value = _quote(snap)
        under = (snap.get("underlying_asset") or {}).get("price")
        if value is None:
            continue
        tr.bid, tr.ask = bid, ask
        tr.mark(now_ms, under)
        bars = five.get(sym) or []
        last5 = next((b for b in reversed(bars) if b.t + FIVE <= now_ms and b.t + FIVE > tr.opened_t), None)
        if now_ms >= close_ms:
            events = tr.exit(now_ms, value, "time")
        elif last5 and invalidated(tr, last5.c):
            tr.low = min(tr.low or value, value)
            events = tr.exit(now_ms, value, "invalidated")
        else:
            events = tr.on_price(now_ms, value, value, value, value, rules)
        if tr.closed and bid and ask and value:
            tr.exit_spread_pct = round(100 * (ask - bid) / value, 1)
        for kind, detail in events:
            store.enqueue(f"ema_{kind}", event_text(tr, kind, detail, value, under), sym)
            sent += 1
        if tr.closed:
            del st["open"][sym]
            st["losses"][sym] = st["losses"].get(sym, 0) + 1 if tr.pnl < 0 else 0
            rec = _close_record(tr, tz, now_ms)
            st["closed"].append(rec)
            store.put(LOG_KEY, (store.get(LOG_KEY) or [])[-499:] + [dict(rec, date=st["date"])])
        else:
            st["open"][sym] = asdict(tr)
    return sent


async def live(client, store, cfg, now: datetime, five: dict[str, list[Bar]], results: dict | None = None) -> int:
    """Run once per cycle: manage open paper trades, open new ones, send the day's summary at the close.
    `five` maps symbol to completed regular-session 5-minute bars (today plus warm-up days); `results` maps symbol
    to the cycle's SymbolResult (for the trend at entry)."""
    rules: DayTradeRules = cfg.daytrade
    tz = cfg.tz
    local = now.astimezone(tz)
    today = local.date()
    now_ms = int(now.timestamp() * 1000)
    close_ms = _at(today, rules.close, tz)
    async with LOCK:
        st = _state(store, today)
        sent = _manage(await _snapshots(client, st), store, cfg, now, five, st)
        for sym in rules.symbols:
            bars = [b for b in five.get(sym) or [] if b.t + FIVE <= now_ms]
            for su in scan(sym, bars, today, tz, rules):
                if su.key in st["seen"] or su.status == "pending":
                    continue
                st["seen"].append(su.key)
                fresh = su.entry_t is not None and now_ms - su.entry_t <= 2 * FIVE
                if su.status != "entry" or not fresh:
                    why = su.status if su.status != "entry" else "missed"
                    st.setdefault("skipped", {})[why] = st.setdefault("skipped", {}).get(why, 0) + 1
                    continue
                r = (results or {}).get(sym)
                swing = r.stock.score if r is not None and r.stock else None
                intra = r.stock.composites.get("intraday", (None, ""))[0] if r is not None and r.stock else None
                against = swing is not None and su.sign * swing < cfg.trend_bias
                losses = st["losses"].get(sym, 0)
                why = "in a trade" if sym in st["open"] else "daily limit" if losses >= rules.max_losses_in_row \
                    else "against the swing trend" if rules.swing_filter and against else None
                if why:
                    skipped = st.setdefault("skipped", {})
                    skipped[why] = skipped.get(why, 0) + 1
                    continue
                try:
                    chain = await _chain(client, sym, su.price, today)
                except Exception as exc:
                    log.warning("ema %s chain failed: %s", sym, exc)
                    continue
                kind = "call" if su.side == "bull" else "put"
                rows = [c for c in chain if (c.get("details") or {}).get("contract_type") == kind and _mid(c)]
                expiry = pick_expiry(list({c["details"]["expiration_date"] for c in rows}), today, now_ms, tz, rules)
                rows = [c for c in rows if c["details"]["expiration_date"] == expiry]
                if not rows:
                    continue
                spot = (rows[0].get("underlying_asset") or {}).get("price") or su.price
                liquid = [c for c in rows if _spread_pct(c) <= MAX_SPREAD_PCT] or rows     # tight quotes first
                c = min(liquid, key=lambda c: abs(c["details"]["strike_price"] - spot))
                bid, ask, mid = _quote(c)
                tr = Trade.open(su, c["details"]["ticker"], float(c["details"]["strike_price"]), expiry, now_ms,
                                ask or mid, rules)
                tr.entry_mid, tr.bid, tr.ask = mid, bid, ask
                tr.mark(now_ms, spot)
                delta = abs(((c.get("greeks") or {}).get("delta")) or 0) or None
                tr.info = entry_info(su, tr, rules, now_ms, spot, delta, swing, intra, cfg)
                st["open"][sym] = asdict(tr)
                store.enqueue("ema_entry", entry_text(su, tr, rules, tz, losses), sym)
                sent += 1
                try:
                    from . import scorecard
                    scorecard.record(store, now.timestamp(), sym, "ema_entry", su.side, su.price, tr.contract,
                                     tr.entry)
                except Exception:
                    log.exception("ema scorecard record failed")

        if now_ms >= close_ms and not st["open"] and not st["summary"]:
            text = summary_text(st, rules, today)
            if text:
                store.enqueue("ema_summary", text)
                sent += 1
            st["summary"] = True
        store.put(KEY, st)
    return sent


def entry_info(su: Setup, tr: Trade, rules: DayTradeRules, now_ms: int, spot: float, delta: float | None,
               swing: float | None, intra: float | None, cfg) -> dict:
    """What a study of the trade needs later: why it was taken, how late the fill was, how far each stop sat."""
    from .trend import label_for
    tz = cfg.tz
    retest = f", pullback {_et(su.retest_t, tz):%H:%M}" if su.retest_t else ""
    prem_stop = tr.entry * rules.stop
    return {
        "reason": f"{'bullish' if su.side == 'bull' else 'bearish'} EMA {rules.fast}/{rules.slow} cross "
                  f"{_et(su.cross_t + FIVE, tz):%H:%M}{retest}, confirmation candle closed {_et(su.entry_t, tz):%H:%M}",
        "confirmed_close": True,                 # only completed 5-minute candles are ever scanned
        "candle_close": _et(su.entry_t, tz).strftime("%H:%M:%S"),
        "fill_at": _et(now_ms, tz).strftime("%H:%M:%S"),
        "fill_delay_s": round((now_ms - su.entry_t) / 1000),
        "chart_stop_pct": round(100 * abs(su.price - su.invalidation) / su.price, 2),
        "premium_stop_pct": round(100 * prem_stop / delta / spot, 2) if delta and spot else None,
        "delta": round(delta, 2) if delta else None,
        "entry_bid": tr.bid, "entry_ask": tr.ask,
        "entry_spread_pct": round(100 * (tr.ask - tr.bid) / tr.entry_mid, 1) if tr.bid and tr.ask and tr.entry_mid
        else None,
        "swing": round(swing) if swing is not None else None,
        "intraday": round(intra) if intra is not None else None,
        "with_swing": None if swing is None else
        ("with" if su.sign * swing >= cfg.trend_bias else "against" if su.sign * swing <= -cfg.trend_bias
         else "neutral"),
        "swing_label": label_for(swing, cfg.trend_strong, cfg.trend_bias) if swing is not None else None,
        "vwap": su.vwap, "orb": su.orb, "regime": su.regime,
    }


def status_text(store, cfg, now: datetime, five: dict[str, list[Bar]]) -> str:
    """/ema: where each day-trade symbol stands right now."""
    rules: DayTradeRules = cfg.daytrade
    tz, today = cfg.tz, now.astimezone(cfg.tz).date()
    st = _state(store, today)
    now_ms = int(now.timestamp() * 1000)
    lines = [f"📐 <b>EMA {rules.fast}/{rules.slow} day-trade watch</b> (5m)  {now.astimezone(tz):%a %H:%M} ET"]
    for sym in rules.symbols:
        bars = [b for b in five.get(sym) or [] if b.t + FIVE <= now_ms]
        if len(bars) < rules.slow + 1:
            lines.append(f"{sym}: no bars yet")
            continue
        closes = [b.c for b in bars]
        f, s = ema(closes, rules.fast)[-1], ema(closes, rules.slow)[-1]
        setups = scan(sym, bars, today, tz, rules)
        last = setups[-1] if setups else None
        side = "above" if f > s else "below"
        line = f"<b>{sym}</b> {closes[-1]:.2f} · EMA9 {f:.2f} {side} EMA21 {s:.2f}"
        if last:
            line += (f" · last cross {'↑' if last.side == 'bull' else '↓'} {_et(last.cross_t + FIVE, tz):%H:%M}: "
                     f"{last.status.replace('_', ' ')}")
        lines.append(line)
        if sym in st["open"]:
            tr = Trade(**st["open"][sym])
            lines.append(f"  open: {tr.strike:g}{'C' if tr.side == 'bull' else 'P'} {tr.expiry[5:]} entry "
                         f"{tr.entry:.2f} · stop {tr.stop:.2f} · {tr.open_qty} left · realized {_money(tr.pnl)}")
    for t in st.get("closed") or []:
        lines.append(f"  closed: {t['symbol']} {t['contract']} {t['in']}→{t['out']} {t['exit_reason']} "
                     f"{_money(t['pnl'])}" + (f" · best {t['mfe_pct']:+.0f}% / worst {t['mae_pct']:+.0f}%"
                                              if "mfe_pct" in t else ""))
    lines.append(f"<i>Entries {rules.start}–{rules.last_entry} ET, all closed by {rules.close}. Paper-tracked.</i>")
    return "\n".join(lines)


async def run_backtest(cfg, symbols: list[str], start: date, end: date, out_dir: Path) -> str:
    from .massive import MassiveClient
    out_dir.mkdir(parents=True, exist_ok=True)
    texts = []
    async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout, max_retries=cfg.max_retries,
                             max_concurrency=cfg.max_concurrency) as client:
        for sym in symbols:
            cache = out_dir / "cache"
            trades, setups = await backtest(client, sym, start, end, cfg.tz, cfg.daytrade, cache)
            control, _ = await backtest(client, sym, start, end, cfg.tz, cfg.daytrade, cache, retest=False)
            text = report(sym, start, end, trades, setups, control, cfg.daytrade)
            (out_dir / f"{sym}_report.md").write_text(text)
            write_csv(out_dir / f"{sym}_trades.csv", trades)
            write_csv(out_dir / f"{sym}_control.csv", control)
            texts.append(text)
    return "\n\n".join(texts)
