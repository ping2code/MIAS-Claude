"""EMA 9/21 Simple — SPY / QQQ / IWM (experimental, paper): completed-candle crosses only, the first two signals per
symbol per day, independent counters, ATM 0DTE / next-expiry contract, all-out +25% / -20% / 15:10, isolation."""
import asyncio
import dataclasses
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mias import config as config_mod
from mias import emacross as E
from mias import simple as S
from mias.indicators import Bar, ema
from mias.store import Store

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
PREV, DAY, NEXT = date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27)
EXPIRIES = ("2026-10-26", "2026-10-27", "2026-10-28", "2026-10-30")


def _t(d, h, m):
    return int(datetime.combine(d, time(h, m), ET).timestamp() * 1000)


def _day(d, closes, start=None):
    """Regular-session 5-minute candles from 09:30, one per close (open = previous close)."""
    out, p = [], start if start is not None else closes[0]
    for k, c in enumerate(closes):
        t = _t(d, 9, 30) + k * E.FIVE
        out.append(Bar(t, p, max(p, c) + 0.01, min(p, c) - 0.01, c, 1000))
        p = c
    return out


def _prev(d=PREV, level=100.0):
    """A quiet down-drifting previous day: EMA 9 below EMA 21 into the open."""
    return _day(d, [level + 0.5 - 0.5 * k / 77 for k in range(78)])


def _wave(d=DAY, amp=1.0, period=24, n=78, level=100.0):
    return _prev(d - timedelta(days=3), level) + _day(d, [level + amp * math.sin(2 * math.pi * k / period)
                                                          for k in range(n)], level)


def _rise(d=DAY, n=78, level=100.0):
    return _prev(d - timedelta(days=3), level) + _day(d, [level + 0.05 * k for k in range(n)], level)


def _up_down(d=DAY, n=78, level=100.0):
    half = n // 2
    return _prev(d - timedelta(days=3), level) + _day(
        d, [level + 0.05 * k for k in range(half)] + [level + 0.05 * half - 0.08 * k for k in range(n - half)], level)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setenv("MIAS_DB_PATH", str(tmp_path / "t.db"))
    c = config_mod.load(ROOT / "config.toml")
    return dataclasses.replace(c, ema_simple_enabled=True)


class Chain:
    """Fake market data: 0DTE + later expiries, strikes every $1 around the spot, quotes at mid +-0.02 (mid settable
    per call), and nothing else: no order API exists to reach."""

    def __init__(self, mid=2.00):
        self.mid, self.calls = mid, []

    def _q(self):
        return {"bid": round(self.mid - 0.02, 2), "ask": round(self.mid + 0.02, 2)}

    async def option_chain(self, symbol, *, spot, today, max_dte, strike_pct, max_pages, **kw):
        self.calls.append(("option_chain", symbol))
        return [{"details": {"contract_type": k, "expiration_date": e, "strike_price": float(s),
                             "ticker": E.option_ticker(symbol, e, "bull" if k == "call" else "bear", s)},
                 "last_quote": self._q(), "underlying_asset": {"price": spot}}
                for e in EXPIRIES if today <= date.fromisoformat(e) <= today + timedelta(days=max_dte)
                for k in ("call", "put") for s in range(round(spot) - 3, round(spot) + 4)], False

    async def snapshots(self, tickers):
        self.calls.append(("snapshots", len(tickers)))
        return {t: {"last_quote": self._q()} for t in tickers}

    def __getattr__(self, name):
        self.calls.append(name)
        raise AttributeError(name)


def _drive(cfg, store, client, five, d=DAY, until=(15, 15)):
    """Run the live step 45 s after every 5-minute close of the day, as the 150 s cycle would see it."""
    t, end = _t(d, 9, 35), _t(d, *until)
    while t <= end:
        now = datetime.fromtimestamp(t / 1000 + 45, timezone.utc)
        asyncio.run(S.live(client, store, cfg, now, {s: [b for b in bars if b.t + E.FIVE <= t + 45_000]
                                                      for s, bars in five.items()}))
        t += E.FIVE


def _closed(store, sym):
    return [t for t in store.get(S.KEY)["closed"] if t["symbol"] == sym]


# ---- crossover detection ---------------------------------------------------------------------------

