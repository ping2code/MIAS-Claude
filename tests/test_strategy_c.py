"""Strategy C (experimental EMA 9/21 cross + retest + qualification): diagnostics, gates, dispositions, isolation
from the A/B controls, MFE/MAE tracking and the comparison report."""
import asyncio
import dataclasses
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mias import config as config_mod
from mias import emacross as E
from mias import qualify as Q
from mias.indicators import Bar, atr, ema
from mias.store import Store

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 26)
RULES = E.DayTradeRules(start="09:35")


def _t(d, h, m):
    return int(datetime.combine(d, time(h, m), ET).timestamp() * 1000)


def _series(today, prev_slope=-0.01, start=101.0):
    """Friday's 78 warm-up candles drifting down, then today's (o, h, l, c) rows from 09:30."""
    bars, p = [], start
    for k in range(78):
        c = p + prev_slope
        bars.append(Bar(_t(date(2026, 10, 23), 9, 30) + k * E.FIVE, p, max(p, c) + 0.03, min(p, c) - 0.03, c, 1000))
        p = c
    return bars + [Bar(_t(DAY, 9, 30) + k * E.FIVE, o, h, l, c, 1000) for k, (o, h, l, c) in enumerate(today)]


def _rally(pullback=True):
    """Drift down to 10:05, a hard rally (bull cross 10:20), a pullback into EMA 9, a confirmation candle (entry at
    the 10:40 close), then a steady climb."""
    rows, p = [], 100.22
    for _ in range(8):
        c = p - 0.02
        rows.append((p, p + 0.02, c - 0.02, c))
        p = c
    for _ in range(4):
        c = p + 0.25
        rows.append((p, c + 0.02, p - 0.02, c))
        p = c
    if pullback:
        rows.append((p, p + 0.02, p - 0.6, p - 0.2))
        p -= 0.2
        rows.append((p, p + 0.32, p - 0.02, p + 0.3))
        p += 0.3
    for _ in range(10):
        c = p + 0.12
        rows.append((p, c + 0.02, p - 0.02, c))
        p = c
    return _series(rows)


def _entry(bars):
    su = next(s for s in E.scan("SPY", bars, DAY, ET, RULES) if s.status == "entry")
    first = next(n for n, b in enumerate(bars) if E._et(b.t, ET).date() == DAY)
    i = next(n for n, b in enumerate(bars) if b.t + E.FIVE == su.entry_t)
    return su, first, i


# ---- diagnostics and gates -----------------------------------------------------------------------

def test_clean_trend_retest_qualifies_with_every_diagnostic_logged():
    bars = _rally()
    su, first, i = _entry(bars)
    assert E._et(su.entry_t, ET).time() == time(10, 40)
    d = Q.evaluate(su, bars, first, i, ET, RULES, Q.QualRules(), {}, su.entry_t)
    assert d["reasons"] == []
    assert d["regime"] == "trend" and d["vwap_dist"] > 0 and d["vwap_prior_same_side"]
    assert d["or_position"] == "above_high" and d["or_low"] < d["or_mid"] < d["or_high"]
    assert d["ema9_slope_pct"] > 0 and d["ema21_slope_pct"] > 0
    # EMA separation: against price (%) and against ATR, from the entry candle's own EMAs (no look-ahead).
    closes = [b.c for b in bars[:i + 1]]
    gap = ema(closes, 9)[-1] - ema(closes, 21)[-1]
    assert d["ema_sep_pct"] == round(100 * abs(gap) / bars[i].c, 4)
    assert d["ema_sep_atr"] == round(abs(gap) / atr(bars[i - 15:i + 1]), 3)
    assert d["market_aligned"] is None and d["rel_volume"] == 1.0          # no SPY/QQQ bars given: unknown
    assert all(v for k, v in d["components"].items() if k != "market")


def test_wrong_side_candidate_fails_vwap_slope_direction_and_opening_range():
    bars = _rally()
    su, first, i = _entry(bars)
    bear = dataclasses.replace(su, side="bear")
    d = Q.evaluate(bear, bars, first, i, ET, RULES, Q.QualRules(), {}, su.entry_t)
    for r in ("EMA_DIRECTION_MISALIGNED", "VWAP_MISALIGNED", "EMA_SLOPE_MISALIGNED", "OPENING_RANGE_MISALIGNED"):
        assert r in d["reasons"]
    assert d["regime"] == "trend" and "REGIME_CHOP" not in d["reasons"]
    # Each hard gate can be switched off; the diagnostics are still logged.
    off = Q.QualRules(require_vwap=False, require_slope=False, require_or=False)
    d2 = Q.evaluate(bear, bars, first, i, ET, RULES, off, {}, su.entry_t)
    assert d2["reasons"] == ["EMA_DIRECTION_MISALIGNED"] and d2["vwap_dist"] == d["vwap_dist"]


