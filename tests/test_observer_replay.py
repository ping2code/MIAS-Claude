"""Replay determinism, restart recovery without duplicates, and live == replay for the same bars (the live runner
with REST gap fill, duplicate and out-of-order stream bars)."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mias.observer.log import ObserverDB
from mias.observer.replay import build_observer, ordered, run_day
from mias.observer.rules import ONE
from mias.observer.service import LiveRunner
from mias.observer.settings import Settings
from observer_synth import ET, daily, history, trading_days

SYMS = ["SPY", "QQQ", "NVDA", "META", "AMD"]
DAYS = trading_days(date(2026, 11, 11), 14)
DAY = DAYS[-1]


def _data(seed=11):
    h = history(SYMS, DAYS, seed=seed)
    return h, daily(h)


def _watch_rows(db):
    return [(w["watch_id"], w["state"], w["end_reason"], w["end_at"], w["upgrade_at"], w["price"])
            for w in db.watches()]


def test_replay_is_deterministic():
    h, d = _data()
    confirms = [{"sym": "NVDA", "side": "bull", "ts": datetime.combine(DAY, datetime.min.time(), ET).timestamp()
                 + 11 * 3600, "source": "B"}]
    a, _ = run_day(SYMS, DAY, ET, h, d, confirms)
    b, _ = run_day(SYMS, DAY, ET, h, d, confirms)
    assert a == b and any(e["kind"] == "pivot" for e in a)


def test_some_watches_occur_on_trending_synthetic_days():
    total = 0
    for seed in range(11, 16):
        h, d = _data(seed)
        evs, _ = run_day(SYMS, DAY, ET, h, d, [])
        total += sum(e["kind"] == "watch" for e in evs)
    assert total > 0


def test_restart_rebuilds_the_same_state_without_duplicates(tmp_path):
    h, d = _data()
    full, _ = run_day(SYMS, DAY, ET, h, d, [])
    a = ObserverDB(tmp_path / "a.db")
    a.apply(full, "replay")
    # "live" run that dies at 12:30, then a restart that replays the whole day into the same database
    b = ObserverDB(tmp_path / "b.db")
    ob = build_observer(SYMS, DAY, ET, h, d, [])
    cut = int(datetime.combine(DAY, datetime.min.time().replace(hour=12, minute=30), ET).timestamp() * 1000)
    today = {s: h[s][DAY] for s in SYMS}
    for sym, bar in ordered(SYMS, today):
        if bar.t + ONE <= cut:
            b.apply(ob.process(sym, bar), "live")
    before = len(b.watches())
    ob2 = build_observer(SYMS, DAY, ET, h, d, [])
    events = []
    for sym, bar in ordered(SYMS, today):
        events += ob2.process(sym, bar)
    events += ob2.finalize()
    b.apply(events, "live")
    b.apply(events, "live")                                   # a second restart changes nothing
    assert _watch_rows(a) == _watch_rows(b)
    assert len(b.watches()) >= before
    n = b.db.execute("SELECT COUNT(*), COUNT(DISTINCT watch_id) FROM watches").fetchone()
    assert n[0] == n[1]


# ---- live runner ------------------------------------------------------------------------------------

class FakeClient:
    def __init__(self, h, d):
        self.h, self.d = h, d
        self.calls = []

    async def aggregates(self, sym, mult, span, start, end):
        self.calls.append((sym, span, str(start), str(end)))
        if span == "day":
            return [{"t": int(datetime.combine(x, datetime.min.time(), ET).timestamp() * 1000), "c": c}
                    for x, c in self.d[sym] if start <= x <= end]
        return [{"t": b.t, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v}
                for x, bars in self.h[sym].items() if start <= x <= end for b in bars]


class FakeSource:
    def confirms(self, day, syms):
        return []

    def options_context(self, sym, ts):
        return {}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _cfg():
    class C:
        tz = ET
    return C()


def test_live_runner_matches_replay_with_gaps_dups_and_out_of_order(tmp_path):
    h, d = _data()
    s = Settings(True, SYMS, "wss://x", tmp_path / "o.db", tmp_path / "cache", 5, 150, datetime.min.time(),
                 datetime.max.time(), 20, False)
    noon = datetime.combine(DAY, datetime.min.time().replace(hour=12), ET).timestamp()
    clock = Clock(noon)
    db = ObserverDB(s.db_path)
    client = FakeClient(h, d)
    runner = LiveRunner(_cfg(), s, client, db, FakeSource(), clock=clock)

    async def go():
        await runner.start_day(DAY)                          # warm + REST backfill to 12:00
        rows = [b for b in h["SPY"][DAY]]
        later = {sym: [b for b in h[sym][DAY] if b.t >= noon * 1000] for sym in SYMS}
        for k in range(30):                                  # 12:00 .. 12:29 from the "stream"
            for sym in SYMS:
                b = later[sym][k]
                if sym == "QQQ" and k == 3:
                    continue                                 # missing on the stream -> REST gap fill
                ev = {"ev": "AM", "sym": sym, "s": b.t, "e": b.t + ONE, "o": b.o, "h": b.h, "l": b.l, "c": b.c,
                      "v": b.v}
                clock.t = (b.t + ONE) / 1000 + 1.5
                await runner.on_bar(ev, clock.t, 1)
                if sym == "NVDA" and k == 5:
                    await runner.on_bar(ev, clock.t, 1)      # duplicate
                if sym == "AMD" and k == 7:
                    old = [x for x in h[sym][DAY] if x.t < noon * 1000][-3]   # older, never on the stream
                    await runner.on_bar({"ev": "AM", "sym": sym, "s": old.t, "e": old.t + ONE, "o": old.o,
                                         "h": old.h, "l": old.l, "c": old.c, "v": old.v}, clock.t, 1)
            clock.t += 10
            await runner.flush()
        return later

    later = asyncio.run(go())
    src = dict(((r[0], r[1]), r[2]) for r in db.db.execute("SELECT sym, s, source FROM bars1"))
    assert src[("QQQ", later["QQQ"][3].t)] == "rest_gap"
    assert src[("SPY", later["SPY"][0].t)] == "ws"
    st = dict(db.db.execute("SELECT status, COUNT(*) FROM ws_bars GROUP BY status").fetchall())
    assert st.get("dup") == 1 and st.get("old") == 1
    # same bars through the replay path -> the same watches
    cut = later["SPY"][29].t + ONE
    ob = build_observer(SYMS, DAY, ET, h, d, [])
    rep = ObserverDB(tmp_path / "r.db")
    today = {sym: [b for b in h[sym][DAY] if b.t + ONE <= cut] for sym in SYMS}
    for sym, bar in ordered(SYMS, today):
        rep.apply(ob.process(sym, bar), "replay")
    live_rows = [r[:4] for r in _watch_rows(db)]
    rep_rows = [r[:4] for r in _watch_rows(rep)]
    assert live_rows == rep_rows
