"""Strategy D (experimental, live paper): the backtested "A + trend + time windows" rule with a +50% first target and
the -20% premium stop. Gates, exits, CALL/PUT symmetry, isolation from A/B/C, no order path, and the report."""
import asyncio
import dataclasses
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mias import config as config_mod
from mias import emacross as E
from mias import qualify as Q
from mias.indicators import Bar
from mias.store import Store
from test_strategy_c import DAY, ET, RULES, _entry, _rally, _series

ROOT = Path(__file__).resolve().parent.parent


class Spy:
    """Fake market data client; records every method the strategy touches (there is no order API to reach)."""

    def __init__(self, spot, mid=2.0):
        self.spot, self.mid, self.calls = spot, mid, []

    def _q(self):
        return {"bid": round(self.mid - 0.02, 2), "ask": round(self.mid + 0.02, 2)}

    async def option_chain(self, symbol, **kw):
        self.calls.append("option_chain")
        return [{"details": {"contract_type": k, "expiration_date": e, "strike_price": s,
                             "ticker": f"O:SPY{e[2:4]}{e[5:7]}{e[8:]}{k[0].upper()}{s * 1000:08.0f}"},
                 "last_quote": self._q(), "underlying_asset": {"price": self.spot}}
                for e in ("2026-10-26", "2026-10-27") for k in ("call", "put")
                for s in (round(self.spot) - 1, round(self.spot), round(self.spot) + 1)], False

    async def snapshots(self, tickers):
        self.calls.append("snapshots")
        return {t: {"last_quote": self._q(), "underlying_asset": {"price": self.spot}} for t in tickers}

    def __getattr__(self, name):
        self.calls.append(name)
        raise AttributeError(name)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setenv("MIAS_DB_PATH", str(tmp_path / "t.db"))
    c = config_mod.load(ROOT / "config.toml")
    return _with(c, Q.D)


def _with(cfg, *strategies):
    return dataclasses.replace(cfg, daytrade=dataclasses.replace(cfg.daytrade, symbols=("SPY",),
                                                                 strategies=strategies))


def _mirror(bars):
    """The same day upside down: a bearish cross + retest exactly mirroring the bullish one."""
    return [Bar(b.t, 200 - b.o, 200 - b.l, 200 - b.h, 200 - b.c, b.v) for b in bars]


def _at(su, sec=45):
    return datetime.fromtimestamp(su.entry_t / 1000 + sec, timezone.utc)


def _run(cfg, store, client, bars, at):
    return asyncio.run(E.live(client, store, cfg, at, {"SPY": bars}))


# ---- the rule --------------------------------------------------------------------------------------

def test_d_is_exactly_the_validated_rule(cfg):
    # Gates: the backtest's "A + trend + time windows" (Q.evaluate with these settings), not C's.
    assert Q.D_GATES == dataclasses.replace(Q.QualRules(), require_vwap=False, require_slope=False,
                                            require_or=False, whipsaw_flips=999)
    assert Q.D_GATES.trend_only and Q.D_GATES.sessions == (("09:35", "11:00"), ("13:30", "14:30"))
    assert not Q.D_GATES.require_market and Q.D_GATES.min_separation_pct == 0
    # Exits: [daytrade] with only the first target changed; A/B/C keep the shared rules object itself.
    d = E.rules_for(cfg, Q.D)
    assert d == dataclasses.replace(cfg.daytrade, first_target=0.50)
    assert d.stop == 0.20 and d.partial == 3 and d.contracts == 5 and d.close == "15:10"
    assert all(E.rules_for(cfg, k) is cfg.daytrade for k in ("retest", "cross", "qualified"))
    # Config: [ema_qualified] (C's settings) can't change D.
    other = dataclasses.replace(cfg, ema_qualified=Q.QualRules(require_vwap=True, sessions=(("09:35", "10:00"),)))
    assert Q.PROFILES[Q.D]["gates"](other) is Q.D_GATES
    assert E.DayTradeRules().strategies == ("retest",)                         # off unless listed


