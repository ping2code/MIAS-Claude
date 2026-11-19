"""2026-11-17 delivery-policy change: EMA / paper-strategy alerts are muted on Telegram ([telegram] muted_rules).

The strategies still run exactly as before (entries, exits, fills, P/L, logs, scorecards, reports); only the
outbox stops sending their live alerts. Early Watch v1 / v2 are sent by mias-observer and are untouched."""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mias import closeplay, paperscore, strategies
from mias import dte as X
from mias import emacross as E
from mias import qualify as Q
from mias import simple as S
from mias.notify import Dispatcher
from mias.store import MUTED, Store
from test_improvements import _CloseClient, _close_cfg, _close_day_raw
from test_strategy_c import DAY, ET, _entry, _rally
from test_strategy_d import _with
from test_strategy_d import cfg as d_cfg  # noqa: F401  (fixture)
from test_strategy_e import Chain, _at, _run
from test_strategy_simple import Chain as SimpleChain
from test_strategy_simple import _drive, _wave

ROOT = Path(__file__).resolve().parent.parent
LIVE_EMA = ("ema_entry", "ema_partial", "ema_trail", "ema_target", "ema_stop", "ema_breakeven", "ema_trailed",
            "ema_invalidated", "ema_time")
ACTIVE = ("qualified", Q.D, X.E7, X.E21)         # config.toml [daytrade] strategies (C, D, E7, E21)


class TG:
    def __init__(self):
        self.sent = []

    async def send(self, text, chat=None):
        self.sent.append((chat, text))


def _deliver(store, muted):
    tg = TG()
    d = Dispatcher(store, tg, ["group"], muted=muted)
    asyncio.run(d.flush())
    return tg, d


def _rows(store):
    return store.db.execute("SELECT rule, text, delivered IS NOT NULL, last_error FROM alerts ORDER BY id").fetchall()


def _kv(store):
    return {k: json.loads(v) for k, v in store.db.execute("SELECT key, value FROM kv ORDER BY key")}


def _ema_day(cfg, store, client, with_stop=True):
    """Cross, entry, every open trade stopped out, then the 15:10 close: entries, exits and the summaries."""
    bars = _rally()
    su, _, i = _entry(bars)
    k = next(n for n, b in enumerate(bars) if b.t == su.cross_t)
    _run(cfg, store, client, bars[:k + 1], datetime.fromtimestamp((bars[k].t + E.FIVE) / 1000 + 30, timezone.utc))
    _run(cfg, store, client, bars[:i + 1], _at(su))
    if with_stop:
        client.mid = 1.60
        asyncio.run(E.manage(client, store, cfg, _at(su) + timedelta(minutes=1), {"SPY": bars[:i + 1]}))
    close = datetime.combine(DAY, datetime.min.time().replace(hour=15, minute=10), ET)
    asyncio.run(E.live(client, store, cfg, close, {"SPY": bars[:i + 1]}))


@pytest.fixture
def policy(d_cfg):  # noqa: F811
    return d_cfg.telegram_muted_rules


# ---- the configured policy ------------------------------------------------------------------------------------

def test_config_mutes_live_strategy_alerts_only(policy):
    assert set(policy) == set(LIVE_EMA) | {"ema_simple", "closeplay"}
    for kept in ("ema_summary", "system", "scorecard", "strategies", "report", "digest", "brief", "picks",
                 "unusual", "changes", "rollcall", "premarket", "market", "swing_trend", "position_take_profit"):
        assert kept not in policy


def test_without_a_policy_nothing_is_muted(tmp_path):
    store = Store(tmp_path / "a.db")
    store.enqueue("ema_entry", "x")
    tg, d = _deliver(store, ())
    assert len(tg.sent) == 1 and d.muted_count == 0 and _rows(store)[0][3] is None


def test_muted_alert_is_recorded_not_sent_and_never_retried(tmp_path, policy):
    store = Store(tmp_path / "a.db")
    store.enqueue("ema_entry", "paper entry", "SPY")
    store.enqueue("system", "⚠️ MIAS degraded")
    tg, d = _deliver(store, policy)
    assert tg.sent == [("group", "⚠️ MIAS degraded")] and d.muted_count == 1 and store.pending() == []
    assert _rows(store)[0] == ("ema_entry", "paper entry", 1, MUTED)
    asyncio.run(d.flush())
    assert len(tg.sent) == 1                                          # nothing re-sent, nothing retried
    a = store.recent_alerts(5)
    assert [x["muted"] for x in a] == [False, True]