def test_bullish_confirmed_crossover():
    bars = _rise()
    xs = S.crosses("SPY", bars, DAY, ET)
    assert [x["side"] for x in xs] == ["bull"]
    i = next(n for n, b in enumerate(bars) if b.t == xs[0]["t"])
    f, s = ema([b.c for b in bars], 9), ema([b.c for b in bars], 21)
    assert f[i - 1] <= s[i - 1] and f[i] > s[i]
    assert xs[0]["close_t"] == bars[i].t + E.FIVE and xs[0]["price"] == bars[i].c
    assert xs[0]["ema9"] == round(f[i], 4) and xs[0]["ema21"] == round(s[i], 4)


def test_bearish_confirmed_crossover():
    bars = _up_down()
    xs = S.crosses("SPY", bars, DAY, ET)
    assert [x["side"] for x in xs] == ["bull", "bear"]
    f, s = ema([b.c for b in bars], 9), ema([b.c for b in bars], 21)
    i = next(n for n, b in enumerate(bars) if b.t == xs[1]["t"])
    assert f[i - 1] >= s[i - 1] and f[i] < s[i]


def test_equal_emas_count_as_not_above_and_not_below():
    """prev EMA9 == EMA21 then above is bullish; prev == then below is bearish (the spec's <= / >=)."""
    flat = _day(DAY, [0.0] * 30, 0.0)                    # exactly equal EMAs (all zeros)
    nxt = flat[-1].t + E.FIVE
    up, dn = flat + [Bar(nxt, 0, 0.51, 0, 0.5, 1)], flat + [Bar(nxt, 0, 0, -0.51, -0.5, 1)]
    f, sl = ema([b.c for b in up], 9), ema([b.c for b in up], 21)
    assert f[-2] == sl[-2]
    assert [x["side"] for x in S.crosses("SPY", up, DAY, ET)] == ["bull"]
    assert [x["side"] for x in S.crosses("SPY", dn, DAY, ET)] == ["bear"]


def test_intrabar_cross_that_reverses_before_the_close_is_no_signal():
    bars = _prev(PREV) + _day(DAY, [100.0] * 20)
    spike = dataclasses.replace(bars[-1], h=104.0)       # traded far above inside the candle, closed back at 100
    assert S.crosses("SPY", bars[:-1] + [spike], DAY, ET) == []
    held = dataclasses.replace(bars[-1], h=104.0, c=104.0)   # the same move held to the close is a cross
    assert [x["side"] for x in S.crosses("SPY", bars[:-1] + [held], DAY, ET)] == ["bull"]


def test_ema_already_above_gives_no_second_bullish_signal():
    xs = S.crosses("SPY", _rise(), DAY, ET)
    assert len(xs) == 1                                  # one transition, then EMA9 stays above all day


def test_ema_already_below_gives_no_second_bearish_signal():
    bars = _prev(PREV) + _day(DAY, [100 - 0.05 * k for k in range(78)], 100.0)
    assert S.crosses("SPY", bars, DAY, ET) == []         # below at the open, below all day: nothing


# ---- per-symbol daily counter ----------------------------------------------------------------------

def _numbered(bars, rules):
    return S.number(S.crosses("SPY", bars, DAY, ET), DAY, ET, rules)


def test_counter_first_two_traded_third_and_later_ignored(cfg):
    sg = [x for x in _numbered(_wave(), S.rules_of(cfg)) if x["n"]]
    assert len(sg) >= 3
    assert [x["action"] for x in sg[:3]] == ["trade", "trade", "ignored_limit"]
    assert all(x["action"] == "ignored_limit" for x in sg[2:]) and [x["n"] for x in sg] == list(range(1, len(sg) + 1))


def test_signals_after_last_entry_are_not_counted(cfg):
    late = [x for x in _numbered(_wave(), S.rules_of(cfg)) if x["close_t"] > _t(DAY, 15, 0)]
    assert late and all(x["n"] is None and x["action"] == "after_last_entry" for x in late)


@pytest.mark.parametrize("sym", S.SYMBOLS)
def test_each_symbol_first_second_accepted_third_ignored(cfg, tmp_path, sym):
    store, client = Store(tmp_path / f"{sym}.db"), Chain()
    _drive(cfg, store, client, {sym: _wave()})
    st = store.get(S.KEY)
    ns = sorted(t["n"] for t in st["closed"] if t["symbol"] == sym)
    assert ns == [1, 2]                                  # trade #1 and #2, nothing more
    assert st["ignored"][sym] >= 1 and st["counts"][sym] >= 3


