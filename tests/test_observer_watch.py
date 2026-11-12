"""Early Watch v1 state machine: creation on 2 closes, invalidation rules, expiry, upgrade, cooldown, chop lock,
caps, one watch per leg, the 09:30-09:45 research-only lane, and the Telegram formatter (prepared, never sent)."""
import dataclasses
from datetime import date, datetime

import pytest

from mias.indicators import Bar
from mias.observer.message import watch_text
from mias.observer.rules import FIVE, ONE
from mias.observer.state import Features
from mias.observer.structure import Candidate, Pivot
from mias.observer.watch import Observer
from observer_synth import ET

DAY = date(2026, 11, 11)


def ms(h, m):
    return int(datetime.combine(DAY, datetime.min.time().replace(hour=h, minute=m), ET).timestamp() * 1000)


H1 = Pivot("H", 102.0, ms(9, 50), ms(10, 5), "HH")
L0 = Pivot("L", 99.0, ms(9, 35), ms(9, 50), "HL")
H2 = Pivot("H", 103.0, ms(11, 0), ms(11, 15), "HH")


def hl(anchor=H1, level=100.3):
    return Candidate("bull", level, anchor, L0, ms(10, 6))


def lh(level=100.6):
    return Candidate("bear", level, Pivot("L", 99.5, ms(10, 0), ms(10, 15), "LL"),
                     Pivot("H", 101.5, ms(9, 40), ms(9, 55), "LH"), ms(10, 16))


def feat(h, m, sym="META", **kw):
    t = ms(h, m)
    base = dict(sym=sym, t=t, e=t + ONE, o=101, h=101.2, l=100.8, c=101.0, v=1000, vwap=100.0, ema9=100.6,
                ema21=100.2, spread=0.4, spread_ref=0.3, ema9_c=100.5, ema21_c=100.2, closed5=False, atr=1.0,
                or_high=101.5, or_low=99.0, or_done=True, hl=hl(), lh=None, stale=False, day_pct=0.5,
                minutes_since_open=(t + ONE - ms(9, 30)) / ONE, rel_vol=1.1, sma200=90.0, rsi14=60.0,
                macd_hist=0.05)
    base.update(kw)
    return Features(**base)


BEAR = dict(c=99.2, vwap=100.0, ema9=100.1, ema21=100.4, spread=-0.3, spread_ref=-0.2, hl=None, lh=lh())


class Feed:
    def __init__(self, ob):
        self.ob = ob
        self.q = {}
        for s, st in ob.states.items():
            st.on_minute = (lambda sym: (lambda bar, gap=False: self.q[sym].pop(0)))(s)

    def run(self, feats):
        out = []
        for f in feats:
            self.q.setdefault(f.sym, []).append(f)
            out += self.ob.process(f.sym, Bar(f.t, f.o, f.h, f.l, f.c, f.v))
        return out


def obs(symbols=("META",)):
    ob = Observer(list(symbols), DAY, ET)
    return ob, Feed(ob)


def kinds(evs, kind):
    return [e for e in evs if e["kind"] == kind]


def test_watch_needs_two_consecutive_qualifying_closes():
    ob, feed = obs()
    evs = feed.run([feat(10, 10)])
    assert kinds(evs, "watch") == []
    evs = feed.run([feat(10, 11)])
    w = kinds(evs, "watch")
    assert len(w) == 1 and w[0]["side"] == "bull" and w[0]["structure_type"] == "HL"
    assert w[0]["created_at"] == ms(10, 12) and w[0]["level"] == 100.3 and w[0]["lane"] == "main"
    assert w[0]["leg_id"] == f"P{H1.bar_t}" and not w[0]["opening_shadow_only"]


def test_one_close_below_vwap_breaks_the_pair():
    ob, feed = obs()
    evs = feed.run([feat(10, 10), feat(10, 11, c=99.9), feat(10, 12)])
    assert kinds(evs, "watch") == []


@pytest.mark.parametrize("bad,reason", [
    (dict(c=100.2), "close_beyond_level"),
    (dict(closed5=True, ema9_c=100.1, ema21_c=100.3), "5m_ema_flip"),
])
def test_invalidation(bad, reason):
    ob, feed = obs()
    feed.run([feat(10, 10), feat(10, 11)])
    evs = feed.run([feat(10, 12, **bad)])
    end = kinds(evs, "end")
    assert end and end[0]["state"] == "INVALIDATED" and end[0]["end_reason"] == reason


