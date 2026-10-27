"""Price confirmation for options flow: is the stock trading the way the flow leans?

For each symbol, from the 5-minute bars the cycle already fetched: the regular-session VWAP and which side of it
the last price is on, and relative volume (today's volume so far against the average volume up to the same time
of day over the last 20 sessions). The market read is the same for the market symbols (SPY, QQQ, IWM).

Bullish flow is confirmed when the stock is above VWAP, bearish flow when below; flow against the side of VWAP
is flagged. Flow that goes against most of the market symbols is flagged too.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .indicators import Bar, session_vwap
from .trend import regular_session, to_bars

SESSIONS = 20


def _day(b: Bar, tz: ZoneInfo) -> date:
    return datetime.fromtimestamp(b.t / 1000, timezone.utc).astimezone(tz).date()


def _tod(b: Bar, tz: ZoneInfo) -> int:
    t = datetime.fromtimestamp(b.t / 1000, timezone.utc).astimezone(tz)
    return t.hour * 60 + t.minute


def read(hist_raw: list[dict], today_raw: list[dict], tz: ZoneInfo, today: date) -> dict | None:
    """{"price", "vwap", "side" (+1 above / -1 below / 0), "rvol" (or None)} from raw 5-minute bars."""
    bars = regular_session(to_bars(hist_raw + today_raw), tz)
    by_day: dict[date, list[Bar]] = {}
    for b in bars:
        by_day.setdefault(_day(b, tz), []).append(b)
    session = by_day.get(today)
    if not session:
        return None
    vwap = session_vwap(session)
    if not vwap:
        return None
    price = session[-1].c
    upto = _tod(session[-1], tz)
    past = [sum(b.v for b in by_day[d] if _tod(b, tz) <= upto) for d in sorted(by_day) if d < today][-SESSIONS:]
    avg = sum(past) / len(past) if past else 0
    side = 1 if price > vwap else -1 if price < vwap else 0
    return {"price": price, "vwap": vwap, "side": side,
            "rvol": sum(b.v for b in session) / avg if avg else None}


def reads(engine, cfg, symbols, today: date) -> dict[str, dict]:
    """Reads for the given symbols (indexes use their trend symbol's bars) from the engine's bar caches."""
    out = {}
    for sym in symbols:
        inst = cfg.instruments.get(sym)
        src = inst.trend_symbol if inst else sym
        hist = (engine._hist5.get(src) or (None, []))[1]
        today5 = engine.today5.get(src) or []
        try:
            x = read(hist, today5, cfg.tz, today)
        except Exception:
            x = None
        if x:
            out[sym] = x
    return out


def market_lean(reads: dict[str, dict], market: list[str]) -> int:
    """+1 when most market symbols are above VWAP, -1 when most are below, 0 when split or unknown."""
    sides = [reads[s]["side"] for s in market if s in reads]
    if not sides:
        return 0
    up, down = sides.count(1), sides.count(-1)
    return 1 if up > len(sides) / 2 else -1 if down > len(sides) / 2 else 0


def market_line(reads: dict[str, dict], market: list[str]) -> str | None:
    parts = []
    for s in market:
        x = reads.get(s)
        if x:
            word = {1: "above", -1: "below", 0: "at"}[x["side"]]
            parts.append(f"{s} {word} VWAP" + (f" ({x['rvol']:.1f}× vol)" if x.get("rvol") else ""))
    return "Market: " + " · ".join(parts) if parts else None


def verdict(lean: str, x: dict | None, market: int) -> tuple[bool | None, str]:
    """(confirmed?, short tag) for flow leaning bull/bear on a symbol read. None when there's nothing to judge."""
    if lean not in ("bull", "bear") or not x:
        return None, ""
    want = 1 if lean == "bull" else -1
    vol = f", {x['rvol']:.1f}× vol" if x.get("rvol") else ""
    word = {1: "above", -1: "below", 0: "at"}[x["side"]]
    ok = x["side"] == want
    tag = ("✅ " if ok else "⚠️ ") + f"{word} VWAP{vol}"
    if market == -want:
        tag += " · ⚠️ vs market"
        ok = False
    return ok, tag