def test_counters_are_independent_per_symbol(cfg, tmp_path):
    """SPY's full day (2 traded, rest ignored) doesn't consume QQQ's or IWM's: QQQ has 1 cross, IWM 2."""
    store, client = Store(tmp_path / "x.db"), Chain()
    _drive(cfg, store, client, {"SPY": _wave(), "QQQ": _rise(level=200.0), "IWM": _up_down(level=150.0)})
    st = store.get(S.KEY)
    assert sorted(t["n"] for t in _closed(store, "SPY")) == [1, 2] and st["ignored"]["SPY"] >= 1
    assert sorted(t["n"] for t in _closed(store, "QQQ")) == [1] and st["ignored"]["QQQ"] == 0
    assert sorted(t["n"] for t in _closed(store, "IWM")) == [1, 2] and st["ignored"]["IWM"] == 0
    # and the same QQQ / IWM results with SPY absent: SPY's counter never reaches them
    store2 = Store(tmp_path / "y.db")
    _drive(cfg, store2, Chain(), {"QQQ": _rise(level=200.0), "IWM": _up_down(level=150.0)})
    for sym in ("QQQ", "IWM"):
        assert [(t["n"], t["in"], t["contract"]) for t in _closed(store2, sym)] == \
            [(t["n"], t["in"], t["contract"]) for t in _closed(store, sym)]


def test_qqq_counter_does_not_affect_iwm(cfg, tmp_path):
    store = Store(tmp_path / "x.db")
    _drive(cfg, store, Chain(), {"QQQ": _wave(level=200.0), "IWM": _rise(level=150.0)})
    assert sorted(t["n"] for t in _closed(store, "QQQ")) == [1, 2]
    assert sorted(t["n"] for t in _closed(store, "IWM")) == [1]


def test_daily_counters_reset(cfg, tmp_path):
    store = Store(tmp_path / "x.db")
    _drive(cfg, store, Chain(), {"SPY": _wave()})
    assert store.get(S.KEY)["counts"]["SPY"] >= 3
    _drive(cfg, store, Chain(), {"SPY": _wave(NEXT)}, d=NEXT)
    st = store.get(S.KEY)
    assert st["date"] == NEXT.isoformat() and st["counts"]["SPY"] >= 3
    assert sorted(t["n"] for t in st["closed"]) == [1, 2]            # a fresh 2 on the new day


def test_trade_two_can_open_while_trade_one_is_open(cfg, tmp_path):
    """Each of the first two signals is its own position (the mid never moves here, so #1 is still open)."""
    store = Store(tmp_path / "x.db")
    _drive(cfg, store, Chain(), {"SPY": _wave()}, until=(14, 0))
    assert sorted(store.get(S.KEY)["open"]) == ["SPY#1", "SPY#2"]


# ---- contract choice -------------------------------------------------------------------------------

def _rows(spot, today=DAY):
    return asyncio.run(Chain().option_chain("SPY", spot=spot, today=today, max_dte=8, strike_pct=0.02,
                                            max_pages=5))[0]


def test_call_for_bullish_put_for_bearish_atm(cfg):
    r = S.rules_of(cfg)
    c = S.atm(_rows(100.3), "bull", 100.3, DAY, _t(DAY, 10, 0), ET, r)
    p = S.atm(_rows(100.3), "bear", 100.3, DAY, _t(DAY, 10, 0), ET, r)
    assert c["details"]["contract_type"] == "call" and p["details"]["contract_type"] == "put"
    assert c["details"]["strike_price"] == p["details"]["strike_price"] == 100.0


def test_atm_tie_takes_the_lower_strike(cfg):
    c = S.atm(_rows(100.5), "bull", 100.5, DAY, _t(DAY, 10, 0), ET, S.rules_of(cfg))
    assert c["details"]["strike_price"] == 100.0