def test_chop_regime_is_rejected():
    rows, p = [], 100.0
    for k in range(30):                                   # back and forth around 100: no trend
        c = 100.0 + (0.08 if k % 2 else -0.08)
        rows.append((p, max(p, c) + 0.02, min(p, c) - 0.02, c))
        p = c
    bars = _series(rows, prev_slope=0.0, start=100.0)
    first = 78
    su = E.Setup("SPY", "bull", bars[first + 15].t, status="entry", entry_t=bars[first + 16].t + E.FIVE,
                 retest_t=bars[first + 15].t)
    d = Q.evaluate(su, bars, first, first + 16, ET, RULES, Q.QualRules(), {}, su.entry_t)
    assert d["regime"] == "chop" and "REGIME_CHOP" in d["reasons"]
    assert "REGIME_CHOP" not in Q.evaluate(su, bars, first, first + 16, ET, RULES, Q.QualRules(trend_only=False), {},
                                           su.entry_t)["reasons"]


def test_time_windows():
    q = Q.QualRules()
    ok = lambda h, m, fh=None, fm=None: Q.in_window(_t(DAY, h, m), _t(DAY, fh or h, fm if fm is not None else m),
                                                    DAY, ET, q)
    assert ok(9, 35) and ok(10, 40) and ok(11, 0)
    assert not ok(9, 30) and not ok(11, 5) and not ok(12, 30) and not ok(13, 25)
    assert ok(13, 30) and ok(14, 30) and not ok(14, 35) and not ok(15, 0)
    assert not ok(14, 30, 14, 46)                        # nothing fills after 14:45, whatever the candle
    q2 = Q.QualRules(sessions=(("09:35", "11:00"),))
    assert not Q.in_window(_t(DAY, 13, 45), _t(DAY, 13, 45), DAY, ET, q2)


def test_whipsaw_detection_and_cooldown():
    q = Q.QualRules()                                     # 2 flips in 30 min -> 60-minute lock
    xs = [(_t(DAY, 10, 0), "bull"), (_t(DAY, 10, 10), "bear"), (_t(DAY, 10, 25), "bull")]
    w = Q.whipsaw(xs, _t(DAY, 10, 20), q)
    assert w == {"whipsaw_count": 1, "chop_lock": False, "cooldown_until": None}
    w = Q.whipsaw(xs, _t(DAY, 10, 25), q)
    assert w["chop_lock"] and w["whipsaw_count"] == 2 and w["cooldown_until"] == _t(DAY, 11, 25)
    assert Q.whipsaw(xs, _t(DAY, 11, 24), q)["chop_lock"]
    assert not Q.whipsaw(xs, _t(DAY, 11, 25), q)["chop_lock"]
    spread_out = [(_t(DAY, 10, 0), "bull"), (_t(DAY, 10, 20), "bear"), (_t(DAY, 10, 40), "bull")]
    assert not Q.whipsaw(spread_out, _t(DAY, 10, 40), q)["chop_lock"]     # 40 minutes apart: no lock
    assert Q.whipsaw(spread_out, _t(DAY, 10, 40), Q.QualRules(whipsaw_window_minutes=45))["chop_lock"]
    # crosses() finds the same crosses scan() does, at the candle close.
    bars = _rally()
    closes = [b.c for b in bars]
    found = Q.crosses(bars, ema(closes, 9), ema(closes, 21), 78)
    scanned = [(s.cross_t + E.FIVE, s.side) for s in E.scan("SPY", bars, DAY, ET, RULES)]
    assert found == scanned


def test_opening_range_fails_closed_until_complete_or_without_the_open():
    bars = _rally()
    assert Q.opening_range(bars, 78, 79, ET) is None                      # 09:35 candle: range not complete
    hi, lo, mid = Q.opening_range(bars, 78, 80, ET)
    assert hi == max(b.h for b in bars[78:81]) and lo == min(b.l for b in bars[78:81]) and mid == (hi + lo) / 2
    assert Q.opening_range(bars, 79, 90, ET) is None                      # session didn't start at 09:30
    su = E.Setup("SPY", "bull", bars[78].t, status="entry", entry_t=bars[79].t + E.FIVE, retest_t=bars[78].t)
    d = Q.evaluate(su, bars, 78, 79, ET, RULES, Q.QualRules(), {}, su.entry_t)
    assert "OPENING_RANGE_UNAVAILABLE" in d["reasons"] and d["or_mid"] is None


