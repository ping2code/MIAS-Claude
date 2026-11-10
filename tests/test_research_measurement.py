"""Measurement-only fixes (2026-11-10): entry-time SPY without look-ahead, deterministic setup ids, the setup-level
Paper Scorecard view, and Simple's exit spread in its trade log. No strategy behaviour is involved."""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from mias import emacross, paperscore as P, simple
from mias.indicators import Bar
from mias.research import SpyAtEntry, direction
from mias.store import Store
from test_paper_scorecard import _t

ET = ZoneInfo("America/New_York")


def _ms(d: str, hms: str) -> int:
    return int(datetime.fromisoformat(f"{d}T{hms}").replace(tzinfo=ET).timestamp() * 1000)


def _bar(d, hm, c):
    return Bar(_ms(d, hm + ":00"), c, c, c, c, 1000)


@pytest.fixture
def spy():
    bars = [_bar("2026-11-05", "15:58", 99.0), _bar("2026-11-05", "15:59", 100.0),        # prev close 100
            _bar("2026-11-06", "09:30", 101.0), _bar("2026-11-06", "09:31", 102.0), _bar("2026-11-06", "09:32", 98.0)]
    return SpyAtEntry(bars, ET)


def test_spy_at_entry_reads_only_completed_minutes(spy):
    assert spy.at(_ms("2026-11-06", "09:30:59"))["pct"] is None              # no minute of the day has ended
    a = spy.at(_ms("2026-11-06", "09:31:00"))                                # the 09:30 bar ended exactly now
    assert (a["price"], a["prev_close"], a["pct"], a["dir"]) == (101.0, 100.0, 1.0, "UP")
    assert spy.at(_ms("2026-11-06", "09:32:30"))["price"] == 102.0          # 09:32 bar is still open: not read
    assert spy.at(_ms("2026-11-06", "09:33:00"))["dir"] == "DOWN"
    assert spy.at(_ms("2026-11-05", "16:00:00"))["prev_close"] is None      # no earlier day in the series


def test_spy_at_entry_never_changes_when_later_bars_are_added(spy):
    before = spy.at(_ms("2026-11-06", "09:32:10"))
    later = SpyAtEntry(spy.bars + [_bar("2026-11-06", "09:33", 50.0), _bar("2026-11-06", "15:59", 10.0)], ET)
    assert later.at(_ms("2026-11-06", "09:32:10")) == before                # future data cannot leak in


def test_direction_has_no_threshold():
    assert [direction(x) for x in (0.0001, -0.0001, 0.0, None)] == ["UP", "DOWN", "FLAT", None]


def test_setup_ids_are_deterministic_and_ignore_strategy_names():
    rows = [_t("D", "2026-11-03", "MSFT", "bull", 100, hhmm="10:10:01"), _t("C", "2026-11-03", "MSFT", "bull", 50,
                                                                            hhmm="10:10:02"),
            _t("E7", "2026-11-03", "MSFT", "bull", 10, hhmm="10:15"), _t("E21", "2026-11-03", "MSFT", "bull", 1,
                                                                         hhmm="10:24:59"),
            _t("B", "2026-11-03", "MSFT", "bear", 7, hhmm="10:11"), _t("D", "2026-11-04", "AMD", "bull", -100)]
    ids = [r["setup_id"] for r in P.with_setups([dict(r) for r in rows], ET)]
    assert ids[:4] == ["2026-11-03_MSFT_BULL_101001"] * 4                   # chained, each within 10 min of the last
    assert ids[4] == "2026-11-03_MSFT_BEAR_101100" and ids[5] == "2026-11-04_AMD_BULL_100000"
    shuffled = [dict(r) for r in reversed(rows)]
    assert [r["setup_id"] for r in P.with_setups(shuffled, ET)] == list(reversed(ids))
    renamed = [dict(r, code="X") for r in rows]
    assert [r["setup_id"] for r in P.with_setups(renamed, ET)] == ids


