"""Early Watch v2 replay with the live code (V2Observer) on REST 1-minute bars, for a fixed date range
(development 2026-07-01..09-16, holdout 2026-09-17..11-11; docs/observer/V2_PREREGISTRATION.md section 6)."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from .replay import daily_closes, minute_history, ordered
from .rules import RULES
from .v2 import V2Observer
from .v2_log import V2DB


def build_v2(symbols, day, tz, history, daily, confirms, context=None) -> V2Observer:
    ob = V2Observer(symbols, day, tz, context=context)
    for sym in symbols:
        prior = [(d, bs) for d, bs in (history.get(sym) or {}).items() if d < day][-RULES.warm_sessions:]
        ob.warm(sym, prior, [c for d, c in (daily.get(sym) or []) if d < day])
    for ev in confirms:
        ob.add_confirm(ev)
    return ob


def run_v2_day(symbols, day, tz, history, daily, confirms, context=None):
    ob = build_v2(symbols, day, tz, history, daily, confirms, context)
    events = []
    for sym, b in ordered(symbols, {s: (history.get(s) or {}).get(day, []) for s in symbols}):
        events += ob.process(sym, b)
    events += ob.finalize()
    return events, ob


async def replay_v2(cfg, settings, start: date, end: date, db_path: Path, source) -> dict:
    from ..massive import MassiveClient
    tz, syms = cfg.tz, settings.symbols
    async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout, max_retries=cfg.max_retries,
                             max_concurrency=cfg.max_concurrency) as client:
        hist = await minute_history(client, settings.cache_dir, syms, start - timedelta(days=21), end, tz)
        daily = await daily_closes(client, settings.cache_dir, syms, end, tz)
    days = sorted({d for s in syms for d in hist.get(s, {}) if start <= d <= end})
    for p in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        if p.exists():
            p.unlink()
    db = V2DB(db_path)
    n = 0
    for d in days:
        confirms = source.confirms(d, syms)
        events, _ = run_v2_day(syms, d, tz, hist, daily, confirms, source.options_context)
        for ev in confirms:
            db.confirm(str(d), ev)
        for s in syms:
            for b in hist.get(s, {}).get(d, []):
                db.bar1(str(d), s, b, "rest")
        n += len(db.apply_v2(events, "replay"))
    db.put("replay", {"start": str(start), "end": str(end), "days": [str(d) for d in days], "symbols": syms})
    db.close()
    return {"days": len(days), "watches": n, "first": str(days[0]) if days else None,
            "last": str(days[-1]) if days else None}
