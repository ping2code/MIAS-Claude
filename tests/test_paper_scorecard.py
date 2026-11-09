"""Strategy A retired from active paper trading (config only), other strategies unchanged, close-fade market context
(report only) and the Paper Strategy Scorecard."""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from mias import closeplay, config as config_mod, emacross, paperscore as P, simple
from mias.indicators import Bar
from mias.store import Store

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    monkeypatch.setenv("MIAS_DB_PATH", str(tmp_path / "t.db"))
    return config_mod.load(ROOT / "config.toml")


# ---- A retired, everything else as before --------------------------------------------------------------

def test_a_is_not_in_the_active_paper_strategies(cfg):
    assert "retest" not in cfg.daytrade.strategies
    assert cfg.daytrade.strategies == ("cross", "qualified", "ema_9_21_d", "ema_9_21_e7", "ema_9_21_e21")
    assert cfg.ema_simple_enabled and cfg.closeplay_enabled
    assert P._status("A", cfg).startswith("retired") and P._status("B", cfg) == "active"


def test_a_implementation_remains_available(tmp_path, monkeypatch):
    assert emacross.STRATEGIES["retest"] == {"key": emacross.KEY, "retest": True, "title": "CROSS + RETEST",
                                             "short": "EMA retest", "rule": "ema_entry"}
    text = (ROOT / "config.toml").read_text().replace(
        'strategies = ["cross",', 'strategies = ["retest", "cross",')
    (tmp_path / "config.toml").write_text(text)
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    again = config_mod.load(tmp_path / "config.toml")                  # re-adding the name runs A again
    assert again.daytrade.strategies[0] == "retest"


def test_other_strategies_and_trade_rules_unchanged(cfg):
    S = emacross.STRATEGIES
    assert S["cross"] == {"key": "ema_cross_day", "retest": False, "title": "CROSS (no retest)", "short": "EMA cross",
                          "rule": "ema_cross_entry"}
    assert S["qualified"]["qualify"] and S["qualified"]["retest"] and "rules" not in S["qualified"]
    assert S["ema_9_21_d"]["rules"] == {"first_target": 0.50}
    assert S["ema_9_21_e7"]["rules"] == {"first_target": 0.25} and S["ema_9_21_e7"]["all_out"]
    assert S["ema_9_21_e21"]["rules"] == {"first_target": 0.30} and S["ema_9_21_e21"]["all_out"]
    r = cfg.daytrade
    assert (r.contracts, r.partial, r.stop, r.first_target, r.trail, r.max_losses_in_row) == \
        (5, 3, 0.20, 0.30, ((0.75, 0.50), (1.00, 0.75)), 2)
    assert (simple.SYMBOLS, simple.MAX_TRADES, simple.TARGET, simple.STOP) == (("SPY", "QQQ", "IWM"), 2, 0.25, 0.20)
    assert (closeplay.RULE, closeplay.SIGNAL, closeplay.LAST_SIGNAL, closeplay.SELL) == \
        ("fade", time(15, 45), time(15, 52), time(15, 59))


# ---- close-fade context is report only ----------------------------------------------------------------

def _day_bars(day, drift):
    out, p = [], 100.0
    for i in range(75):                                                 # 09:30 .. 15:40
        t = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET).timestamp() + i * 300
        out.append(Bar(int(t * 1000), p, p + 0.2, p - 0.2, p + drift, 1000))
        p += drift
    return out


def test_close_fade_context_is_recorded_but_never_changes_the_decision(tmp_path):
    store = Store(tmp_path / "s.db")
    day = date(2026, 11, 5)
    now = datetime(2026, 11, 5, 15, 46, tzinfo=ET)
    store.add_metrics(now.timestamp() - 60, "x", {"SPY": {"price": 734.0, "day_change_pct": -4.05},
                                                   "SPX": {"price": 7360.0, "day_change_pct": -4.1}})
    bars = _day_bars(day, -0.05)
    x = closeplay.read(bars, ET, outcome=False)
    side = closeplay.decide(x, closeplay.RULE)
    c = closeplay.market_context(store, now, bars, x)
    assert closeplay.decide(x, closeplay.RULE) == side == 1          # fade a down day: CALL, with or without context
    assert c["spy_day_pct"] == -4.05 and c["spx_day_pct"] == -4.1 and c["direction"] == "DOWN"
    assert c["beyond_1pct"] and c["beyond_2pct"] and c["beyond_3pct"] and c["day_range_pct"] > 0
    empty = Store(tmp_path / "e.db")                                    # no recorded checks: falls back to the bars
    c2 = closeplay.market_context(empty, now, bars, x)
    assert c2["spy_day_pct"] is None and c2["direction"] == "DOWN"
    assert c2["beyond_3pct"] == (abs(x["day_pct"]) >= 3)               # the bars' own move from the open
    assert closeplay.market_context(empty, now, [], x) is None          # a failure never raises


