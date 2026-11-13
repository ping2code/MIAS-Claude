"""Historical replay with the exact live code (watch.Observer), on Massive REST 1-minute bars.

For each day: every symbol is warmed with its prior regular sessions (rules.warm_sessions), confirmed events for the
day are loaded (sources.MiasSource), and the day's completed 1-minute bars are fed in (bar end, symbol order).
The live service warms and backfills through the same functions (``sessions_by_day``, ``build_observer``).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..indicators import Bar
from ..trend import regular_session, to_bars
from .log import ObserverDB
from .rules import ONE, RULES, Rules
from .watch import Observer


def day_of(ms: int, tz: ZoneInfo) -> date:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz).date()


def sessions_by_day(bars: list[Bar], tz: ZoneInfo) -> dict[date, list[Bar]]:
    out: dict[date, list[Bar]] = defaultdict(list)
    for b in bars:
        out[day_of(b.t, tz)].append(b)
    return {d: sorted(v, key=lambda b: b.t) for d, v in sorted(out.items())}


def build_observer(symbols: list[str], day: date, tz: ZoneInfo, history: dict[str, dict[date, list[Bar]]],
                   daily: dict[str, list[tuple[date, float]]], confirms: list[dict], context=None,
                   rules: Rules = RULES) -> Observer:
    ob = Observer(symbols, day, tz, rules, context=context)
    for sym in symbols:
        prior = [(d, bs) for d, bs in (history.get(sym) or {}).items() if d < day][-rules.warm_sessions:]
        closes = [c for d, c in (daily.get(sym) or []) if d < day]
        ob.warm(sym, prior, closes)
    for ev in confirms:
        ob.add_confirm(ev)
    return ob


def ordered(symbols: list[str], bars: dict[str, list[Bar]]) -> list[tuple[str, Bar]]:
    rank = {s: i for i, s in enumerate(symbols)}
    rows = [(b.t + ONE, rank[s], s, b) for s in symbols for b in bars.get(s) or []]
    return [(s, b) for _, _, s, b in sorted(rows, key=lambda x: (x[0], x[1]))]


def run_day(symbols: list[str], day: date, tz: ZoneInfo, history, daily, confirms, context=None,
            rules: Rules = RULES) -> tuple[list[dict], Observer]:
    ob = build_observer(symbols, day, tz, history, daily, confirms, context, rules)
    events: list[dict] = []
    today = {s: (history.get(s) or {}).get(day, []) for s in symbols}
    from .fresh import V1Fresh
    fresh = V1Fresh(ob)                        # measurement only; never changes the observer
    for sym, b in ordered(symbols, today):
        p = ob.prev.get(sym)
        evs = ob.process(sym, b)
        events += evs + fresh.after(sym, p, evs)
    events += ob.finalize()
    return events, ob


# ---- data -----------------------------------------------------------------------------------------

async def minute_history(client, cache_dir: Path, symbols: list[str], start: date, end: date, tz: ZoneInfo,
                         concurrency: int = 4) -> dict[str, dict[date, list[Bar]]]:
    """Regular-session 1-minute bars per symbol and day (cached a month at a time, as the research tools do)."""
    from ..emacross import _Cache, _five_minute
    cache = _Cache(cache_dir)
    sem = asyncio.Semaphore(concurrency)

    async def one(sym):
        async with sem:
            return sym, await _five_minute(client, cache, sym, start, end, tz, minutes=1)

    out = {}
    for sym, bars in await asyncio.gather(*(one(s) for s in symbols)):
        out[sym] = sessions_by_day(bars, tz)
    return out


async def daily_closes(client, cache_dir: Path, symbols: list[str], end: date, tz: ZoneInfo,
                       days: int = 420) -> dict[str, list[tuple[date, float]]]:
    from ..emacross import _Cache
    cache = _Cache(cache_dir)
    start = end - timedelta(days=days)
    out = {}
    for sym in symbols:
        fetch = lambda s=sym: client.aggregates(s, 1, "day", start, end)
        raw = await (fetch() if end >= date.today() else cache.get(f"{sym}_1d_{start}_{end}", fetch))
        out[sym] = [(day_of(int(r["t"]), tz), float(r["c"])) for r in raw if "c" in r]
    return out


async def today_minutes(client, sym: str, day: date, tz: ZoneInfo, now_ms: int) -> list[Bar]:
    """Today's completed regular-session 1-minute bars from REST (restart backfill and gap fill)."""
    raw = await client.aggregates(sym, 1, "minute", day, day)
    return [b for b in regular_session(to_bars(raw), tz) if b.t + ONE <= now_ms and day_of(b.t, tz) == day]


# ---- historical replay -------------------------------------------------------------------------

async def replay(cfg, settings, days: int, end: date, db_path: Path, cache_dir: Path, source) -> dict:
    from ..massive import MassiveClient
    tz = cfg.tz
    syms = settings.symbols
    start = end - timedelta(days=int(days * 1.6) + 30)
    async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout, max_retries=cfg.max_retries,
                             max_concurrency=cfg.max_concurrency) as client:
        hist = await minute_history(client, cache_dir, syms, start, end, tz)
        daily = await daily_closes(client, cache_dir, syms, end, tz)
    trading = sorted({d for s in syms for d in hist.get(s, {})})
    trading = [d for d in trading if d <= end]
    run = trading[-days:]
    if db_path.exists():
        db_path.unlink()
    for ext in ("-wal", "-shm"):
        p = Path(str(db_path) + ext)
        if p.exists():
            p.unlink()
    db = ObserverDB(db_path)
    summary = {"days": [str(d) for d in run], "watches": 0}
    for d in run:
        confirms = source.confirms(d, syms)
        events, _ = run_day(syms, d, tz, hist, daily, confirms, source.options_context)
        for ev in confirms:
            db.confirm(str(d), ev)
        for s in syms:
            for b in hist.get(s, {}).get(d, []):
                db.bar1(str(d), s, b, "rest")
        summary["watches"] += len(db.apply(events, "replay"))
    db.put("replay", {"days": summary["days"], "end": str(end), "symbols": syms})
    db.close()
    return summary