def test_market_alignment_from_spy_and_qqq():
    up = _rally()
    upto = up[-1].t + E.FIVE
    both = Q.market({"SPY": up, "QQQ": up}, upto, 1, ET, DAY)
    assert both["market_aligned"] is True and both["spy_direction"] == "up" and both["qqq_vwap"] == "above"
    assert Q.market({"SPY": up, "QQQ": up}, upto, -1, ET, DAY)["market_aligned"] is False
    assert Q.market({"SPY": up}, upto, 1, ET, DAY)["market_aligned"] is None              # QQQ missing: unknown
    # Logged by default; a gate only when require_market is on.
    su, first, i = _entry(up)
    bear = dataclasses.replace(su, side="bear")
    d = Q.evaluate(bear, up, first, i, ET, RULES, Q.QualRules(require_market=True), {"SPY": up, "QQQ": up},
                   su.entry_t)
    assert "MARKET_CONFLICT" in d["reasons"]


def test_quality_score_is_unweighted_and_lists_failures():
    assert Q.score({"vwap": True, "regime_trend": False, "volume": True}) == (2, 3, ["regime_trend"])


def test_trade_tracks_early_progress_and_time_to_mfe():
    su = E.Setup("SPY", "bull", 0, status="entry", entry_t=0, price=100.0, invalidation=99.0)
    t0 = _t(DAY, 10, 0)
    tr = E.Trade.open(su, "O:SPY261026C00100000", 100.0, "2026-10-26", t0, 2.0, RULES)
    tr.mark(t0, 100.0)
    tr.mark(t0 + 2 * 60_000, 100.2)
    tr.on_price(t0 + 2 * 60_000, 2.4, 2.4, 2.4, 2.4, RULES)
    tr.mark(t0 + 7 * 60_000, 100.5)
    tr.mark(t0 + 14 * 60_000, 99.9)
    assert tr.early == {"5": 0.2, "10": 0.5, "15": 0.5}
    tr.exit(t0 + 20 * 60_000, 1.9, "time")
    rec = E._close_record(tr, ET, t0 + 20 * 60_000)
    assert rec["first_5m_fav_pct"] == 0.2 and rec["first_10m_fav_pct"] == 0.5 and rec["first_15m_fav_pct"] == 0.5
    assert rec["time_to_mfe_min"] == 2.0 and rec["under_mfe_pct"] == 0.5 and rec["under_mae_pct"] == -0.1


# ---- live: dispositions, option spread, isolation ------------------------------------------------

class FakeClient:
    def __init__(self, spot, spread=0.04):
        self.mid, self.spot, self.spread = 2.0, spot, spread

    def _row(self, kind, strike, expiry="2026-10-26"):
        k = "C" if kind == "call" else "P"
        return {"details": {"contract_type": kind, "expiration_date": expiry, "strike_price": strike,
                            "ticker": f"O:SPY{expiry[2:4]}{expiry[5:7]}{expiry[8:]}{k}{strike * 1000:08.0f}"},
                "last_quote": {"bid": self.mid - self.spread / 2, "ask": self.mid + self.spread / 2},
                "underlying_asset": {"price": self.spot}}

    async def option_chain(self, symbol, **kw):
        return [self._row(k, s) for k in ("call", "put") for s in (round(self.spot) - 1, round(self.spot),
                                                                    round(self.spot) + 1)], False

    async def snapshots(self, tickers):
        return {t: {"last_quote": {"bid": self.mid - self.spread / 2, "ask": self.mid + self.spread / 2},
                    "underlying_asset": {"price": self.spot}} for t in tickers}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setenv("MIAS_DB_PATH", str(tmp_path / "t.db"))
    c = config_mod.load(ROOT / "config.toml")
    return dataclasses.replace(c, daytrade=dataclasses.replace(c.daytrade, symbols=("SPY",),
                                                               strategies=("retest", "qualified")))


def _run(cfg, store, client, bars, at):
    return asyncio.run(E.live(client, store, cfg, at, {"SPY": bars}))


