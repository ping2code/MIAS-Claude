"""Strategies E7 / E21 (experimental, live paper): Strategy D's entry with a 7DTE / 21DTE ~0.50-delta contract and one
all-out exit (+25% / +30%, -20% stop). Expiry and delta selection, exits, isolation from A/B/C/D and from each
other, the 10-minute close confirmation (diagnostic only) and the report."""
import asyncio
import dataclasses
from datetime import date, datetime, timedelta, timezone

import pytest

from mias import dte as X
from mias import emacross as E
from mias import qualify as Q
from mias.indicators import Bar
from mias.store import Store
from test_strategy_c import DAY, ET, _entry, _rally
from test_strategy_d import _mirror, _with
from test_strategy_d import cfg as d_cfg  # noqa: F401  (fixture)

EXPIRIES = ("2026-10-26", "2026-10-27", "2026-10-30", "2026-11-02", "2026-11-06", "2026-11-13", "2026-11-16",
            "2026-11-20")


class Chain:
    """Fake market data: a chain with real-looking deltas (calls 0.5 - 0.12 per dollar above the stock), quotes at
    mid +-0.02, and nothing else (there is no order API to reach)."""

    def __init__(self, spot, mid=3.98, slope=0.12, greeks=True):
        self.spot, self.mid, self.slope, self.greeks, self.calls = spot, mid, slope, greeks, []

    def _q(self):
        return {"bid": round(self.mid - 0.02, 2), "ask": round(self.mid + 0.02, 2)}

    def delta(self, kind, k):
        d = 0.5 - self.slope * (k - self.spot)
        return max(0.01, min(0.99, d)) if kind == "call" else -max(0.01, min(0.99, 1 - d))

    async def option_chain(self, symbol, *, spot, today, max_dte, strike_pct, max_pages, min_dte=0,
                           contract_type=None):
        self.calls.append("option_chain")
        lo, hi = today + timedelta(days=min_dte), today + timedelta(days=max_dte)
        kinds = [contract_type] if contract_type else ["call", "put"]
        return [{"details": {"contract_type": k, "expiration_date": e, "strike_price": float(s),
                             "ticker": f"O:SPY{e[2:4]}{e[5:7]}{e[8:]}{k[0].upper()}{s * 1000:08.0f}"},
                 "last_quote": self._q(), "underlying_asset": {"price": self.spot},
                 **({"greeks": {"delta": self.delta(k, s)}} if self.greeks else {})}
                for e in EXPIRIES if lo <= date.fromisoformat(e) <= hi for k in kinds
                for s in range(round(self.spot) - 4, round(self.spot) + 5)], False

    async def snapshots(self, tickers):
        self.calls.append("snapshots")
        return {t: {"last_quote": self._q(), "underlying_asset": {"price": self.spot}} for t in tickers}

    def __getattr__(self, name):
        self.calls.append(name)
        raise AttributeError(name)


@pytest.fixture
def cfg(d_cfg):  # noqa: F811
    return _with(d_cfg, X.E7, X.E21)


def _at(su, sec=45):
    return datetime.fromtimestamp(su.entry_t / 1000 + sec, timezone.utc)


def _run(cfg, store, client, bars, at):
    return asyncio.run(E.live(client, store, cfg, at, {"SPY": bars}))


def _opened(cfg, tmp_path, name="x", bars=None):
    bars = bars or _rally()
    su, _, i = _entry(bars)
    store, client = Store(tmp_path / f"{name}.db"), Chain(bars[i].c)
    assert _run(cfg, store, client, bars[:i + 1], _at(su)) == len(cfg.daytrade.strategies)
    return store, client, bars, su, i


def _bear(bars):
    su = next(s for s in E.scan("SPY", bars, DAY, ET, E.DayTradeRules(start="09:35")) if s.status == "entry")
    return su, None, next(n for n, b in enumerate(bars) if b.t + E.FIVE == su.entry_t)


def _open(store, strategy):
    return E.Trade(**store.get(E.STRATEGIES[strategy]["key"])["open"]["SPY"])


# ---- rules -----------------------------------------------------------------------------------------

