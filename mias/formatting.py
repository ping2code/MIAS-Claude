"""Telegram message text (HTML parse mode)."""
from __future__ import annotations

from datetime import datetime
from html import escape

from .models import SymbolResult
from .trend import direction

LABELS = {
    "strong_up": "STRONG UP", "up": "UP", "neutral": "NEUTRAL", "down": "DOWN", "strong_down": "STRONG DOWN",
    "strong_bullish": "STRONG BULLISH", "bullish": "BULLISH", "bearish": "BEARISH",
    "strong_bearish": "STRONG BEARISH",
}
ICON = {"bull": "🟢", "bear": "🔴", "neutral": "⚪"}


def money(x: float | None) -> str:
    if x is None:
        return "–"
    sign = "-" if x < 0 else "+" if x > 0 else ""
    a = abs(x)
    if a >= 1e9:
        return f"{sign}${a / 1e9:.1f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:.0f}K"
    return f"{sign}${a:.0f}"


def num(x: float | None, fmt: str = ".2f", signed: bool = False) -> str:
    if x is None:
        return "–"
    return format(x, ("+" if signed else "") + fmt)


def label(lbl: str | None) -> str:
    return LABELS.get(lbl or "", (lbl or "–").upper())


def label_for_score(score: float, cfg) -> str:
    from .trend import label_for
    return label(label_for(score, cfg.trend_strong, cfg.trend_bias))


def price_line(r: SymbolResult) -> str:
    tag = "live" if r.price_is_live else "delayed"
    chg = f" ({num(r.day_change_pct, '.2f', True)}%)" if r.day_change_pct is not None else ""
    return f"Price {num(r.price)} {tag}{chg}"


TF_ORDER = ["1w", "1d", "1h", "15m", "5m"]


def profile_line(r: SymbolResult, name: str, timeframes: list[str]) -> str:
    score, lbl = r.stock.composites.get(name, (r.stock.score, r.stock.label))
    tfs = " · ".join(f"{tf} {num(r.stock.timeframes[tf].score, '.0f', True)}"
                     for tf in TF_ORDER if tf in timeframes and tf in r.stock.timeframes)
    return f"{name.capitalize()}: <b>{label(lbl)}</b> ({num(score, '.0f', True)})  {tfs}"


def stock_lines(r: SymbolResult, profiles: dict[str, dict[str, float]], primary: str) -> list[str]:
    if not r.stock:
        return ["Trend: unavailable"]
    names = [primary] + [n for n in profiles if n != primary]
    return [profile_line(r, n, list(profiles[n])) for n in names]


def options_line(r: SymbolResult) -> str:
    if not r.bias or not r.options:
        return "Options: unavailable"
    o = r.options
    if o.thin:
        return (f"Options: thin market ({o.call_volume + o.put_volume:,.0f} contracts today). "
                f"Flow not meaningful")
    parts = [f"P/C {num(o.pcr_volume)}", f"IV {num(o.atm_iv_front, '.1f')}%"]
    if "session_flow" in r.bias.components:
        parts.insert(0, f"net prem {money(r.bias.session_net_premium)}")
    if o.rr25 is not None:
        parts.append(f"RR25 {num(o.rr25, '.1f', True)}")
    return f"Options: <b>{label(r.bias.label)}</b> ({num(r.bias.score, '.0f', True)})  " + " · ".join(parts)


def levels_line(r: SymbolResult) -> str | None:
    o = r.options
    if not o:
        return None
    gamma = "dealers long γ (pinning)" if o.gex > 0 else "dealers short γ (moves can extend)"
    return f"Levels: call wall {num(o.call_wall, 'g')} · put wall {num(o.put_wall, 'g')} · {gamma}"


def symbol_alert(r: SymbolResult, reasons: list[tuple[str, str]], now: datetime, cfg) -> str:
    """reasons: list of (direction class, text)."""
    from .verdict import verdict
    head_dir = reasons[0][0]
    lines = [f"{ICON.get(head_dir, '🔔')} <b>{escape(r.symbol)}</b>  {now:%H:%M} ET"]
    lines += [f"• {escape(text)}" for _, text in reasons]
    lines += ["", price_line(r), *stock_lines(r, cfg.trend_profiles, cfg.trend_primary), options_line(r),
              escape(verdict(r, list(cfg.trend_profiles[cfg.trend_primary])).line())]
    lv = levels_line(r)
    if lv:
        lines.append(lv)
    if head_dir in ("bull", "bear") and any(text.startswith("STRONG") for _, text in reasons):
        for line in (idea_line(r, "swing", head_dir, cfg, "Swing"), idea_line(r, "day", head_dir, cfg, "Day trade")):
            if line:
                lines.append(line)
    if r.source_note:
        lines.append(f"<i>{escape(r.source_note)}</i>")
    return "\n".join(lines)