def test_d_logs_vwap_slope_and_opening_range_but_never_gates_on_them():
    bars = _rally()
    su, first, i = _entry(bars)
    wrong = dataclasses.replace(su, side="bear")                              # fails VWAP, slope and OR
    d = Q.evaluate(wrong, bars, first, i, ET, RULES, Q.D_GATES, {}, su.entry_t)
    assert d["reasons"] == ["EMA_DIRECTION_MISALIGNED"]                      # A's own retest makes this impossible
    assert d["vwap_dist"] > 0 and d["or_position"] and d["ema9_slope_pct"] > 0


def test_bullish_call_qualifies_with_its_target_and_stop(cfg, tmp_path):
    bars = _rally()
    su, _, i = _entry(bars)
    store, client = Store(tmp_path / "d.db"), Spy(bars[i].c)
    assert _run(cfg, store, client, bars[:i + 1], _at(su)) == 1
    st = store.get("ema_d_day")
    tr = E.Trade(**st["open"]["SPY"])
    assert tr.side == "bull" and tr.contract.startswith("O:SPY261026C") and tr.entry == 2.02 and tr.stop == 1.62
    alert = store.pending()[-1][2]
    assert "D · TREND + TIME + 50/20" in alert and "target +50% 3.03" in alert and "stop -20% 1.62" in alert
    assert "Sell 3 of 5 at 3.03 (+50%)" in alert.replace("sell", "Sell")
    rec = store.get("ema_d_candidates")["items"][su.key]
    assert rec["disposition"] == "TRADE" and rec["regime"] == "trend" and rec["eval_at"] == "10:40"
    assert tr.info["target_price"] == 3.03 and tr.info["stop_price"] == 1.62 and tr.info["first_target_pct"] == 50


def test_bearish_put_mirrors_the_call(cfg, tmp_path):
    bull, bear = _rally(), _mirror(_rally())
    sb, _, ib = _entry(bull)
    sr = next(s for s in E.scan("SPY", bear, DAY, ET, RULES) if s.status == "entry")
    ir = next(n for n, b in enumerate(bear) if b.t + E.FIVE == sr.entry_t)
    assert sr.side == "bear" and sr.entry_t == sb.entry_t and ir == ib
    out = {}
    for name, bars, su, i in (("bull", bull, sb, ib), ("bear", bear, sr, ir)):
        store = Store(tmp_path / f"{name}.db")
        _run(cfg, store, Spy(bars[i].c), bars[:i + 1], _at(su))
        rec = store.get("ema_d_candidates")["items"][su.key]
        tr = E.Trade(**store.get("ema_d_day")["open"]["SPY"])
        out[name] = (rec["disposition"], rec["regime"], rec["eval_at"], round(abs(rec["ema9"] - rec["ema21"]), 6),
                     tr.entry, tr.stop, tr.info["target_price"], tr.contract[11])
    assert out["bull"][:7] == out["bear"][:7]                                  # same decision, levels and timing
    assert (out["bull"][7], out["bear"][7]) == ("C", "P")


def test_non_trend_regime_rejects(cfg, tmp_path, monkeypatch):
    bars = _rally()
    su, _, i = _entry(bars)
    monkeypatch.setattr(Q, "regime", lambda *a: "chop")
    store, client = Store(tmp_path / "c.db"), Spy(bars[i].c)
    assert _run(cfg, store, client, bars[:i + 1], _at(su)) == 0
    rec = store.get("ema_d_candidates")["items"][su.key]
    assert rec["disposition"] == "REJECT" and rec["reasons"] == ["REGIME_CHOP"]
    assert not store.get("ema_d_day")["open"] and store.get("ema_d_day")["skipped"] == {"REGIME_CHOP": 1}


def _shifted(extra):
    """The rally with `extra` more 5-minute candles of drift first, so the entry candle closes later."""
    bars = _rally()
    today = bars[78:]
    step = 5 * 60_000
    drift = [Bar(today[0].t + k * step, 100.22, 100.24, 100.20, 100.22, 1000) for k in range(extra)]
    return bars[:78] + drift + [dataclasses.replace(b, t=b.t + extra * step) for b in today]