def test_e_rules_are_d_entry_with_all_out_exits(cfg):
    for s, tgt in ((X.E7, 0.25), (X.E21, 0.30)):
        r = E.rules_for(cfg, s)
        assert r == dataclasses.replace(cfg.daytrade, first_target=tgt, partial=cfg.daytrade.contracts, trail=())
        assert r.stop == 0.20 and r.close == "15:10" and r.max_losses_in_row == 2
        assert Q.PROFILES[s]["gates"](cfg) is Q.D_GATES                      # D's gates exactly, nothing added
    assert E.rules_for(cfg, Q.D) == dataclasses.replace(cfg.daytrade, first_target=0.50)   # D unchanged
    assert E.rules_for(cfg, Q.D).partial == 3 and E.rules_for(cfg, Q.D).trail
    assert all(E.rules_for(cfg, k) is cfg.daytrade for k in ("retest", "cross", "qualified"))
    keys = [E.STRATEGIES[k]["key"] for k in E.STRATEGIES]
    logs = [Q.PROFILES[k]["log"] for k in Q.PROFILES] + [Q.PROFILES[k]["candidates"] for k in Q.PROFILES]
    assert len(set(keys)) == len(keys) and len(set(logs)) == len(logs)     # every strategy has its own state


# ---- expiry and delta ------------------------------------------------------------------------------

def test_7dte_and_21dte_expiry_selection():
    d = date(2026, 10, 26)
    assert X.pick_expiry(EXPIRIES, d, 7) == "2026-11-02" and X.pick_expiry(EXPIRIES, d, 21) == "2026-11-16"
    fridays = ("2026-10-30", "2026-11-06", "2026-11-13", "2026-11-20")
    assert X.pick_expiry(fridays, d, 7) == "2026-10-30"                     # 3 days early: in the window
    assert X.pick_expiry(fridays, d, 21) == "2026-11-13"
    assert X.pick_expiry(("2026-10-30", "2026-11-05"), d, 7) == "2026-11-05"    # tie: the later one
    assert X.pick_expiry(("2026-10-27", "2026-11-20"), d, 7) is None        # nothing within +-3 days
    assert X.pick_expiry(("2026-10-26",), d, 1) is None                     # never the signal day itself


def _rows(c: Chain, kind):
    return asyncio.run(c.option_chain("SPY", spot=c.spot, today=DAY, max_dte=30, strike_pct=0.05, max_pages=5,
                                      contract_type=kind))[0]


def test_delta_match_for_calls_and_puts():
    c = Chain(101.2)
    call, f = X.choose(_rows(c, "call"), "bull", 101.2, "2026-11-02", 0, ET)
    assert call["details"]["strike_price"] == 101 and f == {"delta": 0.524, "delta_source": "vendor"}
    put, f = X.choose(_rows(c, "put"), "bear", 101.2, "2026-11-02", 0, ET)
    assert put["details"]["strike_price"] == 101 and f["delta"] == -0.476 and 0.45 <= abs(f["delta"]) <= 0.55


def test_delta_tie_prefers_the_tighter_spread():
    c = Chain(100.5, slope=0.08)                                            # 100 (0.54) and 101 (0.46): a tie
    rows = _rows(c, "call")
    pick, _ = X.choose(rows, "bull", 100.5, "2026-11-02", 0, ET)
    assert pick["details"]["strike_price"] == 100                            # same spread: the lower strike
    for r in rows:
        if r["details"]["strike_price"] == 101:
            r["last_quote"] = {"bid": 3.97, "ask": 3.99}                    # tighter
    pick, f = X.choose(rows, "bull", 100.5, "2026-11-02", 0, ET)
    assert pick["details"]["strike_price"] == 101 and f["delta"] == 0.46


def test_no_delta_match_is_never_substituted():
    c = Chain(101.2, slope=0.6)                                             # nearest deltas 0.62 / 0.02: none in range
    pick, f = X.choose(_rows(c, "call"), "bull", 101.2, "2026-11-02", 0, ET)
    assert pick is None and f["near_deltas"] and all(not 0.45 <= d <= 0.55 for d in f["near_deltas"])