def iv_line(r: SymbolResult) -> str | None:
    o, s = r.options, r.stock
    if not o or o.atm_iv_month is None:
        return None
    if not s or not s.hv20:
        return f"IV {o.atm_iv_month:.0f}% (30-day ATM)"
    ratio = o.atm_iv_month / s.hv20
    word = "cheap" if ratio < 0.9 else "fair" if ratio <= 1.25 else "expensive"
    return (f"IV {o.atm_iv_month:.1f}% vs 20d realized {s.hv20:.1f}% ({ratio:.2f}×) → options <b>{word}</b>")


def idea_line(r: SymbolResult, name: str, direction_: str, cfg, title: str) -> str | None:
    if not r.options:
        return None
    t = cfg.idea_templates[name]
    if direction_ == "neutral":
        return f"{title}: no direction, so no contract"
    kind = "call" if direction_ == "bull" else "put"
    i = r.options.ideas.get(name, {}).get(kind)
    if not i:
        return f"{title}: no liquid {kind} near Δ{t.delta:.2f} in {t.min_dte}–{t.max_dte} DTE"
    return (f"{title} {kind}: {i.expiry} {i.strike:g}{kind[0].upper()} ({i.dte}d) Δ{abs(i.delta):.2f} "
            f"mid {i.mid:.2f} (≈${100 * i.mid:,.0f}) · spread {i.spread_pct:.1f}% · OI {i.oi:,.0f} · vol {i.volume:,.0f}")


def day_section(r: SymbolResult, cfg) -> list[str]:
    from .verdict import verdict
    if not r.stock or "intraday" not in cfg.trend_profiles or "day" not in cfg.idea_templates:
        return []
    v = verdict(r, ["5m", "15m", "1h"], profile="intraday")
    head = f"Day trade: {v.icon} {v.level}, {escape(v.reason)}"
    if v.level not in ("HIGH", "MEDIUM"):
        return [head + ". No contract pick"]
    line = idea_line(r, "day", v.direction, cfg, "Day trade")
    return [head] + ([line] if line else [])


def trend_reply(r: SymbolResult, cfg, now: datetime, watched: bool, market_open: bool) -> str:
    from .verdict import verdict
    if not r.ok:
        why = "; ".join(r.errors or []) or "no data"
        return f"❓ <b>{escape(r.symbol)}</b>: no data ({escape(why[:200])}). Check the ticker."
    v = verdict(r, list(cfg.trend_profiles[cfg.trend_primary]))
    lines = [f"{ICON[v.direction]} <b>{escape(r.symbol)}</b>  {now:%a %H:%M} ET", price_line(r),
             *stock_lines(r, cfg.trend_profiles, cfg.trend_primary), options_line(r),
             f"<b>{escape(v.line())}</b>", ""]
    for extra in (iv_line(r), levels_line(r), idea_line(r, "swing", v.direction, cfg, "Swing")):
        if extra:
            lines.append(extra)
    day = day_section(r, cfg)
    if day:
        lines += ["", *day]
    notes = [r.source_note] if r.source_note else []
    if not market_open:
        notes.append("market closed: last session data")
    if not watched:
        notes.append("not on the watchlist: options flow history is not tracked")
    if cfg.stock_delay_minutes:
        notes.append(f"stock bars {cfg.stock_delay_minutes}m delayed, so the intraday trend lags")
    if r.errors:
        notes.append("partial: " + "; ".join(r.errors)[:150])
    lines.append("<i>" + escape(" · ".join(notes)) + "</i>" if notes else "")
    return "\n".join(l for l in lines if l is not None).rstrip()


