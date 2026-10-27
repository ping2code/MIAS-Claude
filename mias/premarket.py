"""Pre-market heads-up at 07:00 ET and a short update at 09:15 ET (after the 08:30 economic data). Also /premarket.

07:00: yesterday's sector board (swing trend at the last check of the previous session, from the recorded checks),
then pre-market change against yesterday's close and pre-market volume against the same time of day over the last
10 sessions, for SPY/QQQ/IWM, each sector ETF and the biggest watchlist gappers. A gap against the sector's (or the
stock's) swing trend is flagged. Estimated earnings in the next few days close the message.

09:15: the same pre-market lines, refreshed. Each sector ETF is marked ✅ when its pre-market move kept the sign
it had at 07:00 and ⚠️ when it flipped, and the pre-market leader (the sector ETF up the most) is compared too.

Pre-market trades are thin before ~08:00: a few prints can move a stock 1-2%, so the volume ratio is shown with
every move, and gappers need $500K+ of pre-market dollar volume. No options data: options open at 09:30.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

from . import events, formatting, sectors
from .trend import direction, label_for

log = logging.getLogger(__name__)

PRE_START, OPEN = time(4, 0), time(9, 30)
SESSIONS = 10
GAP_MIN = 1.0               # % move for a gapper
GAP_DOLLARS = 500_000       # pre-market dollar volume for a gapper
AGAINST_MIN = 0.3           # % move against the trend worth flagging
EARNINGS_DAYS = 5
KEY = "premarket"


def _et(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz)


def is_premarket(now: datetime, tz: ZoneInfo) -> bool:
    local = now.astimezone(tz)
    return local.weekday() < 5 and PRE_START <= local.time() < OPEN


def read(raw: list[dict], today: date, tz: ZoneInfo, now: datetime) -> dict | None:
    """Pre-market read from 5-minute bars (extended hours included) of the last sessions and today.
    {"price", "prev_close", "change" %, "volume", "dollars", "rvol" (or None)}; None without today's pre-market."""
    upto = now.astimezone(tz).time()
    by_day: dict[date, list[tuple[time, dict]]] = {}
    for b in raw:
        t = _et(b["t"], tz)
        by_day.setdefault(t.date(), []).append((t.time(), b))
    pre = [b for t, b in by_day.get(today, []) if PRE_START <= t < OPEN and t < upto]
    past = sorted(d for d in by_day if d < today)
    if not pre or not past:
        return None
    regular = [b for t, b in by_day[past[-1]] if OPEN <= t < time(16, 0)]
    if not regular:
        return None
    prev_close = regular[-1]["c"]
    vols = [sum(b["v"] for t, b in by_day[d] if PRE_START <= t < min(upto, OPEN)) for d in past[-SESSIONS:]]
    vols = [v for v in vols if v > 0]
    avg = sum(vols) / len(vols) if vols else 0
    volume = sum(b["v"] for b in pre)
    price = pre[-1]["c"]
    return {"price": price, "prev_close": prev_close, "change": 100 * (price / prev_close - 1), "volume": volume,
            "dollars": sum(b["v"] * b.get("vw", b["c"]) for b in pre), "rvol": volume / avg if avg else None}


def yesterday_results(store, cfg, today: date) -> list:
    """Each symbol's last recorded check before today, shaped like a SymbolResult for the sector board."""
    cutoff = datetime.combine(today, time(0, 0), cfg.tz).timestamp()
    w = cfg.trend_profiles.get("intraday", {})
    out = []
    for sym in cfg.symbols:
        row = store.db.execute("SELECT data FROM metrics WHERE symbol=? AND ts<? AND ts>? ORDER BY ts DESC LIMIT 1",
                               (sym, cutoff, cutoff - 6 * 86400)).fetchone()
        if not row:
            continue
        d = json.loads(row[0])
        if d.get("stock_score") is None:
            continue
        tfs = d.get("timeframes") or {}
        used = {tf: x for tf, x in w.items() if tf in tfs}
        intra = sum(tfs[tf]["score"] * x for tf, x in used.items()) / sum(used.values()) if used else None
        comps = {"swing": (d["stock_score"], d["stock_label"])}
        if intra is not None:
            comps["intraday"] = (intra, label_for(intra, cfg.trend_strong, cfg.trend_bias))
        out.append(NS(symbol=sym, stock=NS(score=d["stock_score"], label=d["stock_label"], composites=comps),
                      price=d.get("price")))
    return out