def test_setup_stats_exposure_concentration_and_count_once():
    rows = [_t("C", "2026-11-03", "MSFT", "bull", 1000, hhmm="10:00", premium=1000, hold=30),
            _t("D", "2026-11-03", "MSFT", "bull", 600, hhmm="10:00", premium=2000, hold=30),
            _t("E7", "2026-11-03", "MSFT", "bull", -200, hhmm="10:05", premium=3000, hold=5),
            _t("D", "2026-11-03", "AMD", "bull", -500, hhmm="10:20", premium=500, hold=10)]
    s = P.setup_stats(rows)
    assert (s["obs"], s["setups"], s["multi"], s["sizes"]) == (4, 2, 1, {1: 1, 3: 1})
    assert s["premium"] == 6500 and s["largest"][0] == 6000
    assert s["concurrent"][0] == 6000                                        # 10:05-10:10 all three MSFT legs
    net = [r["net"] for r in rows]
    assert s["best"][0] == pytest.approx(sum(net[:3])) and s["worst"][0] == pytest.approx(net[3])
    assert s["top1_share"] == pytest.approx(100.0) and s["no_top1"] == pytest.approx(net[3])
    c = P.count_once(rows[:3])
    assert c["all"] == pytest.approx(sum(net[:3])) and c["mean"] == pytest.approx(sum(net[:3]) / 3)
    assert c["first"] == pytest.approx(net[0])                               # same second: C before D, a convention


def test_max_concurrent_releases_exposure_at_exit():
    rows = [_t("D", "2026-11-03", "SPY", "bull", 0, hhmm="10:00", premium=100, hold=10),
            _t("D", "2026-11-03", "QQQ", "bull", 0, hhmm="10:10", premium=100, hold=10)]
    assert P.max_concurrent(rows)[0] == 100                                  # the first closed as the second opened


def test_paper_rows_carry_premium_and_exit_time(tmp_path):
    store = Store(tmp_path / "s.db")
    store.put(emacross.LOG_KEY, [{"date": "2026-11-02", "symbol": "SPY", "side": "bull", "pnl": 100.0, "entry": 2.0,
                                  "exit_reason": "stop", "fill_at": "10:00:01", "minutes": 6.0,
                                  "strategy": "ema_9_21_d", "contracts": 5}])
    cfg = NS(tz=ET, daytrade=NS(fee_per_contract=0.65, contracts=5))
    r = P.trades(store, cfg)[0]
    assert r["premium"] == 1000 and r["exit_ts"] - r["ts"] == 360


def _cfg():
    return NS(tz=ET, daytrade=emacross.DayTradeRules(symbols=("SPY",), contracts=5))


def test_simple_logs_exit_spread_without_changing_the_fill(tmp_path):
    store = Store(tmp_path / "s.db")
    cfg = _cfg()
    rules = simple.rules_of(cfg)
    now = datetime(2026, 11, 9, 9, 41, 1, tzinfo=ET)
    sg = {"symbol": "SPY", "side": "bull", "t": _ms("2026-11-09", "09:30:00"), "close_t": _ms("2026-11-09", "09:35:00"),
          "price": 743.37}
    tr = emacross.Trade.open(simple.setup_of(sg), "O:SPY261109C00743000", 743, "2026-11-09",
                             _ms("2026-11-09", "09:35:01"), 3.12, rules)
    tr.entry_mid, tr.info = 3.11, {"n": 1}
    from dataclasses import asdict
    st = {"date": "2026-11-09", "open": {"SPY#1": asdict(tr)}, "closed": []}
    snap = {"O:SPY261109C00743000": {"last_quote": {"bid": 3.90, "ask": 3.96, "midpoint": 3.93}}}
    simple._manage(snap, store, cfg, now, st)
    rec = store.get(simple.LOG_KEY)[0]
    assert rec["exit_reason"] == "target" and rec["exit_spread_pct"] == pytest.approx(round(100 * 0.06 / 3.93, 1))
    assert rec["pnl"] == pytest.approx((3.90 - 3.12) * 5 * 100)            # sold at the bid, capped at the target
