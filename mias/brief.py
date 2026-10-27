"""The 09:45 day-trade brief: the market's first 15 minutes, the day's character, the leading sector, 3-5 day-trade
candidates and key levels. Also /brief anytime.

Candidates (watchlist stocks, not funds or indexes) need liquid short-dated options and a real opening move
(gap or first-15-minute move of 0.5%+). They are ranked by first-15-minute volume against the same 15 minutes of
the last 20 sessions (relative volume) times the size of the move. The contract shown is the day-trade pick
(1-14 days, ~0.45 delta, bid/ask spread <= 5% of mid).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from .events import FUNDS
from .indicators import Bar, atr, session_vwap
from .trend import direction, label_for, regular_session, to_bars

KEY_LEVEL_SYMBOLS = ("SPY", "QQQ")
OPEN_MINUTES = 15


def _et(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz)


def read(symbol: str, five_raw: list[dict], daily_raw: list[dict], today: date, tz: ZoneInfo) -> dict | None:
    """Opening read of one symbol from 5-minute bars (prior sessions + today) and daily bars (before today)."""
    bars = regular_session(to_bars(five_raw), tz)
    by_day: dict[date, list[Bar]] = {}
    for b in bars:
        by_day.setdefault(_et(b.t, tz).date(), []).append(b)
    session = by_day.get(today) or []
    past = [d for d in sorted(by_day) if d < today]
    if not session or not past:
        return None
    prev = by_day[past[-1]]
    first = [b for b in session if b.t < session[0].t + OPEN_MINUTES * 60_000]
    open_vols = [sum(b.v for b in by_day[d] if b.t < by_day[d][0].t + OPEN_MINUTES * 60_000) for d in past[-20:]]
    avg_open = sum(open_vols) / len(open_vols) if open_vols else 0
    pdh, pdl, pdc = max(b.h for b in prev), min(b.l for b in prev), prev[-1].c
    o, last = session[0].o, session[-1].c
    daily = to_bars(daily_raw)
    a = atr(daily[-30:]) if len(daily) >= 16 else None
    return {
        "symbol": symbol, "price": last, "gap": 100 * (o / pdc - 1), "move15": 100 * (first[-1].c / o - 1),
        "rvol": sum(b.v for b in first) / avg_open if avg_open else None,
        "orb_high": max(b.h for b in first), "orb_low": min(b.l for b in first),
        "pdh": pdh, "pdl": pdl, "pdc": pdc, "vwap": session_vwap(session),
        "atr_pct": 100 * a / pdc if a and pdc else None,
    }


def _fmt_levels(x: dict, r=None) -> str:
    parts = [f"open range {x['orb_low']:,.2f}–{x['orb_high']:,.2f}", f"VWAP {x['vwap']:,.2f}",
             f"prior day H {x['pdh']:,.2f} / L {x['pdl']:,.2f} / C {x['pdc']:,.2f}"]
    if r is not None and r.options and r.options.call_wall:
        parts.append(f"walls {r.options.put_wall:g}P / {r.options.call_wall:g}C")
    return " · ".join(parts)


def candidates(reads: dict[str, dict], results, cfg, top: int = 5) -> list[tuple[dict, object]]:
    by_sym = {r.symbol: r for r in results}
    out = []
    for sym, x in reads.items():
        r = by_sym.get(sym)
        if sym in FUNDS or sym in cfg.instruments or not r or not r.options or r.options.thin:
            continue
        if x["rvol"] is None or max(abs(x["gap"]), abs(x["move15"])) < 0.5:
            continue
        out.append((x["rvol"] * (abs(x["gap"]) + abs(x["move15"])), x, r))
    out.sort(key=lambda t: -t[0])
    return [(x, r) for _, x, r in out[:top]]


def text(engine, cfg, now: datetime) -> str:
    """Build the brief from the engine's latest cycle and bar caches."""
    from . import sectors
    from .emacross import regime
    from .formatting import ICON, label
    tz = cfg.tz
    local = now.astimezone(tz)
    today = local.date()
    results = engine.last_results
    if not results:
        return "No market data yet: the brief needs a completed check (market hours)."
    reads = {}
    for r in results:
        hist = (engine._hist5.get(r.symbol) or (None, []))[1]
        five = hist + (engine.today5.get(r.symbol) or [])
        daily = (engine._daily.get(r.symbol) or (None, []))[1]
        try:
            x = read(r.symbol, five, daily, today, tz)
        except Exception:
            x = None
        if x:
            reads[r.symbol] = x
    lines = [f"☀️ <b>Day-trade brief</b>  {local:%a %b %d %H:%M} ET"]
    if engine.last_market:
        lbl, s, o = engine.last_market
        lines.append(f"Market: {ICON[direction(lbl)]} <b>{label(lbl)}</b> (stock {s:+.0f}"
                     + (f", options {o:+.0f}" if o is not None else "") + ")")
    for sym in ("SPY", "QQQ", "IWM"):
        x = reads.get(sym)
        if x:
            side = "above" if x["price"] >= x["vwap"] else "below"
            lines.append(f"{sym} {x['price']:,.2f} · gap {x['gap']:+.2f}% · first 15m {x['move15']:+.2f}% · "
                         f"{side} VWAP" + (f" · volume {x['rvol']:.1f}× normal" if x["rvol"] else ""))
    spy = engine.five.get("SPY") if hasattr(engine, "five") else None
    if spy:
        idx = [i for i, b in enumerate(spy) if _et(b.t, tz).date() == today]
        if idx:
            kind = regime(spy, idx[-1], idx[0])
            lines.append("Day so far: <b>trending</b>: breakouts and pullbacks with the trend tend to work"
                         if kind == "trend" else
                         "Day so far: <b>choppy</b>: breakouts tend to fail; smaller size or wait for a range break")
    board = sectors.board_lines(results, cfg.sectors, cfg) if cfg.sectors else []
    focus = next((l for l in board if l.startswith("Focus")), None)
    if focus:
        lines.append(focus)

    picks = candidates(reads, results, cfg)
    lines += ["", "<b>Day-trade candidates</b> (relative volume × opening move)"]
    if not picks:
        lines.append("None: no liquid stock is moving on above-normal volume yet.")
    for x, r in picks:
        intra = r.stock.composites.get("intraday", (None, ""))[0] if r.stock else None
        lean = "bull" if x["move15"] + x["gap"] > 0 else "bear"
        agrees = intra is not None and (intra > 0) == (lean == "bull")
        icon = "🟢" if lean == "bull" else "🔴"
        lines.append(f"{icon} <b>{escape(x['symbol'])}</b> {x['price']:,.2f} · gap {x['gap']:+.1f}% · "
                     f"15m {x['move15']:+.1f}% · volume {x['rvol']:.1f}× · "
                     + (f"ATR {x['atr_pct']:.1f}%/day · " if x["atr_pct"] else "")
                     + ("intraday " + label(label_for(intra, cfg.trend_strong, cfg.trend_bias)) + f" ({intra:+.0f})"
                        + ("" if agrees else " ⚠️ against the move") if intra is not None else ""))
        lines.append(f"   {_fmt_levels(x)}")
        kind = "call" if lean == "bull" else "put"
        idea = (r.options.ideas.get("day") or {}).get(kind) if r.options else None
        if idea:
            lines.append(f"   day {kind}: {idea.expiry[5:].replace('-', '/')} {idea.strike:g}{kind[0].upper()} "
                         f"mid {idea.mid:.2f} (≈${100 * idea.mid:,.0f}) · spread {idea.spread_pct:.1f}% · "
                         f"OI {idea.oi:,.0f}")
        else:
            lines.append(f"   no liquid day-trade {kind} (spread ≤ 5%, OI ≥ 200)")
    by_sym = {r.symbol: r for r in results}
    levels = [(s, reads[s]) for s in KEY_LEVEL_SYMBOLS if s in reads]
    if levels:
        lines += ["", "<b>Key levels</b>"]
        for s, x in levels:
            lines.append(f"{s}: {_fmt_levels(x, by_sym.get(s))}")
    lines.append("<i>Gaps that hold the opening range tend to run; a break back inside it is the warning.</i>")
    return "\n".join(lines)