def test_black_scholes_delta_when_the_snapshot_has_none():
    now = int(datetime(2026, 10, 26, 10, 40, tzinfo=ET).timestamp() * 1000)
    rows = _rows(Chain(101.2, mid=1.6, greeks=False), "call")
    pick, f = X.choose(rows, "bull", 101.2, "2026-11-02", now, ET)
    assert f["delta_source"] == "bs" and 0.45 <= f["delta"] <= 0.55 and pick["details"]["strike_price"] == 101
    d = X.bs_delta(100, 100, 7 / 365, 1.5, True)
    assert 0.5 < d < 0.56 and abs(X.bs_delta(100, 100, 7 / 365, 1.5, False) - (d - 1)) < 1e-3


# ---- live entries ----------------------------------------------------------------------------------

@pytest.mark.parametrize("strategy,expiry", [(X.E7, "261102"), (X.E21, "261116")])
def test_bullish_call_entry(cfg, tmp_path, strategy, expiry):
    c1 = _with(cfg, strategy)
    store, client, bars, su, i = _opened(c1, tmp_path)
    tr = _open(store, strategy)
    tgt = 0.25 if strategy == X.E7 else 0.30
    assert tr.contract == f"O:SPY{expiry}C00101000" and tr.entry == 4.00 and tr.stop == 3.20 and tr.qty == 5
    assert tr.info["target_price"] == round(4.00 * (1 + tgt), 2) and tr.info["risk_per_contract"] == 80.0
    assert tr.info["capital_per_contract"] == 400.0 and 0.45 <= tr.info["delta"] <= 0.55
    assert tr.info["signal_key"] == su.key
    assert tr.info["requested_dte"] == (7 if strategy == X.E7 else 21) and tr.info["actual_dte"] in (7, 21)
    alert = store.pending()[-1][2]
    name = X.PLANS[strategy]["name"]
    for s in (f"{name} PAPER — SPY CALL", "Signal 10:40", "delta 0.5", "Bid 3.96 / ask 4.00 / mid 3.98",
              f"Target +{round(100 * tgt)}%: {4 * (1 + tgt):.2f}", "Stop -20%: 3.20", "ALL-OUT EXIT",
              "Capital/contract $400", "risk/contract $80", "force close 15:10", "pending (10:50)", "PAPER ONLY"):
        assert s in alert, s
    rec = store.get(Q.PROFILES[strategy]["candidates"])["items"][su.key]
    assert rec["disposition"] == "TRADE" and rec["regime"] == "trend" and rec["eval_at"] == "10:40"


@pytest.mark.parametrize("strategy,expiry", [(X.E7, "261102"), (X.E21, "261116")])
def test_bearish_put_entry(cfg, tmp_path, strategy, expiry):
    bars = _mirror(_rally())
    su, _, i = _bear(bars)
    store, client = Store(tmp_path / "b.db"), Chain(bars[i].c)
    assert _run(_with(cfg, strategy), store, client, bars[:i + 1], _at(su)) == 1
    tr = _open(store, strategy)
    assert tr.side == "bear" and tr.contract.startswith(f"O:SPY{expiry}P") and tr.entry == 4.00 and tr.stop == 3.20
    assert tr.info["delta"] < 0 and 0.45 <= abs(tr.info["delta"]) <= 0.55
    assert f"{X.PLANS[strategy]['name']} PAPER — SPY PUT" in store.pending()[-1][2]


def test_no_delta_match_rejects_and_logs(cfg, tmp_path):
    bars = _rally()
    su, _, i = _entry(bars)
    store, client = Store(tmp_path / "n.db"), Chain(bars[i].c, slope=0.6)
    assert _run(cfg, store, client, bars[:i + 1], _at(su)) == 0
    for s in (X.E7, X.E21):
        rec = store.get(Q.PROFILES[s]["candidates"])["items"][su.key]
        assert rec["disposition"] == "REJECT" and rec["reasons"] == ["NO_DELTA_MATCH"] and rec["near_deltas"]
        assert not store.get(E.STRATEGIES[s]["key"])["open"]