@pytest.mark.parametrize("extra,close,ok", [(0, "10:40", True), (4, "11:00", True), (5, "11:05", False),
                                            (18, "12:10", False), (35, "13:35", True), (45, "14:25", True),
                                            (47, "14:35", False)])
def test_time_windows(cfg, tmp_path, monkeypatch, extra, close, ok):
    monkeypatch.setattr(Q, "regime", lambda *a: "trend")                     # isolate the window rule
    bars = _shifted(extra)
    su, _, i = _entry(bars)
    assert E._et(su.entry_t, ET).strftime("%H:%M") == close
    store = Store(tmp_path / f"w{extra}.db")
    _run(cfg, store, Spy(bars[i].c), bars[:i + 1], _at(su))
    rec = store.get("ema_d_candidates")["items"][su.key]
    assert (rec["disposition"] == "TRADE") is ok
    assert ok or rec["reasons"] == ["OUTSIDE_TRADING_WINDOW"]


# ---- exits ---------------------------------------------------------------------------------------

def _opened(cfg, tmp_path, name="x"):
    bars = _rally()
    su, _, i = _entry(bars)
    store, client = Store(tmp_path / f"{name}.db"), Spy(bars[i].c)
    _run(cfg, store, client, bars[:i + 1], _at(su))
    return store, client, bars[:i + 1], _at(su)


def test_first_target_is_plus_50_percent_then_runners_trail(cfg, tmp_path):
    store, client, bars, at = _opened(cfg, tmp_path)
    client.mid = 2.60                                                          # +30% (A's target): D holds
    asyncio.run(E.manage(client, store, cfg, at + timedelta(minutes=1), {"SPY": bars}))
    assert not E.Trade(**store.get("ema_d_day")["open"]["SPY"]).partial_done
    client.mid = 3.05                                                          # +51%: sells 3 at 3.03
    asyncio.run(E.manage(client, store, cfg, at + timedelta(minutes=2), {"SPY": bars}))
    tr = E.Trade(**store.get("ema_d_day")["open"]["SPY"])
    assert tr.partial_done and tr.open_qty == 2 and tr.stop == 2.02            # runners to breakeven
    assert "sold 3 at 3.03 (+50%)" in store.pending()[-1][2]
    client.mid = 4.10                                                          # +103%: trail locks +75%
    asyncio.run(E.manage(client, store, cfg, at + timedelta(minutes=3), {"SPY": bars}))
    assert E.Trade(**store.get("ema_d_day")["open"]["SPY"]).stop == round(2.02 * 1.75, 2)
    client.mid = 3.40                                                          # back through the trail
    asyncio.run(E.manage(client, store, cfg, at + timedelta(minutes=4), {"SPY": bars}))
    rec = store.get(E.LOG_KEY)[-1]
    assert rec["strategy"] == Q.D and rec["exit_reason"] == "trailed"
    assert rec["pnl"] == round((3.03 - 2.02) * 300 + (3.38 - 2.02) * 200, 2)  # sold at the bid, capped at the stop


def test_minus_20_percent_stop(cfg, tmp_path):
    store, client, bars, at = _opened(cfg, tmp_path)
    client.mid = 1.63                                                          # mid above the 1.62 stop: holds
    asyncio.run(E.manage(client, store, cfg, at + timedelta(minutes=1), {"SPY": bars}))
    assert "SPY" in store.get("ema_d_day")["open"]
    client.mid = 1.60                                                          # mid through the stop: out at bid
    asyncio.run(E.manage(client, store, cfg, at + timedelta(minutes=2), {"SPY": bars}))
    rec = store.get(E.LOG_KEY)[-1]
    assert rec["exit_reason"] == "stop" and rec["pnl"] == round((1.58 - 2.02) * 500, 2)
    assert store.get("ema_d_day")["losses"] == {"SPY": 1} and not store.get("ema_d_day")["open"]


