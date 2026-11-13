"""Early Watch v2 research: the three families (candidate -> watch -> invalidation), EMA as context only
(pre-EMA flag, cross events), swing-leg start without look-ahead, the v2 Telegram policy and text, and isolation
from v1 (same v1 records with v2 on, and a v2 failure never reaches v1)."""
import asyncio
from datetime import date, datetime

from mias.indicators import Bar
from mias.observer.log import ObserverDB
from mias.observer.replay import ordered
from mias.observer.service import LiveRunner
from mias.observer.settings import Settings
from mias.observer.structure import Candidate, Pivot
from mias.observer.v2 import V2Observer, V2Telegram, v2_text
from mias.observer.v2_log import V2DB
from observer_synth import ET, daily, history, trading_days
from test_observer_watch import DAY, H1, L0, feat, hl, ms


def v2(symbols=("META",)):
    ob = V2Observer(list(symbols), DAY, ET)
    q = {}
    for s, st in ob.states.items():
        st.on_minute = (lambda sym: (lambda bar, gap=False: q[sym].pop(0)))(s)

    def run(feats):
        out = []
        for f in feats:
            q.setdefault(f.sym, []).append(f)
            out += ob.process(f.sym, Bar(f.t, f.o, f.h, f.l, f.c, f.v))
        return out
    return ob, run


def kinds(evs, k):
    return [e for e in evs if e["kind"] == k]


NO_EMA = dict(ema9=100.0, ema21=100.3, spread=-0.3, spread_ref=-0.2, ema9_c=100.0, ema21_c=100.3)   # not crossed up


def test_structure_candidate_then_watch_without_ema():
    ob, run = v2()
    c = hl()
    evs = run([feat(10, 10, hl=None, **NO_EMA)])
    evs += run([feat(10, 11, hl=c, dev_events=(("DEV_HL", c),), **NO_EMA)])
    cand = kinds(evs, "v2_candidate")
    assert len(cand) == 1 and cand[0]["family"] == "structure" and cand[0]["status"] == "pending"
    assert cand[0]["ema"]["crossed_completed"] is False
    w = kinds(run([feat(10, 12, hl=c, **NO_EMA)]), "v2_watch")
    assert len(w) == 1 and w[0]["candidate_at"] == ms(10, 12) and w[0]["watch_at"] == ms(10, 13)
    assert w[0]["pre_ema"] is True and w[0]["level"] == 100.3 and w[0]["lane"] == "main"
    end = kinds(run([feat(10, 13, hl=None, c=100.2, **NO_EMA)]), "v2_end")
    assert end[0]["state"] == "INVALIDATED" and end[0]["end_reason"] == "close_beyond_level"


def test_structure_candidate_not_confirmed_when_it_fails_next_close():
    ob, run = v2()
    c = hl()
    run([feat(10, 11, hl=c, dev_events=(("DEV_HL", c),))])
    evs = run([feat(10, 12, hl=None, c=100.1)])
    assert kinds(evs, "v2_watch") == [] and kinds(evs, "v2_candidate_end")[0]["status"] == "not_confirmed"


def test_vwap_reclaim_needs_three_closes_below_then_two_holds():
    ob, run = v2()
    below = dict(c=99.8, vwap=100.0, hl=None)
    run([feat(10, 10, **below), feat(10, 11, **below)])
    assert kinds(run([feat(10, 12, c=100.2, vwap=100.0, hl=None)]), "v2_candidate") == []   # only 2 below
    ob, run = v2()
    run([feat(10, 10, **below), feat(10, 11, **below), feat(10, 12, **below)])
    cand = kinds(run([feat(10, 13, c=100.2, vwap=100.0, hl=None, **NO_EMA)]), "v2_candidate")
    assert cand[0]["family"] == "vwap" and cand[0]["side"] == "bull"
    assert kinds(run([feat(10, 14, c=100.3, vwap=100.0, hl=None)]), "v2_watch") == []
    w = kinds(run([feat(10, 15, c=100.4, vwap=100.05, hl=None, **NO_EMA)]), "v2_watch")
    assert w[0]["watch_at"] == ms(10, 16) and w[0]["level"] == 100.05 and w[0]["pre_ema"] is True
    assert kinds(run([feat(10, 16, c=99.9, vwap=100.0, hl=None)]), "v2_end") == []
    end = kinds(run([feat(10, 17, c=99.9, vwap=100.0, hl=None)]), "v2_end")
    assert end[0]["end_reason"] == "two_closes_through_vwap"


