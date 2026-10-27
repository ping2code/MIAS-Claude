"""Event and IV context for choosing a contract: estimated earnings, events priced into the IV term structure, and
where today's IV sits in its own history. The plan has no earnings calendar (Benzinga data is not included), so:

  earnings   estimated from the price history: earnings show up as a gap or big move on 2x+ normal volume about
             every 91 days. The next report is the last one + 91 days (+/- a week). "confirmed" when the year
             shows the quarterly rhythm, "rough" when only one such day was found.
  iv event   the option chain itself: an expiry whose ATM IV implies a forward volatility far above the later
             expiries has an event (earnings, FDA, a court date...) priced in before it.
  iv rank    percentile of today's 30-day ATM IV among the daily values recorded by this service (needs 20+ days).

The advice follows the 2-year swing backtest (README, "Swing entries backtest"): fair IV was the best place to buy;
expensive IV hurt single options and debit spreads alike, so the advice is to size down rather than to switch.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

# Funds have no earnings; their volume spikes are rebalances and expiries.
FUNDS = {"SPY", "QQQ", "IWM", "DIA", "TQQQ", "SQQQ", "GLD", "SLV", "TLT", "XLE", "XLF", "XLV", "XLK", "SMH",
         "SOXL", "SOXS", "USO", "HYG", "EEM", "FXI", "KWEB", "ARKK", "NDX", "SPX"}
QUARTER = 91
IV_RICH_RATIO = 1.25
IV_RICH_PCT = 70
MIN_IV_DAYS = 20


def _bday_count(a: date, b: date) -> int:
    """Weekdays in (a, b]."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


def earnings_days(daily: list[dict], tz: ZoneInfo) -> list[date]:
    """Days that look like earnings reactions: a gap >= 2.5% or a move >= 4% on 2x+ the 20-day volume.
    At most one per 30 days (the strongest)."""
    bars = [(datetime.fromtimestamp(b["t"] / 1000, timezone.utc).astimezone(tz).date(), b) for b in daily
            if all(k in b for k in ("o", "c", "v"))]
    hits: list[tuple[date, float]] = []
    for i in range(21, len(bars)):
        d, b = bars[i]
        prev = bars[i - 1][1]
        avg = sum(x[1]["v"] for x in bars[i - 20:i]) / 20
        if not avg or not prev["c"]:
            continue
        ratio = b["v"] / avg
        gap, move = abs(b["o"] / prev["c"] - 1), abs(b["c"] / prev["c"] - 1)
        if ratio >= 2.0 and (gap >= 0.025 or move >= 0.04):
            hits.append((d, ratio))
    out: list[tuple[date, float]] = []
    for d, ratio in hits:
        if out and (d - out[-1][0]).days < 30:
            if ratio > out[-1][1]:
                out[-1] = (d, ratio)
            continue
        out.append((d, ratio))
    return [d for d, _ in out]


def earnings_estimate(symbol: str, daily: list[dict], tz: ZoneInfo, today: date) -> dict | None:
    """{"last": date, "next": date, "quality": "confirmed" | "rough"} or None (funds, or no pattern)."""
    if symbol in FUNDS or not daily:
        return None
    days = [d for d in earnings_days(daily, tz) if (today - d).days <= 400]
    if not days:
        return None
    last = days[-1]
    if (today - last).days > QUARTER + 45:
        return None                                   # nothing for a long while: no reliable rhythm
    if (today - last).days > QUARTER + 2:
        return None                                   # due date passed without a reaction big enough to see
    quarterly = any(abs((last - d).days - n * QUARTER) <= 20 for d in days[:-1] for n in (1, 2, 3, 4))
    nxt = last + timedelta(days=QUARTER)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return {"last": last.isoformat(), "next": nxt.isoformat(), "quality": "confirmed" if quarterly else "rough"}


def iv_event(iv_term: list, today: date, max_dte: int = 60) -> dict | None:
    """The first expiry (within max_dte) whose forward volatility stands far above the rest of the curve.
    iv_term: (expiry iso, dte, ATM IV %) sorted by expiry. Returns {"by", "after", "move"} or None."""
    pts = []
    for exp, dte, iv in sorted(iv_term, key=lambda x: x[1]):
        e = date.fromisoformat(exp)
        t = _bday_count(today, e) / 252
        if t > 0 and iv:
            pts.append((exp, t, (iv / 100) ** 2 * t))
    if len(pts) < 4:
        return None
    fwd = []                                          # (expiry, previous expiry, forward vol, interval years, var)
    prev_exp, prev_t, prev_w = None, 0.0, 0.0
    for exp, t, w in pts:
        dt = t - prev_t
        if dt <= 0:
            continue
        var = max(0.0, (w - prev_w) / dt)
        fwd.append((exp, prev_exp, math.sqrt(var), dt, w - prev_w))
        prev_exp, prev_t, prev_w = exp, t, w
    later = [f[2] for f in fwd if (date.fromisoformat(f[0]) - today).days > max_dte / 2] or [f[2] for f in fwd]
    base = median(later)
    for exp, prev, vol, dt, dw in fwd:
        if (date.fromisoformat(exp) - today).days > max_dte:
            break
        if vol >= 1.6 * base and vol - base >= 0.15:
            excess = max(0.0, dw - base ** 2 * dt)
            return {"by": exp, "after": prev, "move": round(100 * math.sqrt(excess), 1)}
    return None


