"""Small, dependency-free technical indicators over plain float lists."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Bar:
    t: int          # bar start, epoch milliseconds UTC
    o: float
    h: float
    l: float
    c: float
    v: float


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[:period], closes[1:period + 1]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    for a, b in zip(closes[period:], closes[period + 1:]):
        d = b - a
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    return 100 - 100 / (1 + avg_g / avg_l)


def true_ranges(bars: list[Bar]) -> list[float]:
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.h - b.l)
        else:
            pc = bars[i - 1].c
            out.append(max(b.h - b.l, abs(b.h - pc), abs(b.l - pc)))
    return out


def atr(bars: list[Bar], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    tr = true_ranges(bars)
    value = sum(tr[1:period + 1]) / period
    for x in tr[period + 1:]:
        value = (value * (period - 1) + x) / period
    return value


def adx(bars: list[Bar], period: int = 14) -> float | None:
    """Wilder's ADX."""
    if len(bars) < 2 * period + 1:
        return None
    tr = true_ranges(bars)[1:]
    plus_dm, minus_dm = [], []
    for prev, cur in zip(bars, bars[1:]):
        up, down = cur.h - prev.h, prev.l - cur.l
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    def wilder(xs: list[float]) -> list[float]:
        s = sum(xs[:period])
        out = [s]
        for x in xs[period:]:
            s = s - s / period + x
            out.append(s)
        return out

    str_, sp, sm = wilder(tr), wilder(plus_dm), wilder(minus_dm)
    dx = []
    for t, p, m in zip(str_, sp, sm):
        if t == 0:
            dx.append(0.0)
            continue
        pdi, mdi = 100 * p / t, 100 * m / t
        dx.append(0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi))
    if len(dx) < period:
        return None
    value = sum(dx[:period]) / period
    for x in dx[period:]:
        value = (value * (period - 1) + x) / period
    return value


def linreg_slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2
    my = sum(values) / n
    num = sum((i - mx) * (y - my) for i, y in enumerate(values))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def session_vwap(bars: list[Bar]) -> float | None:
    pv = vol = 0.0
    for b in bars:
        pv += (b.h + b.l + b.c) / 3 * b.v
        vol += b.v
    return pv / vol if vol else None


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def sign(x: float) -> float:
    return 1.0 if x > 0 else -1.0 if x < 0 else 0.0


def tanh(x: float) -> float:
    return math.tanh(x)