def test_opening_range_break_hold_and_failure():
    ob, run = v2()
    base = dict(hl=None, or_high=101.5, or_low=99.0, or_done=True, vwap=100.0)
    run([feat(10, 0, c=101.4, **base)])
    cand = kinds(run([feat(10, 1, c=101.6, **base)]), "v2_candidate")
    assert cand[0]["family"] == "or" and cand[0]["leg_id"] == f"OR{ms(10, 1)}"
    run([feat(10, 2, c=101.7, **base)])
    w = kinds(run([feat(10, 3, c=101.8, **base)]), "v2_watch")
    assert w[0]["level"] == 101.5
    end = kinds(run([feat(10, 4, c=101.4, **base)]), "v2_end")
    assert end[0]["state"] == "INVALIDATED"
    # no OR candidate before 09:45 (range not complete)
    ob, run = v2()
    pre = dict(base, or_done=False)
    assert kinds(run([feat(9, 40, c=101.4, **pre), feat(9, 41, c=101.6, **pre)]), "v2_candidate") == []


def test_ema_cross_event_and_upgrade():
    ob, run = v2()
    ob.sym["META"].ema_diff = -0.1
    evs = run([feat(10, 14, closed5=True, ema9_c=100.5, ema21_c=100.2, hl=None)])
    x = kinds(evs, "v2_ema_cross")
    assert x and x[0]["side"] == "bull" and x[0]["at"] == ms(10, 15)
    c = hl()
    run([feat(10, 20, hl=c, dev_events=(("DEV_HL", c),)), feat(10, 21, hl=c, c=101.3)])
    ob.add_confirm({"sym": "META", "side": "bull", "ts": ms(10, 30) / 1000, "source": "B"})
    end = kinds(run([feat(10, 30, hl=c), feat(10, 31, hl=c)]), "v2_end")
    assert end[0]["state"] == "UPGRADED" and end[0]["upgrade_source"] == "B"


def test_leg_start_is_the_latest_confirmed_pivot_known_at_the_candidate():
    ob, run = v2()
    ob.states["META"].structure.pivots = [L0, H1]
    c = hl()
    cand = kinds(run([feat(10, 11, hl=c, dev_events=(("DEV_HL", c),))]), "v2_candidate")[0]
    assert cand["leg_start"] == {"price": L0.price, "bar_t": L0.bar_t, "confirmed_at": L0.confirmed_at}
    assert cand["leg_start"]["confirmed_at"] <= cand["candidate_at"]


def _w(fam="structure", sym="META", side="bull", t=None, leg="P1", lane="main"):
    t = t or ms(10, 13)
    return {"watch_id": f"{fam}|{sym}|{leg}", "family": fam, "sym": sym, "side": side, "leg_id": leg,
            "day": str(DAY), "watch_at": t, "lane": lane, "watch_price": 101.0, "level": 100.3,
            "context": {"vwap": 100.0, "rel_vol": 1.2, "spy_pct": 0.3},
            "ema_watch": {"crossed_completed": False}}


