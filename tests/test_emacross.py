"""EMA 9/21 cross-and-retest day trade: detection, trade management, live paper trades."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mias import config as config_mod
from mias import emacross as E
from mias.indicators import Bar, ema
from mias.store import Store

ET = ZoneInfo("America/New_York")

ROOT = Path(__file__).resolve().parent.parent
DAY = date(2026, 10, 26)
RULES = E.DayTradeRules()


def _bars_with_bull_setup() -> tuple[list[Bar], int]:
    """A falling prior day, then today: down, a rally that crosses up, a pullback to EMA 9, a green confirmation.
    Returns the bars and the index of the confirmation candle."""
    bars: list[Bar] = []

    def add(t: datetime, o, h, l, c):
        bars.append(Bar(int(t.astimezone(timezone.utc).timestamp() * 1000), o, h, l, c, 1000))

    t, p = datetime.combine(DAY - timedelta(days=3), time(14, 0), ET), 104.0
    for _ in range(24):                                  # prior session, drifting down (EMA warm-up)
        add(t, p, p + 0.05, p - 0.15, p - 0.1)
        p, t = p - 0.1, t + timedelta(minutes=5)
    t = datetime.combine(DAY, time(9, 30), ET)
    for _ in range(4):
        add(t, p, p + 0.05, p - 0.15, p - 0.1)
        p, t = p - 0.1, t + timedelta(minutes=5)
    while True:                                          # rally until EMA 9 crosses above EMA 21
        add(t, p, p + 0.45, p - 0.05, p + 0.4)
        p, t = p + 0.4, t + timedelta(minutes=5)
        closes = [b.c for b in bars]
        if ema(closes, 9)[-1] > ema(closes, 21)[-1]:
            break
    for _ in range(2):                                   # keep going a little, then pull back
        add(t, p, p + 0.35, p - 0.05, p + 0.3)
        p, t = p + 0.3, t + timedelta(minutes=5)
    closes = [b.c for b in bars]
    f = ema(closes, 9)[-1]
    add(t, p, p + 0.05, f - 0.3, p - 0.1)               # red candle whose low tags EMA 9, close stays high
    p, t = p - 0.1, t + timedelta(minutes=5)
    add(t, p, p + 0.5, p - 0.05, p + 0.45)              # green confirmation
    return bars, len(bars) - 1


def test_scan_finds_cross_retest_entry():
    bars, i = _bars_with_bull_setup()
    setups = E.scan("SPY", bars, DAY, ET, RULES)
    entries = [s for s in setups if s.status == "entry"]
    assert len(entries) == 1
    su = entries[0]
    assert su.side == "bull" and su.entry_t == bars[i].t + E.FIVE and su.price == bars[i].c
    assert su.invalidation == min(bars[i - 1].l, bars[i].l)          # the retest's lowest low
    assert su.vwap in ("with", "against") and su.orb in ("with", "against", "inside")


def test_scan_no_chase_without_retest():
    bars, _ = _bars_with_bull_setup()
    bars = bars[:-2]                                     # drop the pullback and the confirmation ...
    t, p = bars[-1].t, bars[-1].c
    for n in range(1, 8):                                # ... and keep running up with no pullback
        bars.append(Bar(t + n * E.FIVE, p, p + 0.45, p - 0.02, p + 0.4, 1000))
        p += 0.4
    setups = E.scan("SPY", bars, DAY, ET, RULES)
    assert setups[-1].side == "bull" and setups[-1].status == "no_retest"


def test_control_group_enters_on_the_cross():
    bars, _ = _bars_with_bull_setup()
    su = [s for s in E.scan("SPY", bars, DAY, ET, RULES, retest=False) if s.side == "bull"][0]
    assert su.status == "entry" and su.entry_t == su.cross_t + E.FIVE


def _trade(premium=2.0):
    su = E.Setup("SPY", "bull", 0, "entry", 0, 760.0, 759.55)
    return E.Trade.open(su, "O:SPY261026C00760000", 760, "2026-10-26", 0, premium, RULES)


def test_trade_reproduces_the_handout_example():
    tr = _trade()
    ev = tr.on_price(1, 2.0, 2.65, 1.95, 2.6, RULES)          # +30%: sell 3, runners to breakeven
    assert [k for k, _ in ev] == ["partial"] and tr.open_qty == 2 and tr.stop == 2.0
    ev = tr.on_price(2, 2.6, 3.5, 2.5, 3.4, RULES)            # +75%: protect +50%
    assert [k for k, _ in ev] == ["trail"] and tr.stop == 3.0
    tr.on_price(3, 3.4, 4.0, 3.3, 3.9, RULES)                 # +100%: protect +75%
    assert tr.stop == 3.5
    ev = tr.on_price(4, 3.6, 3.6, 3.4, 3.45, RULES)           # runners stopped at 3.50
    assert ev[0][0] == "trailed" and tr.closed
    assert tr.pnl == 480.0 and tr.pnl_pct == 48.0              # $180 + $300, as in the handout


def test_trade_stop_and_gap():
    tr = _trade()
    tr.on_price(1, 1.9, 1.95, 1.55, 1.6, RULES)
    assert tr.closed and tr.exit_reason == "stop" and tr.pnl == -200.0
    tr = _trade()
    tr.on_price(1, 1.5, 1.5, 1.4, 1.45, RULES)                # opened below the stop: filled at the open
    assert tr.pnl == -250.0


def test_trade_same_bar_stop_wins_over_target():
    tr = _trade()
    tr.on_price(1, 2.0, 2.7, 1.5, 2.5, RULES)
    assert tr.exit_reason == "stop" and not tr.partial_done


def test_expiry_choice():
    exps = ["2026-10-26", "2026-10-27", "2026-10-28"]
    morning = int(datetime.combine(DAY, time(10, 0), ET).timestamp() * 1000)
    afternoon = int(datetime.combine(DAY, time(13, 30), ET).timestamp() * 1000)
    assert E.pick_expiry(exps, DAY, morning, ET, RULES) == "2026-10-26"
    assert E.pick_expiry(exps, DAY, afternoon, ET, RULES) == "2026-10-27"
    assert E.pick_expiry(exps[1:], DAY, morning, ET, RULES) == "2026-10-27"   # no 0DTE listed


# ---- live paper trades ---------------------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setenv("MIAS_DB_PATH", str(tmp_path / "t.db"))
    return config_mod.load(ROOT / "config.toml")


class FakeClient:
    def __init__(self):
        self.mid = 2.0
        self.spot = 100.0

    def _row(self, kind, strike, expiry="2026-10-26"):
        k = "C" if kind == "call" else "P"
        return {"details": {"contract_type": kind, "expiration_date": expiry, "strike_price": strike,
                            "ticker": f"O:SPY{expiry[2:4]}{expiry[5:7]}{expiry[8:]}{k}{strike * 1000:08.0f}"},
                "last_quote": {"bid": self.mid - 0.02, "ask": self.mid + 0.02},
                "underlying_asset": {"price": self.spot}}

    async def option_chain(self, symbol, **kw):
        return [self._row(k, s) for k in ("call", "put") for s in (round(self.spot) - 1, round(self.spot),
                                                                    round(self.spot) + 1)], False

    async def snapshots(self, tickers):
        return {t: {"last_quote": {"bid": self.mid - 0.02, "ask": self.mid + 0.02},
                    "underlying_asset": {"price": self.spot}} for t in tickers}


def _only(cfg, *strategies):
    from dataclasses import replace
    return replace(cfg, daytrade=replace(cfg.daytrade, strategies=strategies))


def test_live_paper_trade_entry_partial_and_close_summary(cfg, tmp_path):
    cfg = _only(cfg, "retest")
    bars, i = _bars_with_bull_setup()
    store = Store(tmp_path / "live.db")
    client = FakeClient()
    client.spot = bars[i].c
    entry_at = datetime.fromtimestamp((bars[i].t + E.FIVE) / 1000 + 60, timezone.utc)
    sent = asyncio.run(E.live(client, store, cfg, entry_at, {"SPY": bars}))
    assert sent == 1
    rows = store.pending()
    assert rows[-1][1] == "ema_entry" and "CROSS + RETEST" in rows[-1][2] and "PAPER ONLY" in rows[-1][2]
    assert rows[-1][4] is None                                # to the alert chats (the group)
    # Same entry is never alerted twice.
    assert asyncio.run(E.live(client, store, cfg, entry_at + timedelta(minutes=2), {"SPY": bars})) == 0
    client.mid = 2.70
    asyncio.run(E.live(client, store, cfg, entry_at + timedelta(minutes=5), {"SPY": bars}))
    assert store.pending()[-1][1] == "ema_partial"
    close = datetime.combine(DAY, time(15, 10), ET)
    asyncio.run(E.live(client, store, cfg, close, {"SPY": bars}))
    rules = [r[1] for r in store.pending()]
    assert rules[-2:] == ["ema_time", "ema_summary"]
    assert "1 trade" in store.pending()[-1][2]
    text = E.status_text(store, cfg, close, {"SPY": bars})
    assert "EMA 9/21" in text and "EMA retest</b>: 1 closed" in text


def _cross_index(bars):
    closes = [b.c for b in bars]
    f, s = ema(closes, 9), ema(closes, 21)
    return next(n for n in range(1, len(bars)) if f[n] > s[n] and f[n - 1] <= s[n - 1]
                and bars[n].t >= int(datetime.combine(DAY, time(9, 30), ET).timestamp() * 1000))


def test_cross_strategy_enters_on_the_cross_with_its_own_record(cfg, tmp_path):
    bars, i = _bars_with_bull_setup()
    k = _cross_index(bars)
    store = Store(tmp_path / "x.db")
    client = FakeClient()
    client.spot = bars[k].c
    at = datetime.fromtimestamp((bars[k].t + E.FIVE) / 1000 + 60, timezone.utc)
    assert asyncio.run(E.live(client, store, cfg, at, {"SPY": bars[:k + 1]})) == 1
    rows = store.pending()
    assert "CROSS (no retest)" in rows[-1][2]
    assert "SPY" in (store.get("ema_cross_day") or {}).get("open", {})
    assert not (store.get("ema_day") or {}).get("open")          # the retest strategy is still waiting
    # Later the retest entry comes too: both strategies hold SPY, in separate records.
    at2 = datetime.fromtimestamp((bars[i].t + E.FIVE) / 1000 + 60, timezone.utc)
    client.spot = bars[i].c
    asyncio.run(E.live(client, store, cfg, at2, {"SPY": bars}))
    assert "CROSS + RETEST" in store.pending()[-1][2]
    close = datetime.combine(DAY, time(15, 10), ET)
    asyncio.run(E.live(client, store, cfg, close, {"SPY": bars}))
    log = store.get(E.LOG_KEY)
    assert sorted(t["strategy"] for t in log) == ["cross", "retest"]
    summaries = [r[2] for r in store.pending() if r[1] == "ema_summary"]
    assert len(summaries) == 2 and any("cross (no retest)" in s for s in summaries)


def test_health_gate_holds_paper_entries(cfg, tmp_path):
    cfg = _only(cfg, "retest")
    bars, i = _bars_with_bull_setup()
    at = datetime.fromtimestamp((bars[i].t + E.FIVE) / 1000 + 60, timezone.utc)
    store = Store(tmp_path / "h.db")
    client = FakeClient()
    client.spot = bars[i].c
    held = asyncio.run(E.live(client, store, cfg, at, {"SPY": bars}, issues={"SPY": ["stock: ReadTimeout"]}))
    assert held == 0 and store.get("ema_day")["skipped"] == {"data health": 1}
    assert store.get(f"health:{DAY}") == {"SPY": {"stock": 1}}
    # A wide option spread holds the entry too.
    store2 = Store(tmp_path / "h2.db")
    client.mid = 0.20                                            # 0.18 / 0.22: a 20% spread
    assert asyncio.run(E.live(client, store2, cfg, at, {"SPY": bars})) == 0
    assert store2.get("ema_day")["skipped"] == {"data health": 1}


def test_daytrade_config_parses():
    rules = config_mod._daytrade({"symbols": ["spy", "qqq"], "stop": 0.25, "trail": [[0.5, 0.25]]})
    assert rules.symbols == ("SPY", "QQQ") and rules.stop == 0.25 and rules.trail == ((0.5, 0.25),)
    with pytest.raises(config_mod.ConfigError):
        config_mod._daytrade({"stopp": 0.2})


def test_paper_fills_at_bid_and_ask_with_mid_alongside():
    tr = _trade(2.04)                        # bought at the ask
    tr.entry_mid = 2.00
    tr.bid = 2.58                            # mid reaches the +30% target (2.65) but the bid is lower
    tr.on_price(1, 2.65, 2.65, 2.65, 2.65, RULES)
    assert tr.partial_done and tr.open_qty == 2
    assert tr.pnl == pytest.approx((2.58 - 2.04) * 3 * 100)
    assert tr.pnl_mid == pytest.approx((2.65 - 2.00) * 3 * 100)
    tr.bid = 1.90
    tr.on_price(2, 1.95, 1.95, 1.95, 1.95, RULES)      # runners stop at breakeven (2.04): sold at the bid
    assert tr.closed and tr.exit_reason == "breakeven"
    assert tr.pnl == pytest.approx((2.58 - 2.04) * 300 + (1.90 - 2.04) * 200)
    assert tr.mfe_pct == pytest.approx(100 * (2.65 / 2.04 - 1), abs=0.1) and tr.peak_t == 1
    assert tr.mae_pct == pytest.approx(100 * (1.95 / 2.04 - 1), abs=0.1) and tr.low_t == 2


def test_live_entry_records_why_how_and_trend(cfg, tmp_path):
    from types import SimpleNamespace as NS
    bars, i = _bars_with_bull_setup()
    store = Store(tmp_path / "rec.db")
    client = FakeClient()
    client.spot = bars[i].c
    at = datetime.fromtimestamp((bars[i].t + E.FIVE) / 1000 + 45, timezone.utc)
    r = NS(stock=NS(score=-40.0, composites={"intraday": (-20.0, "")}))
    asyncio.run(E.live(client, store, cfg, at, {"SPY": bars}, {"SPY": r}))
    tr = E.Trade(**store.get(E.KEY)["open"]["SPY"])
    assert tr.entry == 2.02 and tr.entry_mid == 2.0                    # ask, mid
    info = tr.info
    assert info["confirmed_close"] and info["fill_delay_s"] == 45 and "pullback" in info["reason"]
    assert info["with_swing"] == "against" and info["swing"] == -40 and info["entry_spread_pct"] == 2.0
    assert info["chart_stop_pct"] > 0
    text = store.pending()[-1][2]
    assert "AGAINST the swing trend" in text and "ask (mid 2.00" in text
    # Closing it logs the full record.
    client.mid = 1.5
    asyncio.run(E.manage(client, store, cfg, at + timedelta(minutes=1), {"SPY": bars}))
    log = store.get(E.LOG_KEY)
    assert log[-1]["exit_reason"] == "stop" and log[-1]["with_swing"] == "against" and "mae_pct" in log[-1]
    assert "worst" in store.pending()[-1][2]


def test_swing_filter_skips_entries_against_the_trend(cfg, tmp_path):
    import dataclasses
    from types import SimpleNamespace as NS
    cfg = dataclasses.replace(cfg, daytrade=dataclasses.replace(cfg.daytrade, swing_filter=True))
    bars, i = _bars_with_bull_setup()
    store = Store(tmp_path / "f.db")
    client = FakeClient()
    at = datetime.fromtimestamp((bars[i].t + E.FIVE) / 1000 + 45, timezone.utc)
    r = NS(stock=NS(score=-40.0, composites={"intraday": (-20.0, "")}))
    assert asyncio.run(E.live(client, store, cfg, at, {"SPY": bars}, {"SPY": r})) == 0
    assert store.get(E.KEY)["skipped"] == {"against the swing trend": 1}


def test_watcher_reprices_open_trades_between_cycles(cfg, tmp_path):
    from mias.app import Service
    bars, i = _bars_with_bull_setup()
    svc = Service(cfg)
    svc.store = Store(tmp_path / "w.db")
    svc.engine.store = svc.store
    client = FakeClient()
    client.spot = bars[i].c
    at = datetime.fromtimestamp((bars[i].t + E.FIVE) / 1000 + 30, timezone.utc)
    asyncio.run(E.live(client, svc.store, cfg, at, {"SPY": bars}))

    async def aggregates(*a, **k):
        return []
    client.aggregates = aggregates
    svc.client = client
    svc.market_state = "open"
    client.mid = 1.0
    sent = asyncio.run(svc.watch_once(at + timedelta(seconds=30)))
    assert sent == 1 and svc.store.get(E.KEY)["closed"][-1]["exit_reason"] == "stop"
