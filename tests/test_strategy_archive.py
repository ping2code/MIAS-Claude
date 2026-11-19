"""2026-11-18 strategy-status cleanup (config only): C / D / close fade paused, E7 / E21 retired, B / Simple paused and
A retired as before. No new forward paper trades from any of them; code, history, replay and reports are kept.
Early Watch v1 / v2 (mias-observer) are untouched."""
import asyncio
import dataclasses
from datetime import datetime, time
from pathlib import Path

import pytest

from mias import closeplay, emacross, paperscore as P, qualify, simple, strategies
from mias import dte as X
from mias.store import Store
from test_service import ET, TODAY, FakeMassive, _engine
from test_service import cfg as svc_cfg  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parent.parent
ARCHIVED = ("qualified", "ema_9_21_d", "ema_9_21_e7", "ema_9_21_e21")


def _calls(monkeypatch):
    seen = []

    def rec(name):
        async def f(*a, **k):
            seen.append(name)
            return 0
        return f
    for mod, fn in ((emacross, "live"), (simple, "live"), (closeplay, "live")):
        monkeypatch.setattr(mod, fn, rec(f"{mod.__name__}.{fn}"))
    return seen


def _cycle(cfg, at):
    fake = FakeMassive(TODAY, at, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0})
    eng, store, _ = _engine(cfg, fake)
    asyncio.run(eng.cycle(at))
    return store


@pytest.mark.parametrize("hhmm", [(10, 0), (14, 0), (15, 46)])
def test_live_cycle_opens_no_paper_trade_for_any_strategy(svc_cfg, monkeypatch, hhmm):  # noqa: F811
    """C, D, E7, E21 (emacross.live), Simple and the close fade (15:45) are never called by the live cycle."""
    assert not svc_cfg.daytrade.enabled and not svc_cfg.closeplay_enabled and not svc_cfg.ema_simple_enabled
    seen = _calls(monkeypatch)
    store = _cycle(svc_cfg, datetime.combine(TODAY, time(*hhmm), ET))
    assert seen == []
    for s in ARCHIVED + ("retest", "cross"):
        assert not (store.get(emacross.STRATEGIES[s]["key"]) or {}).get("open")
    assert store.get(closeplay.KEY) is None and store.get(simple.KEY) is None


def test_the_same_cycle_runs_them_when_switched_on(svc_cfg, monkeypatch):  # noqa: F811
    """Sanity: the switch is the only difference (re-enabling later, on request, works)."""
    on = dataclasses.replace(svc_cfg, daytrade=dataclasses.replace(svc_cfg.daytrade, enabled=True),
                             closeplay_enabled=True)
    seen = _calls(monkeypatch)
    _cycle(on, datetime.combine(TODAY, time(15, 46), ET))
    assert "mias.emacross.live" in seen and "mias.closeplay.live" in seen


def test_close_fade_live_step_refuses_when_paused(tmp_path, svc_cfg):  # noqa: F811
    store = Store(tmp_path / "c.db")
    assert asyncio.run(closeplay.live(None, store, svc_cfg, datetime.combine(TODAY, time(15, 46), ET), [])) == 0
    assert store.get(closeplay.KEY) is None


def test_e7_e21_are_off_the_list_and_c_d_are_kept_for_re_enabling(svc_cfg):  # noqa: F811
    assert svc_cfg.daytrade.strategies == ("qualified", "ema_9_21_d")
    assert P._status("E7", svc_cfg).startswith("retired") and P._status("E21", svc_cfg).startswith("retired")
    assert P._status("A", svc_cfg) == "retired 2026-11-09"
    assert P._status("B", svc_cfg) == P._status("Simple", svc_cfg) == "paused — benchmark"
    for c in ("C", "D", "Close fade"):
        assert P._status(c, svc_cfg) == "paused — benchmark/research"


def test_code_and_replay_paths_remain():
    for s in ARCHIVED + ("retest", "cross"):
        assert s in emacross.STRATEGIES
    assert set(X.PLANS) >= {X.E7, X.E21} and qualify.D == "ema_9_21_d"
    for fn in (emacross.live, emacross.manage, emacross.backtest, closeplay.live, closeplay.manage,
               closeplay.backtest, closeplay.run_backtest, simple.live):
        assert callable(fn)


def test_history_and_reports_keep_every_strategy(tmp_path, svc_cfg):  # noqa: F811
    store = Store(tmp_path / "h.db")
    log = [{"date": "2026-11-12", "symbol": "SPY", "side": "bull", "pnl": p, "exit_reason": "time", "in": "10:00",
            "strategy": s} for s, p in (("retest", 10.0), ("cross", 20.0), ("qualified", 30.0), ("ema_9_21_d", 40.0),
                                        ("ema_9_21_e7", 50.0), ("ema_9_21_e21", 60.0))]
    store.put(emacross.LOG_KEY, log)
    store.put(closeplay.LOG_KEY, [{"date": "2026-11-12", "symbol": "SPX", "side": "bull", "pnl": 70.0, "qty": 1}])
    before = store.get(emacross.LOG_KEY), store.get(closeplay.LOG_KEY)
    text = P.report(store, svc_cfg, full=True)
    for name in P.NAMES.values():
        if name != P.NAMES["Simple"]:
            assert name in text
    assert "(retired 2026-11-18 21:01 ET)" in text and "(paused — benchmark/research)" in text
    assert "no new forward paper entries after 2026-11-18 21:01 ET" in text
    assert "All strategies incl. retired and paused history</b>: 7 trades" in text
    weekly = strategies.report(store, svc_cfg, datetime(2026, 11, 13, 16, 0, tzinfo=ET), 7)
    assert "(paused — benchmark/research; no new entries after 2026-11-18 21:01 ET)" in weekly
    assert "(retired 2026-11-18 21:01 ET)" in weekly and "3:45 PM close fade</b> (paused" in weekly
    assert (store.get(emacross.LOG_KEY), store.get(closeplay.LOG_KEY)) == before          # nothing rewritten


def test_telegram_strategy_mute_unchanged(svc_cfg):  # noqa: F811
    assert set(svc_cfg.telegram_muted_rules) == {"ema_entry", "ema_partial", "ema_trail", "ema_target", "ema_stop",
                                                 "ema_breakeven", "ema_trailed", "ema_invalidated", "ema_time",
                                                 "ema_simple", "closeplay"}


def test_early_watch_v1_v2_untouched():
    """The observer never reads the strategy switches; its settings load as before (Structure muted alone)."""
    from mias.observer.settings import load
    s = load(ROOT / "config.toml")
    assert s.telegram is True and s.v2_telegram is True and s.v2_telegram_muted == ("structure",)
    for f in (ROOT / "mias" / "observer").glob("*.py"):
        src = f.read_text()
        assert "daytrade.enabled" not in src and "closeplay_enabled" not in src