def test_0dte_before_13_next_expiry_from_13(cfg):
    r = S.rules_of(cfg)
    assert S.atm(_rows(100), "bull", 100, DAY, _t(DAY, 12, 55), ET, r)["details"]["expiration_date"] == "2026-10-26"
    assert S.atm(_rows(100), "bull", 100, DAY, _t(DAY, 13, 0), ET, r)["details"]["expiration_date"] == "2026-10-27"
    assert S.atm(_rows(100), "bear", 100, DAY, _t(DAY, 14, 30), ET, r)["details"]["expiration_date"] == "2026-10-27"


def test_entry_alert_and_record(cfg, tmp_path):
    store = Store(tmp_path / "x.db")
    _drive(cfg, store, Chain(), {"SPY": _rise()}, until=(11, 0))
    tr = E.Trade(**store.get(S.KEY)["open"]["SPY#1"])
    assert tr.entry == 2.02 and tr.entry_mid == 2.0 and tr.qty == 5 and tr.stop == round(2.02 * 0.8, 2)
    assert tr.info["dte"] == 0 and tr.info["entry_spread"] == 0.04 and tr.contract.startswith("O:SPY261026C")
    text = [r[2] for r in store.pending() if "EMA SIMPLE" in r[2]][0]
    for s in ("EMA SIMPLE — PAPER", "Symbol: <b>SPY</b>", "Direction: <b>CALL</b>", "1 of 2", f"Strike {tr.strike:g}",
              "Target +25%: 2.52", "Stop -20%: 1.62", "Force close: 15:10", "PAPER ONLY", "EMA9", "EMA21"):
        assert s in text


# ---- exits -----------------------------------------------------------------------------------------

def _one_open(cfg, tmp_path):
    store, client = Store(tmp_path / "x.db"), Chain()
    _drive(cfg, store, client, {"SPY": _rise()}, until=(11, 0))
    return store, client


def _watch(cfg, store, client, h, m):
    return asyncio.run(S.manage(client, store, cfg, datetime.combine(DAY, time(h, m), ET)))


def test_target_sells_all_at_plus_25(cfg, tmp_path):
    store, client = _one_open(cfg, tmp_path)
    client.mid = 2.53                                    # mid >= target round(2.02 * 1.25, 2) = 2.52
    assert _watch(cfg, store, client, 11, 30) == 1
    t = store.get(S.KEY)["closed"][0]
    assert t["exit_reason"] == "target" and not store.get(S.KEY)["open"]
    assert t["pnl"] == round((2.51 - 2.02) * 5 * 100, 2)  # all 5 sold at min(target 2.52, bid 2.51)


def test_stop_sells_all_at_minus_20_at_the_bid(cfg, tmp_path):
    store, client = _one_open(cfg, tmp_path)
    client.mid = 1.60                                    # mid <= stop 1.62
    _watch(cfg, store, client, 11, 30)
    t = store.get(S.KEY)["closed"][0]
    assert t["exit_reason"] == "stop" and t["pnl"] == round((1.58 - 2.02) * 5 * 100, 2)


def test_no_trail_no_breakeven_no_partial(cfg, tmp_path):
    store, client = _one_open(cfg, tmp_path)
    client.mid = 2.40                                    # +19%: below target, nothing happens
    assert _watch(cfg, store, client, 11, 30) == 0
    tr = E.Trade(**store.get(S.KEY)["open"]["SPY#1"])
    assert tr.open_qty == 5 and tr.stop == 1.62 and not tr.partial_done


def test_force_close_at_1510(cfg, tmp_path):
    store, client = _one_open(cfg, tmp_path)
    _watch(cfg, store, client, 15, 9)
    assert store.get(S.KEY)["open"]
    _watch(cfg, store, client, 15, 10)
    t = store.get(S.KEY)["closed"][0]
    assert t["exit_reason"] == "time" and t["pnl"] == round((1.98 - 2.02) * 5 * 100, 2)


def test_no_chart_invalidation_exit(cfg, tmp_path):
    """A 5-minute close far back through the cross candle (A-D's invalidation) doesn't close a Simple trade."""
    store, client = Store(tmp_path / "x.db"), Chain()
    bars = _rise()
    _drive(cfg, store, client, {"SPY": bars}, until=(11, 0))
    crash = [b if b.t < _t(DAY, 11, 0) else dataclasses.replace(b, o=90.0, h=90.01, l=89.9, c=90.0) for b in bars]
    asyncio.run(S.live(client, store, cfg, datetime.combine(DAY, time(11, 6), ET),
                       {"SPY": [b for b in crash if b.t + E.FIVE <= _t(DAY, 11, 6)]}))
    assert "SPY#1" in store.get(S.KEY)["open"]


