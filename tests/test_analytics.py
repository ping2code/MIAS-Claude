from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from mias import indicators as ind
from mias.indicators import Bar
from mias.options import Window, analyze, implied_spot, options_bias, parse_contract
from mias.trend import complete_bars, direction, label_for, resample, stock_trend, timeframe_trend, to_bars

from fakes import chain, contract, daily_bars, session_5m_bars, trading_days

ET = ZoneInfo("America/New_York")
TODAY = date(2026, 10, 7)
NOW = datetime.combine(TODAY, time(15, 0), ET)
NOW_NS = int(NOW.timestamp() * 1e9)


# ---- indicators --------------------------------------------------------------------------------

def test_rsi_extremes():
    assert ind.rsi([float(i) for i in range(30)]) == 100.0
    assert ind.rsi([float(30 - i) for i in range(30)]) == pytest.approx(0.0)
    assert ind.rsi([1.0] * 5) is None


def test_ema_and_slope():
    xs = [float(i) for i in range(50)]
    assert ind.ema(xs, 9)[-1] < 49 and ind.ema(xs, 9)[-1] > ind.ema(xs, 21)[-1]
    assert ind.linreg_slope(xs) == pytest.approx(1.0)


def test_adx_high_in_trend_low_in_chop():
    trend = [Bar(i, i, i + 1.2, i - 0.2, i + 1, 1) for i in range(60)]
    chop = [Bar(i, 10, 11, 9, 10 + (0.5 if i % 2 else -0.5), 1) for i in range(60)]
    assert ind.adx(trend) > 40
    assert ind.adx(chop) < 20


# ---- trend -------------------------------------------------------------------------------------

PROFILES = {"swing": {"1w": .35, "1d": .45, "1h": .2}, "intraday": {"5m": .25, "15m": .3, "1h": .3, "1d": .15}}


def _trend(drift, noise=0.0, daily_drift=None):
    days = trading_days(TODAY, 25)
    five = session_5m_bars(days, 100, drift, noise)
    dd = drift * 78 if daily_drift is None else daily_drift
    daily = daily_bars(trading_days(TODAY - timedelta(days=1), 250), 1000, dd)
    return stock_trend(five, daily, tz=ET, available_until_ms=int(NOW.timestamp() * 1000),
                       profiles=PROFILES, primary="swing", strong=60, bias=25)


def test_uptrend_downtrend_chop():
    up, down, chop = _trend(0.05), _trend(-0.05), _trend(0.0, noise=0.3, daily_drift=0.0)
    assert up.score > 60 and up.label == "strong_up" and up.composites["intraday"][1] == "strong_up"
    assert down.score < -60 and down.label == "strong_down"
    assert abs(chop.composites["intraday"][0]) < 40
    assert set(up.timeframes) == {"5m", "15m", "1h", "1d", "1w"}
    assert up.profile == "swing" and up.hv20 is not None


def test_swing_and_intraday_can_disagree():
    t = _trend(-0.05, daily_drift=3.0)      # falling today, rising for months
    assert t.composites["swing"][0] > 25 and t.composites["intraday"][0] < t.composites["swing"][0]


def test_weekly_and_realized_vol():
    from mias.trend import realized_vol, weekly
    daily = to_bars(daily_bars(trading_days(TODAY, 50), 100, 1.0))
    wk = weekly(daily, ET)
    assert 10 <= len(wk) <= 11 and wk[-1].c == daily[-1].c
    assert realized_vol(daily) == pytest.approx(0.0, abs=1.0)        # constant drift: almost no variance
    assert realized_vol(daily[:5]) is None


def test_incomplete_bars_are_dropped():
    bars = to_bars(session_5m_bars([TODAY], 100, 0.1))
    cutoff = int(datetime.combine(TODAY, time(10, 0), ET).timestamp() * 1000)
    kept = complete_bars(bars, 5, cutoff)
    assert len(kept) == 6     # 09:30 .. 09:55 closed by 10:00