def scheduled_report(r: SymbolResult, cfg, now: datetime, minutes: int, prev_price: float | None) -> str:
    """Periodic full read of one symbol (e.g. NDX every 10 minutes)."""
    head = f"⏱ <b>{escape(r.symbol)}</b> {minutes}-min report"
    if prev_price and r.price:
        d = r.price - prev_price
        head += f" · {d:+,.2f} ({100 * d / prev_price:+.2f}%) since last"
    return head + "\n" + trend_reply(r, cfg, now, watched=True, market_open=True)


def market_alert(old: str, new: str, stock: float, opts: float | None, symbols: list[str], now: datetime) -> str:
    return (f"{ICON[direction(new)]} <b>MARKET</b> trend {label(old)} → <b>{label(new)}</b>  {now:%H:%M} ET\n"
            f"{'/'.join(symbols)} stock {num(stock, '.0f', True)} · options {num(opts, '.0f', True)}")


def digest(results: list[SymbolResult], market: tuple[str, float, float | None] | None, title: str,
           now: datetime, stock_delay_minutes: int) -> str:
    lines = [f"📊 <b>{escape(title)}</b>  {now:%a %H:%M} ET"]
    if market:
        lbl, s, o = market
        lines.append(f"Market: {ICON[direction(lbl)]} <b>{label(lbl)}</b> (stock {num(s, '.0f', True)}, "
                     f"options {num(o, '.0f', True)})")
    bull = sum(1 for r in results if r.stock and direction(r.stock.label) == "bull")
    bear = sum(1 for r in results if r.stock and direction(r.stock.label) == "bear")
    lines.append(f"Breadth: {bull} up · {bear} down · {len(results) - bull - bear} neutral")
    rows = ["SYM     PRICE  DAY%  SWNG INTR OPTS  IV"]
    for r in sorted(results, key=lambda x: -(x.stock.score if x.stock else -999)):
        comp = r.stock.composites if r.stock else {}
        opts = None if (not r.bias or (r.options and r.options.thin)) else r.bias.score
        rows.append(
            f"{r.symbol:<5}{num(r.price, '.2f'):>9}{num(r.day_change_pct, '.1f', True):>6}"
            f"{num(comp.get('swing', (None,))[0], '.0f', True):>6}"
            f"{num(comp.get('intraday', (None,))[0], '.0f', True):>5}"
            f"{num(opts, '.0f', True):>5}"
            f"{num(r.options.atm_iv_month if r.options else None, '.0f'):>4}")
    lines.append("<pre>" + escape("\n".join(rows)) + "</pre>")

    unusual = [(r.symbol, p) for r in results if r.options for p in r.options.unusual[:2]]
    unusual.sort(key=lambda sp: -sp[1].premium)
    if unusual:
        lines.append("<b>Unusual (vol &gt; OI)</b>")
        for sym, p in unusual[:8]:
            lines.append(escape(f"{sym} {p.expiry} {p.strike:g}{p.kind[0].upper()} vol {p.volume:,.0f} "
                                f"vs OI {p.oi:,.0f} · {money(p.premium).lstrip('+')} · last at {p.side}"))
    errors = [r.symbol for r in results if r.errors]
    if errors:
        lines.append(f"⚠️ partial data: {', '.join(errors)}")
    if stock_delay_minutes:
        lines.append(f"<i>Stock bars {stock_delay_minutes}m delayed; prices marked live come from options quotes.</i>")
    return "\n".join(lines)


def _spread_block(s, now: datetime) -> list[str]:
    from .picks import HARD_EXIT_DAYS, TAKE_PROFIT, TIME_STOP_DAYS, add_trading_days
    from datetime import timedelta
    k = "C" if s.side == "call" else "P"
    move = 100 * (s.breakeven / s.price - 1)
    full = 100 * (s.short_strike / s.price - 1)
    target = s.cost + TAKE_PROFIT * s.max_profit
    sign = 1 if s.side == "call" else -1          # Spread stores scores signed toward the trade
    if s.options is None:
        opt = "options –"
    else:
        note = "with the trade" if s.options >= 25 else "mildly against" if s.options <= -10 else "neutral"
        opt = f"options {sign * s.options:+.0f} ({note})"
    return [
        f"<b>{escape(s.symbol)}</b> ${s.price:,.2f} · tier {s.tier} · confidence {s.confidence}",
        f"Swing {sign * s.swing:+.0f} · intraday {sign * s.intraday:+.0f} · {opt}",
        f"Buy {s.expiry:%m/%d} {s.long_strike:g}{k} / Sell {s.expiry:%m/%d} {s.short_strike:g}{k} ({s.dte}d)",
        f"Limit <b>{s.cost:.2f}</b> debit (≈${100 * s.cost:,.0f}), natural {s.natural[0]:.2f}–{s.natural[1]:.2f}",
        f"Max profit ${100 * s.max_profit:,.0f} ({s.max_profit / s.cost:.1f}×) · breakeven {s.breakeven:,.2f} "
        f"({move:+.1f}%) · full at {s.short_strike:g} ({full:+.1f}%)",
        f"OI {s.long_oi:,.0f} / {s.short_oi:,.0f}",
        f"Exit: take profit at ≈{target:.2f} · time stop {add_trading_days(now.date(), TIME_STOP_DAYS):%a %b %d} "
        f"· hard exit {s.expiry - timedelta(days=HARD_EXIT_DAYS):%a %b %d}",
    ]