def test_qualified_entry_trades_with_its_own_state_and_alert(cfg, tmp_path):
    bars = _rally()
    su, _, i = _entry(bars)
    store, client = Store(tmp_path / "c.db"), FakeClient(bars[i].c)
    at = datetime.fromtimestamp(su.entry_t / 1000 + 45, timezone.utc)
    assert _run(cfg, store, client, bars[:i + 1], at) == 2                  # A and C both enter
    a, c = store.get(E.KEY), store.get("ema_qual_day")
    assert "SPY" in a["open"] and "SPY" in c["open"]
    alert = store.pending()[-1][2]
    assert "CROSS + RETEST + QUALIFIED" in alert and "Qualified: regime trend ✅" in alert and "score 10/11" in alert
    book = store.get(Q.CANDIDATES)["items"]
    rec = book[su.key]
    assert rec["disposition"] == "TRADE" and rec["reasons"] == [] and rec["opt_spread_pct"] == 2.0
    assert rec["opt_bid"] == 1.98 and rec["opt_ask"] == 2.02 and rec["opt_mid"] == 2.0 and rec["opt_spread"] == 0.04
    assert rec["failed_components"] == ["market"]
    # Closing it logs a "qualified" row carrying the q_ fields; A's row has none of them.
    client.mid = 1.5
    asyncio.run(E.manage(client, store, cfg, at + timedelta(minutes=1), {"SPY": bars[:i + 1]}))
    log = store.get(E.LOG_KEY)
    rows = {t["strategy"]: t for t in log}
    assert set(rows) == {"retest", "qualified"}
    assert rows["qualified"]["q_regime"] == "trend" and rows["qualified"]["q_disposition"] == "TRADE"
    assert not any(k.startswith("q_") for k in rows["retest"])
    assert rows["qualified"]["pnl"] == rows["retest"]["pnl"]                # same contract, fills and management


def test_wide_spread_is_rejected_and_logged(cfg, tmp_path):
    bars = _rally()
    su, _, i = _entry(bars)
    store, client = Store(tmp_path / "s.db"), FakeClient(bars[i].c, spread=0.18)    # 9% of a 2.00 mid
    at = datetime.fromtimestamp(su.entry_t / 1000 + 45, timezone.utc)
    _run(cfg, store, client, bars[:i + 1], at)
    rec = store.get(Q.CANDIDATES)["items"][su.key]
    assert rec["disposition"] == "REJECT" and rec["reasons"] == ["OPTION_SPREAD_TOO_WIDE"]
    assert rec["opt_spread_pct"] == 9.0 and not rec["components"]["option_spread"]
    assert not store.get("ema_qual_day")["open"] and store.get("ema_qual_day")["skipped"] == {
        "OPTION_SPREAD_TOO_WIDE": 1}
    assert "SPY" in store.get(E.KEY)["open"]                                # A still trades (its own 10% limit)
    assert store.get(Q.CAND_LOG)[-1]["date"] == "2026-10-26"


def test_watch_then_no_valid_retest(cfg, tmp_path):
    bars = _rally(pullback=False)                                          # the rally never pulls back
    store, client = Store(tmp_path / "w.db"), FakeClient(bars[-1].c)
    cross = next(s for s in E.scan("SPY", bars, DAY, ET, RULES))
    k = next(n for n, b in enumerate(bars) if b.t == cross.cross_t)
    _run(cfg, store, client, bars[:k + 2], datetime.fromtimestamp((bars[k + 1].t + E.FIVE) / 1000 + 30, timezone.utc))
    assert store.get(Q.CANDIDATES)["items"][cross.key]["disposition"] == "WATCH"
    _run(cfg, store, client, bars, datetime.fromtimestamp((bars[-1].t + E.FIVE) / 1000 + 30, timezone.utc))
    rec = store.get(Q.CANDIDATES)["items"][cross.key]
    assert rec["disposition"] == "REJECT" and rec["reasons"][0] == "NO_VALID_RETEST"
    assert rec["eval_at"] == E._et(cross.cross_t + E.FIVE, ET).strftime("%H:%M")      # judged at the cross candle


def test_stale_signal_and_outside_window(cfg, tmp_path):
    bars = _rally()
    su, _, i = _entry(bars)
    store = Store(tmp_path / "o.db")
    late = datetime.fromtimestamp(su.entry_t / 1000 + 20 * 60, timezone.utc)     # first seen 20 minutes later
    _run(cfg, store, FakeClient(bars[i].c), bars[:i + 1], late)
    assert store.get(Q.CANDIDATES)["items"][su.key]["reasons"][0] == "STALE_SIGNAL"
    narrow = dataclasses.replace(cfg, ema_qualified=Q.QualRules(sessions=(("09:35", "10:30"),)))
    store2 = Store(tmp_path / "o2.db")
    _run(narrow, store2, FakeClient(bars[i].c), bars[:i + 1],
         datetime.fromtimestamp(su.entry_t / 1000 + 45, timezone.utc))
    rec = store2.get(Q.CANDIDATES)["items"][su.key]
    assert rec["reasons"] == ["OUTSIDE_TRADING_WINDOW"] and not store2.get("ema_qual_day")["open"]