def _move(sym: str, x: dict) -> str:
    vol = f", {x['rvol']:.1f}× vol" if x.get("rvol") else ""
    return f"{sym} {x['change']:+.2f}%{vol}"


def _against(score: float | None, change: float, bias: float) -> bool:
    return score is not None and abs(score) >= bias and abs(change) >= AGAINST_MIN and (score > 0) != (change > 0)


def text(cfg, reads: dict[str, dict], prior: list, upcoming: list[tuple[str, dict]], now: datetime,
         earlier: dict | None = None) -> tuple[str, dict]:
    """The message and a snapshot (sector ETF moves, leader) for the 09:15 comparison.
    earlier: the 07:00 snapshot; with it the message is the short update."""
    local = now.astimezone(cfg.tz)
    update = earlier is not None
    title = "Pre-market update" if update else "Pre-market heads-up"
    lines = [f"🌅 <b>{title}</b>  {local:%a %b %d %H:%M} ET"]
    board = sectors.read(prior, cfg.sectors) if cfg.sectors else []
    board = [sr for sr in board if sr.swing is not None]
    if not update:
        lines.append("<b>Yesterday's close: sector trend (swing)</b>")
        lines += sectors.board_lines(prior, cfg.sectors, cfg) or ["no recorded checks from the last session"]
    elif board:
        lines.append(f"Yesterday's focus: <b>{escape(board[0].sector.name)}</b>")

    lines += ["", "<b>Pre-market vs yesterday's close</b> (volume vs normal for this time)"]
    mk = [_move(s, reads[s]) for s in cfg.market_symbols if s in reads]
    lines.append("Market: " + (" · ".join(mk) if mk else "no pre-market trades yet"))

    snap: dict = {"sectors": {}}
    ranked = []
    for sr in board:
        etf = sr.sector.etf
        x = reads.get(etf) if etf else None
        lbl = label_for(sr.swing, cfg.trend_strong, cfg.trend_bias)
        line = f"{formatting.ICON[direction(lbl)]} {escape(sr.sector.name)} {formatting.label(lbl)}"
        if x:
            snap["sectors"][sr.sector.key] = round(x["change"], 3)
            ranked.append((x["change"], sr.sector))
            line += f" · {escape(_move(etf, x))}"
            if _against(sr.swing, x["change"], cfg.trend_bias):
                line += " · ⚠️ gapping against its trend"
            elif abs(sr.swing) >= cfg.trend_bias and abs(x["change"]) >= AGAINST_MIN:
                line += " · ✅ with its trend"
            if update:
                was = earlier.get("sectors", {}).get(sr.sector.key)
                if was is None:
                    line += " · (no 07:00 read)"
                elif (was > 0) == (x["change"] > 0) or abs(was) < 0.05:
                    line += f" · ✅ held (07:00 {was:+.2f}%)"
                else:
                    line += f" · ⚠️ flipped (07:00 {was:+.2f}%)"
        else:
            line += f" · {escape(etf or 'no ETF')}: no pre-market trades yet"
        lines.append(line)
    if ranked:
        ranked.sort(key=lambda t: -t[0])
        lead = ranked[0][1]
        snap["leader"] = lead.key
        line = f"Pre-market leader: <b>{escape(lead.name)}</b> ({lead.etf} {ranked[0][0]:+.2f}%)"
        if update and earlier.get("leader"):
            if earlier["leader"] == lead.key:
                line += " ✅ same as 07:00"
            else:
                was = next((s.name for s in cfg.sectors if s.key == earlier["leader"]), earlier["leader"])
                line += f" ⚠️ was {escape(was)} at 07:00"
        lines.append(line)

    skip = set(cfg.market_symbols) | {s.etf for s in cfg.sectors if s.etf} | set(cfg.instruments)
    score = {r.symbol: r.stock.score for r in prior}
    gappers = [(s, x) for s, x in reads.items() if s not in skip and abs(x["change"]) >= GAP_MIN
               and x["dollars"] >= GAP_DOLLARS]
    gappers.sort(key=lambda sx: -abs(sx[1]["change"]))
    lines += ["", f"<b>Gappers</b> (±{GAP_MIN:g}%+, ${GAP_DOLLARS / 1e6:g}M+ traded)"]
    if not gappers:
        lines.append("none yet")
    for s, x in gappers[:8]:
        sc = score.get(s)
        trend = ""
        if sc is not None:
            lbl = label_for(sc, cfg.trend_strong, cfg.trend_bias)
            trend = f" · swing {formatting.label(lbl)}" + (" ⚠️ against its trend" if _against(sc, x["change"],
                                                                                            cfg.trend_bias) else "")
        lines.append(f"{'🟢' if x['change'] > 0 else '🔴'} {escape(_move(s, x))} · "
                     f"${x['dollars'] / 1e6:.1f}M{trend}")

    if not update:
        lines += ["", f"<b>Earnings</b> (estimated from past report gaps, ±1 week; next {EARNINGS_DAYS} days)"]
        if upcoming:
            lines.append(" · ".join(f"{s} ~{datetime.fromisoformat(e['next']):%b %d}"
                                    + (" (rough)" if e["quality"] != "confirmed" else "") for s, e in upcoming))
        else:
            lines.append("none estimated")
        lines.append("<i>Pre-market is thin before 08:00; the 09:15 update re-checks after the 08:30 data. "
                     "No options data until the open.</i>")
    return "\n".join(lines), snap