def test_resample_aligns_to_open():
    bars = to_bars(session_5m_bars([TODAY], 100, 0.1))
    hourly = resample(bars, 60, ET)
    assert len(hourly) == 7    # 09:30, 10:30, ... 15:30 (half bar)
    assert hourly[0].o == bars[0].o and hourly[0].c == bars[11].c


def test_too_few_bars():
    assert timeframe_trend("5m", to_bars(session_5m_bars([TODAY], 100, 0.1))[:10], strong=60, bias=25) is None


def test_labels():
    assert label_for(70, 60, 25) == "strong_up" and label_for(-30, 60, 25) == "down"
    assert direction("strong_bullish") == "bull" and direction("bearish") == "bear"
    assert direction("neutral") == "neutral"


# ---- options -----------------------------------------------------------------------------------

def test_implied_spot_recovers_live_price():
    items = chain("X", 105.37, TODAY, NOW_NS)
    cs = [parse_contract(i, TODAY, ET) for i in items]
    assert implied_spot(cs, 100.0, NOW_NS) == pytest.approx(105.37, abs=0.01)


def test_implied_spot_rejects_stale_quotes():
    items = chain("X", 105.0, TODAY, NOW_NS - 3600 * 10**9)
    cs = [parse_contract(i, TODAY, ET) for i in items]
    assert implied_spot(cs, 105.0, NOW_NS) is None


def test_old_day_record_is_not_today_volume():
    c = parse_contract(contract("X", "call", 100, TODAY + timedelta(days=5), spot=100, volume=500,
                                now_ns=NOW_NS, day_today=False), TODAY, ET)
    assert c.volume == 0


def test_expired_contracts_ignored():
    assert parse_contract(contract("X", "call", 100, TODAY - timedelta(days=1), spot=100, now_ns=NOW_NS),
                          TODAY, ET) is None


def _analyze(items, prev, **kw):
    args = dict(ref_spot=100.0, prev_volumes=prev, today=TODAY, tz=ET, now_ns=NOW_NS, block_premium=1e6,
                unusual_min_premium=5e5, unusual_vol_oi_ratio=1.0)
    args.update(kw)
    return analyze(items, **args)


def test_flow_signs_and_blocks():
    first = chain("X", 100, TODAY, NOW_NS)
    _, vols = _analyze(first, None)
    # Calls bought at the ask (bullish) and puts sold at the bid (bullish); a few puts bought.
    vol = {("call", 100.0, 8): 10 + 5000, ("put", 95.0, 8): 10 + 2000, ("put", 105.0, 8): 10 + 100}
    at = {("call", 100.0, 8): "buy", ("put", 95.0, 8): "sell", ("put", 105.0, 8): "buy"}
    snap, _ = _analyze(chain("X", 100, TODAY, NOW_NS, volumes=vol, last_at=at), vols, block_premium=4e5)
    w = snap.window
    assert w.bull_premium > w.bear_premium > 0
    assert w.net_delta > 0
    assert [b.side for b in snap.blocks] == ["buy"] and snap.blocks[0].kind == "call"
    assert snap.call_wall is not None and snap.put_wall is not None


def test_0dte_excluded_from_blocks_unless_enabled():
    items = [contract("X", "call", 100, TODAY, spot=100, volume=10, now_ns=NOW_NS)]
    _, vols = _analyze(items, None)
    big = [contract("X", "call", 100, TODAY, spot=100, volume=20000, now_ns=NOW_NS, last_at="buy")]
    snap, _ = _analyze(big, vols)
    assert snap.blocks == [] and snap.unusual == []
    snap, _ = _analyze(big, vols, alert_0dte=True)
    assert len(snap.blocks) == 1 and len(snap.unusual) == 1


def test_first_snapshot_has_no_window():
    snap, _ = _analyze(chain("X", 100, TODAY, NOW_NS), None)
    assert snap.window is None


def test_options_bias_direction():
    snap, _ = _analyze(chain("X", 100, TODAY, NOW_NS), None)
    bull = Window(ts=0, bull_premium=9e6, bear_premium=1e6, net_delta=5e6, gross_delta=6e6)
    bear = Window(ts=0, bull_premium=1e6, bear_premium=9e6, net_delta=-5e6, gross_delta=6e6)
    assert options_bias(snap, bull, bull, None, strong=60, bias=25).score > 50
    assert options_bias(snap, bear, bear, None, strong=60, bias=25).score < -50
    # No flow and no skew history: positioning alone is damped toward neutral.
    lone = options_bias(snap, None, None, None, strong=60, bias=25)
    assert abs(lone.score) <= 25


