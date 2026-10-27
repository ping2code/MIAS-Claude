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


def symbol_alert(r: SymbolResult, reasons: list[tuple[str, str]], now: datetime, cfg,
                 context: list[str] | None = None) -> str:
    """reasons: list of (direction class, text). context: market/sector lines shown under the reasons."""
    from .verdict import verdict
    head_dir = reasons[0][0]
    lines = [f"{ICON.get(head_dir, '🔔')} <b>{escape(r.symbol)}</b>  {now:%H:%M} ET"]
    lines += [f"• {escape(text)}" for _, text in reasons]
    lines += [escape(c) for c in context or []]
    lines += ["", price_line(r), *stock_lines(r, cfg.trend_profiles, cfg.trend_primary), options_line(r),
              escape(verdict(r, list(cfg.trend_profiles[cfg.trend_primary])).line())]
    lv = levels_line(r)
    if lv:
        lines.append(lv)
    if head_dir in ("bull", "bear") and any(text.startswith(("STRONG", "PULLBACK", "BOUNCE")) for _, text in reasons):
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
    state = (r.events or {}).get("iv_state")
    word = {"cheap": "cheap", "fair": "fair", "rich": "expensive"}.get(state) or \
        ("cheap" if ratio < 0.9 else "fair" if ratio <= 1.25 else "expensive")
    pct = (r.events or {}).get("iv_pct")
    rank = f" · {pct:.0f}th percentile of its last {r.events['iv_days']} days" if pct is not None else ""
    return (f"IV {o.atm_iv_month:.1f}% vs 20d realized {s.hv20:.1f}% ({ratio:.2f}×){rank} → options <b>{word}</b>")


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
    line = (f"{title} {kind}: {i.expiry} {i.strike:g}{kind[0].upper()} ({i.dte}d) Δ{abs(i.delta):.2f} "
            f"mid {i.mid:.2f} (≈${100 * i.mid:,.0f}) · spread {i.spread_pct:.1f}% · OI {i.oi:,.0f} · vol {i.volume:,.0f}")
    if name == "swing" and r.events:
        from datetime import date as _date
        from .events import advice
        line = "\n".join([line, *(escape(a) for a in advice(i, r.events, _date.today()))])
    return line


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
           now: datetime, stock_delay_minutes: int, sector_lines: list[str] | None = None) -> str:
    lines = [f"📊 <b>{escape(title)}</b>  {now:%a %H:%M} ET"]
    if market:
        lbl, s, o = market
        lines.append(f"Market: {ICON[direction(lbl)]} <b>{label(lbl)}</b> (stock {num(s, '.0f', True)}, "
                     f"options {num(o, '.0f', True)})")
    if sector_lines:
        lines += ["<b>Sectors</b>", *sector_lines]
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
        *(escape(w) for w in getattr(s, "warnings", ())),
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


def _new_volume(p, prev: dict[str, float] | None) -> float:
    return p.volume - prev.get(p.ticker, 0.0) if prev is not None else p.volume


def _new_premium(p, prev: dict[str, float] | None, tapes: dict | None) -> float:
    t = (tapes or {}).get(p.ticker)
    if t is not None and t.total > 0:
        return t.total
    return max(0.0, _new_volume(p, prev)) * (p.premium / p.volume if p.volume else 0.0)


def _mix(t) -> str:
    parts = [f"buy {t.pct(t.buy)}%", f"sell {t.pct(t.sell)}%"]
    if t.pct(t.mid):
        parts.append(f"mid {t.pct(t.mid)}%")
    if t.pct(t.multi):
        parts.append(f"multi-leg {t.pct(t.multi)}%")
    if t.sweeps:
        parts.append(f"{t.sweeps} sweep{'s' if t.sweeps != 1 else ''}")
    return " · ".join(parts)