def test_same_d_signal_feeds_e7_and_e21_independently(cfg, tmp_path):
    store, client, bars, su, i = _opened(cfg, tmp_path)
    e7, e21 = _open(store, X.E7), _open(store, X.E21)
    assert e7.info["signal_key"] == e21.info["signal_key"] == su.key and e7.contract != e21.contract
    assert e7.expiry == "2026-11-02" and e21.expiry == "2026-11-16"
    assert list(store.get(X.DIAG_KEY)) == [su.key]                          # one signal, one diagnostic


# ---- exits -----------------------------------------------------------------------------------------

def _manage(cfg, store, client, bars, at, mid):
    client.mid = mid
    asyncio.run(E.manage(client, store, cfg, at, {"SPY": bars}))


def test_targets_sell_everything(cfg, tmp_path):
    store, client, bars, su, i = _opened(cfg, tmp_path)
    b = bars[:i + 1]
    _manage(cfg, store, client, b, _at(su) + timedelta(minutes=1), 4.97)      # +24% at the mid: both hold
    assert _open(store, X.E7).open_qty == 5 and _open(store, X.E21).open_qty == 5
    _manage(cfg, store, client, b, _at(su) + timedelta(minutes=2), 5.04)      # mid >= 5.00 (+25%): E7 sells all
    rec = [t for t in store.get(E.LOG_KEY) if t["strategy"] == X.E7][-1]
    assert rec["exit_reason"] == "target" and rec["pnl"] == round((5.00 - 4.00) * 500, 2)   # min(target, bid 5.02)
    assert not store.get("ema_e7_day")["open"] and _open(store, X.E21).open_qty == 5
    assert any("E7 PAPER trade: ALL SOLD" in p[2] and "sold all 5 at 5.00 (+25%)" in p[2] for p in store.pending())
    _manage(cfg, store, client, b, _at(su) + timedelta(minutes=3), 5.21)      # mid >= 5.20 (+30%): E21 sells all
    rec = [t for t in store.get(E.LOG_KEY) if t["strategy"] == X.E21][-1]
    assert rec["exit_reason"] == "target" and rec["pnl"] == round((5.19 - 4.00) * 500, 2)   # the bid, under target


def test_minus_20_percent_stop_on_the_mid_sold_at_the_bid(cfg, tmp_path):
    store, client, bars, su, i = _opened(cfg, tmp_path)
    b = bars[:i + 1]
    _manage(cfg, store, client, b, _at(su) + timedelta(minutes=1), 3.21)      # bid 3.19 is under, mid isn't: hold
    assert _open(store, X.E7).open_qty == 5
    _manage(cfg, store, client, b, _at(su) + timedelta(minutes=2), 3.20)      # mid at the stop
    for s in (X.E7, X.E21):
        rec = [t for t in store.get(E.LOG_KEY) if t["strategy"] == s][-1]
        assert rec["exit_reason"] == "stop" and rec["pnl"] == round((3.18 - 4.00) * 500, 2)
        assert store.get(E.STRATEGIES[s]["key"])["losses"] == {"SPY": 1}


def test_invalidation_and_force_close(cfg, tmp_path):
    store, client, bars, su, i = _opened(cfg, tmp_path)
    tr = _open(store, X.E7)
    drop = Bar(su.entry_t, 101, 101, tr.invalidation - 1, tr.invalidation - 0.5, 1000)   # 5m close through it
    _manage(cfg, store, client, bars[:i + 1] + [drop], _at(su) + timedelta(minutes=5), 3.90)
    for s in (X.E7, X.E21):
        rec = [t for t in store.get(E.LOG_KEY) if t["strategy"] == s][-1]
        assert rec["exit_reason"] == "invalidated" and rec["pnl"] == round((3.88 - 4.00) * 500, 2)
    store, client, bars, su, i = _opened(cfg, tmp_path, "f")
    close = datetime.combine(DAY, datetime.min.time().replace(hour=15, minute=10), ET)
    client.mid = 4.40
    asyncio.run(E.live(client, store, cfg, close, {"SPY": bars[:i + 1]}))
    for s in (X.E7, X.E21):
        rec = [t for t in store.get(E.LOG_KEY) if t["strategy"] == s][-1]
        assert rec["exit_reason"] == "time" and rec["pnl"] == round((4.38 - 4.00) * 500, 2)
    texts = [p[2] for p in store.pending()]
    assert any("E7 / E21 paper report" in t for t in texts)