# ---- isolation -------------------------------------------------------------------------------------

def test_only_spy_qqq_iwm(cfg, tmp_path):
    store, client = Store(tmp_path / "x.db"), Chain()
    _drive(cfg, store, client, {"NVDA": _wave(), "TSLA": _rise()})
    assert store.get(S.KEY)["closed"] == [] and not [c for c in client.calls if c[0] == "option_chain"]


def test_isolated_from_a_to_e21(cfg, tmp_path):
    """The A-E21 engine on the same bars produces identical state, logs and alerts with or without Simple; Simple
    writes only its own keys and never runs from emacross.live."""
    from mias import dte as X, qualify as Q
    keys = [E.STRATEGIES[s]["key"] for s in E.STRATEGIES] + [E.LOG_KEY, "ema_d_candidates", "ema_d_log", X.DIAG_KEY]
    c2 = dataclasses.replace(cfg, daytrade=dataclasses.replace(cfg.daytrade, symbols=("SPY",), strategies=(
        "retest", "cross", "ema_9_21_d", X.E7, X.E21)))
    bars = _wave()
    out = {}
    for name, with_simple in (("a", False), ("b", True)):
        store, t = Store(tmp_path / f"{name}.db"), _t(DAY, 9, 35)
        while t <= _t(DAY, 15, 15):
            now = datetime.fromtimestamp(t / 1000 + 45, timezone.utc)
            five = {"SPY": [b for b in bars if b.t + E.FIVE <= t + 45_000]}
            asyncio.run(E.live(Chain(), store, c2, now, five))
            if with_simple:
                asyncio.run(S.live(Chain(), store, c2, now, five))
            t += E.FIVE
        out[name] = ({k: store.get(k) for k in keys},
                     [r[2] for r in store.pending() if "EMA SIMPLE" not in r[2] and "Simple" not in r[2]])
    assert out["a"] == out["b"]
    assert S.NAME not in E.STRATEGIES and S.NAME not in Q.PROFILES and S.KEY not in keys and S.LOG_KEY != E.LOG_KEY


def test_off_by_default_and_switch_parses(tmp_path, monkeypatch):
    import re
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setenv("MIAS_DB_PATH", str(tmp_path / "t.db"))
    p = tmp_path / "c.toml"
    base = re.sub(r"\n\[ema_simple\]\n(?:(?!\[).*\n)*", "\n", (ROOT / "config.toml").read_text())
    assert "[ema_simple]" not in base
    p.write_text(base)
    assert config_mod.load(p).ema_simple_enabled is False
    p.write_text(base + "\n[ema_simple]\nenabled = true\n")
    assert config_mod.load(p).ema_simple_enabled is True


def test_no_order_path(cfg, tmp_path):
    store, client = Store(tmp_path / "x.db"), Chain()
    _drive(cfg, store, client, {"SPY": _wave(), "QQQ": _rise(level=200.0)})
    assert {c[0] if isinstance(c, tuple) else c for c in client.calls} <= {"option_chain", "snapshots"}


class SlowChain(Chain):
    """Quotes that take a moment, so the cycle and the 30 s watcher overlap while both wait for them."""

    async def snapshots(self, tickers):
        await asyncio.sleep(0.02)
        return await super().snapshots(tickers)


def test_cycle_and_watcher_together_close_a_trade_once(cfg, tmp_path):
    """2026-11-10: the cycle and the watcher both closed QQQ 673P at 09:40 (two alerts, two log rows)."""
    store, _ = _one_open(cfg, tmp_path)
    client = SlowChain(mid=1.60)                         # at the stop
    now = datetime.combine(DAY, time(11, 30), ET)
    five = {"SPY": [b for b in _rise() if b.t + E.FIVE <= now.timestamp() * 1000]}

    async def both():
        return await asyncio.gather(S.manage(client, store, cfg, now), S.live(client, store, cfg, now, five))

    asyncio.run(both())
    assert len(store.get(S.KEY)["closed"]) == 1 and not store.get(S.KEY)["open"]
    assert len(store.get(S.LOG_KEY)) == 1
    assert len([m for m in store.pending() if "STOPPED" in str(m)]) == 1
