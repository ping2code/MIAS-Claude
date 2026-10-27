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


# ---- pre-market ----------------------------------------------------------------------------------

def _pre_bars(d: date, prev_close: float, pre: list[tuple[int, int, float, float]]) -> list[dict]:
    out = []
    if prev_close:
        out.append({"t": _ms(d, 15, 55), "o": prev_close, "h": prev_close, "l": prev_close, "c": prev_close,
                    "v": 1000})
    for h, m, c, v in pre:
        out.append({"t": _ms(d, h, m), "o": c, "h": c, "l": c, "c": c, "v": v, "vw": c})
    return out


def test_premarket_read_change_and_relative_volume():
    from mias.premarket import read
    d0, d1, d2 = date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26)
    raw = (_pre_bars(d0, 100.0, [(7, 0, 100.0, 500)]) + _pre_bars(d1, 100.0, [(7, 0, 100.0, 1500)])
           + _pre_bars(d2, 0, [(6, 30, 101.0, 1000), (7, 5, 102.0, 3000), (8, 0, 103.0, 9999)]))
    x = read(raw, d2, ET, datetime.combine(d2, time(7, 15), ET))
    assert x["price"] == 102.0 and x["change"] == pytest.approx(2.0) and x["volume"] == 4000
    assert x["rvol"] == pytest.approx(4.0) and x["dollars"] == pytest.approx(101 * 1000 + 102 * 3000)
    assert read(raw, d2, ET, datetime.combine(d2, time(6, 0), ET)) is None          # nothing traded yet


def test_premarket_text_flags_and_update(cfg):
    from mias.premarket import text
    prior = [NS(symbol=s, stock=NS(score=sc, label="UP" if sc > 0 else "DOWN", composites={}), price=1)
             for s, sc in (("NVDA", 60), ("MU", 50), ("XOM", -60), ("CVX", -50), ("SOFI", -70))]
    mv = lambda c, dollars=2e6: {"change": c, "rvol": 1.5, "dollars": dollars, "price": 1, "prev_close": 1,
                                 "volume": 1}
    reads = {"SPY": mv(0.2), "QQQ": mv(0.4), "IWM": mv(-0.1), "SMH": mv(1.2), "XLE": mv(0.9), "SOFI": mv(9.5),
             "MU": mv(2.0, 1e5)}
    now = datetime.combine(date(2026, 10, 27), time(7, 0), ET)
    msg, snap = text(cfg, reads, prior, [("MSFT", {"next": "2026-10-29", "quality": "confirmed"})], now)
    assert "Pre-market heads-up" in msg and "Yesterday's close: sector trend" in msg
    assert "SMH +1.20%, 1.5× vol · ✅ with its trend" in msg
    assert "XLE +0.90%, 1.5× vol · ⚠️ gapping against its trend" in msg
    assert "SOFI +9.50%" in msg and "⚠️ against its trend" in msg and "MU +2.00%" not in msg   # MU: too thin
    assert "MSFT ~Oct 29" in msg and snap["leader"] == "tech" and snap["sectors"]["energy"] == 0.9
    reads["XLE"] = mv(-0.4)
    upd, _ = text(cfg, reads, prior, [], now.replace(hour=9, minute=15), earlier=snap)
    assert "Pre-market update" in upd and "Earnings" not in upd
    assert "SMH +1.20%, 1.5× vol · ✅ with its trend · ✅ held (07:00 +1.20%)" in upd
    assert "⚠️ flipped (07:00 +0.90%)" in upd and "✅ same as 07:00" in upd


# ---- 3:45 close play -----------------------------------------------------------------------------

def test_close_play_read_and_rules():
    from datetime import date as _date, datetime as _dt, time as _time, timedelta as _td
    from zoneinfo import ZoneInfo
    from mias import closeplay
    from mias.indicators import Bar
    et = ZoneInfo("America/New_York")
    day = _date(2026, 10, 26)
    bars, t, p = [], _dt.combine(day, _time(9, 30), et), 100.0
    while t.time() < _time(16, 0):
        up = 0.05 if t.time() < _time(15, 45) else -0.1          # rising all day, dips in the last 15 minutes
        bars.append(Bar(int(t.timestamp() * 1000), p, p + 0.1, p - 0.1, p + up, 1000))
        p, t = p + up, t + _td(minutes=5)
    x = closeplay.read(bars, et)
    assert x["day"] == x["vwap_side"] == x["momentum"] == 1 and x["move_pct"] < 0
    assert closeplay.decide(x, "trend") == 1 and closeplay.decide(x, "fade") == -1
    assert closeplay.decide(x, "strong") == (1 if x["day_pct"] >= closeplay.STRONG_DAY else 0)
    assert closeplay.read(bars[:60], et) is None                  # half day: no 15:40 candle
    rows = [dict(x, date="2026-10-26", **{f"rule_{r}": closeplay.decide(x, r) for r in closeplay.RULES},
                 call_pnl=-30.0, put_pnl=80.0)]
    s = closeplay.summary(rows, "fade")
    assert s["signals"] == 1 and s["hit"] == 100 and s["expectancy"] == 80.0
    assert closeplay.summary(rows, "trend")["hit"] == 0


# ---- strategy report -----------------------------------------------------------------------------