# ---- scorecard calculations ---------------------------------------------------------------------------

def _t(code, d, sym, side, pnl, exit_="stop", qty=5, hhmm="10:00", **kw):
    ts = datetime.fromisoformat(f"{d}T{hhmm}").replace(tzinfo=ET).timestamp()
    return {"code": code, "date": date.fromisoformat(d), "symbol": sym, "side": side, "qty": qty, "gross": pnl,
            "fee": 2 * 0.65 * qty, "net": pnl - 2 * 0.65 * qty, "exit": exit_, "hold": 10, "ts": ts,
            "entry_time": hhmm, "n": kw.get("n"), "ema_regime": kw.get("regime"), "context": None,
            "regime": kw.get("spy")}


def test_commissions_net_and_core_stats():
    rows = [_t("B", "2026-11-02", "SPY", "bull", 200), _t("B", "2026-11-02", "QQQ", "bear", -100),
            _t("B", "2026-11-03", "SPY", "bull", 50)]
    s = P.stats(rows)
    assert s["fees"] == pytest.approx(19.5) and s["gross"] == 150 and s["net"] == pytest.approx(130.5)
    assert (s["n"], s["days"], s["wins"], s["losses"]) == (3, 2, 2, 1)
    assert s["pf"] == pytest.approx((193.5 + 43.5) / 106.5) and s["max_dd"] == pytest.approx(-106.5)
    assert s["best"] == pytest.approx(193.5) and s["worst"] == pytest.approx(-106.5)
    assert s["calls"] == (2, pytest.approx(237.0)) and s["puts"] == (1, pytest.approx(-106.5))


def test_trades_are_read_from_every_log_with_commissions(tmp_path, cfg):
    store = Store(tmp_path / "s.db")
    store.put(emacross.LOG_KEY, [{"date": "2026-11-02", "symbol": "SPY", "side": "bull", "pnl": 100.0,
                                  "exit_reason": "trailed", "fill_at": "10:05:01", "minutes": 12},
                                 {"date": "2026-11-02", "symbol": "SPY", "side": "bull", "pnl": -50.0,
                                  "strategy": "cross", "exit_reason": "invalidated", "in": "10:06"}])
    store.put(simple.LOG_KEY, [{"date": "2026-11-03", "symbol": "IWM", "side": "bear", "pnl": 30.0,
                                "exit_reason": "target", "n": 2, "fill_at": "13:10:01"}])
    store.put(closeplay.LOG_KEY, [{"date": "2026-11-05", "symbol": "SPX", "side": "bull", "pnl": -1210.0, "qty": 1}])
    rows = P.trades(store, cfg)
    assert [r["code"] for r in rows] == ["A", "B", "Simple", "Close fade"]
    assert [r["exit"] for r in rows] == ["trailing", "invalidation", "target", "time"]
    assert rows[0]["net"] == pytest.approx(100 - 6.5) and rows[3]["net"] == pytest.approx(-1210 - 1.3)
    assert rows[2]["n"] == 2


@pytest.mark.parametrize("n, days, label", [
    (100, 3, "VERY LIMITED"), (100, 5, "LIMITED"), (100, 10, "EARLY"), (100, 20, "DEVELOPING"),
    (100, 40, "MORE MATURE"), (29, 40, "DEVELOPING"), (29, 5, "VERY LIMITED"), (500, 9, "LIMITED")])
def test_evidence_labels_from_days_first(n, days, label):
    assert P.evidence(n, days) == label
    assert P.insufficient(n, days) == (label in ("VERY LIMITED", "LIMITED"))


def test_symbol_concentration_without_and_outliers():
    rows = [_t("B", "2026-11-02", "MSFT", "bull", 3000), _t("B", "2026-11-03", "MSFT", "bull", 1000),
            _t("B", "2026-11-02", "IWM", "bull", 500), _t("B", "2026-11-02", "SPY", "bear", -800),
            _t("B", "2026-11-03", "QQQ", "bear", -700)]
    c = P.concentration(rows)
    msft = 4000 - 13
    assert c["best"] == ("MSFT", pytest.approx(msft)) and c["worst"][0] == "SPY"
    assert c["best_share"] == pytest.approx(100 * msft / (msft + 493.5))
    assert P.without(rows, "MSFT")["net"] == pytest.approx(493.5 - 806.5 - 706.5)
    o = P.outliers(rows)
    assert o["no_best"] == pytest.approx(o["net"] - 2993.5)
    assert o["no_top3"] == pytest.approx(o["net"] - 2993.5 - 993.5 - 493.5)
    assert o["no_worst"] == pytest.approx(o["net"] + 806.5)