def test_two_closes_below_vwap_invalidate():
    ob, feed = obs()
    feed.run([feat(10, 10), feat(10, 11)])
    level_ok = dict(c=100.4, vwap=100.5)                   # above the 100.3 level, below VWAP
    assert kinds(feed.run([feat(10, 12, **level_ok)]), "end") == []
    end = kinds(feed.run([feat(10, 13, **level_ok)]), "end")
    assert end[0]["end_reason"] == "two_closes_beyond_vwap"


def test_bearish_flip_invalidates_then_cooldown_blocks_the_bear_watch():
    """Bearish criteria on 2 closes imply 2 closes below VWAP, so that rule (checked first) is the reason."""
    ob, feed = obs()
    feed.run([feat(10, 10), feat(10, 11)])
    bear = dict(BEAR, c=100.5, vwap=100.8)                 # above the 100.3 level, bearish otherwise
    evs = feed.run([feat(10, 12, **bear), feat(10, 13, **bear)])
    end = kinds(evs, "end")
    assert end[0]["state"] == "INVALIDATED" and end[0]["end_reason"] == "two_closes_beyond_vwap"
    assert kinds(evs, "watch") == []
    assert [s["reason"] for s in kinds(evs, "suppressed")] == ["cooldown"]


def test_expiry_after_60_minutes():
    ob, feed = obs()
    feed.run([feat(10, 10), feat(10, 11)])
    evs = feed.run([feat(10 + (12 + i) // 60, (12 + i) % 60) for i in range(0, 59)])   # 10:12 .. 11:10
    assert kinds(evs, "end") == []
    end = kinds(feed.run([feat(11, 11)]), "end")           # ends 11:12 = 60 min after the 10:12 watch
    assert end[0]["state"] == "EXPIRED" and end[0]["end_at"] == ms(11, 12)


def test_upgrade_by_a_confirmed_event_within_60_minutes():
    ob, feed = obs()
    feed.run([feat(10, 10), feat(10, 11)])
    feed.run([feat(10, 12, c=101.4)])
    ob.add_confirm({"sym": "META", "side": "bull", "ts": ms(10, 13) / 1000 + 14, "source": "D"})
    evs = feed.run([feat(10, 13, c=101.6), feat(10, 14, c=101.9)])
    end = kinds(evs, "end")
    assert end[0]["state"] == "UPGRADED" and end[0]["upgrade_source"] == "D"
    assert end[0]["upgrade_price"] == 101.4                # the 10:12 bar (ends 10:13) is the last one before 10:13:14
    assert end[0]["upgrade_at"] == ms(10, 13) + 14_000


def test_a_confirmed_event_before_the_watch_or_other_side_is_not_an_upgrade():
    ob, feed = obs()
    ob.add_confirm({"sym": "META", "side": "bull", "ts": ms(10, 5) / 1000, "source": "B"})
    ob.add_confirm({"sym": "META", "side": "bear", "ts": ms(10, 13) / 1000, "source": "B"})
    evs = feed.run([feat(10, 10), feat(10, 11), feat(10, 12), feat(10, 13), feat(10, 14)])
    assert kinds(evs, "end") == []


def test_one_watch_per_leg_and_cooldown_then_new_leg():
    ob, feed = obs()
    feed.run([feat(10, 10), feat(10, 11)])
    evs = feed.run([feat(10, 12, c=100.2)])                # invalidated at 10:13: cooldown until 10:43
    evs += feed.run([feat(10, 20), feat(10, 21)])
    assert kinds(evs, "watch") == [] and {s["reason"] for s in kinds(evs, "suppressed")} == {"cooldown"}
    evs = feed.run([feat(10, 50), feat(10, 51)])           # cooldown over, same leg H1
    assert kinds(evs, "watch") == [] and {s["reason"] for s in kinds(evs, "suppressed")} == {"same_leg"}
    evs = feed.run([feat(11, 20, hl=hl(H2, 102.0), c=102.5), feat(11, 21, hl=hl(H2, 102.0), c=102.6)])
    assert len(kinds(evs, "watch")) == 1                    # a new swing leg re-arms


def test_chop_lock_after_two_invalidations_in_60_minutes():
    ob, feed = obs()
    feed.run([feat(10, 10), feat(10, 11), feat(10, 12, c=100.2)])           # invalidation 1 at 10:13
    h3 = Pivot("H", 102.5, ms(10, 30), ms(10, 45), "HH")
    feed.run([feat(10, 46, hl=hl(h3)), feat(10, 47, hl=hl(h3)), feat(10, 48, hl=hl(h3), c=100.2)])  # 2 at 10:49
    h4 = Pivot("H", 103.5, ms(11, 0), ms(11, 15), "HH")
    evs = feed.run([feat(11, 25, hl=hl(h4), c=102), feat(11, 26, hl=hl(h4), c=102)])
    assert {s["reason"] for s in kinds(evs, "suppressed")} == {"chop_lock"}  # cooldown ended 11:19, lock 11:49
    evs = feed.run([feat(11, 50, hl=hl(h4), c=102), feat(11, 51, hl=hl(h4), c=102)])
    assert len(kinds(evs, "watch")) == 1


def test_symbol_cap_three_per_day():
    ob, feed = obs()
    made = 0
    for i, (h, m) in enumerate([(10, 0), (11, 0), (12, 0), (13, 0)]):
        p = Pivot("H", 102 + i, ms(h, m) - 30 * ONE, ms(h, m) - 15 * ONE, "HH")
        evs = feed.run([feat(h, m, hl=hl(p)), feat(h, m + 1, hl=hl(p))])
        made += len(kinds(evs, "watch"))
        ob.add_confirm({"sym": "META", "side": "bull", "ts": (ms(h, m) + 5 * ONE) / 1000, "source": "B"})
        feed.run([feat(h, m + 6, hl=hl(p))])               # upgraded: no cooldown
        if i == 3:
            assert {s["reason"] for s in kinds(evs, "suppressed")} == {"symbol_cap"}
    assert made == 3


def test_hourly_cap_six_across_the_universe():
    syms = ["S%d" % i for i in range(8)]
    ob, feed = obs(syms)
    evs = []
    for k in range(2):
        evs += feed.run([feat(10, 10 + k, sym=s) for s in syms])
    assert len(kinds(evs, "watch")) == 6
    assert [s["reason"] for s in kinds(evs, "suppressed")] == ["hourly_cap", "hourly_cap"]


def test_no_main_watch_after_1500_and_stale_blocks():
    ob, feed = obs()
    evs = feed.run([feat(15, 0), feat(15, 1)])
    assert kinds(evs, "watch") == [] and kinds(evs, "suppressed")[0]["reason"] == "after_15_00"
    ob2, feed2 = obs()
    evs = feed2.run([feat(10, 10), feat(10, 11, stale=True)])
    assert kinds(evs, "watch") == [] and kinds(evs, "suppressed")[0]["reason"] == "stale"


def test_opening_lane_is_research_only_and_independent():
    ob, feed = obs()
    evs = feed.run([feat(9, 40, or_done=False), feat(9, 41, or_done=False)])
    w = kinds(evs, "watch")
    assert len(w) == 1 and w[0]["lane"] == "opening" and w[0]["opening_shadow_only"]
    evs = feed.run([feat(9, 42, or_done=False, c=100.2)])     # invalidated in the opening lane
    assert kinds(evs, "end")[0]["lane"] == "opening"
    assert ob.lanes["main"]["META"].cooldown_until == 0      # the main lane is untouched
    evs = feed.run([feat(9, 45), feat(9, 46)])               # main lane may still use the same leg
    assert [x["lane"] for x in kinds(evs, "watch")] == ["main"]


def test_bearish_mirror_and_orb():
    ob, feed = obs()
    evs = feed.run([feat(10, 10, **BEAR), feat(10, 11, **BEAR)])
    w = kinds(evs, "watch")
    assert w[0]["side"] == "bear" and w[0]["structure_type"] == "LH" and w[0]["level"] == 100.6
    ob2, feed2 = obs()
    orb = dict(hl=None, c=101.8)
    evs = feed2.run([feat(10, 10, **orb), feat(10, 11, **orb)])
    w = kinds(evs, "watch")
    assert w[0]["structure_type"] == "ORB" and w[0]["level"] == 101.5 and w[0]["leg_id"] == "OR2026-11-11"


def test_telegram_text_is_watch_only():
    ob, feed = obs()
    w = kinds(feed.run([feat(10, 10), feat(10, 11)]), "watch")[0]
    text = watch_text(w, ET)
    assert text.startswith("👀 EARLY WATCH · META · BULLISH · 10:12 ET @ $101.00")
    assert "WATCH ONLY — NOT A TRADE SIGNAL" in text and "Invalid below: $100.30" in text
    for word in ("strike", "expir", "contract", "target", "stop", "buy", "sell"):
        assert word not in text.lower()


def test_observer_never_uses_the_mias_outbox_or_notifier():
    import pathlib
    src = "".join(p.read_text() for p in pathlib.Path("mias/observer").glob("*.py"))
    assert "enqueue(" not in src and "Dispatcher" not in src and "from ..notify" not in src
    others = [p for p in pathlib.Path("mias/observer").glob("*.py") if p.name != "telegram.py"]
    assert not any("api.telegram.org" in p.read_text() for p in others)   # one sender, in telegram.py only
