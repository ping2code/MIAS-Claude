"""V3 replay of a stored session with the live V3 code path (V3Runner.process): the observer's processed bars for the
day in their original order (read-only), the same warm-up files, the confirmed events V2 recorded that day
(read-only, data/observer/v2.db), and the historical MIAS options rows. Writes only its own replay database. Used for
the V2-vs-V3 parity check and the example renders; never sends Telegram."""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from pathlib import Path

from ..indicators import Bar
from .config import ONE, V3Config
from .service import V3Runner, ro_connect
from .store import V3DB


def v2_confirms(v2_db: Path | None, day: date) -> list[dict] | None:
    if not v2_db or not Path(v2_db).exists():
        return None
    c = ro_connect(v2_db)
    try:
        return [{"sym": s, "side": sd, "ts": ts, "source": src} for s, sd, ts, src in
                c.execute("SELECT sym, side, ts, source FROM confirms WHERE day = ? ORDER BY ts", (str(day),))]
    finally:
        c.close()


async def replay_day(cfg, v3: V3Config, day: date, db_path: Path, client=None, options: bool = True,
                     source=None) -> dict:
    from ..observer.optctx import OptionsContext
    db_path = Path(db_path)
    for p in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        if p.exists():
            p.unlink()
    store = V3DB(db_path)
    sim = {"t": datetime.combine(day, dtime(9, 22), cfg.tz).timestamp()}
    clock = lambda: sim["t"]                                   # noqa: E731
    runner = V3Runner(cfg, v3, store, client=client, clock=clock, source=source, mode="replay",
                      options_factory=(lambda: OptionsContext(Path(cfg.db_path), cfg.tz, clock=clock,
                                                              source="replay")) if options else None)
    confirms = v2_confirms(v3.v2_db, day)
    if confirms is None and source is not None:
        confirms = source.confirms(day, runner.syms)
    await runner.start_day(day, confirms or [])
    conn = ro_connect(v3.bars_db)
    try:
        rows = conn.execute("SELECT rowid, sym, s, o, h, l, c, v, source FROM bars1 WHERE day = ? ORDER BY rowid",
                            (str(day),)).fetchall()
        for rowid, sym, s, o, h, l, c, v, src in rows:
            runner.last_rowid = rowid
            if sym not in runner.det.states:
                continue
            sim["t"] = (s + ONE) / 1000 + 1
            rec, gap = runner._bar_meta(conn, sym, s, src)
            runner.process(sym, Bar(int(s), float(o), float(h), float(l), float(c), float(v)), src, rowid, rec, gap)
    finally:
        conn.close()
    sim["t"] = datetime.combine(day, dtime(16, 5), cfg.tz).timestamp()
    report = runner.end_of_day(force=True)
    out = {"day": str(day), "bars": len(rows), "watches": runner.counts["watches"], "counts": runner.counts,
           "db": str(db_path), "report": report}
    store.close()
    return out