# ---- isolation ---------------------------------------------------------------------------------------

def test_e7_e21_leave_a_b_c_d_untouched(cfg, tmp_path):
    """Same inputs with and without E7/E21: A's, B's, C's and D's state, alerts and logged trades are identical."""
    bars = _rally()
    su, _, i = _entry(bars)
    out = {}
    base = ("retest", "cross", "qualified", Q.D)
    for name, strategies in (("abcd", base), ("abcde", base + (X.E7, X.E21))):
        c = _with(cfg, *strategies)
        store, client = Store(tmp_path / f"{name}.db"), Chain(bars[i].c, mid=2.0)
        k = next(n for n, b in enumerate(bars) if b.t == su.cross_t)
        _run(c, store, client, bars[:k + 1], datetime.fromtimestamp((bars[k].t + E.FIVE) / 1000 + 30, timezone.utc))
        _run(c, store, client, bars[:i + 1], _at(su))
        client.mid = 1.60                                                      # every open trade stops out
        asyncio.run(E.manage(client, store, c, _at(su) + timedelta(minutes=1), {"SPY": bars[:i + 1]}))
        out[name] = ({s: store.get(E.STRATEGIES[s]["key"]) for s in base},
                     [p[2] for p in store.pending() if "E7" not in p[2] and "E21" not in p[2]],
                     [t for t in store.get(E.LOG_KEY) if t["strategy"] in base],
                     store.get("ema_d_candidates"), store.get("ema_d_log"))
        if name == "abcde":
            for s in (X.E7, X.E21):
                st = store.get(E.STRATEGIES[s]["key"])
                assert st["losses"] == {"SPY": 1} and len(st["closed"]) == 1 and not st["open"]
    assert out["abcd"] == out["abcde"]


def test_e7_and_e21_do_not_share_positions_or_counters(cfg, tmp_path):
    store, client, bars, su, i = _opened(cfg, tmp_path)
    _manage(cfg, store, client, bars[:i + 1], _at(su) + timedelta(minutes=1), 5.04)   # E7 target only
    e7, e21 = store.get("ema_e7_day"), store.get("ema_e21_day")
    assert not e7["open"] and e7["losses"] == {"SPY": 0} and "SPY" in e21["open"] and e21["losses"] == {}


def test_no_order_path_is_reachable(cfg, tmp_path):
    store, client, bars, su, i = _opened(cfg, tmp_path)
    _manage(cfg, store, client, bars[:i + 1], _at(su) + timedelta(minutes=1), 3.0)
    assert set(client.calls) == {"option_chain", "snapshots"}


# ---- 10-minute close confirmation (diagnostic only) --------------------------------------------------

def _diag_bars(entry_t, closes):
    return [Bar(entry_t + k * E.FIVE, 100, 101, 99, c, 1000) for k, c in enumerate(closes)]


@pytest.mark.parametrize("side,close,want", [("bull", 100.10, "PASS"), ("bull", 100.20, "PASS"),
                                             ("bull", 100.09, "FAIL"), ("bull", 99.0, "FAIL"),
                                             ("bear", 99.90, "PASS"), ("bear", 99.50, "PASS"),
                                             ("bear", 99.91, "FAIL"), ("bear", 101.0, "FAIL")])
def test_close_confirmation(side, close, want):
    t0 = int(datetime(2026, 10, 26, 10, 40, tzinfo=ET).timestamp() * 1000)
    bars = _diag_bars(t0, [100.5 if side == "bull" else 99.5, close])        # the FIRST candle doesn't count
    d = X.close_confirm(bars, side, t0, 100.0, t0 + 2 * E.FIVE, ET)
    assert d["close_confirm_10m"] == want and d["close_confirm_10m_timestamp"] == "10:50"
    assert d["close_confirm_10m_move_pct"] == round(close - 100, 3)
    assert d["close_confirm_10m_fav_pct"] == round((close - 100) * (1 if side == "bull" else -1), 3)


