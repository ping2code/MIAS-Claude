"""Research measurement helpers (REPORT ONLY; nothing in the live service imports this module).

Entry-time SPY measurement, free of look-ahead:
  spy_day_pct_at_entry = 100 * (SPY price at entry / previous regular-session close - 1)
  - SPY price at entry = the close of the last COMPLETED regular-session 1-minute bar whose end (bar start + 60 s) is
    at or before the entry timestamp, on the entry's own trading day. The minute that contains the entry is never read
    (its close lies in the future). Before 09:31:00 ET no bar of the day is complete, so the value is None.
  - Previous regular-session close = the close of the last regular-session 1-minute bar (09:30-16:00 ET, the
    15:59 bar, or the last bar of an early-close day) of the most recent earlier trading day in the series.
  spy_dir_at_entry = UP if > 0, DOWN if < 0, FLAT only when exactly 0 (no threshold: none is defined for entry time;
  the +/-0.25% FLAT band in mias.scorecard belongs to the full-day label, which uses the day's last check).
The full-day label (paperscore / scorecard 'regime', exported as spy_day_regime_eod) uses information from after the
entry and must never be used for entry-time research.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .indicators import Bar

MINUTE = 60_000


class SpyAtEntry:
    """Index over regular-session 1-minute SPY bars (any order; duplicates by start time are dropped)."""

    def __init__(self, bars: list[Bar], tz: ZoneInfo):
        self.tz = tz
        uniq = {b.t: b for b in bars}
        self.bars = [uniq[t] for t in sorted(uniq)]
        self.ends = [b.t + MINUTE for b in self.bars]
        self.day = [datetime.fromtimestamp(b.t / 1000, timezone.utc).astimezone(tz).date() for b in self.bars]
        self.days = sorted(set(self.day))
        self.last_close: dict[date, float] = {}
        for d, b in zip(self.day, self.bars):
            self.last_close[d] = b.c                     # bars are sorted: the last one wins

    def prev_close(self, day: date) -> float | None:
        i = bisect_right(self.days, day) - 1             # days[i] <= day
        if i >= 0 and self.days[i] == day:
            i -= 1
        return self.last_close[self.days[i]] if i >= 0 else None

    def at(self, ts_ms: int) -> dict:
        """{'price', 'prev_close', 'pct', 'dir', 'bar_end'}; values None when the data cannot say."""
        day = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).astimezone(self.tz).date()
        out = {"price": None, "prev_close": self.prev_close(day), "pct": None, "dir": None, "bar_end": None}
        i = bisect_right(self.ends, ts_ms) - 1           # last bar that had ended by ts_ms
        if i < 0 or self.day[i] != day:
            return out
        out["price"], out["bar_end"] = self.bars[i].c, self.ends[i]
        if out["prev_close"]:
            pct = 100 * (out["price"] / out["prev_close"] - 1)
            out["pct"] = round(pct, 4)
            out["dir"] = direction(pct)
        return out


def direction(pct: float | None) -> str | None:
    if pct is None:
        return None
    return "UP" if pct > 0 else "DOWN" if pct < 0 else "FLAT"


async def spy_minutes(client, cache_dir, start: date, end: date, tz: ZoneInfo) -> list[Bar]:
    """Regular-session SPY 1-minute bars from start to end, through the existing backtest cache (emacross._Cache,
    a month per request; months that reach today are fetched fresh and not cached)."""
    from .emacross import _Cache, _five_minute
    from pathlib import Path
    return await _five_minute(client, _Cache(Path(cache_dir)), "SPY", start, end, tz, minutes=1)