def picks_message(p: dict, now: datetime) -> str:
    lines = [f"🎯 <b>Daily spread picks</b>  {now:%a %b %d %H:%M} ET"]
    for side, icon, title, word in (("call", "🟢", "CALL SPREAD (bullish)", "uptrend"),
                                    ("put", "🔴", "PUT SPREAD (bearish)", "downtrend")):
        lines += ["", f"{icon} <b>{title}</b>"]
        s = p.get(side)
        if s:
            lines += _spread_block(s, now)
        else:
            near = p.get(f"{side}_near")
            hint = ""
            if near and near.stock:
                sc = near.stock.score
                hint = f" Closest: {escape(near.symbol)} (swing {sc:+.0f})."
            lines.append(f"No trade today: nothing qualifies (needs a confirmed {word}, options not against it, "
                         f"liquid strikes).{hint}")
    lines += ["", "<i>Up to one of each per day. Skip if you're at your open-position limit.</i>"]
    return "\n".join(lines)


def _flow_side(p) -> str:
    if p.side == "mid":
        return "neutral"
    return "bull" if (p.side == "buy") == (p.kind == "call") else "bear"


def unusual_report(results: list[SymbolResult], now: datetime, prev: dict[str, float] | None, top: int,
                   per_symbol: int = 3) -> tuple[str | None, dict[str, float]]:
    """Unusual contracts across symbols, ranked by premium. Returns (text or None if empty, volumes for next time).

    prev holds each contract's volume at the previous report; contracts not in it are marked new."""
    rows = [(r.symbol, p) for r in results if r.options for p in r.options.unusual[:per_symbol]]
    rows.sort(key=lambda sp: -sp[1].premium)
    rows = rows[:top]
    volumes = {p.ticker: p.volume for _, p in rows}
    if not rows:
        return None, volumes
    lines = [f"🔥 <b>Unusual options activity</b>  {now:%H:%M} ET",
             "<i>volume &gt; open interest · ≥ $500K premium · ranked by premium today</i>"]
    new = grew = 0
    bull = bear = 0.0
    for sym, p in rows:
        side = _flow_side(p)
        bull += p.premium if side == "bull" else 0
        bear += p.premium if side == "bear" else 0
        tag = ""
        if prev is not None:
            if p.ticker not in prev:
                tag, new = "🆕 ", new + 1
            elif p.volume > prev[p.ticker]:
                tag, grew = "⬆️ ", grew + 1
        added = ""
        if prev is not None and p.ticker in prev and p.volume > prev[p.ticker]:
            added = f" · +{p.volume - prev[p.ticker]:,.0f} since last"
        icon = {"bull": "🟢", "bear": "🔴", "neutral": "⚪"}[side]
        lines.append(escape(f"{tag}{icon} {sym} {p.expiry[5:].replace('-', '/')} {p.strike:g}{p.kind[0].upper()} · "
                            f"vol {p.volume:,.0f} vs OI {p.oi:,.0f} · {money(p.premium).lstrip('+')} · last at {p.side}{added}"))
    summary = f"Lean (by last trade): bullish {money(bull).lstrip('+')} · bearish {money(bear).lstrip('+')}"
    if prev is not None:
        summary += f" · {new} new, {grew} growing"
    lines += ["", escape(summary)]
    return "\n".join(lines), volumes