def test_controls_are_unchanged_by_strategy_c(cfg, tmp_path, monkeypatch):
    """Same inputs with and without C: A's state, alerts and record are identical; a C failure can't stop A."""
    bars = _rally()
    su, _, i = _entry(bars)
    at = datetime.fromtimestamp(su.entry_t / 1000 + 45, timezone.utc)
    out = {}
    for name, strategies in (("a", ("retest",)), ("ac", ("retest", "qualified"))):
        c = dataclasses.replace(cfg, daytrade=dataclasses.replace(cfg.daytrade, strategies=strategies))
        store, client = Store(tmp_path / f"{name}.db"), FakeClient(bars[i].c)
        _run(c, store, client, bars[:i + 1], at)
        client.mid = 2.8
        asyncio.run(E.manage(client, store, c, at + timedelta(minutes=2), {"SPY": bars[:i + 1]}))
        out[name] = (store.get(E.KEY), [p[2] for p in store.pending() if "QUALIFIED" not in p[2]
                                        and "EMA qualified" not in p[2]])
    assert out["a"] == out["ac"]

    async def boom(*a, **k):
        raise RuntimeError("strategy C bug")
    monkeypatch.setattr(Q, "enter", boom)
    store = Store(tmp_path / "boom.db")
    assert _run(cfg, store, FakeClient(bars[i].c), bars[:i + 1], at) == 1           # A still entered
    assert "SPY" in store.get(E.KEY)["open"] and not store.get("ema_qual_day")["open"]


def test_disabled_by_default_and_config_parsing():
    assert E.DayTradeRules().strategies == ("retest",)                     # C runs only when listed
    q = config_mod._qualified({"sessions": [["09:40", "10:30"]], "max_spread_pct": 6, "cooldown_minutes": 45})
    assert q.sessions == (("09:40", "10:30"),) and q.max_spread_pct == 6.0 and q.cooldown_minutes == 45
    with pytest.raises(config_mod.ConfigError):
        config_mod._qualified({"min_score": 3})
    with pytest.raises(config_mod.ConfigError):
        config_mod._qualified({"sessions": [["9", "10:30"]]})


def test_comparison_report(cfg, tmp_path):
    store = Store(tmp_path / "r.db")
    row = lambda strategy, pnl, cc, **kw: dict({"date": "2026-10-26", "strategy": strategy, "symbol": "SPY",
                                                "side": "bull", "pnl": pnl, "pnl_mid": pnl + 10, "in": cc,
                                                "candle_close": cc + ":00", "first_10m_fav_pct": 0.05}, **kw)
    store.put(E.LOG_KEY, [row("retest", 300.0, "10:40"), row("retest", -200.0, "12:10"),
                          row("cross", -100.0, "10:20"), row("cross", -150.0, "12:00"),
                          row("qualified", 300.0, "10:40", q_regime="trend", q_entry_close="10:40",
                              q_components={"vwap": True, "ema_slope": True}, q_opt_spread_pct=2.0,
                              q_market_aligned=True, q_quality_score=10, q_quality_max=11)])
    cand = lambda cross, entry, disp, reasons, **kw: dict({"date": "2026-10-26", "symbol": "SPY", "side": "bull",
                                                           "cross_close": cross, "entry_close": entry,
                                                           "disposition": disp, "reasons": reasons}, **kw)
    store.put(Q.CAND_LOG, [cand("10:20", "10:40", "TRADE", []),
                           cand("12:00", "12:10", "REJECT", ["REGIME_CHOP", "OUTSIDE_TRADING_WINDOW"],
                                chop_lock=True, whipsaw_count=2),
                           cand("13:05", None, "REJECT", ["NO_VALID_RETEST"])])
    text = Q.report(store, cfg, DAY)
    assert "A · Cross + retest</b>: 2 trades · 1W/1L" in text and "fees $13" in text and "net +$87" in text
    assert "C · Cross + retest + qualified</b>: 1 trade · 1W/0L" in text
    assert "3 EMA crosses · 1 traded · 2 rejected" in text
    assert "First reason: REGIME_CHOP 1 · NO_VALID_RETEST 1" in text
    assert "A's trades by C's decision: C rejected: REGIME_CHOP 1 (0W, -$206) · C took it too 1 (1W, +$294)" in text
    assert "B's trades by C's decision: C rejected: REGIME_CHOP 1 (0W, -$156) · C took it too 1 (0W, -$106)" in text
    assert "regime: trend 1 (1W, +$294)" in text and "SPY/QQQ: aligned 1" in text and "quality: 10/11 1" in text
    assert "under 0.1% in 10 min: 2" in text and "1 candidates in a chop lock (SPY)" in text