def test_contract_picks_follow_templates():
    from mias.config import DEFAULT_IDEAS
    from mias.options import pick_contracts
    items = []
    for dte in (0, 3, 9, 30, 64, 92, 120):
        for k in range(90, 111):
            for kind in ("call", "put"):
                items.append(contract("X", kind, float(k), TODAY + timedelta(days=dte), spot=100, oi=500,
                                      now_ns=NOW_NS))
    cs = [parse_contract(i, TODAY, ET) for i in items]
    ideas = pick_contracts(cs, DEFAULT_IDEAS["swing"])
    assert ideas["call"].dte in (64, 92) and ideas["call"].strike < 100      # ITM call ~0.6 delta
    assert ideas["put"].strike > 100 and abs(ideas["put"].delta) == pytest.approx(0.6, abs=0.12)
    day = pick_contracts(cs, DEFAULT_IDEAS["day"])
    assert day["call"].dte in (3, 9) and abs(day["call"].delta) == pytest.approx(0.45, abs=0.06)
    assert day["call"].strike >= 100                                         # slightly OTM
    # A wide spread disqualifies an otherwise perfect contract.
    wide = [contract("X", "call", 101, TODAY + timedelta(days=7), spot=100, oi=5000, now_ns=NOW_NS, spread=0.5)]
    assert pick_contracts([parse_contract(i, TODAY, ET) for i in wide], DEFAULT_IDEAS["day"]) == {}


def test_latest_session_off_hours():
    from mias.options import latest_session
    friday = date(2026, 10, 9)
    fri_ns = int(datetime.combine(friday, time(15, 0), ET).timestamp() * 1e9)
    items = chain("X", 100, friday, fri_ns, volumes={("call", 100.0, 8): 7000})
    sat = friday + timedelta(days=1)
    assert latest_session(items, sat, ET) == friday
    snap, _ = analyze(items, ref_spot=100.0, prev_volumes=None, today=sat, tz=ET, now_ns=fri_ns,
                      block_premium=1e6, unusual_min_premium=5e5, unusual_vol_oi_ratio=1.0, thin_volume=5000)
    assert snap.call_volume >= 7000 and not snap.thin


def test_thin_flag():
    snap, _ = _analyze(chain("X", 100, TODAY, NOW_NS), None, thin_volume=5000)
    assert snap.thin


# ---- screen ------------------------------------------------------------------------------------

def test_weighted_median_ignores_thin_contracts():
    from mias.screen import weighted_median
    # Two busy contracts at ~2% and many idle ones at 35%: the busy ones decide.
    pairs = [(2.0, 5000), (2.5, 4000)] + [(35.0, 10)] * 20
    assert weighted_median(pairs) in (2.0, 2.5)
    assert weighted_median([]) is None and weighted_median([(5.0, 0)]) is None


def test_screen_gates_and_ranks():
    from mias.screen import Row, score
    good = Row("GOOD", "T", 100, 0, 500e6, 1e6, 2e6, 1.0, 1.5, 6, 40, 42, 1500)
    wide = Row("WIDE", "T", 100, 0, 500e6, 1e6, 2e6, 12.0, 9.0, 6, 40, 42, 1500)
    thin = Row("THIN", "T", 100, 0, 1e6, 1e4, 1e4, 1.0, 1.0, 6, 40, 42, 1500)
    monthly = Row("MONTHLY", "T", 100, 0, 50e6, 1e5, 1e5, 2.0, 2.0, 1, 40, 42, 1500)
    rows = score([thin, wide, monthly, good])
    assert rows[0].symbol == "GOOD" and rows[0].passes
    by = {r.symbol: r for r in rows}
    assert "wide spreads" in by["WIDE"].why_not and "premium" in by["THIN"].why_not
    assert "no weeklies" in by["MONTHLY"].why_not