def test_v2_telegram_policy():
    p = V2Telegram()
    now = ms(10, 13) + 2000
    assert p.decide(_w(), now)[0] == "send"
    assert p.decide(_w(), now) == ("skip", "setup already sent")
    v, g = p.decide(_w("vwap", leg="VW1", t=ms(10, 16)), ms(10, 16) + 1000)
    assert v == "merged" and g == "structure|META|P1"                       # same symbol / side within 5 min
    assert p.decide(_w("vwap", leg="VW2", t=ms(10, 25)), ms(10, 25) + 1000)[0] == "send"
    assert p.decide(_w(lane="opening", leg="P9"), now)[0] == "skip"
    assert p.decide(_w(leg="P8"), now + 600_000) == ("skip", "found late")
    q = V2Telegram()                    # no MIAS hourly cap since 2026-11-12: every distinct setup is sent
    for i in range(12):
        assert q.decide(_w(sym=f"S{i}", leg="P1", t=ms(11, 2 * i)), ms(11, 2 * i) + 1000)[0] == "send"
    o = V2Telegram()
    assert o.decide(_w("or", leg="OR1"), now)[0] == "send"
    assert o.decide(_w("or", leg="OR2", t=ms(11, 0)), ms(11, 0) + 1000) == ("skip", "setup already sent")


def test_v2_text():
    one = v2_text(_w(), ["structure"], ET)
    assert one.startswith("🧭 V2 STRUCTURE WATCH · META · BULLISH · 10:13 ET @ $101.00")
    assert one.endswith("RESEARCH WATCH ONLY — NOT A TRADE SIGNAL") and "NOT crossed yet" in one
    both = v2_text(_w(), ["structure", "vwap"], ET)
    assert both.startswith("🧭 V2 EARLY WATCH") and "✓ Structure" in both and "✓ VWAP reclaim / loss" in both
    assert "○ Opening range" in both
    for word in ("strike", "expir", "contract", "target", "stop", "buy", "sell", "call", "put"):
        assert word not in both.lower()


# ---- isolation from v1 ------------------------------------------------------------------------------

SYMS = ["SPY", "QQQ", "NVDA", "META", "AMD"]
DAYS = trading_days(date(2026, 11, 11), 14)


class FakeClient:
    def __init__(self, h, d):
        self.h, self.d = h, d

    async def aggregates(self, sym, mult, span, start, end):
        if span == "day":
            return [{"t": int(datetime.combine(x, datetime.min.time(), ET).timestamp() * 1000), "c": c}
                    for x, c in self.d[sym] if start <= x <= end]
        return [{"t": b.t, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v}
                for x, bars in self.h[sym].items() if start <= x <= end for b in bars]


class Src:
    def confirms(self, day, syms):
        return []

    def options_context(self, sym, ts):
        return {}


def _run(tmp_path, name, with_v2, break_v2=False):
    h = history(SYMS, DAYS, seed=13)
    s = Settings(True, SYMS, "wss://x", tmp_path / f"{name}.db", tmp_path / "cache", 5, 150, datetime.min.time(),
                 datetime.max.time(), 20, False, 180, with_v2, tmp_path / f"{name}_v2.db", False)

    class C:
        tz = ET
    end = datetime.combine(DAYS[-1], datetime.min.time().replace(hour=15, minute=30), ET).timestamp()
    v2db = V2DB(s.v2_db_path) if with_v2 else None
    r = LiveRunner(C(), s, FakeClient(h, daily(h)), ObserverDB(s.db_path), Src(), clock=lambda: end, v2db=v2db)
    if break_v2:
        import mias.observer.v2 as V
        orig = V.V2Observer.process
        V.V2Observer.process = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        asyncio.run(r.start_day(DAYS[-1]))
    finally:
        if break_v2:
            V.V2Observer.process = orig
    rows = [(w["watch_id"], w["state"], w["end_reason"], w["end_at"], w["price"]) for w in r.db.watches()]
    return rows, r


def test_v1_records_are_identical_with_v2_on_and_v2_errors_stay_in_v2(tmp_path):
    off, _ = _run(tmp_path, "off", False)
    on, r_on = _run(tmp_path, "on", True)
    broken, r_b = _run(tmp_path, "broken", True, break_v2=True)
    assert off == on == broken
    assert r_on.counts["v2_errors"] == 0 and r_b.counts["v2_errors"] > 0
    n = r_on.v2db.db.execute("SELECT COUNT(*) FROM v2_candidate_log").fetchone()[0]
    assert n > 0