def unusual_report(results: list[SymbolResult], now: datetime, prev: dict[str, float] | None, top: int,
                   per_symbol: int = 3, blocks: list[dict] | None = None, tapes: dict | None = None,
                   since: datetime | None = None, oi_lines: list[str] | None = None, *, material: float = 0.0,
                   row_min: float = 0.0, quiet: str = "line", structures: list[dict] | None = None,
                   confirm: dict | None = None, market: list[str] | None = None,
                   info: dict | None = None) -> tuple[str | None, dict[str, float]]:
    """Unusual contracts (volume above open interest) that traded since the previous report, ranked by the premium
    traded in that window. Returns (text or None when nothing to send, every flagged contract's volume for next time).

    prev: each contract's volume at the previous report (None = first report of the day, so "new" = since the open).
    tapes: trade-by-trade classification per contract for the window (mias/flow.py); without one, a contract is
    signed by where its last trade printed. blocks: large prints since the previous report. oi_lines: the morning
    check of yesterday's flow against today's open interest. structures: contracts legged together (flow.structures),
    shown as one structure and left out of the bullish/bearish totals.

    Only material activity makes a full report: a contract with >= `material` new premium (not part of a structure),
    a one-sided block, a structure or the open-interest check. Contracts under `row_min` new premium are counted,
    not listed. Otherwise one line (quiet="line") or nothing (quiet="skip").
    confirm: symbol -> price read (mias/confirm.py) to tag each line with the stock's side of VWAP and relative
    volume; market: the market symbols whose reads give the market line. info, if given, is filled with counts."""
    from .confirm import market_lean, market_line, verdict
    rows = [(r.symbol, p) for r in results if r.options for p in r.options.unusual[:per_symbol]]
    volumes = {p.ticker: p.volume for _, p in rows}
    struct_of = {tk: c for c in structures or [] for tk in c["tickers"]}
    active = [(s, p) for s, p in rows if _new_volume(p, prev) > 0]
    active.sort(key=lambda sp: -_new_premium(sp[1], prev, tapes))
    listed = [sp for sp in active if _new_premium(sp[1], prev, tapes) >= row_min]
    minor = len(active) - len(listed)
    quiet_n = len(rows) - len(active)
    shown = listed[:top]
    extra = block_lines(blocks, tapes) if blocks else []
    big = [sp for sp in shown if _new_premium(sp[1], prev, tapes) >= material and sp[1].ticker not in struct_of]
    is_material = bool(big or extra or oi_lines or structures)
    if info is not None:
        info.update(active=len(active), listed=len(shown), minor=minor, quiet=quiet_n, material=is_material,
                    big=len(big), structures=len(structures or []), blocks=len(extra) > 0)
    window = f"since {since:%H:%M}" if since else "since the open"
    if not is_material:
        if quiet == "skip" or not active:
            return None, volumes
        sym, p = active[0]
        return (escape(f"🔥 {now:%H:%M} ET · no material new flow {window}. Largest: {sym} "
                       f"{p.expiry[5:].replace('-', '/')} {p.strike:g}{p.kind[0].upper()} new "
                       f"{money(_new_premium(p, prev, tapes)).lstrip('+')} (+{_new_volume(p, prev):,.0f}). "
                       f"{len(active)} contracts traded, none ≥ {money(material).lstrip('+')}.")), volumes
    reads = confirm or {}
    mkt = market_lean(reads, market or []) if confirm else 0
    lines = [f"🔥 <b>Unusual options activity</b>  {now:%H:%M} ET"]
    if oi_lines:
        lines += [*oi_lines, ""]
    if shown:
        lines.append(f"<i>volume &gt; open interest today: possibly new positions (tomorrow's open interest "
                     f"confirms) · ranked by premium {window}</i>")
        ml = market_line(reads, market or []) if confirm else None
        if ml:
            lines.append(escape(ml))
    new = grew = agree = against = 0
    bull = bear = mixed = multi = struct = 0.0
    by_tape = False
    for sym, p in shown:
        t = (tapes or {}).get(p.ticker)
        prem = _new_premium(p, prev, tapes)
        if p.ticker in struct_of:
            side = "neutral"
            struct += prem
            how = "🧩 part of a structure"
            by_tape = by_tape or t is not None
        elif t is not None and t.total > 0:
            by_tape = True
            side = {"bull": "bull", "bear": "bear"}.get(t.lean, "neutral")
            bull += t.bull
            bear += t.bear
            mixed += t.mid
            multi += t.multi
            how = _mix(t)
        else:
            side = _flow_side(p)
            bull += prem if side == "bull" else 0
            bear += prem if side == "bear" else 0
            mixed += prem if side == "neutral" else 0
            how = f"last at {p.side}"
        tag = ""
        if prev is not None:
            if p.ticker not in prev:
                tag, new = "🆕 ", new + 1
            else:
                tag, grew = "⬆️ ", grew + 1
        ok, check = verdict(side, reads.get(sym), mkt) if confirm else (None, "")
        agree += ok is True
        against += ok is False
        icon = {"bull": "🟢", "bear": "🔴", "neutral": "⚪"}[side]
        lines.append(escape(f"{tag}{icon} {sym} {p.expiry[5:].replace('-', '/')} {p.strike:g}{p.kind[0].upper()} · "
                            f"new {money(prem).lstrip('+')} (+{_new_volume(p, prev):,.0f}) · {how} · "
                            f"day {money(p.premium).lstrip('+')}, vol {p.volume:,.0f} vs OI {p.oi:,.0f}"
                            + (f" · {check}" if check else "")))
    if shown:
        how = "trade by trade" if by_tape else "by last trade"
        summary = (f"New flow ({how}): bullish {money(bull).lstrip('+')} · bearish {money(bear).lstrip('+')} · "
                   f"no side {money(mixed).lstrip('+')}" + (f" · multi-leg {money(multi).lstrip('+')}" if multi else "")
                   + (f" · structures {money(struct).lstrip('+')}" if struct else ""))
        if prev is not None:
            summary += f" · {new} new, {grew} growing"
        if minor:
            summary += f" · {minor} minor"
        if quiet_n:
            summary += f" · {quiet_n} quiet"
        if agree or against:
            summary += f" · price confirms {agree}, disagrees {against}"
        lines += ["", escape(summary)]
    if structures:
        lines += ["", "<b>Structures</b> (legs traded together: no direction counted)"]
        for c in structures[:4]:
            legs = "/".join(f"{k:g}{kind[0].upper()}" for kind, k in c["legs"])
            lines.append(escape(f"🧩 {c['symbol']} {c['expiry'][5:].replace('-', '/')} {legs} · "
                                f"{money(c['premium']).lstrip('+')} · {c['matched']:,.0f} contracts matched"))
    if extra:
        lines += ["", *extra]
    return "\n".join(lines), volumes