def test_force_close_at_1510(cfg, tmp_path):
    store, client, bars, at = _opened(cfg, tmp_path)
    close = datetime.combine(DAY, datetime.min.time().replace(hour=15, minute=10), ET)
    client.mid = 2.10
    asyncio.run(E.live(client, store, cfg, close, {"SPY": bars}))
    rec = store.get(E.LOG_KEY)[-1]
    assert rec["exit_reason"] == "time" and rec["pnl"] == round((2.08 - 2.02) * 500, 2)
    texts = [p[2] for p in store.pending()]
    assert any("d · trend + time + 50/20 day trades" in t for t in texts)          # D's own closing summary
    assert any("A/B/C/D comparison" in t or "D comparison" in t for t in texts)    # one comparison report


# ---- isolation, order path ------------------------------------------------------------------------

def test_d_is_isolated_from_a_b_and_c(cfg, tmp_path):
    """Same inputs with and without D: A's, B's and C's state and alerts are identical, and D's stop-out doesn't
    touch their loss counters or open positions."""
    bars = _rally()
    su, _, i = _entry(bars)
    out = {}
    for name, strategies in (("abc", ("retest", "cross", "qualified")),
                             ("abcd", ("retest", "cross", "qualified", Q.D))):
        c = _with(cfg, *strategies)
        store, client = Store(tmp_path / f"{name}.db"), Spy(bars[i].c)
        k = next(n for n, b in enumerate(bars) if b.t == su.cross_t)
        _run(c, store, client, bars[:k + 1], datetime.fromtimestamp((bars[k].t + E.FIVE) / 1000 + 30, timezone.utc))
        _run(c, store, client, bars[:i + 1], _at(su))
        client.mid = 1.60                                                      # every open trade stops out
        asyncio.run(E.manage(client, store, c, _at(su) + timedelta(minutes=1), {"SPY": bars[:i + 1]}))
        out[name] = ({k2: store.get(STRATEGIES_KEY[k2]) for k2 in ("retest", "cross", "qualified")},
                     [p[2] for p in store.pending() if "D · TREND" not in p[2] and "EMA D trade" not in p[2]],
                     [t for t in store.get(E.LOG_KEY) if t["strategy"] != Q.D])
        if name == "abcd":
            d = store.get("ema_d_day")
            assert d["losses"] == {"SPY": 1} and len(d["closed"]) == 1 and not d["open"]
    assert out["abc"] == out["abcd"]


STRATEGIES_KEY = {k: v["key"] for k, v in E.STRATEGIES.items()}


def test_no_order_path_is_reachable(cfg, tmp_path):
    store, client, bars, at = _opened(cfg, tmp_path)
    client.mid = 1.60
    asyncio.run(E.manage(client, store, cfg, at + timedelta(minutes=1), {"SPY": bars}))
    assert set(client.calls) == {"option_chain", "snapshots"}                 # market data reads only
    src = "\n".join(p.read_text() for p in (ROOT / "mias").glob("*.py"))
    assert not re.search(r"def (place|submit|send)_order|/v\d/orders|broker", src, re.I)


def test_comparison_report_has_d(cfg, tmp_path):
    store, client, bars, at = _opened(cfg, tmp_path)
    client.mid = 1.60
    asyncio.run(E.manage(client, store, cfg, at + timedelta(minutes=1), {"SPY": bars}))
    text = Q.report(store, cfg, DAY)
    assert "A/B/C/D comparison" in text
    net = E._money(-220 - 6.5)
    assert "<b>D · Trend + time + 50/20</b>: 1 trade · 0W/1L (0%) · gross -$220 · fees $6" in text
    assert "1 signal" in text and f"calls {net} / puts +$0" in text
    assert "Strategy D</b> (EMA 9/21 D — Trend + Time + 50/20): 1 EMA crosses · 1 traded · 0 rejected" in text
    assert "D exits: first target +50% reached 0 · STOP_-20 1 · TRAILING_EXIT 0" in text
    assert "🟢 CALL SPY in" in text and "at 2.02 · target 3.03 · stop 1.62 → out" in text
    assert f"avg exit 1.58 · stop · gross -$220 · fees $6.50 · net {net}" in text
    assert "regime trend · window 09:35-11:00" in text and "diagnostics: VWAP with" in text
    empty = Q.report(Store(tmp_path / "e.db"), cfg, DAY)
    assert "No D trade today: a valid outcome" in empty