def test_strategy_report_separates_strategies(tmp_path, monkeypatch):
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _P
    from mias import config as config_mod, emacross, strategies
    from mias.store import Store
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    monkeypatch.setenv("MIAS_DB_PATH", str(tmp_path / "s.db"))
    cfg = config_mod.load(_P(__file__).resolve().parent.parent / "config.toml")
    store = Store(tmp_path / "s.db")
    log = [{"date": "2026-10-27", "side": "bull", "pnl": 200.0},
           {"date": "2026-10-27", "side": "bear", "pnl": -100.0, "strategy": "retest"},
           {"date": "2026-10-27", "side": "bull", "pnl": -50.0, "strategy": "cross"}]
    store.put(emacross.LOG_KEY, log)
    store.put("health:2026-10-27", {"BAC": {"stock": 2}})
    text = strategies.report(store, cfg, _dt(2026, 10, 27, 21, 0, tzinfo=_tz.utc))
    retest, cross = text.split("cross + retest")[1].split("cross (no retest)")
    assert "2 trades" in retest and "win 50%" in retest and "expectancy +$44/trade" in retest  # (193.5-106.5)/2
    assert "1 trade ·" in cross and "-$56" in cross
    assert "Health gate held 2 signals" in text
    s = strategies.stats([{"pnl": 100.0}, {"pnl": -300.0}, {"pnl": 50.0}], 0)
    assert s["max_dd"] == -300 and round(s["pf"], 2) == 0.5


def _close_day_raw(day, et, drift_after=-0.1, turn=(15, 45)):
    from datetime import datetime as _dt, time as _time, timedelta as _td
    raw, t, p = [], _dt.combine(day, _time(9, 30), et), 100.0
    while t.time() < _time(16, 0):
        up = 0.05 if t.time() < _time(*turn) else drift_after
        raw.append({"t": int(t.timestamp() * 1000), "o": p, "h": p + 0.1, "l": p - 0.1, "c": p + up, "v": 1000})
        p, t = p + up, t + _td(minutes=5)
    return raw


class _CloseClient:
    def __init__(self, day):
        self.day, self.bid, self.ask = day, 0.50, 0.52

    async def option_chain(self, symbol, *, spot, today, **kw):
        return [{"details": {"contract_type": k, "expiration_date": today.isoformat(), "strike_price": s,
                             "ticker": f"O:SPY{today:%y%m%d}{k[0].upper()}{int(s * 1000):08d}"},
                 "last_quote": {"bid": self.bid, "ask": self.ask}, "underlying_asset": {"price": spot}}
                for k in ("call", "put") for s in (round(spot) - 1, round(spot), round(spot) + 1)], False

    async def snapshots(self, tickers):
        return {t: {"last_quote": {"bid": self.bid, "ask": self.ask}, "underlying_asset": {"price": 104.0}}
                for t in tickers}


def _close_cfg(tmp_path, monkeypatch):
    from pathlib import Path as _P
    from mias import config as config_mod
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    monkeypatch.setenv("MIAS_DB_PATH", str(tmp_path / "c.db"))
    return config_mod.load(_P(__file__).resolve().parent.parent / "config.toml")


def test_close_play_live_fade_trade(tmp_path, monkeypatch):
    import asyncio
    from datetime import date as _date, datetime as _dt, time as _time
    from mias import closeplay, strategies
    from mias.store import Store
    cfg = _close_cfg(tmp_path, monkeypatch)
    et = cfg.tz
    day = _date(2026, 10, 28)
    raw = _close_day_raw(day, et)
    store, client = Store(tmp_path / "c.db"), _CloseClient(day)
    at = lambda h, m: _dt.combine(day, _time(h, m), et)
    early = [b for b in raw if b["t"] + 300_000 <= at(15, 44).timestamp() * 1000]
    assert asyncio.run(closeplay.live(client, store, cfg, at(15, 45), early)) == 0     # 15:40 candle not in yet
    assert asyncio.run(closeplay.live(client, store, cfg, at(15, 46), raw, [], 6880.0)) == 1
    text = store.pending()[-1][2]
    assert "PUT" in text and "fade it" in text and "SPX equivalent: 0DTE 6880P" in text and "0.52 ask" in text
    assert asyncio.run(closeplay.live(client, store, cfg, at(15, 48), raw)) == 0       # once a day
    assert asyncio.run(closeplay.manage(client, store, cfg, at(15, 58))) == 0           # not before 15:59
    client.bid, client.ask = 0.90, 0.92
    assert asyncio.run(closeplay.manage(client, store, cfg, at(15, 59))) == 1
    assert "SOLD" in store.pending()[-1][2] and "+$38" in store.pending()[-1][2]
    log = store.get(closeplay.LOG_KEY)
    assert log[0]["pnl"] == 38.0 and log[0]["side"] == "bear" and log[0]["qty"] == 1
    text = strategies.report(store, cfg, at(16, 30))
    assert "3:45 PM close fade" in text and "expectancy +$37/trade" in text           # 38 - 2 x 0.65


def test_close_play_no_trade_and_health_hold(tmp_path, monkeypatch):
    import asyncio
    from datetime import date as _date, datetime as _dt, time as _time
    from mias import closeplay
    from mias.store import Store
    cfg = _close_cfg(tmp_path, monkeypatch)
    day = _date(2026, 10, 28)
    at = _dt.combine(day, _time(15, 46), cfg.tz)
    raw = _close_day_raw(day, cfg.tz, -0.02, (15, 0))     # up on the day, down the last 45 minutes: mixed
    store = Store(tmp_path / "n.db")
    assert asyncio.run(closeplay.live(_CloseClient(day), store, cfg, at, raw)) == 1
    assert "NO TRADE" in store.pending()[-1][2] and store.get(closeplay.KEY)["status"] == "no_trade"
    store2 = Store(tmp_path / "h.db")
    asyncio.run(closeplay.live(_CloseClient(day), store2, cfg, at, _close_day_raw(day, cfg.tz), ["stock: timeout"]))
    assert "health gate" in store2.pending()[-1][2] and store2.get(closeplay.KEY)["status"] == "held"