def oi_lines(checks: list[dict], top: int = 8) -> list[str]:
    """Yesterday's flagged contracts against today's open interest (mias/flow.oi_check)."""
    if not checks:
        return []
    icon = {"opened": "✅", "partly": "↔️", "closed": "✖️"}
    word = {"opened": "opened", "partly": "partly opened", "closed": "closing/turnover"}
    lines = ["<b>Yesterday's flow, checked against today's open interest</b>"]
    for c in checks[:top]:
        delta = c["oi_after"] - c["oi"]
        lean = {"bull": "bullish", "bear": "bearish"}.get(c.get("lean"), "mixed")
        lines.append(escape(f"{icon[c['verdict']]} {c['symbol']} {c['expiry'][5:].replace('-', '/')} "
                            f"{c['strike']:g}{c['kind'][0].upper()} · vol {c['volume']:,.0f} → OI {delta:+,.0f} "
                            f"({100 * c['share']:.0f}%) · {word[c['verdict']]} · was {lean}"))
    n = {v: sum(c["verdict"] == v for c in checks) for v in icon}
    stuck = {d: sum(c["premium"] for c in checks if c["verdict"] == "opened" and c.get("lean") == d)
             for d in ("bull", "bear")}
    lines.append(escape(f"Opened {n['opened']} · partly {n['partly']} · closing/turnover {n['closed']}. "
                        f"Opened and one-sided: bullish {money(stuck['bull']).lstrip('+')} · "
                        f"bearish {money(stuck['bear']).lstrip('+')}"))
    return lines


CHANGE_GROUPS = (("Trend", ("trend_change", "market")), ("Options", ("options_flip",)),
                 ("Levels", ("wall_break",)), ("IV", ("iv_spike",)), ("Alignment", ("alignment_ended",)))


