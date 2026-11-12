"""Synthetic regular-session 1-minute bars for the observer tests (deterministic, seeded)."""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from mias.indicators import Bar

ET = ZoneInfo("America/New_York")


def trading_days(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def session(day: date, start_px: float, seed: int, drift: list[float] | None = None) -> list[Bar]:
    """390 one-minute bars 09:30-16:00 ET: a random walk whose drift changes every 45 minutes (trends + pullbacks)."""
    rng = random.Random(seed)
    t0 = int(datetime.combine(day, datetime.min.time().replace(hour=9, minute=30), ET).timestamp() * 1000)
    drift = drift or [rng.choice([-1, 1]) * rng.uniform(0.0002, 0.0008) for _ in range(9)]
    px, out = start_px, []
    for i in range(390):
        d = drift[min(i // 45, len(drift) - 1)]
        o = px
        c = o * (1 + d + rng.gauss(0, 0.0007))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.0003)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.0003)))
        out.append(Bar(t0 + i * 60_000, round(o, 4), round(h, 4), round(lo, 4), round(c, 4),
                       float(rng.randint(5_000, 50_000))))
        px = c
    return out


def history(symbols: list[str], days: list[date], seed: int = 7) -> dict[str, dict[date, list[Bar]]]:
    out = {}
    for k, s in enumerate(symbols):
        px = 100.0 + 50 * k
        per = {}
        for j, d in enumerate(days):
            bars = session(d, px, seed * 1000 + k * 100 + j)
            per[d] = bars
            px = bars[-1].c
        out[s] = per
    return out


def daily(hist) -> dict:
    return {s: [(d, bars[-1].c) for d, bars in per.items()] for s, per in hist.items()}