def test_close_confirmation_pending_and_not_available():
    t0 = int(datetime(2026, 10, 26, 10, 40, tzinfo=ET).timestamp() * 1000)
    bars = _diag_bars(t0, [100.5, 100.5])
    assert X.close_confirm(bars, "bull", t0, 100, t0 + 2 * E.FIVE - 1, ET)["close_confirm_10m"] == "PENDING"
    assert X.close_confirm(bars[:1], "bull", t0, 100, t0 + 3 * E.FIVE, ET)["close_confirm_10m"] == "PENDING"
    assert X.close_confirm(bars[:1], "bull", t0, 100, t0 + 8 * E.FIVE, ET)["close_confirm_10m"] == "NOT_AVAILABLE"


def _session(cfg, tmp_path, name, verdict, monkeypatch):
    """Entry, one re-price, the 10:50 cycle that resolves the diagnostic, then a stop: everything but the verdict
    is the same."""
    monkeypatch.setattr(X, "close_confirm", lambda *a, **k: {"close_confirm_10m": verdict,
                                                            "close_confirm_10m_timestamp": "10:50"})
    store, client, bars, su, i = _opened(cfg, tmp_path, name)
    b = bars[:i + 3]
    _manage(cfg, store, client, b, _at(su) + timedelta(minutes=2), 4.5)
    client.mid = 4.5
    _run(cfg, store, client, b, _at(su) + timedelta(minutes=11))
    _manage(cfg, store, client, b, _at(su) + timedelta(minutes=12), 3.1)
    return store


def test_diagnostic_never_changes_entry_exit_or_size(cfg, tmp_path, monkeypatch):
    a = _session(cfg, tmp_path, "pass", "PASS", monkeypatch)
    b = _session(cfg, tmp_path, "fail", "FAIL", monkeypatch)
    assert a.get(X.DIAG_KEY)[next(iter(a.get(X.DIAG_KEY)))]["close_confirm_10m"] == "PASS"
    assert b.get(X.DIAG_KEY)[next(iter(b.get(X.DIAG_KEY)))]["close_confirm_10m"] == "FAIL"
    for s in (X.E7, X.E21):
        assert a.get(E.STRATEGIES[s]["key"]) == b.get(E.STRATEGIES[s]["key"])      # same entry, size, exit
    strip = lambda st: [t for t in st.get(E.LOG_KEY)]
    assert strip(a) == strip(b) and [p[2] for p in a.pending()] == [p[2] for p in b.pending()]
    assert [t["exit_reason"] for t in strip(a)] == ["stop", "stop"]


def test_diagnostic_observed_after_exit(cfg, tmp_path):
    store, client, bars, su, i = _opened(cfg, tmp_path)
    _manage(cfg, store, client, bars[:i + 1], _at(su) + timedelta(minutes=1), 5.04)   # E7 out at 10:41
    client.mid = 5.04
    _run(cfg, store, client, bars[:i + 3], _at(su) + timedelta(minutes=11))       # 10:50 candle complete
    d = store.get(X.DIAG_KEY)[su.key]
    assert d["close_confirm_10m"] == "PASS" and d["close_confirm_10m_fav_pct"] > 0.10
    assert d["observed_after_exit"] == {X.E7: True, X.E21: False}


def test_daily_report_counts_one_signal_and_pairs_it(cfg, tmp_path):
    store, client, bars, su, i = _opened(cfg, tmp_path)
    client.mid = 5.04
    _run(cfg, store, client, bars[:i + 3], _at(su) + timedelta(minutes=11))
    close = datetime.combine(DAY, datetime.min.time().replace(hour=15, minute=10), ET)
    asyncio.run(E.live(client, store, cfg, close, {"SPY": bars[:i + 3]}))
    text = next(p[2] for p in store.pending() if "E7 / E21 paper report" in p[2])
    assert "Unique D signals</b>: 1 · both valid 1 · only E7 0 · only E21 0" in text
    assert "E7: valid 1" in text and "E21: valid 1" in text
    assert "Same signal, both expiries" in text and "10m PASS" in text
    assert "E7 — 7DTE 25/20 All-Out</b>: 1 trade" in text and "E21 — 21DTE 30/20 All-Out</b>: 1 trade" in text
    assert "correlated observations" in text and "PASS 1" in text