def changes_message(events: list[dict], start: datetime, end: datetime,
                    sector_line: str | None = None) -> str | None:
    """Tier 2: everything that changed in the window, grouped. Only the latest change per symbol and rule is
    kept (a trend that went UP then back NEUTRAL inside the window shows its final state). None if empty."""
    latest: dict[tuple[str, str], dict] = {}
    for e in events:
        latest[(e["symbol"], e["rule"])] = e
    if not latest:
        return None
    lines = [f"🔄 <b>Changes {start:%H:%M}–{end:%H:%M} ET</b>"]
    for title, rules in CHANGE_GROUPS:
        rows = [e for e in latest.values() if e["rule"] in rules]
        if not rows:
            continue
        lines.append(f"<b>{title}</b>")
        rows.sort(key=lambda e: (e["rule"] != "market", e["symbol"]))
        for e in rows:
            icon = "⚡" if e["rule"] == "iv_spike" else ICON.get(e["dir"], "⚪")
            lines.append(f"{icon} <b>{escape(e['symbol'])}</b> {escape(e['text'])}")
    if sector_line:
        lines.append(sector_line)
    return "\n".join(lines)


def block_lines(items: list[dict], tapes: dict | None = None, top: int = 6) -> list[str]:
    """Blocks since the last report: one-sided ones listed, multi-leg and matched pairs (spreads, combos, rolls)
    skipped. With a tape (mias/flow.py) a block is judged by its classified prints; without one, by its last
    trade and by looking for a same-size block on another strike or expiry."""
    merged: dict[str, dict] = {}
    for b in items:
        m = merged.setdefault(b["ticker"], dict(b, premium=0.0, volume=0.0))
        m["premium"] += b["premium"]
        m["volume"] += b["volume"]
        m["side"] = b["side"]
    blocks = list(merged.values())
    paired: set[str] = set()
    for b in blocks:
        t = (tapes or {}).get(b["ticker"])
        if t is None or t.total <= 0:
            continue
        b["taped"] = True
        if t.multi >= 0.5 * t.total:
            paired.add(b["ticker"])
        elif t.lean in ("bull", "bear"):
            b["side"] = "buy" if (t.lean == "bull") == (b["kind"] == "call") else "sell"
        else:
            b["side"] = "mid"
    for a in blocks:
        if a.get("taped") or a["ticker"] in paired:
            continue
        for b in blocks:
            if a is b or b.get("taped") or a["symbol"] != b["symbol"] or a["expiry"] != b["expiry"] and a["kind"] != b["kind"]:
                continue
            if max(a["volume"], b["volume"]) <= 1.25 * min(a["volume"], b["volume"]):
                paired |= {a["ticker"], b["ticker"]}      # same size on two strikes/expiries or call + put
    single = [b for b in blocks if b["ticker"] not in paired and b["side"] != "mid"]
    single.sort(key=lambda b: -b["premium"])
    lines = []
    if single:
        lines.append("<b>Blocks (one-sided) since last report</b>")
        for b in single[:top]:
            bullish = (b["side"] == "buy") == (b["kind"] == "call")
            lines.append(escape(f"{'🟢' if bullish else '🔴'} {b['symbol']} {b['expiry'][5:].replace('-', '/')} "
                                f"{b['strike']:g}{b['kind'][0].upper()} {b['volume']:,.0f}x · "
                                f"{money(b['premium']).lstrip('+')} "
                                + (("mostly bought" if b["side"] == "buy" else "mostly sold") if b.get("taped") else
                                   f"at {'ask' if b['side'] == 'buy' else 'bid'}")))
    skipped = sorted({b["symbol"] for b in blocks if b["ticker"] in paired})
    mid = sorted({b["symbol"] for b in blocks if b["ticker"] not in paired and b["side"] == "mid"})
    notes = ([f"multi-leg or paired (spread/roll/combo): {', '.join(skipped)}"] if skipped else []) + \
            ([f"at mid (no side): {', '.join(mid)}"] if mid else [])
    if notes:
        lines.append(f"<i>Skipped blocks: {escape(' · '.join(notes))}</i>")
    return lines
