"""Stock trend per timeframe and as a weighted composite.

Each timeframe gets a score from -100 (strong downtrend) to +100 (strong uptrend). The score is the
weighted direction of five transparent components, scaled by trend strength (ADX):

  stack      close / EMA9 / EMA21 / EMA50 ordering
  slope      20-bar regression slope, measured in ATRs
  momentum   RSI(14) distance from 50
  structure  higher highs / higher lows over the last 10 bars vs the 10 before
  vwap       distance from session VWAP in ATRs (intraday timeframes only)
"""
from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from .indicators import Bar, adx, atr, clamp, ema, linreg_slope, rsi, session_vwap, sign, tanh

SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
MIN_BARS = 30


@dataclass
class TimeframeTrend:
    timeframe: str
    score: float
    label: str
    close: float
    bar_time: int
    components: dict[str, float] = field(default_factory=dict)
    adx: float | None = None
    rsi: float | None = None
    vwap: float | None = None


@dataclass
class StockTrend:
    score: float                 # primary profile composite
    label: str
    timeframes: dict[str, TimeframeTrend]
    last_close: float | None
    last_bar_time: int | None
    profile: str = "swing"
    composites: dict[str, tuple[float, str]] = field(default_factory=dict)   # profile -> (score, label)
    hv20: float | None = None    # 20-day realized volatility, percent annualized


def label_for(score: float, strong: float, bias: float) -> str:
    if score >= strong:
        return "strong_up"
    if score >= bias:
        return "up"
    if score <= -strong:
        return "strong_down"
    if score <= -bias:
        return "down"
    return "neutral"


def direction(label: str) -> str:
    """Coarse class used for alerting: bull / bear / neutral."""
    if label.endswith("up") or label.endswith("bullish"):
        return "bull"
    if label.endswith("down") or label.endswith("bearish"):
        return "bear"
    return "neutral"


def to_bars(raw: list[dict]) -> list[Bar]:
    out = []
    for r in raw:
        try:
            out.append(Bar(int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r.get("v", 0))))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _et(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz)


def regular_session(bars: list[Bar], tz: ZoneInfo) -> list[Bar]:
    out = []
    for b in bars:
        t = _et(b.t, tz)
        if t.weekday() < 5 and SESSION_OPEN <= t.time() < SESSION_CLOSE:
            out.append(b)
    return out


def complete_bars(bars: list[Bar], minutes: int, available_until_ms: int) -> list[Bar]:
    """Drop bars that had not closed by the time the data is valid for."""
    span = minutes * 60_000
    return [b for b in bars if b.t + span <= available_until_ms]


