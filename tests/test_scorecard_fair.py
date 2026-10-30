"""Scorecard measurement layer: SPY-relative returns, bull/bear split, day coverage, evidence, live vs replay,
concentration, SPY-day regimes, option/stock separation, small-sample intervals. Report only."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from mias import scorecard
from mias.store import Store

ET = ZoneInfo("America/New_York")


def _t(d: date, h: int, m: int = 0) -> float:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp()


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "s.db")
    scorecard.ensure(s)
    return s


def _alert(store, ts, sym, rule, d, price, outcome, source="live"):
    store.db.execute("INSERT INTO signals(ts, symbol, rule, dir, price, outcome, source, done) VALUES(?,?,?,?,?,?,?,1)",
                     (ts, sym, rule, d, price, json.dumps(outcome), source))


def _move(d, p0, p1):
    return round((1 if d == "bull" else -1) * 100 * (p1 / p0 - 1), 3)


DAY = date(2026, 10, 19)                                    # Monday


def _one(tmp_path, sym, d, p0, p1, spy0, spy1):
    """One alert at 11:00 scored at 1h, with SPY checks at both ends."""
    store = _store(tmp_path)
    t0 = _t(DAY, 11)
    store.add_metrics(t0, "x", {"SPY": {"price": spy0, "day_change_pct": 0.0}})
    store.add_metrics(t0 + 3600, "x", {"SPY": {"price": spy1, "day_change_pct": 0.0}})
    _alert(store, t0, sym, "wall_break", d, p0, {"1h": {"price": p1, "move": _move(d, p0, p1)}})
    return store, scorecard.scored(store, 0, "live")[0]["h"]["1h"]


def test_bullish_excess_is_stock_minus_spy(tmp_path):
    assert scorecard.excess("bull", 2.0, 0.5) == pytest.approx(1.5)
    _, h = _one(tmp_path, "AAA", "bull", 100.0, 102.0, 500.0, 502.5)         # stock +2%, SPY +0.5%
    assert h["raw"] == pytest.approx(2.0) and h["spy"] == pytest.approx(0.5) and h["xs"] == pytest.approx(1.5)


def test_bearish_excess_is_direction_adjusted(tmp_path):
    assert scorecard.excess("bear", -2.0, -0.5) == pytest.approx(1.5)         # fell more than SPY: favourable
    assert scorecard.excess("bear", -0.2, -1.0) == pytest.approx(-0.8)        # fell less than SPY: unfavourable
    _, h = _one(tmp_path, "AAA", "bear", 100.0, 99.8, 500.0, 495.0)           # stock -0.2%, SPY -1%
    assert h["raw"] == pytest.approx(0.2) and h["xs"] == pytest.approx(-0.8)  # right on raw, wrong vs SPY


def test_spy_alerts_have_no_excess(tmp_path):
    store, h = _one(tmp_path, "SPY", "bull", 500.0, 502.5, 500.0, 502.5)
    assert h["bench"] and h["xs"] is None and h["raw"] == pytest.approx(0.5)
    text = scorecard.report(store, datetime.fromtimestamp(_t(DAY, 18), timezone.utc), 7, full=True)
    assert "N/A — benchmark is SPY" in text


def test_distinct_day_count_and_summary(tmp_path):
    store = _store(tmp_path)
    for i, (day, n) in enumerate(((DAY, 5), (DAY + timedelta(days=1), 2))):
        for k in range(n):
            _alert(store, _t(day, 10, k), "AAA", "trend_change", "bull", 100.0, {"1d": {"price": 101, "move": 1.0}})
    s = scorecard.summarize(scorecard.scored(store, 0, "live"), "1d")
    assert s["n"] == 7 and s["days"] == 2 and s["right"] == 100 and s["raw_med"] == 1.0
    assert s["xs_n"] == 0                                                       # no SPY checks: no excess invented


@pytest.mark.parametrize("alerts, days, label", [
    (43, 3, "VERY LIMITED"), (43, 5, "LIMITED"), (43, 9, "LIMITED"), (43, 10, "EARLY"), (43, 18, "EARLY"),
    (43, 20, "DEVELOPING"), (5000, 4, "VERY LIMITED"), (8, 12, "LIMITED"), (9, 25, "EARLY"), (2, 2, "VERY LIMITED")])
def test_evidence_label_is_driven_by_days(alerts, days, label):
    assert scorecard.evidence(alerts, days) == label


def test_live_and_replayed_are_reported_separately(tmp_path):
    store = _store(tmp_path)
    for k in range(3):
        _alert(store, _t(DAY, 10, k), "AAA", "pullback_entry", "bull", 100.0, {"1d": {"price": 101, "move": 1.0}})
    for k in range(5):
        _alert(store, _t(DAY - timedelta(days=7), 10, k), "AAA", "pullback_entry", "bear", 100.0,
               {"1d": {"price": 101, "move": -1.0}}, source="replay")
    now = datetime.fromtimestamp(_t(DAY, 18), timezone.utc)
    assert len(scorecard.scored(store, 0, "live")) == 3 and len(scorecard.scored(store, 0, "replay")) == 5
    text = scorecard.report(store, now, 7)
    assert "<b>LIVE</b> · 3 alerts" in text and "<b>REPLAYED</b> · 5 alerts" in text and "LIVE + REPLAYED" not in text
    rows = [r for r in text.split("<pre>")[1].split("</pre>")[0].splitlines() if r.startswith("Pullback")]
    assert [r.split()[1:3] for r in rows] == [["L", "3"], ["R", "5"]]
    only = scorecard.report(store, now, 7, source="replay")
    assert "REPLAYED" in only and "<b>LIVE</b>" not in only


def test_day_concentration():
    d = [date(2026, 10, i) for i in (19,) * 6 + (20,) * 3 + (21, 22)]
    c = scorecard.concentration(d)
    assert c["alerts"] == 11 and c["days"] == 4 and c["max_per_day"] == 6 and c["median_per_day"] == 2
    assert c["top1_pct"] == pytest.approx(600 / 11) and c["top3_pct"] == pytest.approx(1000 / 11)


def test_spy_day_regimes_and_grouping(tmp_path):
    store = _store(tmp_path)
    days = {DAY: 0.8, DAY + timedelta(days=1): -0.6, DAY + timedelta(days=2): 0.1}
    for day, chg in days.items():
        store.add_metrics(_t(day, 15, 55), "x", {"SPY": {"price": 500.0, "day_change_pct": chg}})
        store.add_metrics(_t(day, 18), "x", {"SPY": {"price": 500.0, "day_change_pct": -5.0}})   # after hours: ignored
        _alert(store, _t(day, 11), "AAA", "wall_break", "bear", 100.0, {"1d": {"price": 99, "move": 1.0}})
    reg = scorecard.spy_regimes(store)
    assert reg == {DAY: "UP", DAY + timedelta(days=1): "DOWN", DAY + timedelta(days=2): "FLAT"}
    g = scorecard.by_regime(scorecard.scored(store, 0, "live"), "1d")
    assert all(g[k]["n"] == 1 for k in scorecard.REGIMES)


def test_option_and_stock_scores_stay_separate(tmp_path):
    store = _store(tmp_path)
    _alert(store, _t(DAY, 11), "AAA", "flow", "bull", 100.0,
           {"1d": {"price": 101.0, "move": 1.0, "opt": 1.0, "opt_ret": -50.0}})
    _alert(store, _t(DAY, 12), "BBB", "flow", "bull", 100.0, {"1d": {"price": 102.0, "move": 2.0}})
    s = scorecard.summarize(scorecard.scored(store, 0, "live"), "1d")
    assert s["right"] == 100 and s["raw_mean"] == pytest.approx(1.5)            # stock unaffected by the option
    assert s["opt_n"] == 1 and s["opt_right"] == 0 and s["opt_mean"] == -50.0
    text = scorecard.report(store, datetime.fromtimestamp(_t(DAY, 18), timezone.utc), 7)
    assert "1d all: 100% right" in text and "1d option: 0% right" in text


def test_small_samples_get_no_interval(tmp_path):
    days = [date(2026, 10, 19)] * 40 + [date(2026, 10, 20)] * 40
    assert scorecard.clustered_ci([1.0] * 80, days) is None                    # 80 alerts, 2 days
    five = [date(2026, 10, 19 + i) for i in range(5) for _ in range(2)]
    assert scorecard.clustered_ci([1.0] * 9, five[:9]) is None                  # 9 alerts
    lo, hi = scorecard.clustered_ci([1.0] * 10, five)
    assert lo == pytest.approx(1.0) and hi == pytest.approx(1.0)
    vals = [1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 0.0]                   # days disagree
    lo, hi = scorecard.clustered_ci(vals, five)
    assert lo < 0.5 < hi
    store = _store(tmp_path)
    _alert(store, _t(DAY, 11), "AAA", "trend_change", "bull", 100.0, {"1d": {"price": 101, "move": 1.0}})
    text = scorecard.report(store, datetime.fromtimestamp(_t(DAY, 18), timezone.utc), 7)
    assert "95% CI: insufficient sample" in text


def test_clustered_interval_widens_when_days_disagree():
    days = [date(2026, 10, 19 + i) for i in range(6) for _ in range(5)]
    same = [1.0 if k % 2 else 0.0 for k in range(30)]                          # every day 40–60% right
    split = [1.0] * 15 + [0.0] * 15                                             # whole days right or wrong
    a, b = scorecard.clustered_ci(same, days), scorecard.clustered_ci(split, days)
    assert (b[1] - b[0]) > (a[1] - a[0])