def test_daily_weekly_and_day_summary():
    rows = [_t("B", "2026-10-30", "SPY", "bull", 100), _t("B", "2026-11-02", "SPY", "bull", -200),
            _t("B", "2026-11-03", "SPY", "bull", 300)]
    assert list(P.by_period(rows, week=True)) == [date(2026, 10, 26), date(2026, 11, 2)]
    d = P.day_summary(rows)
    assert (d["positive"], d["negative"]) == (2, 1) and d["best"][0] == date(2026, 11, 3)


def test_exit_reasons_are_bucketed_with_the_descriptive_note(tmp_path, cfg):
    store = Store(tmp_path / "s.db")
    store.put(emacross.LOG_KEY, [{"date": "2026-11-02", "symbol": "SPY", "side": "bull", "pnl": p, "strategy": "cross",
                                  "exit_reason": e, "in": "10:00"}
                                 for p, e in ((-300.0, "stop"), (500.0, "time"), (50.0, "weird"))])
    text = P.report(store, cfg, full=True)
    block = text.split("EMA Cross (B)")[1].split("\n<b>")[0]
    assert "stop 1 (-$306" in block and "time 1 (+$494" in block and "other 1" in block
    assert P.EXIT_NOTE in text


def test_spy_day_regime_and_spy_at_entry(tmp_path):
    from mias.scorecard import REGIME_FLAT_PCT
    store = Store(tmp_path / "s.db")
    for d, chg in (("2026-11-04", 1.2), ("2026-11-05", -4.0), ("2026-11-06", 0.1)):
        for hh, c in (("10:00", chg / 2), ("15:55", chg)):
            ts = datetime.fromisoformat(f"{d}T{hh}").replace(tzinfo=ET).timestamp()
            store.add_metrics(ts, "x", {"SPY": {"price": 700.0, "day_change_pct": c}})
    rows = P.with_market(store, [_t("B", d, "SPY", "bull", 10, hhmm="11:00")
                                 for d in ("2026-11-04", "2026-11-05", "2026-11-06")], ET)
    assert [r["regime"] for r in rows] == ["UP", "DOWN", "FLAT"] and REGIME_FLAT_PCT == 0.25
    assert [r["spy_at_entry"] for r in rows] == [0.6, -2.0, 0.05]       # last check at or before 11:00 that day


def test_forward_and_historical_are_shown_separately(tmp_path, cfg):
    store = Store(tmp_path / "s.db")
    store.put(emacross.LOG_KEY, [{"date": "2026-11-02", "symbol": "SPY", "side": "bull", "pnl": 100.0,
                                  "exit_reason": "time", "in": "10:00"}])
    text = P.report(store, cfg)
    a = text.split("EMA Retest (A)")[1].split("\n<b>")[0]
    assert "FORWARD PAPER: 1 trades / 1 trading days" in a and "net +$94" in a
    assert "HISTORICAL: Oct 2022–Oct 2026" in a and "8,932 trades" in a
    assert "All strategies incl. retired A history</b>: 1 trades" in text
    assert "Current active set (excluding A)</b>: no trades" in text
    assert "VALIDATED" not in text.upper().replace("NEVER LABELLED VALIDATED", "")


def test_correlated_strategies_count_as_one_setup():
    rows = [_t("C", "2026-11-03", "MSFT", "bull", 100, hhmm="10:00"), _t("D", "2026-11-03", "MSFT", "bull", 100,
                                                                          hhmm="10:00"),
            _t("E7", "2026-11-03", "MSFT", "bull", 100, hhmm="10:04"), _t("E21", "2026-11-03", "MSFT", "bull", 100,
                                                                            hhmm="10:30"),
            _t("C", "2026-11-03", "MSFT", "bear", 100, hhmm="10:01"), _t("D", "2026-11-04", "AMD", "bull", -100)]
    g = P.setups(rows)
    assert sorted(len(x) for x in g) == [1, 1, 1, 3]                    # E21 30 min later is a separate setup
    text = "\n".join(P._overlap(rows))
    assert "6 strategy observations from 4 unique underlying setups" in text