def test_critical_operational_alerts_still_send(tmp_path, policy):
    store = Store(tmp_path / "a.db")
    for rule in ("system", "scorecard", "strategies", "digest"):
        store.enqueue(rule, rule)
    store.enqueue("position_stop_loss", "your position", "AMZN", chat="user1")
    tg, _ = _deliver(store, policy)
    assert [t for _, t in tg.sent] == ["system", "scorecard", "strategies", "digest", "your position"]
    assert tg.sent[-1][0] == "user1"


def test_app_wires_the_policy_into_the_dispatcher(d_cfg, monkeypatch):  # noqa: F811
    src = (ROOT / "mias" / "app.py").read_text()
    assert re.search(r"Dispatcher\(self\.store, tg, cfg\.telegram_alert_chats,\s*muted=", src)


# ---- EMA C / D / E7 / E21 run, but stay silent ---------------------------------------------------------------

@pytest.mark.parametrize("strategy", ACTIVE)
def test_active_ema_strategy_executes_but_does_not_telegram(d_cfg, tmp_path, policy, strategy):  # noqa: F811
    cfg = _with(d_cfg, strategy)
    bars = _rally()
    su, _, i = _entry(bars)
    store, client = Store(tmp_path / "s.db"), Chain(bars[i].c, mid=2.0)
    _ema_day(cfg, store, client)
    st = store.get(E.STRATEGIES[strategy]["key"])
    assert len(st["closed"]) == 1 and not st["open"] and st["losses"] == {"SPY": 1}       # entered, stopped
    rec = [t for t in store.get(E.LOG_KEY) if t["strategy"] == strategy]
    assert len(rec) == 1 and rec[0]["exit_reason"] == "stop" and rec[0]["pnl"] < 0 and rec[0]["contract"]
    rules = [r[0] for r in _rows(store)]
    assert "ema_entry" in rules and "ema_stop" in rules                                   # still queued + logged
    tg, _ = _deliver(store, policy)
    sent = [t for _, t in tg.sent]
    assert sent and all(r == "ema_summary" for r, *_ in _rows(store) if r not in policy)
    assert not any("PAPER" in t and ("Entry" in t or "STOPPED" in t) for t in sent)        # no live alerts
    assert all(row[2] for row in _rows(store))                                            # nothing pending


def test_open_positions_keep_being_managed_while_muted(d_cfg, tmp_path, policy):  # noqa: F811
    """Delivery runs between management passes, exactly as live: muting never closes, skips or alters a trade."""
    cfg = _with(d_cfg, *ACTIVE)
    bars = _rally()
    su, _, i = _entry(bars)
    store, client = Store(tmp_path / "m.db"), Chain(bars[i].c, mid=2.0)
    _run(cfg, store, client, bars[:i + 1], _at(su))
    _deliver(store, policy)
    assert all("SPY" in store.get(E.STRATEGIES[s]["key"])["open"] for s in ACTIVE)          # still open
    client.mid = 1.60
    asyncio.run(E.manage(client, store, cfg, _at(su) + timedelta(minutes=1), {"SPY": bars[:i + 1]}))
    _deliver(store, policy)
    for s in ACTIVE:
        st = store.get(E.STRATEGIES[s]["key"])
        assert not st["open"] and st["closed"][0]["exit_reason"] == "stop"


def test_paper_state_identical_with_notifications_on_and_muted(d_cfg, tmp_path, policy):  # noqa: F811
    cfg = _with(d_cfg, "retest", "cross", *ACTIVE)
    out = {}
    for name, muted in (("on", ()), ("muted", policy)):
        bars = _rally()
        su, _, i = _entry(bars)
        store, client = Store(tmp_path / f"{name}.db"), Chain(bars[i].c, mid=2.0)
        bars_i = bars[:i + 1]
        k = next(n for n, b in enumerate(bars) if b.t == su.cross_t)
        _run(cfg, store, client, bars[:k + 1], datetime.fromtimestamp((bars[k].t + E.FIVE) / 1000 + 30, timezone.utc))
        _deliver(store, muted)
        _run(cfg, store, client, bars_i, _at(su))
        _deliver(store, muted)                                        # delivery between every step, as live
        client.mid = 2.70
        asyncio.run(E.manage(client, store, cfg, _at(su) + timedelta(minutes=1), {"SPY": bars_i}))
        _deliver(store, muted)
        client.mid = 1.60
        asyncio.run(E.manage(client, store, cfg, _at(su) + timedelta(minutes=2), {"SPY": bars_i}))
        _deliver(store, muted)
        close = datetime.combine(DAY, datetime.min.time().replace(hour=15, minute=10), ET)
        asyncio.run(E.live(client, store, cfg, close, {"SPY": bars_i}))
        _deliver(store, muted)
        out[name] = (_kv(store), [(r, t) for r, t, *_ in _rows(store)],
                     strategies.report(store, cfg, close + timedelta(hours=1)), paperscore.report(store, cfg))
    assert out["on"] == out["muted"]                                  # state, logs, fills, P/L, alerts, reports
    log = out["muted"][0][E.LOG_KEY]
    assert {t["strategy"] for t in log} == {"retest", "cross", *ACTIVE}
    for t in log:                                                     # fills and P/L still recorded
        assert t["entry"] and t["exit_reason"] and t["out"] and "pnl" in t and "mfe_pct" in t and t["contract"]


