"""Scorecard, IV/earnings context, day-trade setups, swing backtest helpers and the morning brief."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from mias import config as config_mod
from mias import events, scorecard
from mias.indicators import Bar
from mias.store import Store

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setenv("MIAS_DB_PATH", str(tmp_path / "t.db"))
    return config_mod.load(ROOT / "config.toml")


def _ms(d: date, h: int, m: int) -> int:
    return int(datetime.combine(d, time(h, m), ET).timestamp() * 1000)


# ---- scorecard -----------------------------------------------------------------------------------

def test_scorecard_records_and_scores_from_history(tmp_path):
    store = Store(tmp_path / "s.db")
    t0 = datetime(2026, 10, 19, 11, 0, tzinfo=ET).timestamp()        # Monday 11:00
    prices = {t0: 100.0, t0 + 3600: 101.0, t0 + 86400: 99.0, t0 + 7 * 86400: 104.0}
    for ts, p in prices.items():
        store.add_metrics(ts, "x", {"AAA": {"price": p}})
    assert scorecard.record(store, t0, "AAA", "pullback_entry", "bull", 100.0, "O:AAA", 2.0)
    assert not scorecard.record(store, t0 + 600, "AAA", "pullback_entry", "bull", 100.5)      # repeat: skipped
    assert not scorecard.record(store, t0, "AAA", "alignment_ended", "neutral", 100.0)       # no direction
    now = datetime.fromtimestamp(t0 + 8 * 86400, timezone.utc)

    class Client:
        async def snapshots(self, tickers):
            return {}
    asyncio.run(scorecard.update(Client(), store, ET, now))
    st = scorecard.stats(store, t0 - 1)["pullback_entry"]
    assert st["n"] == 1 and st["1h"] == [1.0] and st["1d"] == [-1.0] and st["5d"] == [4.0]
    text = scorecard.report(store, now, 30)
    assert "Pullback entry" in text and "1h 100% right" in text and "1d 0% right" in text


def test_scorecard_due_times_cap_the_hour_at_the_close():
    t = datetime(2026, 10, 23, 15, 30, tzinfo=ET).timestamp()        # Friday 15:30
    d = scorecard.due(t, ET)
    assert datetime.fromtimestamp(d["1h"], ET).time() == time(16, 0)
    assert datetime.fromtimestamp(d["1d"], ET).date() == date(2026, 10, 26)     # next trading day
    assert datetime.fromtimestamp(d["5d"], ET).date() == date(2026, 10, 30)


# ---- events --------------------------------------------------------------------------------------

def _daily(gaps: dict[date, float], start: date, end: date) -> list[dict]:
    out, p, d = [], 100.0, start
    while d <= end:
        if d.weekday() < 5:
            g = gaps.get(d, 0.0)
            o = p * (1 + g)
            out.append({"t": int(datetime.combine(d, time(0, 0), ET).timestamp() * 1000), "o": o, "h": o * 1.01,
                        "l": o * 0.99, "c": o, "v": 3e6 if g else 1e6})
            p = o
        d += timedelta(days=1)
    return out


def test_earnings_estimate_from_quarterly_gaps():
    gaps = {date(2026, 1, 29): 0.05, date(2026, 4, 30): -0.06, date(2026, 7, 30): 0.04}
    daily = _daily(gaps, date(2025, 11, 1), date(2026, 10, 1))
    e = events.earnings_estimate("AAA", daily, ET, date(2026, 10, 1))
    assert e == {"last": "2026-07-30", "next": "2026-10-29", "quality": "confirmed"}
    assert events.earnings_estimate("SPY", daily, ET, date(2026, 10, 1)) is None       # funds have none
    assert events.earnings_estimate("AAA", daily, ET, date(2026, 11, 20)) is None      # overdue: no guess


def test_iv_event_found_in_term_structure():
    today = date(2026, 10, 26)
    calm = [("2026-10-30", 4, 30.0), ("2026-11-06", 11, 30.5), ("2026-11-13", 18, 30.0), ("2026-11-20", 25, 31.0),
            ("2026-12-18", 53, 31.0)]
    assert events.iv_event(calm, today) is None
    bump = [("2026-10-30", 4, 31.0), ("2026-11-06", 11, 62.0), ("2026-11-13", 18, 50.0), ("2026-11-20", 25, 45.0),
            ("2026-12-18", 53, 38.0)]
    ev = events.iv_event(bump, today)
    assert ev["by"] == "2026-11-06" and ev["after"] == "2026-10-30" and ev["move"] > 4


def test_iv_state_and_percentile():
    hist = {f"d{i}": float(20 + i) for i in range(30)}
    assert events.iv_percentile(hist, 45.0) == (87, 30)          # 26 of 30 values <= 45
    assert events.iv_percentile({"a": 1.0}, 2.0) == (None, 1)
    assert events.iv_state(40, 30, None) == "rich"
    assert events.iv_state(25, 30, 20) == "cheap"
    assert events.iv_state(30, 30, 75) == "rich"


def test_advice_follows_iv_state_and_warns_on_earnings():
    from mias.options import Idea
    idea = Idea("call", "2026-12-18", 53, 200, 0.6, 12.0, 3.0, 500, 100, 45.0, 220, 4.0, 0.3)
    today = date(2026, 10, 26)
    ctx = {"iv_state": "rich", "iv_pct": 82, "earnings": {"next": "2026-11-19", "quality": "confirmed"},
           "iv_event": {"by": "2026-11-20", "move": 8.0}}
    lines = events.advice(idea, ctx, today)
    assert "IV is expensive, 82th percentile" in lines[0] and "size down" in lines[0]
    assert "Spread: 200/220C debit 8.00" in lines[0]
    assert any("Earnings ~Nov 19" in x and "before this expiry" in x for x in lines)
    assert any("event by Nov 20" in x for x in lines)
    soon = events.advice(idea, dict(ctx, iv_state="fair", earnings={"next": "2026-10-29", "quality": "rough"}),
                         today)
    assert "best conditions" in soon[0] and any("consider waiting" in x for x in soon)
    assert "below realized" in events.advice(idea, dict(ctx, iv_state="cheap"), today)[0]


# ---- day-trade setups ----------------------------------------------------------------------------

def _day_bars(day: date, closes: list[float], vols: list[float] | None = None, start=(9, 30)) -> list[Bar]:
    t = _ms(day, *start)
    out = []
    p = closes[0]
    for n, c in enumerate(closes):
        o = p
        out.append(Bar(t + n * 300_000, o, max(o, c) + 0.05, min(o, c) - 0.05, c, (vols or [1000] * len(closes))[n]))
        p = c
    return out


def test_orb_breakout_needs_volume_and_fires_once():
    from mias.daysetups import orb
    from mias.emacross import DayTradeRules
    prev = _day_bars(date(2026, 10, 23), [100.0] * 78)
    closes = [100, 100.4, 99.8, 100.1, 100.3, 101.2, 101.5, 99.0]
    vols = [1000, 1000, 1000, 800, 900, 3000, 2000, 5000]
    today = _day_bars(date(2026, 10, 26), closes, vols)
    setups = orb("AAA", prev + today, date(2026, 10, 26), ET, DayTradeRules())
    assert [(s.side, s.status) for s in setups] == [("bull", "entry"), ("bear", "entry")]
    assert setups[0].price == 101.2 and setups[0].invalidation == pytest.approx((100.45 + 99.75) / 2)


def test_vwap_reclaim_and_pdhl():
    from mias.daysetups import pdhl, vwap_reclaim
    from mias.emacross import DayTradeRules
    prev = _day_bars(date(2026, 10, 23), [100.0] * 77 + [100.0])
    closes = [100, 99.5, 99.0, 98.8, 98.7, 98.9, 99.8, 100.4, 101.0]
    today = _day_bars(date(2026, 10, 26), closes)
    rec = vwap_reclaim("AAA", prev + today, date(2026, 10, 26), ET, DayTradeRules())
    assert rec and rec[0].side == "bull" and rec[0].status == "entry"
    brk = pdhl("AAA", prev + today, date(2026, 10, 26), ET, DayTradeRules())
    assert [s.side for s in brk] == ["bear", "bull"]        # broke the prior low first, then the high
    assert brk[0].status == "outside_hours"                  # 09:40 is before the first entry time (09:45)
    assert brk[1].status == "entry" and brk[1].price > 100.05


def test_trade_slippage_costs_both_ways():
    from mias.emacross import DayTradeRules, Setup, Trade
    su = Setup("SPY", "bull", 0, "entry", 0, 760.0, 759.0)
    tr = Trade.open(su, "O:X", 760, "2026-10-26", 0, 2.0, DayTradeRules(), slip=0.01)
    assert tr.entry == 2.02
    tr.exit(1, 2.02, "time")
    assert tr.pnl == pytest.approx(-0.0202 * 5 * 100)


# ---- swing backtest helpers ----------------------------------------------------------------------

def test_black_scholes_round_trip_and_delta_strike():
    from mias.swingtest import _bs, implied_vol, strike_for_delta
    p = _bs(100, 105, 0.25, 0.4, True)
    assert implied_vol(p, 100, 105, 0.25, True) == pytest.approx(0.4, abs=1e-4)
    k = strike_for_delta(100, 0.25, 0.4, 0.6, True)
    assert 93 < k < 100                                   # in the money for 0.60 delta
    assert strike_for_delta(100, 0.25, 0.4, 0.6, False) > 100


def test_manage_exits():
    from mias.swingtest import manage
    b = lambda t, o, h, l, c: Bar(t, o, h, l, c, 1)
    path = [b(1, 2.0, 2.2, 1.9, 2.1), b(2, 2.1, 3.1, 2.0, 3.0)]
    assert manage(path, 1, 2.0, 3.0, 1.0, 99, None) == (3.0, "target", 2)
    assert manage([b(1, 2.0, 2.1, 0.9, 1.0)], 1, 2.0, 3.0, 1.0, 99, None) == (1.0, "stop", 1)
    assert manage(path, 1, 2.0, 3.0, 1.0, 99, 2)[1] == "trend"
    assert manage(path, 1, 2.0, 9.0, 0.1, 1, None) == (2.1, "time", 2)


def test_pullback_entries_from_a_score_series(cfg):
    from mias.swingtest import entries
    d = date(2026, 10, 19)
    series = []
    intra = [10, -20, -25, 10, 36, 40, 40]
    for n, x in enumerate(intra):
        series.append((_ms(d, 10, 0) + n * 1_800_000, 100.0, 45.0, float(x), 30.0))
    es = entries("AAA", series, cfg, ET)
    pb = [e for e in es if e.group == "pullback"]
    assert len(pb) == 1 and pb[0].side == "bull" and pb[0].ms == series[5][0]


# ---- brief ---------------------------------------------------------------------------------------

def test_brief_read_measures_the_open():
    from mias.brief import read
    d0, d1 = date(2026, 10, 23), date(2026, 10, 26)
    raw = []
    for d, base, vol in ((d0, 100.0, 1000), (d1, 102.0, 3000)):
        for n in range(78 if d == d0 else 3):
            raw.append({"t": _ms(d, 9, 30) + n * 300_000, "o": base, "h": base + 0.5, "l": base - 0.5,
                        "c": base + (0.3 if d == d1 else 0), "v": vol})
    x = read("AAA", raw, [], d1, ET)
    assert x["gap"] == pytest.approx(2.0) and x["rvol"] == pytest.approx(3.0)
    assert x["pdh"] == 100.5 and x["orb_high"] == 102.5 and x["move15"] == pytest.approx(100 * 0.3 / 102)


def test_scorecard_short_horizons_need_a_nearby_check(tmp_path):
    store = Store(tmp_path / "s.db")
    t0 = datetime(2026, 10, 19, 11, 0, tzinfo=ET).timestamp()
    for dt, p in ((0, 100.0), (900, 100.5), (1800, 99.0)):
        store.add_metrics(t0 + dt, "x", {"AAA": {"price": p}})
    scorecard.record(store, t0, "AAA", "flow_confirmed", "bear", 100.0)
    asyncio.run(scorecard.update(None, store, ET, datetime.fromtimestamp(t0 + 7200, timezone.utc)))
    st = scorecard.stats(store, t0 - 1)["flow_confirmed"]
    assert st["15m"] == [-0.5] and st["30m"] == [1.0] and st["1h"] == []      # no check within 20 min of 12:00
    text = scorecard.report(store, datetime.fromtimestamp(t0 + 7200, timezone.utc), 7)
    assert "Flow, price confirms" in text and "15m 0% right" in text and "30m 100% right" in text