async def build(client, store, cfg, now: datetime, cache: dict, earlier: dict | None = None) -> tuple[str, dict]:
    """Fetch and build. `cache` keeps daily bars for the day (the 09:15 update reuses them)."""
    import asyncio
    tz = cfg.tz
    today = now.astimezone(tz).date()
    syms = [s for s in cfg.symbols if s not in cfg.instruments]
    sem = asyncio.Semaphore(8)

    async def five(sym):
        async with sem:
            try:
                return sym, await client.aggregates(sym, 5, "minute", today - timedelta(days=16), today)
            except Exception as exc:
                log.warning("pre-market bars %s failed: %s", sym, exc)
                return sym, []

    async def daily(sym):
        key = f"daily:{sym}:{today}"
        if key not in cache:
            async with sem:
                try:
                    cache[key] = await client.aggregates(sym, 1, "day", today - timedelta(days=420),
                                                         today - timedelta(days=1))
                except Exception as exc:
                    log.warning("daily bars %s failed: %s", sym, exc)
                    cache[key] = []
        return sym, cache[key]

    raws = dict(await asyncio.gather(*(five(s) for s in syms)))
    reads = {}
    for s, raw in raws.items():
        try:
            x = read(raw, today, tz, now)
        except Exception:
            x = None
        if x:
            reads[s] = x
    upcoming = []
    if earlier is None:
        stocks = [s for s in syms if s not in events.FUNDS and s not in {x.etf for x in cfg.sectors}]
        for s, d in await asyncio.gather(*(daily(s) for s in stocks)):
            e = events.earnings_estimate(s, d, tz, today)
            if e and -1 <= (date.fromisoformat(e["next"]) - today).days <= EARNINGS_DAYS:
                upcoming.append((s, e))
        upcoming.sort(key=lambda se: se[1]["next"])
    prior = yesterday_results(store, cfg, today)
    return text(cfg, reads, prior, upcoming, now, earlier)