def iv_percentile(history: dict[str, float], iv: float | None) -> tuple[float | None, int]:
    """(percentile of iv among the recorded daily values, number of days recorded)."""
    vals = [v for v in history.values() if v]
    if iv is None or len(vals) < MIN_IV_DAYS:
        return None, len(vals)
    return round(100 * sum(v <= iv for v in vals) / len(vals)), len(vals)


def iv_state(iv: float | None, hv: float | None, pct: float | None) -> str:
    """cheap / fair / rich, from IV vs 20-day realized volatility and the IV percentile when known."""
    if iv is None:
        return "unknown"
    ratio = iv / hv if hv else None
    if (ratio is not None and ratio >= IV_RICH_RATIO) or (pct is not None and pct >= IV_RICH_PCT):
        return "rich"
    if ratio is not None and ratio < 0.9 and (pct is None or pct <= 40):
        return "cheap"
    return "fair"


def record_iv(store, results, today: date) -> None:
    """Keep each symbol's latest 30-day ATM IV of the day (one value per day, a year of history)."""
    hist = store.get("ivhist") or {}
    day = today.isoformat()
    for r in results:
        if r.options and r.options.atm_iv_month:
            h = hist.setdefault(r.symbol, {})
            h[day] = r.options.atm_iv_month
            if len(h) > 260:
                for k in sorted(h)[:-260]:
                    del h[k]
    store.put("ivhist", hist)


def backfill_iv(store, tz: ZoneInfo) -> int:
    """First run: seed the IV history from the recorded metrics (the last value of each day)."""
    import json
    hist: dict[str, dict[str, float]] = {}
    for ts, sym, data in store.db.execute("SELECT ts, symbol, data FROM metrics ORDER BY ts"):
        iv = ((json.loads(data).get("options") or {}).get("atm_iv_month"))
        if iv:
            day = datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).date().isoformat()
            hist.setdefault(sym, {})[day] = iv
    store.put("ivhist", hist)
    return sum(len(v) for v in hist.values())


def context(r, ivhist: dict, tz: ZoneInfo, today: date, daily: list[dict] | None) -> dict:
    """Everything the contract advice needs, stored on the SymbolResult. ivhist: the kv 'ivhist' record."""
    iv = r.options.atm_iv_month if r.options else None
    pct, n = iv_percentile(ivhist.get(r.symbol) or {}, iv)
    hv = r.stock.hv20 if r.stock else None
    return {
        "earnings": earnings_estimate(r.symbol, daily or [], tz, today),
        "iv_event": iv_event(r.options.iv_term, today) if r.options else None,
        "iv_pct": pct, "iv_days": n, "iv_state": iv_state(iv, hv, pct),
    }


def advice(idea, ctx: dict | None, today: date) -> list[str]:
    """Lines under a swing contract: single vs debit spread from IV, then earnings and event warnings."""
    if not idea or not ctx:
        return []
    out = []
    k = idea.kind[0].upper()
    spread = (f"{idea.strike:g}/{idea.short_strike:g}{k} debit {idea.spread_cost:.2f} "
              f"(≈${100 * idea.spread_cost:,.0f}, max ${100 * (idea.spread_width - idea.spread_cost):,.0f} profit)"
              if idea.short_strike is not None and idea.spread_cost and idea.spread_cost > 0 else None)
    pct = f", {ctx['iv_pct']:.0f}th percentile" if ctx.get("iv_pct") is not None else ""
    state = ctx.get("iv_state")
    alt = f" Spread: {spread}." if spread else ""
    # From the 2-year swing backtest (python -m mias pullback-backtest): entries at fair IV did best; expensive IV
    # hurt singles and spreads alike, and IV below realized volatility (the stock already swinging hard) too.
    if state == "rich":
        out.append(f"→ IV is expensive{pct}. In the 2-year backtest, entries this pricey did worse as singles and "
                   f"as spreads alike: size down or wait for IV to settle.{alt}")
    elif state == "cheap":
        out.append(f"→ IV is below realized volatility{pct}: the stock is already swinging hard, and in the "
                   f"backtest these entries did worse. Size down.{alt}")
    elif state == "fair":
        out.append(f"→ IV is fair{pct}: the best conditions in the backtest. The single option is fine.{alt}")
    exp = date.fromisoformat(idea.expiry)
    e = ctx.get("earnings")
    if e:
        nxt = date.fromisoformat(e["next"])
        q = "" if e["quality"] == "confirmed" else ", rough"
        if nxt >= today - timedelta(days=1) and nxt - timedelta(days=7) <= exp:
            soon = (nxt - today).days <= 7
            out.append(f"⚠️ Earnings ~{nxt:%b %d} (estimated ±1 week{q}) "
                       + ("is close: consider waiting until after the report." if soon else
                          "falls before this expiry: IV usually drops after the report. Size down or use the spread."))
    ev = ctx.get("iv_event")
    if ev and date.fromisoformat(ev["by"]) <= exp:
        out.append(f"⚠️ Options price an event by {date.fromisoformat(ev['by']):%b %d} "
                   f"(about ±{ev['move']:.0f}% move implied).")
    return out
