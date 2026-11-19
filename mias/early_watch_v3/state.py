"""V3 copy of mias/observer/state.py (frozen V2 state engine; V3 owns this copy; V1 / V2 never import it).

Deterministic per-symbol chart state, built only from completed regular-session 1-minute bars.

The forming 5-minute bar is the merge of the completed 1-minute bars inside it. EMAs, ATR, RSI and MACD advance
only on completed 5-minute bars; the *provisional* EMAs apply one more step with the forming 5-minute close and are
never stored. ``on_minute`` returns an immutable ``Features`` snapshot for the watch logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from statistics import mean
from zoneinfo import ZoneInfo

from ..indicators import Bar, ema, rsi
from .atr import atr_5m
from .config import FIVE, ONE, DetectRules as Rules

RULES = Rules()
from .structure import Candidate, Pivot, Structure


def ema_step(prev: float | None, value: float, period: int) -> float:
    """One step of indicators.ema (same seed and k): ema(xs + [v])[-1] == ema_step(ema(xs)[-1], v)."""
    if prev is None:
        return value
    k = 2 / (period + 1)
    return value * k + prev * (1 - k)


def typical(b: Bar) -> float:
    """The price indicators.session_vwap weights by volume."""
    return (b.h + b.l + b.c) / 3


def five_minute(bars1: list[Bar]) -> list[Bar]:
    """Merge completed 1-minute bars into clock-aligned 5-minute bars (the same rule the live state uses)."""
    out: list[Bar] = []
    cur: Bar | None = None
    for b in bars1:
        t = b.t // FIVE * FIVE
        if cur is None or cur.t != t:
            if cur is not None:
                out.append(cur)
            cur = Bar(t, b.o, b.h, b.l, b.c, b.v)
        else:
            cur = Bar(t, cur.o, max(cur.h, b.h), min(cur.l, b.l), b.c, cur.v + b.v)
    if cur is not None:
        out.append(cur)
    return out


def et(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz)


def at(day: date, hm, tz: ZoneInfo) -> int:
    return int(datetime.combine(day, hm, tz).timestamp() * 1000)


@dataclass(frozen=True)
class Features:
    sym: str
    t: int                    # 1-minute bar start
    e: int                    # 1-minute bar end (the moment this snapshot became knowable)
    o: float
    h: float
    l: float
    c: float
    v: float
    vwap: float | None
    ema9: float | None        # provisional (with the forming 5m close) unless this bar completed a 5m bar
    ema21: float | None
    spread: float | None      # ema9 - ema21 (provisional)
    spread_ref: float | None  # spread at the last completed 5m state before this one
    ema9_c: float | None      # completed-5m values
    ema21_c: float | None
    closed5: bool             # this 1-minute bar completed a 5-minute bar
    atr: float | None
    or_high: float | None
    or_low: float | None
    or_done: bool
    hl: Candidate | None      # developing Higher Low (bull)
    lh: Candidate | None      # developing Lower High (bear)
    stale: bool
    day_pct: float | None
    minutes_since_open: float
    rel_vol: float | None     # context only
    sma200: float | None      # context only
    rsi14: float | None       # context only (5m)
    macd_hist: float | None   # context only (5m, 12/26/9)
    pivots: tuple[Pivot, ...] = ()            # pivots confirmed at this bar
    dev_events: tuple = ()                    # (kind, Candidate) formed / failed at this bar


@dataclass
class SymbolState:
    sym: str
    tz: ZoneInfo
    rules: Rules = RULES
    structure: Structure = field(init=False)
    bars5: list[Bar] = field(default_factory=list)
    day: date | None = None
    prev_close: float | None = None
    today: list[Bar] = field(default_factory=list)
    forming: Bar | None = None
    ema9_c: float | None = None
    ema21_c: float | None = None
    spread_c: float | None = None
    spread_c_prev: float | None = None
    macd_fast: float | None = None
    macd_slow: float | None = None
    macd_signal: float | None = None
    atr5: float | None = None
    rsi5: float | None = None
    or_high: float | None = None
    or_low: float | None = None
    pv: float = 0.0
    vol: float = 0.0
    cum_profile: dict[int, float] = field(default_factory=dict)   # minute-of-session -> avg cumulative volume
    sma200: float | None = None
    stale_left: int = 0
    last_e: int | None = None

    def __post_init__(self):
        self.structure = Structure(self.rules)

    # ---- warm-up ----------------------------------------------------------------------------------

    def warm(self, sessions: list[tuple[date, list[Bar]]], daily_closes: list[float] | None = None) -> None:
        """Prior regular sessions (oldest first, 1-minute bars). All of them warm the 5-minute indicators; the last
        one is replayed minute by minute so the structure (pivots, developing legs) and the previous close match
        what a live run would hold at the open."""
        sessions = [(d, bars) for d, bars in sessions if bars][-self.rules.warm_sessions:]
        for _, bars in sessions[:-1]:
            for b5 in five_minute(bars):
                self._advance5(b5, structure=False)
        if self.bars5:
            self._window_indicators()
        cums = []
        for d, bars in sessions:
            o = at(d, self.rules.session_open, self.tz)
            c, prof = 0.0, {}
            for b in bars:
                c += b.v
                prof[(b.t - o) // ONE] = c
            cums.append(prof)
        if sessions:
            last_d, last = sessions[-1]
            self.start_day(last_d, None)
            for b in last:
                self.on_minute(b)
            self.prev_close = last[-1].c
        keys = sorted({k for p in cums for k in p})
        filled = []                                        # minutes with no trade carry the last cumulative value
        for p in cums:
            last, row = None, {}
            for k in keys:
                last = p.get(k, last)
                row[k] = last
            filled.append(row)
        for k in keys:
            vals = [row[k] for row in filled if row[k] is not None]
            self.cum_profile[k] = mean(vals) if vals else 0.0
        if daily_closes and len(daily_closes) >= 200:
            self.sma200 = mean(daily_closes[-200:])

    def start_day(self, day: date, prev_close: float | None) -> None:
        if self.forming is not None:          # a 5m bar left open by a missing last minute closes at the day roll
            self._close5()
        self.day = day
        if prev_close is not None:
            self.prev_close = prev_close
        elif self.today:
            self.prev_close = self.today[-1].c
        prev_first = self.today[0].t if self.today else None
        self.today = []
        self.or_high = self.or_low = None
        self.pv = self.vol = 0.0
        self.stale_left = 0
        self.last_e = None
        if prev_first is not None:
            self.structure.trim(prev_first)   # keep the previous session's 1-minute bars for leg extremes

    # ---- per minute -------------------------------------------------------------------------------

    def on_minute(self, b: Bar, gap: bool = False) -> Features:
        r = self.rules
        if gap:
            self.stale_left = r.stale_bars_after_gap
        bucket = b.t // FIVE * FIVE
        pivots: list[Pivot] = []
        if self.forming is not None and self.forming.t != bucket:
            pivots += self._close5()
        if self.forming is None:
            self.forming = Bar(bucket, b.o, b.h, b.l, b.c, b.v)
        else:
            f = self.forming
            self.forming = Bar(bucket, f.o, max(f.h, b.h), min(f.l, b.l), b.c, f.v + b.v)
        closed5 = b.t + ONE == bucket + FIVE
        spread_before = self.spread_c
        if closed5:
            pivots += self._close5()
        self.today.append(b)
        self.pv += typical(b) * b.v
        self.vol += b.v
        open_ms = at(self.day, r.session_open, self.tz) if self.day else b.t
        or_end = at(self.day, r.or_end, self.tz) if self.day else b.t
        if b.t < or_end:
            self.or_high = b.h if self.or_high is None else max(self.or_high, b.h)
            self.or_low = b.l if self.or_low is None else min(self.or_low, b.l)
        dev = self.structure.on_one(b, self.atr5)
        if self.forming is not None:          # provisional: one more EMA step with the forming close
            e9 = ema_step(self.ema9_c, self.forming.c, r.ema_fast) if self.ema9_c is not None else None
            e21 = ema_step(self.ema21_c, self.forming.c, r.ema_slow) if self.ema21_c is not None else None
            ref = self.spread_c
        else:
            e9, e21 = self.ema9_c, self.ema21_c
            ref = spread_before if closed5 else self.spread_c_prev
        spread = e9 - e21 if e9 is not None and e21 is not None else None
        cum_avg = self.cum_profile.get((b.t - open_ms) // ONE)
        stale = self.stale_left > 0
        if self.stale_left:
            self.stale_left -= 1
        self.last_e = b.t + ONE
        return Features(
            sym=self.sym, t=b.t, e=b.t + ONE, o=b.o, h=b.h, l=b.l, c=b.c, v=b.v,
            vwap=self.pv / self.vol if self.vol else None, ema9=e9, ema21=e21, spread=spread, spread_ref=ref,
            ema9_c=self.ema9_c, ema21_c=self.ema21_c, closed5=closed5, atr=self.atr5,
            or_high=self.or_high, or_low=self.or_low, or_done=b.t + ONE >= or_end,
            hl=self.structure.cand["bull"], lh=self.structure.cand["bear"], stale=stale,
            day_pct=100 * (b.c / self.prev_close - 1) if self.prev_close else None,
            minutes_since_open=(b.t + ONE - open_ms) / ONE,
            rel_vol=self.vol / cum_avg if cum_avg else None, sma200=self.sma200, rsi14=self.rsi5,
            macd_hist=(self.macd_fast - self.macd_slow - self.macd_signal)
            if None not in (self.macd_fast, self.macd_slow, self.macd_signal) else None,
            pivots=tuple(pivots), dev_events=tuple(dev))

    # ---- 5-minute bars ----------------------------------------------------------------------------

    def _close5(self) -> list[Pivot]:
        b, self.forming = self.forming, None
        return self._advance5(b, structure=True)

    def _advance5(self, b: Bar, structure: bool) -> list[Pivot]:
        r = self.rules
        self.bars5.append(b)
        if len(self.bars5) > 1200:
            del self.bars5[:-1200]
        self.ema9_c = ema_step(self.ema9_c, b.c, r.ema_fast)
        self.ema21_c = ema_step(self.ema21_c, b.c, r.ema_slow)
        self.spread_c_prev, self.spread_c = self.spread_c, self.ema9_c - self.ema21_c
        self.macd_fast = ema_step(self.macd_fast, b.c, 12)
        self.macd_slow = ema_step(self.macd_slow, b.c, 26)
        self.macd_signal = ema_step(self.macd_signal, self.macd_fast - self.macd_slow, 9)
        if not structure:                 # fast warm-up: ATR / RSI are pure functions of the last 300 bars,
            return []                     # so computing them once afterwards gives the same values
        self._window_indicators()
        return self.structure.on_five(b)

    def _window_indicators(self) -> None:
        recent = self.bars5[-300:]
        self.atr5 = atr_5m(self.bars5, self.rules.atr_period)
        self.rsi5 = rsi([x.c for x in recent], 14)


def check_ema(closes: list[float], period: int) -> float:
    """indicators.ema's last value, for the equality test of ema_step."""
    return ema(closes, period)[-1]