def resample(bars: list[Bar], minutes: int, tz: ZoneInfo) -> list[Bar]:
    """Resample regular-session 5-minute bars into N-minute bars aligned to the 09:30 open."""
    groups: OrderedDict[tuple, list[Bar]] = OrderedDict()
    for b in bars:
        t = _et(b.t, tz)
        since_open = (t.hour * 60 + t.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)
        groups.setdefault((t.date(), since_open // minutes), []).append(b)
    return [Bar(g[0].t, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c, sum(x.v for x in g))
            for g in groups.values()]


def weekly(daily: list[Bar], tz: ZoneInfo) -> list[Bar]:
    groups: OrderedDict[tuple, list[Bar]] = OrderedDict()
    for b in daily:
        groups.setdefault(_et(b.t, tz).date().isocalendar()[:2], []).append(b)
    return [Bar(g[0].t, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c, sum(x.v for x in g))
            for g in groups.values()]


def realized_vol(daily: list[Bar], days: int = 20) -> float | None:
    closes = [b.c for b in daily[-(days + 1):]]
    if len(closes) < days + 1 or min(closes) <= 0:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round(100 * math.sqrt(var * 252), 2)


def last_session(bars: list[Bar], tz: ZoneInfo) -> list[Bar]:
    if not bars:
        return []
    day = _et(bars[-1].t, tz).date()
    return [b for b in bars if _et(b.t, tz).date() == day]


def timeframe_trend(timeframe: str, bars: list[Bar], *, strong: float, bias: float,
                    vwap_bars: list[Bar] | None = None) -> TimeframeTrend | None:
    if len(bars) < MIN_BARS:
        return None
    closes = [b.c for b in bars]
    c = closes[-1]
    a = atr(bars) or 0.0
    comps: dict[str, float] = {}
    weights: dict[str, float] = {}

    e9, e21 = ema(closes, 9)[-1], ema(closes, 21)[-1]
    stack = [sign(c - e9), sign(e9 - e21)]
    if len(closes) >= 50:
        stack.append(sign(e21 - ema(closes, 50)[-1]))
    comps["stack"] = sum(stack) / len(stack)
    weights["stack"] = 0.30

    if a > 0:
        comps["slope"] = tanh(linreg_slope(closes[-20:]) * 20 / a / 2)
        weights["slope"] = 0.25

    r = rsi(closes)
    if r is not None:
        comps["momentum"] = clamp((r - 50) / 20)
        weights["momentum"] = 0.15

    if len(bars) >= 20:
        recent, prior = bars[-10:], bars[-20:-10]
        hh = sign(max(b.h for b in recent) - max(b.h for b in prior))
        hl = sign(min(b.l for b in recent) - min(b.l for b in prior))
        comps["structure"] = (hh + hl) / 2
        weights["structure"] = 0.15

    vw = session_vwap(vwap_bars) if vwap_bars else None
    if vw is not None and a > 0:
        comps["vwap"] = tanh((c - vw) / a)
        weights["vwap"] = 0.15

    total_w = sum(weights.values())
    raw = sum(comps[k] * weights[k] for k in comps) / total_w if total_w else 0.0
    strength = adx(bars)
    factor = 0.8 if strength is None else 0.6 + 0.4 * clamp((strength - 15) / 15, 0.0, 1.0)
    score = round(100 * raw * factor, 1)
    return TimeframeTrend(timeframe, score, label_for(score, strong, bias), c, bars[-1].t,
                          {k: round(v, 3) for k, v in comps.items()}, strength, r, vw)


def stock_trend(bars_5m_raw: list[dict], bars_1d_raw: list[dict], *, tz: ZoneInfo, available_until_ms: int,
                profiles: dict[str, dict[str, float]], primary: str, strong: float, bias: float) -> StockTrend:
    """Build every timeframe from regular-session 5-minute bars plus daily bars, then one composite per profile."""
    five = complete_bars(regular_session(to_bars(bars_5m_raw), tz), 5, available_until_ms)
    daily = to_bars(bars_1d_raw)
    session = last_session(five, tz)
    needed = {tf for w in profiles.values() for tf in w}

    tfs: dict[str, TimeframeTrend] = {}
    series = {"5m": lambda: five, "15m": lambda: resample(five, 15, tz), "1h": lambda: resample(five, 60, tz),
              "1d": lambda: daily, "1w": lambda: weekly(daily, tz)}
    for tf, build in series.items():
        if tf not in needed:
            continue
        intraday = tf in ("5m", "15m", "1h")
        res = timeframe_trend(tf, build(), strong=strong, bias=bias, vwap_bars=session if intraday else None)
        if res:
            tfs[tf] = res

    composites = {}
    for name, weights in profiles.items():
        used = {tf: w for tf, w in weights.items() if tf in tfs}
        total = sum(used.values())
        score = round(sum(tfs[tf].score * w for tf, w in used.items()) / total, 1) if total else 0.0
        composites[name] = (score, label_for(score, strong, bias))
    score, label = composites[primary]
    last = five[-1] if five else (daily[-1] if daily else None)
    return StockTrend(score, label, tfs, last.c if last else None, last.t if last else None,
                      primary, composites, realized_vol(daily))