# ---- B / Simple / A benchmarks, close fade -------------------------------------------------------------------

def test_benchmark_config_unchanged(d_cfg):  # noqa: F811
    from mias import config as config_mod
    import os
    os.environ.setdefault("MASSIVE_API_KEY", "k")
    c = config_mod.load(ROOT / "config.toml")
    assert c.daytrade.strategies == ("qualified", "ema_9_21_d") and not c.daytrade.enabled   # 2026-11-18 cleanup
    assert c.ema_simple_enabled is False                               # paused benchmark, as before
    assert "retest" in E.STRATEGIES and "cross" in E.STRATEGIES        # A / B code kept for history and replay


def test_simple_benchmark_runs_if_enabled_but_is_muted(tmp_path, monkeypatch, policy):
    import dataclasses
    from mias import config as config_mod
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    monkeypatch.setenv("MIAS_DB_PATH", str(tmp_path / "t.db"))
    cfg = dataclasses.replace(config_mod.load(ROOT / "config.toml"), ema_simple_enabled=True)
    store = Store(tmp_path / "s.db")
    _drive(cfg, store, SimpleChain(), {"SPY": _wave()})
    assert store.get(S.KEY)["closed"]                                 # traded and closed at 15:10
    tg, _ = _deliver(store, policy)
    assert not any(r == "ema_simple" for r, *_ in _rows(store) if r not in policy)
    assert all(row[3] == MUTED for row in _rows(store) if row[0] == "ema_simple")
    assert not any("Simple" in t and "TARGET" in t for _, t in tg.sent)


def test_close_fade_still_trades_and_logs_but_is_muted(tmp_path, monkeypatch, policy):
    from datetime import date as _date, time as _time
    cfg = _close_cfg(tmp_path, monkeypatch)
    day = _date(2026, 10, 28)
    raw = _close_day_raw(day, cfg.tz)
    store, client = Store(tmp_path / "c.db"), _CloseClient(day)
    at = lambda h, m: datetime.combine(day, _time(h, m), cfg.tz)  # noqa: E731
    assert asyncio.run(closeplay.live(client, store, cfg, at(15, 46), raw, [], 6880.0)) == 1
    tg, _ = _deliver(store, policy)
    assert tg.sent == [] and store.get(closeplay.KEY)["status"] == "open"
    client.bid, client.ask = 0.90, 0.92
    assert asyncio.run(closeplay.manage(client, store, cfg, at(15, 59))) == 1        # the 15:59 sale still runs
    tg, _ = _deliver(store, policy)
    assert tg.sent == []
    log = store.get(closeplay.LOG_KEY)
    assert log[0]["pnl"] == 38.0 and log[0]["qty"] == 1                               # fills and P/L recorded
    assert "3:45 PM close fade" in strategies.report(store, cfg, at(16, 30))        # still reported
    assert all(row[3] == MUTED for row in _rows(store))


# ---- Early Watch v1 / v2 and the options context are untouched ------------------------------------------------

def test_early_watch_is_outside_this_policy():
    from mias.observer.settings import load
    from mias.observer.v2 import V2Telegram
    from test_observer_v2 import _w
    from test_observer_watch import ms
    s = load(ROOT / "config.toml")
    assert s.telegram is True and s.v2_telegram is True and s.v2_telegram_muted == ("structure",)
    for f in (ROOT / "mias" / "observer").glob("*.py"):              # observer has its own sender, not the outbox
        assert "Dispatcher" not in f.read_text() and "muted_rules" not in f.read_text()
    p = V2Telegram(muted=s.v2_telegram_muted)
    assert p.decide(_w("vwap", leg="VW1", t=ms(10, 14)), ms(10, 14) + 1000)[0] == "send"
    assert p.decide(_w("or", leg="OR1", sym="SPY"), ms(10, 13) + 2000)[0] == "send"
    assert p.decide(_w("structure"), ms(10, 13) + 2000)[0] == "muted"
